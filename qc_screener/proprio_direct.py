"""Scraper ProprioDirect via leur API JSON publique + parse HTML detail.

Decouverte:
- POST https://propriodirect.com/fr/api/searchListings avec
  filter.genre='multiplex' renvoie un JSON structure (id, prix, adresse,
  genre, slug URL).
- La page de detail HTML contient annee de construction, revenus bruts
  potentiels, evaluation municipale (Total).

Conduite: User-Agent identifiable, throttling 3s, cache disque par defaut.
"""
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from .models import Listing

USER_AGENT = "qc-screener/0.1 (personal real-estate research)"
THROTTLE_SECONDS = 3.0
CACHE_DIR = Path("data/cache/proprio_direct")
BASE = "https://propriodirect.com"
SEARCH_API = f"{BASE}/fr/api/searchListings"
PAGE_SIZE = 30

# Code genre PD → nombre de logements (fallback aussi par nom).
_UNITS_FROM_GENRE = {"DX": 2, "TX": 3, "4X": 4, "5X": 5}
_UNITS_FROM_NAME = [
    ("quintuplex", 5),
    ("quadruplex", 4),
    ("triplex", 3),
    ("duplex", 2),
]


def _client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-CA,fr;q=0.9",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"{BASE}/recherche/",
        },
        follow_redirects=True,
        timeout=30,
    )


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.html"


def _fetch_detail(client: httpx.Client, url: str, *, use_cache: bool = True) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(url)
    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8")
    time.sleep(THROTTLE_SECONDS)
    r = client.get(url, headers={"Accept": "text/html"})
    r.raise_for_status()
    cached.write_text(r.text, encoding="utf-8")
    return r.text


def _search_page(client: httpx.Client, page_index: int) -> dict:
    body = {
        "query": "",
        "filter": {"genre": "multiplex"},
        "from": page_index * PAGE_SIZE,
        "includeGeoJson": False,
    }
    if page_index > 0:
        time.sleep(THROTTLE_SECONDS)
    r = client.post(SEARCH_API, json=body)
    r.raise_for_status()
    return r.json()


def _parse_money(s: str | None) -> float | None:
    """Parse "27 500,00 $" (FR), "484,100 $" (EN-CA), "$1,234.56" (US).

    Heuristique: si le dernier separateur (, ou .) est suivi d'exactement 2
    chiffres avant la fin / le $, c'est un decimal — droppe. Sinon c'est un
    separateur de milliers — ignore.
    """
    if not s:
        return None
    s = s.replace("\xa0", " ")
    m = re.search(r"[,.](\d{2})\s*\$?\s*$", s)
    if m:
        s = s[: m.start()]
    digits = "".join(ch for ch in s if ch.isdigit())
    return float(digits) if digits else None


def _highlight(html: str, label_regex: str) -> str | None:
    m = re.search(
        r"<div>\s*(?:" + label_regex + r")\s*</div>\s*<div>\s*([^<]+?)\s*</div>",
        html, re.I,
    )
    return m.group(1).strip() if m else None


def _eval_total(html: str) -> float | None:
    m = re.search(
        r"Évaluation municipale[\s\S]{0,800}?<div>\s*Total\s*</div>\s*<div>\s*([^<]+?)\s*</div>",
        html,
    )
    return _parse_money(m.group(1)) if m else None


def _eval_part(html: str, label: str) -> float | None:
    """Récupère Terrain ou Bâtiment depuis la section Évaluation municipale."""
    m = re.search(
        r"Évaluation municipale[\s\S]{0,800}?<div>\s*"
        + label
        + r"\s*</div>\s*<div>\s*([^<]+?)\s*</div>",
        html,
    )
    return _parse_money(m.group(1)) if m else None


def _tax(html: str, kind: str) -> float | None:
    """Récupère Taxes municipales ou scolaires (montant annuel)."""
    # "Taxes municipales (2026)" suivi du montant
    m = re.search(
        r"Taxes\s+" + kind + r"\s*\([^)]+\)\s*</div>\s*<div>\s*([^<]+?)\s*</div>",
        html, re.I,
    )
    return _parse_money(m.group(1)) if m else None


def _units(api_entry: dict) -> int | None:
    code = api_entry.get("genre") or ""
    if code in _UNITS_FROM_GENRE:
        return _UNITS_FROM_GENRE[code]
    name = (api_entry.get("genreName") or "").lower()
    for keyword, n in _UNITS_FROM_NAME:
        if keyword in name:
            return n
    return None


