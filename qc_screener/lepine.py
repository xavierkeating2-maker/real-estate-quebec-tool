from .config import LepineCriteria
from .models import Listing, Metrics, ScreenVerdict


def _monthly_mortgage_payment(principal: float, annual_rate: float, years: int) -> float:
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def compute_metrics(listing: Listing, c: LepineCriteria) -> Metrics:
    m = Metrics()
    price = listing.asking_price
    units = listing.units
    revenue = listing.annual_gross_revenue

    if price and revenue:
        m.mrb = round(price / revenue, 2)
    if price and units:
        m.price_per_door = round(price / units, 0)
    if price and listing.municipal_evaluation:
        m.price_to_eval = round(price / listing.municipal_evaluation, 3)

    if price and revenue:
        effective_revenue = revenue * (1 - c.vacancy_rate_default)
        expenses = listing.annual_expenses or revenue * c.expense_ratio_default
        noi = effective_revenue - expenses
        m.estimated_noi = round(noi, 0)

        loan = price * (1 - c.down_payment_ratio)
        annual_debt = _monthly_mortgage_payment(loan, c.mortgage_rate, c.amortization_years) * 12
        annual_cashflow = noi - annual_debt
        if units:
            m.estimated_cashflow_per_door_month = round(annual_cashflow / units / 12, 0)

    return m


def screen(listing: Listing, c: LepineCriteria) -> ScreenVerdict:
    m = compute_metrics(listing, c)
    checks: dict[str, bool | None] = {}
    reasons: list[str] = []

    if listing.units is None:
        checks["min_units"] = None
    else:
        ok = listing.units >= c.min_units
        checks["min_units"] = ok
        if not ok:
            reasons.append(f"{listing.units} logements < min {c.min_units}")

    if m.mrb is None:
        checks["mrb"] = None
    else:
        ok = m.mrb <= c.max_mrb
        checks["mrb"] = ok
        if not ok:
            reasons.append(f"MRB {m.mrb} > plafond {c.max_mrb}")

    if m.price_to_eval is None:
        checks["price_to_eval"] = None
    else:
        ok = m.price_to_eval <= c.max_price_to_municipal_eval
        checks["price_to_eval"] = ok
        if not ok:
            reasons.append(f"Prix/eval {m.price_to_eval} > {c.max_price_to_municipal_eval}")

    if m.estimated_cashflow_per_door_month is None:
        checks["cashflow_per_door"] = None
    else:
        ok = m.estimated_cashflow_per_door_month >= c.min_cashflow_per_door
        checks["cashflow_per_door"] = ok
        if not ok:
            reasons.append(
                f"Cashflow {m.estimated_cashflow_per_door_month}$/porte/mois "
                f"< min {c.min_cashflow_per_door}$"
            )

    decided = [v for v in checks.values() if v is not None]
    score = (sum(1 for v in decided if v) / len(decided)) if decided else 0.0
    # Statut a trois niveaux:
    #   - "fail"         : aucun check decide, ou un check decide echoue, ou
    #                      aucun check financier ne passe.
    #   - "pass_partial" : prix/eval ou cashflow passe MAIS MRB non evaluable
    #                      (revenu non divulgue) — drapeau "potentiel,
    #                      mais on n'a pas pu calculer le metric coeur de Lepine".
    #   - "pass"         : MRB evaluable et passe (avec les autres checks decides).
    financial_keys = {"mrb", "price_to_eval", "cashflow_per_door"}
    financial_decided = [k for k in financial_keys if checks.get(k) is not None]
    any_financial_passes = any(checks[k] for k in financial_decided)
    all_decided_pass = bool(decided) and all(decided)

    if not all_decided_pass or not any_financial_passes:
        status = "fail"
    elif checks.get("mrb") is True:
        status = "pass"
    else:
        # Au moins prix/eval ou cashflow passe, mais MRB n'est pas evaluable.
        status = "pass_partial"
        reasons.append("MRB non calculable — revenu non divulgue par le vendeur")

    return ScreenVerdict(
        listing_source_id=listing.source_id,
        status=status,
        score=round(score, 2),
        checks=checks,
        reasons=reasons,
        metrics=m,
    )
