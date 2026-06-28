"""Scraper Centris — catalogue le plus complet pour le multilogement QC.

Decouverte: contrairement aux peurs sur la protection Akamai, le site repond
en plain HTML aux requetes httpx avec un UA navigateur, sans defi. La
pagination utilise l'endpoint XHR /Property/GetInscriptions (POST JSON)
qui retourne un fragment HTML par page. Les pages detail sont aussi
accessibles directement.

Catalogue: ~4400 plex (vs 482 DuProprio + 238 ProprioDirect — 6x notre
volume actuel combine).

Conduite: User-Agent identifiable comme navigateur recent, throttling 4s,
cache disque par defaut.
"""
import hashlib
import html
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
from selectolax.parser import HTMLParser

from .models import Listing

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
THROTTLE_SECONDS = 4.0
CACHE_DIR = Path("data/cache/centris")
BASE = "https://www.centris.ca"
SEARCH_URL = f"{BASE}/fr/plex~a-vendre"
INSCRIPTIONS_API = f"{BASE}/Property/GetInscriptions"
PAGE_SIZE = 20

# Specification du filtre (gzip+base64 decode du parametre q sur la page).
SEARCH_QUERY: dict = {
    "mls": "0",
    "brokerCode": "",
    "officeKey": "",
    "useGeographyShapes": 0,
    "shapeViews": [],
    "searchName": "",
    "filters": [],
    "fieldsValues": [
        {"fieldId": "Category", "value": "Residential"},
        {"fieldId": "SellingType", "value": "Sale"},
        {"fieldId": "PropertyType", "value": "Plex"},
    ],
}

# Type-slug dans l'URL → nombre de logements (override possible par le DOM).
_UNITS_FROM_TYPE = {
    "duplex": 2,
    "triplex": 3,
    "quadruplex": 4,
    "quintuplex": 5,
}

# Regex regex de capture: "Résidentiel (2)" → 2.
_NB_UNITES_RE = re.compile(r"\((\d+)\)")


def _client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-CA,fr;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        },
        follow_redirects=True,
        timeout=30,
    )


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.html"


def _fetch_html(client: httpx.Client, url: str, *, use_cache: bool = True,
                throttle: bool = True) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(url)
    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8")
    if throttle:
        time.sleep(THROTTLE_SECONDS)
    r = client.get(url)
    r.raise_for_status()
    cached.write_text(r.text, encoding="utf-8")
    return r.text


def _prime_session(client: httpx.Client) -> tuple[int, int]:
    """Etablit les cookies + recupere sortSeed et itemsCount."""
    text = _fetch_html(client, SEARCH_URL)
    sort_seed_match = re.search(r'id="sortSeed">(\d+)<', text)
    sort_seed = int(sort_seed_match.group(1)) if sort_seed_match else 0
    count_match = re.search(r"itemsCount:\s*(\d+)", text)
    count = int(count_match.group(1)) if count_match else 0
    return sort_seed, count


def _fetch_search_page(client: httpx.Client, page: int, sort_seed: int) -> str:
    """Recupere la liste d'annonces de la page N via l'API XHR.

    Retourne le fragment HTML (chaine vide si echec)."""
    time.sleep(THROTTLE_SECONDS)
    payload = {
        "mode": "Result",
        "searchView": "Thumbnail",
        "sortSeed": sort_seed,
        "sort": "None",
        "pageSize": PAGE_SIZE,
        "page": page,
        "query": SEARCH_QUERY,
        "region": "Quebec",
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=utf-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": SEARCH_URL,
    }
    r = client.post(INSCRIPTIONS_API, json=payload, headers=headers)
    r.raise_for_status()
    data = r.json()
    if not data.get("d", {}).get("Succeeded"):
        return ""
    return data["d"]["Result"].get("html") or ""


