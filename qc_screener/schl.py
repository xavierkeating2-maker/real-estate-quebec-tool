"""Taux d'inoccupation SCHL/CMHC via StatCan Web Data Service.

Sources:
- StatCan 34-10-0127-01: taux d'inoccupation par RMR (Montreal, Quebec, etc.)
- StatCan 34-10-0132-01: taux d'inoccupation par agglomeration (AR) plus petite

Ensemble: ~36 centres du QC. Mises a jour annuelles (~mars) par CMHC via
leur "Rapport sur le marche locatif". Public, gratuit, CSV bulk download.

Retourne le taux en pourcentage (ex: 2.5 = 2.5% de vacance) et une fraction
pour usage direct dans DealInputs.vacancy_rate.
"""
from __future__ import annotations

import csv
import io
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

USER_AGENT = "qc-screener/0.1 (personal real-estate research)"
CACHE_DIR = Path("data/cache/schl")
META_NAME = "vacancy.meta"                    # simple timestamp file

# Deux tables complementaires (RMR + AR) — merge en une seule dict city→rate.
TABLES = {
    "34100127": "vacancy_cma.csv",            # RMR: Montreal, Quebec, Gatineau, etc.
    "34100132": "vacancy_ca.csv",             # AR: Rimouski, Alma, Val-d'Or, etc.
    "34100133": "rents.csv",                  # Loyer moyen par centre × type de logt (Bachelor/1BR/2BR/3BR)
}
WDS_URL = "https://www150.statcan.gc.ca/t1/wds/rest/getFullTableDownloadCSV/{tid}/en"

# Nombre d'annees pour calculer un CAGR de croissance de loyer (lisse le pic).
RENT_CAGR_YEARS = 3


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60
    )


def _cache_paths() -> dict[str, Path]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {tid: CACHE_DIR / name for tid, name in TABLES.items()}


def refresh(force: bool = False) -> dict[str, int]:
    """Telecharge (ou re-telecharge) les deux tables StatCan.

    Retourne {table_id: lignes_du_csv}.
    """
    counts: dict[str, int] = {}
    paths = _cache_paths()
    with _client() as c:
        for tid, path in paths.items():
            if path.exists() and not force:
                counts[tid] = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
                continue
            wds = c.get(WDS_URL.format(tid=tid)).json()
            r = c.get(wds["object"])
            r.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(r.content))
            csv_name = next(
                (n for n in z.namelist() if n.endswith(".csv") and "MetaData" not in n),
                None,
            )
            if not csv_name:
                raise RuntimeError(f"CSV introuvable dans le zip StatCan {tid}.")
            with z.open(csv_name) as f:
                text = f.read().decode("utf-8-sig")
            path.write_text(text, encoding="utf-8")
            counts[tid] = sum(1 for _ in text.splitlines())
    (CACHE_DIR / META_NAME).write_text(
        datetime.now(timezone.utc).isoformat(), encoding="utf-8"
    )
    return counts


def last_refresh_at() -> str | None:
    meta = CACHE_DIR / META_NAME
    if not meta.exists():
        return None
    return meta.read_text(encoding="utf-8").strip()


