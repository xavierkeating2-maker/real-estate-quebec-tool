"""Scraper LogisQuebec — comparables de loyer via sitemap.

Decouverte: les pages /recherche/* ne SSR qu'environ 5 annonces (le reste
est XHR), mais les sitemaps XML exposent ~8200 URLs d'appartements. On les
recolte gratuitement, puis on fetch chaque page de detail pour:
- Prix:    <meta name="twitter:data1" content="745$ par mois">
- Lieu:    <meta name="twitter:data2" content="Quartier (Ville)">
- Taille:  <div class="caracteristique-titre">N chambres</div>
"""
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from .models import RentComp

USER_AGENT = "qc-screener/0.1 (personal real-estate research)"
THROTTLE_SECONDS = 3.0
CACHE_DIR = Path("data/cache/logisquebec")
BASE = "https://www.logisquebec.com"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"


def _client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-CA,fr;q=0.9",
        },
        follow_redirects=True,
        timeout=30,
    )


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.html"


def _fetch(client: httpx.Client, url: str, *, use_cache: bool = True,
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


def _apartment_urls(client: httpx.Client) -> list[str]:
    """Collecte tous les URLs d'appartements depuis les sitemaps (sans throttle)."""
    idx = _fetch(client, SITEMAP_INDEX, throttle=False)
    sub_sitemaps = re.findall(r"<loc>([^<]+)</loc>", idx)
    urls: list[str] = []
    for sm in sub_sitemaps:
        body = _fetch(client, sm, throttle=False)
        for loc in re.findall(r"<loc>([^<]+)</loc>", body):
            if "appartement-a-louer-" in loc and re.search(r"-l\d+$", loc):
                urls.append(loc)
    # Dedup en conservant l'ordre.
    return list(dict.fromkeys(urls))


# 745$ par mois  ou  1 200 $ / mois
_RENT_RE = re.compile(
    r'twitter:data1"\s*content="([^"]+)"', re.I,
)
_LOC_RE = re.compile(
    r'twitter:data2"\s*content="([^"]+)"', re.I,
)
_BR_RE = re.compile(
    r'caracteristique-titre">\s*(\d+)\s*chambres?\s*</div>', re.I,
)
_PIECES_RE = re.compile(
    r'caracteristique-titre">\s*(\d+)\s*pi[eè]ces?\s*</div>', re.I,
)
_HEAT_RE = re.compile(r'\b(chauff[ée]\s+inclus|chauffage\s+inclus)\b', re.I)
_AREA_RE = re.compile(r'caracteristique-titre">\s*(\d{2,4})\s*pi[²2]\s*</div>', re.I)


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace("\xa0", " ")
    digits = "".join(ch for ch in s if ch.isdigit())
    return float(digits) if digits else None


def _parse_location(raw: str | None) -> tuple[str | None, str | None]:
    """ 'Quartier (Ville)' → (raw, 'Ville').  Fallback: (raw, raw). """
    if not raw:
        return None, None
    m = re.search(r"\(([^)]+)\)\s*$", raw)
    city = m.group(1).strip() if m else raw.strip()
    return raw.strip(), city


def _build_comp(url: str, html: str) -> RentComp | None:
    rent_raw = _RENT_RE.search(html)
    loc_raw = _LOC_RE.search(html)
    br_match = _BR_RE.search(html)
    rent = _parse_money(rent_raw.group(1)) if rent_raw else None
    if not rent:
        return None
    address, city = _parse_location(loc_raw.group(1) if loc_raw else None)
    bedrooms = int(br_match.group(1)) if br_match else None
    if bedrooms is None:
        pcm = _PIECES_RE.search(html)
        if pcm:
            # X pieces = X 1/2 → bedrooms = X - 2 (convention QC).
            x = int(pcm.group(1))
            bedrooms = max(0, x - 2)
    size_label = f"{bedrooms} ch" if bedrooms is not None else None
    area_match = _AREA_RE.search(html)
    area = float(area_match.group(1)) if area_match else None
    heat = bool(_HEAT_RE.search(html)) if rent else None
    listing_id_match = re.search(r"-l(\d+)$", url)
    source_id = listing_id_match.group(1) if listing_id_match else url.rsplit("/", 1)[-1]

    return RentComp(
        source="logisquebec",
        source_id=source_id,
        url=url,
        fetched_at=datetime.now(timezone.utc),
        title=address,
        address=address,
        city=city,
        monthly_rent=rent,
        size_label=size_label,
        bedrooms=bedrooms,
        area_sqft=area,
        heat_included=heat,
    )


def crawl_rent_comps(max_pages: int = 1, max_listings: int = 300) -> Iterator[RentComp]:
    """Iter de RentComp. `max_pages` ignore (sitemap est plat); utiliser max_listings."""
    with _client() as c:
        urls = _apartment_urls(c)
        # Echantillonnage uniforme: pas constant pour couvrir toute la liste.
        if len(urls) > max_listings:
            step = len(urls) // max_listings
            urls = urls[::max(1, step)][:max_listings]
        for url in urls:
            try:
                html = _fetch(c, url)
            except Exception as e:
                print(f"[logisquebec] echec {url}: {e}")
                continue
            comp = _build_comp(url, html)
            if comp and comp.monthly_rent and 300 <= comp.monthly_rent < 15000:
                yield comp


def dump_html(url: str, path: str | Path) -> Path:
    with _client() as c:
        html = _fetch(c, url, use_cache=False)
    p = Path(path)
    p.write_text(html, encoding="utf-8")
    return p