def _extract_listing_urls(fragment: str) -> list[str]:
    """Extrait les URLs de detail depuis le fragment HTML de la page de resultats."""
    pattern = r'href="(/fr/[a-z0-9~\-]+~a-vendre~[a-z0-9~\-]+/\d{6,})"'
    return list(dict.fromkeys(re.findall(pattern, fragment)))


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace("\xa0", " ").replace("&#xA0;", " ")
    digits = "".join(ch for ch in s if ch.isdigit())
    return float(digits) if digits else None


def _parse_url(url: str) -> tuple[str | None, str | None, str | None]:
    """Retourne (type_slug, city_slug, source_id)."""
    m = re.match(r"^/?fr/([a-z\-]+)~a-vendre~([a-z0-9~\-]+)/(\d+)$", url)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3)


def _carac_value(tree, label: str) -> str | None:
    """Retourne la valeur d'une boite carac dont .carac-title matche `label`."""
    for box in tree.css(".carac-container"):
        title_node = box.css_first(".carac-title")
        if not title_node:
            continue
        if label.lower() not in title_node.text(strip=True).lower():
            continue
        value_node = box.css_first(".carac-value span")
        if value_node:
            return value_node.text(strip=True)
    return None


def _decode(s: str) -> str:
    """Decode HTML entities; Centris emit &#xE2; au lieu de â p.ex."""
    return html.unescape(s)


def _eval_total(html_text: str) -> float | None:
    """Extrait le Total de la table Évaluation municipale."""
    m = re.search(
        r"valuation municipale[\s\S]{0,2000}?financial-details-table-total"
        r"[\s\S]{0,200}?text-right\">\s*([^<]+?)\s*</td>",
        _decode(html_text),
    )
    return _parse_money(m.group(1)) if m else None


def _eval_part(html_text: str, label: str) -> float | None:
    """Terrain ou Bâtiment dans la section Évaluation municipale."""
    m = re.search(
        r"valuation municipale[\s\S]{0,1500}?<td>\s*"
        + label
        + r"\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>",
        _decode(html_text),
    )
    return _parse_money(m.group(1)) if m else None


def _tax_yearly(html_text: str, label: str) -> float | None:
    """Récupère taxes municipales/scolaires depuis financial-details-table-yearly."""
    m = re.search(
        r"financial-details-table-yearly[\s\S]{0,2000}?<td>\s*"
        + label
        + r"\s*\([^)]+\)\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>",
        _decode(html_text),
    )
    return _parse_money(m.group(1)) if m else None