# Villes-satellites qui n'ont pas leur propre entree StatCan mais qui font
# partie d'une RMR. Mappe vers le nom-clef normalise (accents-strippes, lowercase).
SATELLITE_TO_CMA = {
    # RMR Montreal — banlieues + arrondissements
    "laval": "montreal",
    "longueuil": "montreal",
    "brossard": "montreal",
    "terrebonne": "montreal",
    "mascouche": "montreal",
    "repentigny": "montreal",
    "boucherville": "montreal",
    "saint-hubert": "montreal",
    "saint-bruno-de-montarville": "montreal",
    "sainte-julie": "montreal",
    "chateauguay": "montreal",
    "mirabel": "montreal",
    "blainville": "montreal",
    "vaudreuil-dorion": "montreal",
    "saint-eustache": "montreal",
    "saint-lambert": "montreal",
    "saint-jean-sur-richelieu": "montreal",
    "candiac": "montreal",
    "la prairie": "montreal",
    "beloeil": "montreal",
    "montreal-nord": "montreal",
    "saint-leonard": "montreal",
    "verdun": "montreal",
    "lasalle": "montreal",
    "anjou": "montreal",
    "montreal-est": "montreal",
    "westmount": "montreal",
    "outremont": "montreal",
    # RMR Quebec
    "levis": "quebec",
    "sainte-foy": "quebec",
    "beauport": "quebec",
    "charlesbourg": "quebec",
    # RMR Ottawa-Gatineau (partie QC)
    "gatineau": "ottawa-gatineau",
    "hull": "ottawa-gatineau",
    "aylmer": "ottawa-gatineau",
    # RMR Saguenay
    "chicoutimi": "saguenay",
    "jonquiere": "saguenay",
    # RMR Sherbrooke
    "lennoxville": "sherbrooke",
}


def _strip_accents_lower(s: str | None) -> str:
    """Normalise pour matching robuste: accents strippes, lowercase, espaces
    reduits, on garde uniquement le nom de ville (avant la virgule)."""
    if not isinstance(s, str) or not s:
        return ""
    head = s.split(",")[0].strip()
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", head)
        if unicodedata.category(c) != "Mn"
    )
    return stripped.lower()


def load_rates() -> tuple[dict[str, tuple[int, float]], float | None]:
    """Charge {ville_normalisee: (annee, taux_pct)} pour tous les centres du QC.

    Fusionne les deux tables StatCan (RMR + AR). Pour chaque ville,
    garde la valeur la plus recente disponible.
    Retourne aussi la moyenne provinciale QC (moyenne des taux les plus
    recents disponibles pour l'annee la plus recente).
    """
    latest_by_city: dict[str, tuple[int, float]] = {}
    vacancy_tables = {"34100127", "34100132"}
    for tid, path in _cache_paths().items():
        if tid not in vacancy_tables or not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            geo = row.get("GEO", "")
            if ", Quebec" not in geo and "Quebec/Ontario" not in geo:
                continue
            raw_val = (row.get("VALUE") or "").strip()
            if not raw_val:
                continue
            try:
                value = float(raw_val)
                year = int(row["REF_DATE"])
            except (ValueError, KeyError):
                continue
            key = _strip_accents_lower(geo)
            prev = latest_by_city.get(key)
            if prev is None or year > prev[0]:
                latest_by_city[key] = (year, value)
    if not latest_by_city:
        return {}, None
    latest_year = max(y for y, _ in latest_by_city.values())
    same_year = [v for y, v in latest_by_city.values() if y == latest_year]
    qc_avg = sum(same_year) / len(same_year) if same_year else None
    return latest_by_city, qc_avg


