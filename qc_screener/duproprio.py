"""Scraper DuProprio — categorie multiplex.

Les selecteurs CSS marques `TODO` doivent etre verifies contre le DOM courant.
Utiliser `qc-screener dump <url>` pour ecrire le HTML brut a inspecter.

Conduite : un User-Agent identifiable, throttling 3s, cache disque par defaut.
Le scraping enfreint les ToS de DuProprio mais usage personnel non-redistribue
a faible debit est l'usage que l'on accepte ici.
"""
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

from .models import Listing

USER_AGENT = "qc-screener/0.1 (personal real-estate research)"
THROTTLE_SECONDS = 3.0
CACHE_DIR = Path("data/cache/duproprio")
BASE = "https://duproprio.com"
SEARCH_URL = f"{BASE}/fr/multiplex-a-vendre"
_ID_RE = re.compile(r"-(\d{5,})/?$")


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "fr-CA,fr;q=0.9"},
        follow_redirects=True,
        timeout=30,
    )


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.html"


def fetch(url: str, *, use_cache: bool = True) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(url)
    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8")
    time.sleep(THROTTLE_SECONDS)
    with _client() as c:
        r = c.get(url)
        r.raise_for_status()
        cached.write_text(r.text, encoding="utf-8")
        return r.text


def search_listings(max_pages: int = 1, region: str | None = None) -> list[str]:
    urls: list[str] = []
    for page in range(1, max_pages + 1):
        page_url = SEARCH_URL + (f"?pageNumber={page}" if page > 1 else "")
        if region:
            sep = "&" if "?" in page_url else "?"
            page_url += f"{sep}regions={region}"
        html = fetch(page_url)
        tree = HTMLParser(html)
        for li in tree.css("li.search-results-listings-list__item"):
            a = li.css_first('a[property="significantLink"]')
            if a is None:
                a = li.css_first("a[href]")
            if a is None:
                continue
            href = a.attributes.get("href")
            if not href:
                continue
            # Ignore les fiches "vendu" (annonces de temoignages reaffichees).
            if "-vendu-" in href:
                continue
            urls.append(href if href.startswith("http") else BASE + href)
    return list(dict.fromkeys(urls))


def _extract_id(url: str) -> str:
    m = _ID_RE.search(url)
    if m:
        return m.group(1)
    return url.rstrip("/").rsplit("/", 1)[-1]


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    # DuProprio formats: "534 900 $" or "27 500,00 $" — keep digits, drop cents.
    cleaned = s.replace("\xa0", " ").split(",")[0]
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    return float(digits) if digits else None


# Type-slug → nombre de logements (multiplex = 6+ inconnu, fallback None).
_UNITS_FROM_TYPE = {
    "duplex": 2,
    "triplex": 3,
    "quadruplex": 4,
    "quintuplex": 5,
}


def _parse_url_parts(url: str) -> tuple[str | None, str | None, str | None]:
    """(region_slug, city_slug, type_slug) from a DuProprio listing URL."""
    parts = url.split("/fr/", 1)
    if len(parts) != 2:
        return None, None, None
    segments = parts[1].split("/")
    if len(segments) < 4:
        return None, None, None
    region, city, type_seg = segments[0], segments[1], segments[2]
    type_slug = type_seg.replace("-a-vendre", "")
    return region, city, type_slug


