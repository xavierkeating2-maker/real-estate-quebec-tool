"""Seuils de la methode Lepine pour le screener.

Defauts de depart — a ajuster au fur et a mesure de la lecture du livre et
selon le marche cible (Montreal vs region).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class LepineCriteria:
    # MRB (Multiplicateur de Revenu Brut) = prix demande / revenus bruts annuels.
    # Lepine strict <= 7-8; relache a 12 pour voir les "presque" sur le marche QC actuel.
    max_mrb: float = 12.0
    target_mrb: float = 7.0

    # Prix demande / evaluation municipale.
    # Lepine strict <= 0.90-1.05; relache a 1.20 (eval souvent 3 ans en retard).
    max_price_to_municipal_eval: float = 1.20
    target_price_to_municipal_eval: float = 0.95

    # Cashflow estime par porte par mois ($CAD). Plancher relache: non-negatif.
    min_cashflow_per_door: float = 0.0
    target_cashflow_per_door: float = 75.0

    # Nombre de logements. Relache a 2 pour inclure duplex/triplex.
    min_units: int = 2
    target_units: int = 6

    # Hypotheses utilisees quand l'annonce n'a pas tous les chiffres.
    expense_ratio_default: float = 0.40    # 40% des revenus bruts pour <= 6 logements
    vacancy_rate_default: float = 0.05     # 5% inoccupation
    down_payment_ratio: float = 0.20       # 20% (residentiel 4 logements et moins)
    mortgage_rate: float = 0.055           # taux hypothecaire annuel
    amortization_years: int = 25


@dataclass(frozen=True)
class LocationFilter:
    """Filtre geographique — n'affiche que les annonces dans un rayon de la maison.

    Distance calculee a vol d'oiseau (haversine). Pour ~2h de route au QC,
    150 km est un bon depart, mais varie beaucoup: 150 km au sud-ouest sur
    le 20 (route droite) vs. 100 km au nord dans les Laurentides (sinueux).
    Ajuster a la main ou via le slider Streamlit / l'option --max-km du CLI.
    """
    # Centroide Grand Montreal — remplacer par votre adresse precise si voulu.
    home_lat: float = 45.5019
    home_lon: float = -73.5674
    # Rayon a vol d'oiseau (km). 175 inclut Gatineau (166 km).
    max_km: float = 175.0


@dataclass(frozen=True)
class NotifyConfig:
    """Config pour les notifications ntfy.sh.

    Le topic doit rester prive (les topics ntfy.sh publics sont indexes).
    Definir dans l'env: `export NTFY_TOPIC=qc-screener-xxx-random-slug`.
    S'abonner via l'app ntfy sur mobile ou https://ntfy.sh/<topic>.
    """
    base_url: str = "https://ntfy.sh"
    # Seuil de baisse (%) pour declencher une notif de drop.
    drop_threshold_pct: float = 1.0
    # Fenetre (jours) pour considerer un drop comme "recent".
    drop_window_days: int = 30

    @staticmethod
    def topic_from_env() -> str | None:
        import os
        return os.environ.get("NTFY_TOPIC") or None
