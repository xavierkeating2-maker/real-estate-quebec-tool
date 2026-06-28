"""Helpers de distance geographique."""
from __future__ import annotations

import math


EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance grande-cercle entre deux points (km, ligne droite).

    Note: c'est la distance a vol d'oiseau. Pour estimer un temps de route,
    multiplier par ~1.3x (sinuosite typique des routes du QC).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