def parse_listing(url: str, html: str) -> Listing:
    tree = HTMLParser(html)

    def first_text(sel: str) -> str | None:
        node = tree.css_first(sel)
        return node.text(strip=True) if node else None

    def meta(prop: str) -> str | None:
        node = tree.css_first(f'meta[property="{prop}"]')
        return node.attributes.get("content") if node else None

    # Type de propriete depuis le <a> du h1 (innerText p.ex. "Duplex à vendre"
    # → on droppe le " à vendre" pour garder "Duplex").
    property_type: str | None = None
    type_node = tree.css_first("h1.listing-main__title a")
    if type_node:
        type_node_text = type_node.text(strip=True)
        property_type = re.sub(
            r"\s+(?:à|a)\s+vendre$", "", type_node_text, flags=re.I
        ).strip() or None

    # Adresse civique depuis le premier span de p.listing-location__address.
    first_addr_span: str | None = None
    addr_node_top = tree.css_first("p.listing-location__address")
    if addr_node_top:
        first = addr_node_top.css_first("span")
        if first:
            first_addr_span = first.text(strip=True) or None

    asking_price = _parse_money(first_text(".listing-price__amount"))

    # Latitude / longitude — DuProprio embeds dans un JSON dans la page.
    lat: float | None = None
    lon: float | None = None
    lat_m = re.search(r'"latitude"\s*:\s*(-?\d+\.\d+)', html)
    lon_m = re.search(r'"longitude"\s*:\s*(-?\d+\.\d+)', html)
    if lat_m and lon_m:
        lat = float(lat_m.group(1))
        lon = float(lon_m.group(1))

    # Description — depuis JSON-LD plus complete que la meta SEO.
    description: str | None = None
    desc_m = re.search(r'"description"\s*:\s*"([^"]{50,5000})"', html)
    if desc_m:
        import html as html_mod
        description = html_mod.unescape(desc_m.group(1).replace("\\n", " "))

    addr_node = tree.css_first("p.listing-location__address")
    if addr_node:
        spans = [s.text(strip=True) for s in addr_node.css("span") if s.text(strip=True)]
        address = ", ".join(spans) if spans else addr_node.text(strip=True)
    else:
        address = None

    region, city, type_slug = _parse_url_parts(url)
    units = _UNITS_FROM_TYPE.get(type_slug or "")

    # Titre propre: "Duplex — 858-860 rue Hardy, St-Roch-De-Richelieu"
    descriptor = property_type or (type_slug.replace("-", " ").title() if type_slug else None)
    city_human = city.title() if city else None
    loc_parts = [p for p in (first_addr_span, city_human) if p]
    if descriptor and loc_parts:
        title = f"{descriptor} — {', '.join(loc_parts)}"
    elif descriptor:
        title = descriptor
    else:
        title = meta("og:title") or first_text("h1.listing-main__title a")

    revenue: float | None = None
    eval_: float | None = None
    year_built: int | None = None
    characteristics: dict[str, str] = {}
    for row in tree.css(".listing-box__dotted-row"):
        # selectolax's `row.css("div")` retourne aussi le row lui-meme +
        # le separateur vide; on prend [1] comme label et [-1] comme valeur.
        divs = row.css("div")
        if len(divs) < 3:
            continue
        label = divs[1].text(strip=True).lower()
        value = divs[-1].text(strip=True)
        if not value or value == label:
            continue
        if "évaluation municipale" in label or "evaluation municipale" in label:
            parsed_eval = _parse_money(value)
            # Garde-fou: meme famille que pour les revenus. Des vendeurs entrent
            # "369 $" comme placeholder; aucune evaluation reelle < 20 000 $.
            eval_ = parsed_eval if (parsed_eval is not None and parsed_eval >= 20_000) else None
        elif "revenus annuels" in label or ("revenu" in label and "brut" in label):
            parsed = _parse_money(value)
            # Garde-fou: certains vendeurs entrent "44 $" comme placeholder.
            # Tout revenu annuel realiste pour un multilog est >= 5000 $.
            revenue = parsed if (parsed is not None and parsed >= 5000) else None
        elif "année de construction" in label or "annee de construction" in label:
            digits = "".join(ch for ch in value if ch.isdigit())
            if len(digits) >= 4:
                year_built = int(digits[:4])
        elif "logement" in label or "nombre d'unit" in label:
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                units = int(digits)
        else:
            # On garde les autres caracteristiques utiles (chauffage, parking,
            # superficie du terrain, certificat de localisation, etc.).
            label_text = divs[1].text(strip=True)
            if 2 < len(label_text) < 60:
                characteristics[label_text] = value

    # Taxes annuelles depuis le tableau "Sommaire des depenses" (Desjardins calc).
    municipal_tax: float | None = None
    school_tax: float | None = None
    for row in tree.css(".mortgage-data__table__row"):
        cells = row.css(".mortgage-data__table__row__item")
        if len(cells) < 3:
            continue
        label = cells[0].text(strip=True).lower()
        yearly = _parse_money(cells[2].text(strip=True))
        if not yearly:
            continue
        if "municipal" in label:
            municipal_tax = yearly
        elif "scolaire" in label:
            school_tax = yearly

    return Listing(
        source="duproprio",
        source_id=_extract_id(url),
        url=url,
        title=title,
        address=address,
        city=city,
        region=region,
        asking_price=asking_price,
        municipal_evaluation=eval_,
        annual_gross_revenue=revenue,
        units=units,
        year_built=year_built,
        fetched_at=datetime.now(timezone.utc),
        raw_html_path=str(_cache_path(url)),
        lat=lat,
        lon=lon,
        municipal_tax=municipal_tax,
        school_tax=school_tax,
        characteristics=characteristics,
        description=description,
    )


def fetch_listing(url: str) -> Listing:
    return parse_listing(url, fetch(url))


def crawl_listings(max_pages: int = 1, region: str | None = None):
    """Iterateur de Listing — interface uniforme pour la commande `crawl`."""
    urls = search_listings(max_pages=max_pages, region=region)
    for url in urls:
        try:
            yield fetch_listing(url)
        except Exception as e:
            # Yield None; le caller log/skip.
            print(f"[duproprio] echec {url}: {e}")


def dump_html(url: str, path: str | Path) -> Path:
    p = Path(path)
    p.write_text(fetch(url, use_cache=False), encoding="utf-8")
    return p