def parse_detail(url: str, page_html: str) -> Listing:
    full_url = url if url.startswith("http") else BASE + url
    type_slug, city_slug, source_id = _parse_url(url if not url.startswith("http") else url[len(BASE):])
    tree = HTMLParser(page_html)

    # Prix demande
    price_meta = tree.css_first('meta[itemprop="price"]')
    asking_price = _parse_money(price_meta.attributes.get("content")) if price_meta else None

    # Annee de construction
    year_built: int | None = None
    yc = _carac_value(tree, "Année de construction") or _carac_value(tree, "Annee de construction")
    if yc:
        digits = "".join(ch for ch in yc if ch.isdigit())
        if len(digits) >= 4:
            year_built = int(digits[:4])

    # Nombre de logements (override sur le slug si dispo)
    units = _UNITS_FROM_TYPE.get(type_slug or "")
    nb_unites_raw = _carac_value(tree, "Nombre d") or _carac_value(tree, "unite")
    if nb_unites_raw:
        m = _NB_UNITES_RE.search(nb_unites_raw)
        if m:
            units = int(m.group(1))

    # Revenus bruts
    revenue_raw = _carac_value(tree, "Revenus bruts")
    revenue = _parse_money(revenue_raw)
    if revenue is not None and revenue < 5000:
        revenue = None

    # Eval municipale (Total + breakdown Terrain/Bâtiment)
    eval_total = _eval_total(page_html)
    eval_land = _eval_part(page_html, "Terrain")
    eval_building = _eval_part(page_html, "Bâtiment")

    # Taxes annuelles
    municipal_tax = _tax_yearly(page_html, "Municipales")
    school_tax = _tax_yearly(page_html, "Scolaires")

    # Caractéristiques flexibles
    characteristics: dict[str, str] = {}
    for box in tree.css(".carac-container"):
        title_node = box.css_first(".carac-title")
        value_node = box.css_first(".carac-value span") or box.css_first(".carac-value")
        if title_node and value_node:
            characteristics[title_node.text(strip=True)] = value_node.text(strip=True)

    # Description complete (la meta SEO est tronquee).
    description: str | None = None
    desc_node = tree.css_first('div[itemprop="description"]')
    if desc_node:
        description = desc_node.text(strip=True) or None

    # Lat/lon — Centris injecte `var latitude = X; var longitude = Y;` dans le JS.
    lat: float | None = None
    lon: float | None = None
    lat_m = re.search(r"\blatitude\s*=\s*(-?\d+\.\d+)", page_html)
    lon_m = re.search(r"\blongitude\s*=\s*(-?\d+\.\d+)", page_html)
    if lat_m and lon_m:
        lat = float(lat_m.group(1))
        lon = float(lon_m.group(1))

    # Adresse + ville
    addr_node = tree.css_first('h2[itemprop="address"]')
    address = addr_node.text(strip=True) if addr_node else None
    city_human = city_slug.replace("-", " ").title() if city_slug else None

    # Region extraite de la balise <title>:
    # "Duplex à vendre à Dégelis, Bas-Saint-Laurent, 523 - 525, ..." → index 1 = region.
    region: str | None = None
    title_node = tree.css_first("title")
    if title_node:
        parts = [p.strip() for p in title_node.text().split(",")]
        # Parts: ["Type à vendre à City", "Region", "Address...", "ID - Centris.ca"]
        if len(parts) >= 2:
            region = parts[1] or None

    # Titre
    type_human = type_slug.replace("-", " ").title() if type_slug else "Plex"
    loc_parts = [p for p in (address, city_human) if p]
    title = f"{type_human} — {', '.join(loc_parts)}" if loc_parts else type_human

    return Listing(
        source="centris",
        source_id=source_id or full_url.rsplit("/", 1)[-1],
        url=full_url,
        title=title,
        address=address,
        city=city_slug,
        region=region,
        asking_price=asking_price,
        municipal_evaluation=eval_total,
        annual_gross_revenue=revenue,
        units=units,
        year_built=year_built,
        fetched_at=datetime.now(timezone.utc),
        raw_html_path=str(_cache_path(full_url)),
        lat=lat,
        lon=lon,
        eval_land=eval_land,
        eval_building=eval_building,
        municipal_tax=municipal_tax,
        school_tax=school_tax,
        characteristics=characteristics,
        description=description,
    )


def crawl_listings(max_pages: int = 5, region: str | None = None) -> Iterator[Listing]:
    """Iter les Listing Centris. `region` non utilise (filtre fait coté query si on veut)."""
    with _client() as c:
        sort_seed, total = _prime_session(c)
        for page in range(1, max_pages + 1):
            fragment = _fetch_search_page(c, page, sort_seed)
            if not fragment:
                break
            urls = _extract_listing_urls(fragment)
            if not urls:
                break
            for path in urls:
                full = BASE + path
                try:
                    html_text = _fetch_html(c, full)
                except Exception as e:
                    print(f"[centris] echec detail {full}: {e}")
                    continue
                try:
                    yield parse_detail(path, html_text)
                except Exception as e:
                    print(f"[centris] parse echec {full}: {e}")


def dump_html(url: str, path: str | Path) -> Path:
    with _client() as c:
        text = _fetch_html(c, url, use_cache=False)
    p = Path(path)
    p.write_text(text, encoding="utf-8")
    return p
