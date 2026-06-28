"""Scraper Kijiji — comparables de loyer pour les multilogements QC.

Approche: Kijiji est une app Next.js + Apollo. Les annonces sont serialisees
dans <script id="__NEXT_DATA__"> sous props.pageProps.__APOLLO_STATE__ avec
des cles "RealEstateListing:<id>". On extrait directement le JSON, pas besoin
de fetch detail par annonce.

Pagination: /b-appartements-condos/quebec/page-<N>/c37l9001
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from .models import RentComp

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
THROTTLE_SECONDS = 4.0
CACHE_DIR = Path("data/cache/kijiji")
BASE = "https://www.kijiji.ca"
# Categorie Apartments-Condos, location 9001 = Quebec province
SEARCH_PATH = "/b-appartements-condos/quebec"
CATEGORY_SUFFIX = "/c37l9001"

# X 1/2  ou  X½  →  nombre de chambres a coucher.
# Convention QC: 2½=studio(0), 3½=1ch, 4½=2ch, 5½=3ch, 6½=4ch.
_HALVES_TO_BR = {2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6}


def _client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        },
        follow_redirects=True,
        timeout=30,
    )


def _cache_path(key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.html"


def _fetch(client: httpx.Client, url: str, *, use_cache: bool = True) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(url)
    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8")
    time.sleep(THROTTLE_SECONDS)
    r = client.get(url)
    r.raise_for_status()
    cached.write_text(r.text, encoding="utf-8")
    return r.text


def _extract_apollo(html: str) -> dict | None:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        html, re.S,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__")


# Patterns pour extraire la taille du logement depuis le titre / description.
# Ordre: half-notation (4 1/2, 4½, 4.5) avant "X chambre/bedroom".
_HALF_RE = re.compile(r"(?<!\d)([2-8])\s*(?:1\s*/\s*2|½|\.5)\s*(?!\d)")
_BR_RE = re.compile(r"\b(\d)\s*(?:chambre|bedroom|bdrm|br|cc)\b", re.I)
_STUDIO_RE = re.compile(r"\b(studio|bachelor)\b", re.I)


def _parse_size(text: str) -> tuple[str | None, int | None]:
    """Retourne (label_brut, nb_chambres) ou (None, None)."""
    if not text:
        return None, None
    m = _HALF_RE.search(text)
    if m:
        halves = int(m.group(1))
        return f"{halves} 1/2", _HALVES_TO_BR.get(halves)
    m = _BR_RE.search(text)
    if m:
        br = int(m.group(1))
        return f"{br} ch", br
    if _STUDIO_RE.search(text):
        return "studio", 0
    return None, None


def _build_comp(obj: dict) -> RentComp | None:
    """Construit un RentComp depuis une entree Apollo RealEstateListing."""
    listing_id = obj.get("id")
    url = obj.get("url")
    if not listing_id or not url:
        return None

    price = obj.get("price") or {}
    # Kijiji stocke en cents (229500 = 2295 $).
    amount = price.get("amount")
    monthly_rent = amount / 100 if isinstance(amount, (int, float)) and amount > 0 else None

    location = obj.get("location") or {}
    city = location.get("name")
    address = location.get("address")
    coords = location.get("coordinates") or {}

    title = obj.get("title")
    description = obj.get("description")
    size_label, bedrooms = _parse_size(f"{title or ''} {description or ''}")

    # Attributes Kijiji: heat, hydro, parking, yard, date dispo, etc.
    heat_included: bool | None = None
    extras: dict[str, str] = {}
    for attr in (obj.get("attributes") or {}).get("all") or []:
        cname = attr.get("canonicalName")
        vals = attr.get("canonicalValues") or []
        if not cname or not vals:
            continue
        if cname == "heat":
            heat_included = vals == ["1"]
        # On garde tous les autres comme strings utiles (numberparkingspots, dateavailable, etc.).
        extras[cname] = ",".join(str(v) for v in vals)

    return RentComp(
        source="kijiji",
        source_id=str(listing_id),
        url=url,
        fetched_at=datetime.now(timezone.utc),
        title=title,
        description=description,
        city=city,
        address=address,
        lat=coords.get("latitude"),
        lon=coords.get("longitude"),
        monthly_rent=monthly_rent,
        size_label=size_label,
        bedrooms=bedrooms,
        heat_included=heat_included,
        characteristics=extras,
    )


def _page_url(page: int) -> str:
    if page <= 1:
        return f"{BASE}{SEARCH_PATH}{CATEGORY_SUFFIX}"
    return f"{BASE}{SEARCH_PATH}/page-{page}{CATEGORY_SUFFIX}"


def crawl_rent_comps(max_pages: int = 1) -> Iterator[RentComp]:
    with _client() as c:
        for page in range(1, max_pages + 1):
            url = _page_url(page)
            try:
                html = _fetch(c, url)
            except Exception as e:
                print(f"[kijiji] echec page {page}: {e}")
                continue
            apollo = _extract_apollo(html)
            if not apollo:
                print(f"[kijiji] aucun __APOLLO_STATE__ sur page {page}, stop")
                break
            count = 0
            for key, obj in apollo.items():
                if not key.startswith("RealEstateListing:"):
                    continue
                comp = _build_comp(obj)
                if comp and comp.monthly_rent and comp.monthly_rent < 15000:
                    # Filtre les prix de vente ($229,500) — rent comps only.
                    yield comp
                    count += 1
            if count == 0:
                # Page sans annonces - probablement passe la fin du catalogue.
                break


def dump_html(url: str, path: str | Path) -> Path:
    with _client() as c:
        html = _fetch(c, url, use_cache=False)
    p = Path(path)
    p.write_text(html, encoding="utf-8")
    return p
