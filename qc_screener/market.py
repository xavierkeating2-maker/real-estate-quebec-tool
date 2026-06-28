"""Lookup des loyers du marche par cohorte (ville canonique, chambres)."""
import sqlite3

from .cities import normalize_city


def estimate_monthly_rent(
    conn: sqlite3.Connection,
    city: str | None,
    bedrooms: int,
    min_samples: int = 3,
) -> tuple[float | None, int]:
    """Retourne (mediane_loyer_mensuel, n_echantillons) pour la cohorte.

    None si la cohorte a moins de `min_samples` comparables.
    """
    canon = normalize_city(city)
    if not canon:
        return None, 0
    rows = conn.execute(
        "SELECT monthly_rent FROM rent_comps "
        "WHERE city = ? AND bedrooms = ? AND monthly_rent IS NOT NULL",
        (canon, bedrooms),
    ).fetchall()
    rents = sorted(r[0] for r in rows)
    n = len(rents)
    if n < min_samples:
        return None, n
    median = rents[n // 2] if n % 2 else (rents[n // 2 - 1] + rents[n // 2]) / 2
    return median, n


def estimate_market_revenue(
    conn: sqlite3.Connection,
    city: str | None,
    unit_mix: list[int],
    min_samples: int = 3,
) -> tuple[float | None, list[tuple[int, float | None, int]], str | None]:
    """Calcule le revenu brut annuel projete au loyer du marche.

    Retourne (revenu_annuel_total, ventilation_par_logt, ville_canonique).
    revenu_annuel_total est None si AU MOINS UN logement n'a pas assez de comparables.
    ventilation_par_logt = [(chambres, loyer_mensuel_ou_none, n_echantillons), ...]
    """
    canon = normalize_city(city)
    breakdown: list[tuple[int, float | None, int]] = []
    total = 0.0
    complete = True
    for br in unit_mix:
        rent, n = estimate_monthly_rent(conn, city, br, min_samples)
        breakdown.append((br, rent, n))
        if rent is None:
            complete = False
        else:
            total += rent
    return ((total * 12) if complete else None), breakdown, canon
