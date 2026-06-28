"""Normalisation des noms de ville pour fusionner les cohortes de loyer.

Les sources epellent la meme ville differemment:
- Kijiji  : "Montréal", "Montreal", "City of Montréal", "Longueuil / South Shore"
- LogisQc : extrait souvent "(Ville)" de "Quartier (Ville)"; parfois le tout
            sans parentheses ("Centre-Ville, Vieux-Montréal").

`normalize_city` retourne une forme canonique commune.
"""
import re
import unicodedata


# Cle accent-strip + lowercase → forme canonique a afficher.
_CANONICAL: dict[str, str] = {
    "montreal": "Montréal",
    "quebec": "Québec",
    "longueuil": "Longueuil",
    "laval": "Laval",
    "gatineau": "Gatineau",
    "sherbrooke": "Sherbrooke",
    "trois-rivieres": "Trois-Rivières",
    "saguenay": "Saguenay",
    "levis": "Lévis",
    "drummondville": "Drummondville",
    "saint-jerome": "Saint-Jérôme",
    "saint-hyacinthe": "Saint-Hyacinthe",
    "repentigny": "Repentigny",
    "terrebonne": "Terrebonne",
    "brossard": "Brossard",
    "boucherville": "Boucherville",
    "blainville": "Blainville",
    "granby": "Granby",
    "salaberry-de-valleyfield": "Salaberry-de-Valleyfield",
    "rouyn-noranda": "Rouyn-Noranda",
    "shawinigan": "Shawinigan",
    "rimouski": "Rimouski",
    "saint-jean-sur-richelieu": "Saint-Jean-sur-Richelieu",
    "victoriaville": "Victoriaville",
    "thetford mines": "Thetford Mines",
    "mascouche": "Mascouche",
    "mirabel": "Mirabel",
    "chateauguay": "Châteauguay",
    "vaudreuil-dorion": "Vaudreuil-Dorion",
    "joliette": "Joliette",
    "saint-eustache": "Saint-Eustache",
    "amos": "Amos",
    "bromont": "Bromont",
    "saint-jacques": "Saint-Jacques",
    "saint-jacques-de-leeds": "Saint-Jacques-de-Leeds",
    "ste-agathe-des-monts": "Sainte-Agathe-des-Monts",
    "sainte-agathe-des-monts": "Sainte-Agathe-des-Monts",
    "saint-hubert": "Saint-Hubert",
    "saint-bruno-de-montarville": "Saint-Bruno-de-Montarville",
    "mont-tremblant": "Mont-Tremblant",
    "magog": "Magog",
    "alma": "Alma",
    "val-d-or": "Val-d'Or",
}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def normalize_city(raw: str | None) -> str | None:
    """Tente de fusionner les variantes vers un nom canonique.

    Strategie:
    1. Si parentheses en fin (ex "X (Montréal)"), candidat #1 = contenu des parens.
    2. Si " / " present (ex "Longueuil / South Shore"), candidat = partie avant.
    3. Candidat fallback = chaine telle quelle.
    Pour chaque candidat: enleve prefixe "City of ", strip accents, lowercase,
    cherche match exact dans _CANONICAL. Sinon: cherche un sous-mot canonique
    (>=5 chars) dans la chaine — capte "Vieux-Montréal" → "Montréal".
    """
    if not raw:
        return None
    s = raw.strip()
    candidates: list[str] = []
    paren = re.search(r"\(([^)]+)\)\s*$", s)
    if paren:
        candidates.append(paren.group(1).strip())
    if " / " in s:
        candidates.append(s.split(" / ", 1)[0].strip())
    candidates.append(s)

    for cand in candidates:
        c = re.sub(r"^City of\s+", "", cand, flags=re.I).strip()
        key = _strip_accents(c).lower()
        if key in _CANONICAL:
            return _CANONICAL[key]
        for canon_key, canon in _CANONICAL.items():
            if len(canon_key) >= 5 and canon_key in key:
                return canon
    return candidates[0] if candidates else None
