"""Normalisation des chaines de region des annonces vers les regions
administratives canoniques (cf. registre_foncier.REGIONS)."""
import re
import unicodedata

from .registre_foncier import REGIONS


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _normalize_key(s: str) -> str:
    s = _strip_accents(s).lower().strip()
    s = s.replace("/", "-").replace(" ", "-")
    return re.sub(r"-+", "-", s)


# Accent-strip + lowercase lookup → forme canonique.
_CANON: dict[str, str] = {_normalize_key(name): name for name in REGIONS.values()}

# Aliases pour les chaines qui ne matchent pas directement le nom canonique.
_ALIASES: dict[str, str] = {
    "monteregie-rive-sud-montreal": "Montérégie",
    "monteregie-rive-nord-montreal": "Montérégie",
    "monteregie-rive-sud": "Montérégie",
    "monteregie-rive-nord": "Montérégie",
    "ile-de-montreal": "Montréal",
    "quebec-rive-nord": "Capitale-Nationale",
    "quebec-rive-sud": "Chaudière-Appalaches",
    "portneuf": "Capitale-Nationale",
    "lac-saint-jean": "Saguenay-Lac-Saint-Jean",
    "saguenay-lac-saint-jean": "Saguenay-Lac-Saint-Jean",
    "abitibi": "Abitibi-Témiscamingue",
    "cote-nord": "Côte-Nord",
    "gaspesie-iles-de-la-madeleine": "Gaspésie-Îles-de-la-Madeleine",
    "centre-du-quebec": "Centre-du-Québec",
    "bas-saint-laurent": "Bas-Saint-Laurent",
    "chaudiere-appalaches": "Chaudière-Appalaches",
    "capitale-nationale": "Capitale-Nationale",
}


def normalize_region(raw: str | None, city_hint: str | None = None) -> str | None:
    """Map a raw region string (and optional city slug) to a canonical admin region.

    Returns None if no match.
    """
    for cand in (raw, city_hint):
        if not cand:
            continue
        key = _normalize_key(cand)
        if key in _CANON:
            return _CANON[key]
        if key in _ALIASES:
            return _ALIASES[key]
        # Substring fallback: e.g. "saint-jean-sur-richelieu" doesn't match
        # but "monteregie-blah" matches "monteregie".
        for k, name in _ALIASES.items():
            if len(k) >= 8 and k in key:
                return name
        for k, name in _CANON.items():
            if len(k) >= 8 and k in key:
                return name
    return None
