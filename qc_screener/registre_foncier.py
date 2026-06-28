"""Signal macro régional via le Registre foncier du Québec.

Source: donneesquebec.ca / CC-BY 4.0 — données agrégées par région
administrative et par mois. Pas de détail par propriété; sert de couche de
biais "chaleur de marché" et "stress financier" pour pondérer le screener.

Resources CKAN exposés:
- Nombre de transferts de propriété (volume de transactions)
- Nombre d'actes de difficulté financière (faillites, préavis, etc.)
- Nombre d'hypothèques
- Nombre de ventes par plage de prix
"""
import csv
import io
from collections import defaultdict
from pathlib import Path

import httpx

USER_AGENT = "qc-screener/0.1 (personal real-estate research)"
CACHE_DIR = Path("data/cache/registre_foncier")
CKAN_API = "https://www.donneesquebec.ca/recherche/api/3/action"

RESOURCES = {
    "transferts": "7b8e1f0b-8715-491a-a398-685ecae6438d",
    "difficulte": "84ed216a-3284-4d05-aa85-d2ef30dd5d0f",
    "hypotheques": "739ac2bb-e549-4bcd-893d-768e37a03af6",
    "ventes_par_prix": "c05ac154-4745-46d0-a158-e84655f66084",
}

# Etiquettes des plages de prix (decouvertes dans le XLSX de Donnees Quebec).
PRICE_BANDS = {
    "1": "Moins de 250 000 $",
    "2": "De 250 000 $ à 500 000 $",
    "3": "Plus de 500 000 $",
}

# Régions administratives du Québec.
REGIONS: dict[str, str] = {
    "01": "Bas-Saint-Laurent",
    "02": "Saguenay-Lac-Saint-Jean",
    "03": "Capitale-Nationale",
    "04": "Mauricie",
    "05": "Estrie",
    "06": "Montréal",
    "07": "Outaouais",
    "08": "Abitibi-Témiscamingue",
    "09": "Côte-Nord",
    "10": "Nord-du-Québec",
    "11": "Gaspésie-Îles-de-la-Madeleine",
    "12": "Chaudière-Appalaches",
    "13": "Laval",
    "14": "Lanaudière",
    "15": "Laurentides",
    "16": "Montérégie",
    "17": "Centre-du-Québec",
}


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60
    )


def _resource_url(client: httpx.Client, resource_id: str) -> str:
    r = client.get(f"{CKAN_API}/resource_show?id={resource_id}")
    r.raise_for_status()
    return r.json()["result"]["url"]


def _download_csv(client: httpx.Client, name: str, *, force: bool = False) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{name}.csv"
    if cache.exists() and not force:
        return cache.read_text(encoding="utf-8")
    url = _resource_url(client, RESOURCES[name])
    r = client.get(url)
    r.raise_for_status()
    cache.write_text(r.text, encoding="utf-8")
    return r.text


def refresh(force: bool = False) -> dict[str, int]:
    """Télécharge (ou re-télécharge si force) chaque CSV. Retourne {nom: lignes}."""
    counts: dict[str, int] = {}
    with _client() as c:
        for name in RESOURCES:
            text = _download_csv(c, name, force=force)
            counts[name] = sum(1 for _ in csv.reader(io.StringIO(text))) - 1
    return counts


def _aggregate_by_region_month(text: str) -> dict[tuple[str, str], int]:
    """Pour un CSV avec colonnes (DT_DEBUT_MOIS, ID_REGN_ADMIN, ..., NB_REQST):
    retourne {(region_id, month_str): nb_requetes_total}."""
    out: dict[tuple[str, str], int] = defaultdict(int)
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            out[(row["ID_REGN_ADMIN"], row["DT_DEBUT_MOIS"])] += int(row["NB_REQST"])
        except (KeyError, ValueError):
            continue
    return out


def _aggregate_by_region_month_band(text: str) -> dict[tuple[str, str, str], int]:
    """Pour le CSV ventes_par_prix: (region_id, month, band_code) -> nb."""
    out: dict[tuple[str, str, str], int] = defaultdict(int)
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            key = (row["ID_REGN_ADMIN"], row["DT_DEBUT_MOIS"], row["CD_PLAGE_PRIX"])
            out[key] += int(row["NB_REQST"])
        except (KeyError, ValueError):
            continue
    return out


def region_stats(months: int = 12) -> list[dict]:
    """Statistiques par région sur les `months` derniers mois disponibles.

    Retourne une liste de dicts par région avec:
    - transferts_recents     : volume transactions sur la fenêtre
    - transferts_prior       : même fenêtre 12 mois plus tôt (YoY)
    - transferts_yoy_pct     : croissance YoY %
    - hypotheques_recents    : nb d'hypothèques nouvelles
    - difficulte_recents     : actes de difficulté financière (proxy distress)
    - ratio_distress         : difficulte / transferts (vendeurs motivés)
    - share_band1/2/3        : part des ventes par plage de prix (cumul 12 mois)
    - share_band1_yoy_pp     : variation YoY de la part band 1 en points de %
    """
    with _client() as c:
        t_text = _download_csv(c, "transferts")
        h_text = _download_csv(c, "hypotheques")
        d_text = _download_csv(c, "difficulte")
        v_text = _download_csv(c, "ventes_par_prix")

    t = _aggregate_by_region_month(t_text)
    h = _aggregate_by_region_month(h_text)
    d = _aggregate_by_region_month(d_text)
    v = _aggregate_by_region_month_band(v_text)

    all_months = sorted({m for _, m in t.keys()})
    if not all_months:
        return []
    recent = all_months[-months:]
    prior = all_months[-(months + 12) : -12] if len(all_months) >= months + 12 else []

    rows = []
    for rid, name in REGIONS.items():
        t_recent = sum(t.get((rid, m), 0) for m in recent)
        t_prior = sum(t.get((rid, m), 0) for m in prior)
        h_recent = sum(h.get((rid, m), 0) for m in recent)
        d_recent = sum(d.get((rid, m), 0) for m in recent)
        yoy = ((t_recent - t_prior) / t_prior * 100) if t_prior else None
        ratio = (d_recent / t_recent) if t_recent else None

        # Plages de prix
        band_counts_recent = {b: sum(v.get((rid, m, b), 0) for m in recent) for b in PRICE_BANDS}
        band_counts_prior = {b: sum(v.get((rid, m, b), 0) for m in prior) for b in PRICE_BANDS}
        total_recent = sum(band_counts_recent.values())
        total_prior = sum(band_counts_prior.values())
        share_recent = {b: (band_counts_recent[b] / total_recent) if total_recent else None for b in PRICE_BANDS}
        share_prior = {b: (band_counts_prior[b] / total_prior) if total_prior else None for b in PRICE_BANDS}

        rows.append({
            "region_id": rid,
            "region": name,
            "transferts_recents": t_recent,
            "transferts_prior": t_prior,
            "transferts_yoy_pct": yoy,
            "hypotheques_recents": h_recent,
            "difficulte_recents": d_recent,
            "ratio_distress": ratio,
            "share_band1": share_recent["1"],
            "share_band2": share_recent["2"],
            "share_band3": share_recent["3"],
            "share_band1_yoy_pp": (
                (share_recent["1"] - share_prior["1"]) * 100
                if (share_recent["1"] is not None and share_prior["1"] is not None) else None
            ),
            "fenetre_debut": recent[0] if recent else None,
            "fenetre_fin": recent[-1] if recent else None,
        })
    return rows