def _build_listing(api_entry: dict, html: str | None) -> Listing:
    slug = api_entry.get("slugURLFr") or ""
    url = BASE + slug if slug.startswith("/") else slug

    revenue: float | None = None
    eval_total: float | None = None
    eval_land: float | None = None
    eval_building: float | None = None
    municipal_tax: float | None = None
    school_tax: float | None = None
    year_built: int | None = None
    description: str | None = None
    characteristics: dict[str, str] = {}
    if html:
        raw_year = _highlight(html, r"Ann[éeèê]e de construction")
        if raw_year:
            d = "".join(ch for ch in raw_year if ch.isdigit())
            if len(d) >= 4:
                year_built = int(d[:4])
        raw_rev = _highlight(html, r"Revenus bruts (?:potentiels|annuels)")
        if raw_rev:
            parsed = _parse_money(raw_rev)
            # Meme garde-fou que DuProprio: floor 5000 $ pour rejeter les saisies bidon.
            revenue = parsed if (parsed is not None and parsed >= 5000) else None
        eval_total = _eval_total(html)
        eval_land = _eval_part(html, "Terrain")
        eval_building = _eval_part(html, "Bâtiment")
        municipal_tax = _tax(html, "municipales")
        school_tax = _tax(html, "scolaires")

        # Description: JSON-LD est le plus propre.
        desc_m = re.search(r'"description"\s*:\s*"([^"]{50,5000})"', html)
        if desc_m:
            import html as html_mod
            description = html_mod.unescape(desc_m.group(1).replace("\\n", " "))

        # Caracteristiques flexibles depuis la section "Détails" (meme pattern label/value).
        # On reutilise le mecanisme de _highlight mais en iterant: on capture toutes les
        # paires <div>label</div><div>value</div> dans le bloc Detail.
        for m in re.finditer(
            r"<div>\s*([^<]{2,60})\s*</div>\s*<div>\s*([^<]{1,200})\s*</div>",
            html,
        ):
            label = m.group(1).strip()
            value = m.group(2).strip()
            if not label or not value or "$" in label or "$" in value:
                # On ignore les rows financieres (taxes/eval) deja captures plus haut.
                continue
            if label.lower() in ("terrain", "bâtiment", "total"):
                continue
            if 2 < len(label) < 60 and 1 < len(value) < 200:
                characteristics.setdefault(label, value)

    price = api_entry.get("priceInCents")
    asking_price = price / 100 if price else None

    # Lat/lon depuis l'API.
    geo = api_entry.get("geoLocation") or {}
    lat = geo.get("lat") or geo.get("latitude")
    lon = geo.get("lon") or geo.get("longitude")

    # Date d'inscription depuis l'API.
    posted_iso = api_entry.get("inscriptionDate")
    posted_dt = None
    if posted_iso:
        try:
            posted_dt = datetime.fromisoformat(posted_iso.replace("Z", "+00:00"))
        except ValueError:
            posted_dt = None

    # Titre propre: "Quadruplex — 71-71C Rue Ste-Anne, Saint-Jacques"
    addr_line = api_entry.get("addressLine") or None
    city_name = api_entry.get("cityName") or None
    genre = api_entry.get("genreName") or None
    loc_parts = [p for p in (addr_line, city_name) if p]
    if genre and loc_parts:
        title = f"{genre} — {', '.join(loc_parts)}"
    else:
        title = addr_line or city_name

    return Listing(
        source="propriodirect",
        source_id=str(api_entry["id"]),
        url=url,
        title=title,
        address=addr_line,
        city=city_name,
        region=api_entry.get("regionName") or None,
        postal_code=api_entry.get("postalCode") or None,
        asking_price=asking_price,
        municipal_evaluation=eval_total,
        annual_gross_revenue=revenue,
        units=_units(api_entry),
        year_built=year_built,
        fetched_at=datetime.now(timezone.utc),
        raw_html_path=str(_cache_path(url)) if html else None,
        lat=lat,
        lon=lon,
        eval_land=eval_land,
        eval_building=eval_building,
        municipal_tax=municipal_tax,
        school_tax=school_tax,
        date_posted=posted_dt,
        description=description,
        characteristics=characteristics,
    )


def crawl_listings(max_pages: int = 1, region: str | None = None) -> Iterator[Listing]:
    """Iter des Listing pour multiplex ProprioDirect.

    `region` n'est pas appliquee actuellement (l'API utilise geoCode/geoType
    qu'on n'a pas exposes pour cette V1).
    """
    with _client() as c:
        for page in range(max_pages):
            payload = _search_page(c, page)
            for entry in payload.get("listings", []) or []:
                slug = entry.get("slugURLFr") or ""
                if not slug:
                    continue
                url = BASE + slug if slug.startswith("/") else slug
                try:
                    html = _fetch_detail(c, url)
                except Exception:
                    html = None
                yield _build_listing(entry, html)
            total_pages = payload.get("totalPages") or 1
            if page + 1 >= total_pages:
                break


def dump_html(url: str, path: str | Path) -> Path:
    with _client() as c:
        html = _fetch_detail(c, url, use_cache=False)
    p = Path(path)
    p.write_text(html, encoding="utf-8")
    return p
