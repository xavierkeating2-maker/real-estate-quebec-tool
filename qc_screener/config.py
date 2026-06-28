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