def load_rent_growth() -> tuple[dict[str, tuple[int, float]], float | None]:
    """Charge {ville_normalisee: (annee_terminale, cagr_3ans)} de la croissance
    des loyers moyens (aggregation Bachelor + 1BR + 2BR + 3BR par ville pour
    lisser le mix).

    Retourne aussi la mediane provinciale QC comme fallback.
    """
    rent_path = _cache_paths().get("34100133")
    if rent_path is None or not rent_path.exists():
        return {}, None

    text = rent_path.read_text(encoding="utf-8-sig")
    # {(city, year): [rent_values_all_unit_types]}
    accum: dict[tuple[str, int], list[float]] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        geo = row.get("GEO", "")
        if ", Quebec" not in geo and "Quebec/Ontario" not in geo:
            continue
        # On garde uniquement les structures multi-loger (3+ units) — c'est ce qui matche Lepine.
        if "three units and over" not in (row.get("Type of structure") or ""):
            continue
        raw_val = (row.get("VALUE") or "").strip()
        if not raw_val:
            continue
        try:
            value = float(raw_val)
            year = int(row["REF_DATE"])
        except (ValueError, KeyError):
            continue
        key = _strip_accents_lower(geo)
        accum.setdefault((key, year), []).append(value)

    # Loyer moyen par ville×annee = moyenne des types disponibles
    avg_rent: dict[tuple[str, int], float] = {
        k: sum(v) / len(v) for k, v in accum.items() if v
    }
    # CAGR sur RENT_CAGR_YEARS pour chaque ville — utilise la fenetre la plus recente disponible.
    cagr_by_city: dict[str, tuple[int, float]] = {}
    cities = {c for c, _ in avg_rent}
    for c in cities:
        years = sorted(y for cc, y in avg_rent if cc == c)
        if len(years) < RENT_CAGR_YEARS + 1:
            continue
        end_year = years[-1]
        start_year = end_year - RENT_CAGR_YEARS
        if (c, start_year) not in avg_rent:
            continue
        r_end = avg_rent[(c, end_year)]
        r_start = avg_rent[(c, start_year)]
        if r_start <= 0:
            continue
        cagr = (r_end / r_start) ** (1 / RENT_CAGR_YEARS) - 1
        cagr_by_city[c] = (end_year, cagr)

    if not cagr_by_city:
        return {}, None
    latest_year = max(y for y, _ in cagr_by_city.values())
    same_year = [g for y, g in cagr_by_city.values() if y == latest_year]
    qc_median = sorted(same_year)[len(same_year) // 2] if same_year else None
    return cagr_by_city, qc_median


def rent_growth_estimate(city: str | None) -> tuple[float, str] | None:
    """Retourne (fraction, source_label) pour la croissance annuelle des loyers.

    Meme logique de matching que vacancy_rate_estimate (satellite → RMR → sous-chaine → moyenne QC).
    """
    rates, qc_median = load_rent_growth()
    if not rates:
        return None
    if city:
        key = _strip_accents_lower(city)
        hit = rates.get(key)
        if hit:
            year, val = hit
            return val, f"SCHL {city} CAGR {RENT_CAGR_YEARS}a ({year}, {val*100:.1f}%)"
        parent = SATELLITE_TO_CMA.get(key)
        if parent and parent in rates:
            year, val = rates[parent]
            return val, f"SCHL {parent.title()} CAGR {RENT_CAGR_YEARS}a ({year}, RMR de {city})"
        for ck, (year, val) in rates.items():
            if ck in key or key in ck:
                return val, f"SCHL {ck.title()} CAGR {RENT_CAGR_YEARS}a ({year}, ~{city})"
    if qc_median is not None:
        latest_year = max(y for y, _ in rates.values())
        return qc_median, f"SCHL mediane QC CAGR {RENT_CAGR_YEARS}a ({latest_year})"
    return None


def vacancy_rate_estimate(city: str | None) -> tuple[float, str] | None:
    """Retourne (taux_fraction, source_label) pour une ville.

    Ordre de matching:
      1. Match exact accents-strippes
      2. Sous-chaine (ex "Saint-Hippolyte" → utilise "Saint-Jerome" si aucun match)
         [pas implemente pour l'instant — retourne fallback provincial]
      3. Fallback moyenne QC

    Retourne None si aucune donnee disponible.
    """
    rates, qc_avg = load_rates()
    if not rates:
        return None
    if city:
        key = _strip_accents_lower(city)
        hit = rates.get(key)
        if hit:
            year, val = hit
            return val / 100.0, f"SCHL {city} {year}"
        # Ville-satellite d'une RMR (Laval → Montréal, etc.)
        parent = SATELLITE_TO_CMA.get(key)
        if parent:
            hit = rates.get(parent)
            if hit:
                year, val = hit
                return val / 100.0, f"SCHL {parent.title()} {year} (RMR de {city})"
        # Sous-chaine (fallback — capte des variantes de nom)
        for ck, (year, val) in rates.items():
            if ck in key or key in ck:
                return val / 100.0, f"SCHL {ck.title()} {year} (~{city})"
    if qc_avg is not None:
        latest_year = max(y for y, _ in rates.values())
        return qc_avg / 100.0, f"SCHL moyenne QC {latest_year}"
    return None
