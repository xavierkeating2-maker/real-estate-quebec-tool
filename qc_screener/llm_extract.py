"""Extraction LLM des champs structures depuis le texte libre de l'annonce.

Cible: Claude Haiku 4.5 — rapide et bon marche pour un batch ~700 annonces.
Cache disque par hash de description: re-runs gratuits si rien change.
Prompt caching active sur le system prompt + few-shots (le user message
varie a chaque appel).

Cles ANTHROPIC_API_KEY doit etre dans l'environnement.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic

CACHE_DIR = Path("data/cache/llm_extract")
MODEL = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT_VERSION = "v1"   # bumper a chaque changement de prompt → invalide cache

SYSTEM_PROMPT = """Tu extrais des faits structures depuis des descriptions d'annonces multilogement au Quebec.

Tu reponds UNIQUEMENT avec un objet JSON valide (pas de markdown, pas d'explications) avec ces champs:
- per_unit_rents: tableau de loyers mensuels par logement (nombres CAD)
- per_unit_sizes: tableau de tailles ("3 1/2", "4 1/2", "5 1/2", "studio", etc.)
- total_annual_revenue: revenu brut annuel total si mentionne explicitement, sinon null
- renovations_done: liste des renovations recentes mentionnees (courtes, ex: "toiture 2022", "fenetres", "plomberie")
- renovations_needed: liste des travaux signales comme necessaires
- units_occupied: nombre de logements actuellement loues, sinon null
- vacant_unit_notes: liste des logements signales vacants ou bientot vacants (ex: "RDC dispo juillet 2026")
- seller_motivation: phrase courte si motivation mentionnee (succession, demenagement, retraite, etc.), sinon null
- rent_reset_potential: "low" / "medium" / "high" / "unknown" - haut si loyers explicitement sous le marche ou locataires longue-duree
- confidence: 0..1 - ta confiance globale dans l'extraction

Regles:
- N'invente RIEN. Si ce n'est pas dans la description, retourne [] ou null.
- Convertis "1 200$" ou "1,200 $/mois" en 1200 (entier).
- Normalise les tailles: "3½", "3 et demi", "3 1/2" → "3 1/2".
- rent_reset_potential = "high" si la description dit explicitement "loyers sous le marche" / "potentiel d'augmentation" / "locataires longue duree depuis X ans".
- rent_reset_potential = "medium" si les revenus sont mentionnes mais sans comparaison.
- rent_reset_potential = "low" si la description suggere des loyers au marche.

Exemples:

INPUT: 4 logements: 3 1/2 loue 950$, 4 1/2 loue 1100$/mois, 5 1/2 loue 1200$, RDC propriete du vendeur (libre des juillet 2026). Revenus actuels 37 800$/an.
OUTPUT: {"per_unit_rents":[950,1100,1200],"per_unit_sizes":["3 1/2","4 1/2","5 1/2"],"total_annual_revenue":37800,"renovations_done":[],"renovations_needed":[],"units_occupied":3,"vacant_unit_notes":["RDC libre des juillet 2026"],"seller_motivation":null,"rent_reset_potential":"unknown","confidence":0.92}

INPUT: Triplex renove en 2019 (toiture, plomberie, electricite). Revenus actuels: 19 320$/annee. Locataires stables depuis plus de 10 ans. Loyers nettement sous le marche, fort potentiel d'optimisation.
OUTPUT: {"per_unit_rents":[],"per_unit_sizes":[],"total_annual_revenue":19320,"renovations_done":["toiture 2019","plomberie 2019","electricite 2019"],"renovations_needed":[],"units_occupied":null,"vacant_unit_notes":[],"seller_motivation":null,"rent_reset_potential":"high","confidence":0.85}

INPUT: Belle propriete bien entretenue dans secteur paisible. Pres des ecoles. Visite sur rendez-vous.
OUTPUT: {"per_unit_rents":[],"per_unit_sizes":[],"total_annual_revenue":null,"renovations_done":[],"renovations_needed":[],"units_occupied":null,"vacant_unit_notes":[],"seller_motivation":null,"rent_reset_potential":"unknown","confidence":0.3}
"""


def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY manquant dans l'environnement. "
            "Set it: export ANTHROPIC_API_KEY=sk-ant-..."
        )
    return anthropic.Anthropic()


def _cache_key(description: str) -> str:
    return hashlib.sha256(
        (SYSTEM_PROMPT_VERSION + description).encode()
    ).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def extract(description: str, units_hint: int | None = None,
            client: anthropic.Anthropic | None = None,
            use_cache: bool = True) -> dict | None:
    """Extrait les champs structures depuis la description. None si trop courte ou parsing echoue."""
    if not description or len(description) < 50:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(description)
    cached = _cache_path(key)
    if use_cache and cached.exists():
        return json.loads(cached.read_text())

    if client is None:
        client = _client()
    user_msg = (
        f"L'annonce a {units_hint or 'inconnu'} logement(s). Extrais les faits de cette description:\n\n"
        f"{description}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    # Defensive: trouver le JSON meme s'il y a du texte autour
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    cached.write_text(json.dumps(data, ensure_ascii=False))
    return data


def apply_to_listing(listing_payload: dict, extracted: dict) -> dict:
    """Fusionne les champs extraits dans le payload Listing JSON."""
    listing_payload["per_unit_rents"] = [float(r) for r in extracted.get("per_unit_rents") or []]
    listing_payload["per_unit_sizes"] = list(extracted.get("per_unit_sizes") or [])
    listing_payload["renovations_done"] = list(extracted.get("renovations_done") or [])
    listing_payload["renovations_needed"] = list(extracted.get("renovations_needed") or [])
    listing_payload["units_occupied"] = extracted.get("units_occupied")
    listing_payload["vacant_unit_notes"] = list(extracted.get("vacant_unit_notes") or [])
    listing_payload["seller_motivation"] = extracted.get("seller_motivation")
    listing_payload["rent_reset_potential"] = extracted.get("rent_reset_potential")
    listing_payload["extraction_confidence"] = extracted.get("confidence")
    listing_payload["extracted_at"] = datetime.now(timezone.utc).isoformat()
    # Si l'annonce ne disait pas de revenue, prend l'extraction
    if not listing_payload.get("annual_gross_revenue") and extracted.get("total_annual_revenue"):
        listing_payload["annual_gross_revenue"] = float(extracted["total_annual_revenue"])
    return listing_payload
