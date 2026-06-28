"""Analyseur de deal — methode Lepine.

Inputs: prix d'offre, mix de chambres, revenus stabilises, financement
(MdF, hypotheque, balance de vente), capex. Outputs: projection annuelle
sur N annees (TGA, MRB, NOI, cashflow/porte, valeur, dette, equite), IRR
5 ans, mise de fonds totale (incl. taxe de bienvenue + CMHC si applicable),
table de sensibilite prix x revenus stabilises.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DealInputs:
    purchase_price: float
    units: int
    gross_annual_revenue: float          # revenus stabilises projetes
    annual_expenses: float | None = None  # si None: revenue * expense_ratio
    known_taxes: float | None = None      # taxes municipales + scolaires connues; sert de plancher aux depenses
    expense_ratio: float = 0.40
    vacancy_rate: float = 0.05

    # Financement
    down_payment_pct: float = 0.20
    mortgage_rate: float = 0.055
    amortization_years: int = 25

    # Balance de vente — interets seulement
    vtb_pct: float = 0.0
    vtb_rate: float = 0.07

    # Capital initial
    initial_capex: float = 0.0
    other_closing_pct: float = 0.012     # notaire, inspection, etc. (taxe de bienvenue calculee a part)

    # Croissance
    annual_rent_growth: float = 0.025
    annual_expense_growth: float = 0.02
    annual_appreciation: float = 0.025

    # Sortie
    sell_costs_pct: float = 0.05         # courtage + frais a la sortie (annee N)


@dataclass
class DealSnapshot:
    year: int
    effective_revenue: float
    expenses: float
    noi: float
    cap_rate: float
    mrb: float
    annual_cashflow: float
    cashflow_per_door_month: float
    property_value: float
    loan_balance: float
    equity: float


@dataclass
class DealAnalysis:
    inputs: DealInputs
    yearly: list[DealSnapshot]
    cash_invested: float
    welcome_tax: float
    cmhc_premium: float
    mortgage_principal: float            # incl. CMHC roll-in
    annual_mortgage_payment: float
    annual_vtb_payment: float
    cash_on_cash_year_1: float
    five_year_cf_cumulative: float
    five_year_total_return: float
    irr_5_year: float | None

    @property
    def year_1(self) -> DealSnapshot:
        return self.yearly[0]

    @property
    def year_5(self) -> DealSnapshot:
        return self.yearly[min(4, len(self.yearly) - 1)]


# ─────────────────────────  Helpers financiers  ─────────────────────────


def _monthly_payment(principal: float, annual_rate: float, years: int) -> float:
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return principal / n if n else 0.0
    return principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def _loan_balance_after(principal: float, annual_rate: float, years: int, months_elapsed: int) -> float:
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return max(0.0, principal * (1 - months_elapsed / n))
    pmt = _monthly_payment(principal, annual_rate, years)
    bal = principal
    for _ in range(min(months_elapsed, n)):
        interest = bal * r
        bal = bal - (pmt - interest)
    return max(0.0, bal)


def quebec_welcome_tax(price: float) -> float:
    """Taxe de bienvenue (droits de mutation) — bareme provincial QC standard.

    Brackets 2025-2026:
      0  - 61 500 $:    0.5%
      61 500 - 307 800: 1.0%
      au-dessus:        1.5%
    Note: Montreal et certaines villes ajoutent des paliers superieurs.
    Pour un calcul exact, faut le bareme municipal — c'est une approximation.
    """
    if price <= 0:
        return 0.0
    b1, b2 = 61_500, 307_800
    if price <= b1:
        return price * 0.005
    if price <= b2:
        return b1 * 0.005 + (price - b1) * 0.010
    return b1 * 0.005 + (b2 - b1) * 0.010 + (price - b2) * 0.015


def cmhc_premium_rate(down_pct: float, units: int) -> float:
    """Prime SCHL si applicable. 5+ logements = commercial CMHC (different), retourne 0."""
    if units > 4:
        return 0.0
    if down_pct >= 0.20:
        return 0.0
    if down_pct >= 0.15:
        return 0.028
    if down_pct >= 0.10:
        return 0.031
    if down_pct >= 0.05:
        return 0.040
    return 0.0


def irr(cashflows: list[float], guess: float = 0.10) -> float | None:
    """Taux de rendement interne via Newton (iteration sur NPV)."""
    if not cashflows or all(cf == 0 for cf in cashflows):
        return None
    rate = guess
    for _ in range(200):
        npv = sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))
        d_npv = sum(-t * cf / (1 + rate) ** (t + 1) for t, cf in enumerate(cashflows))
        if abs(d_npv) < 1e-12:
            return None
        rate_new = rate - npv / d_npv
        if rate_new <= -0.99:
            rate_new = -0.5
        if abs(rate_new - rate) < 1e-6:
            return rate_new
        rate = rate_new
    return None


# ─────────────────────────  Analyse  ─────────────────────────


def _snapshot(d: DealInputs, year: int, mortgage_principal: float,
              mortgage_payment: float, vtb_payment: float) -> DealSnapshot:
    revenue = d.gross_annual_revenue * (1 + d.annual_rent_growth) ** (year - 1)
    effective = revenue * (1 - d.vacancy_rate)
    if d.annual_expenses is not None:
        base_expenses = d.annual_expenses
    else:
        ratio_based = d.gross_annual_revenue * d.expense_ratio
        # Si on connait les taxes, on les ajoute aux autres depenses (estimees a
        # 25% des revenus pour entretien/assurance/etc.) plutot que le 40% bloc.
        if d.known_taxes is not None:
            base_expenses = d.known_taxes + d.gross_annual_revenue * 0.25
            base_expenses = max(base_expenses, ratio_based * 0.7)  # plancher
        else:
            base_expenses = ratio_based
    expenses = base_expenses * (1 + d.annual_expense_growth) ** (year - 1)
    noi = effective - expenses
    annual_cf = noi - mortgage_payment - vtb_payment
    value = d.purchase_price * (1 + d.annual_appreciation) ** year
    bal = _loan_balance_after(mortgage_principal, d.mortgage_rate, d.amortization_years, year * 12)
    equity = value - bal - (d.purchase_price * d.vtb_pct)
    return DealSnapshot(
        year=year,
        effective_revenue=effective,
        expenses=expenses,
        noi=noi,
        cap_rate=noi / value if value else 0.0,
        mrb=d.purchase_price / revenue if revenue else 0.0,
        annual_cashflow=annual_cf,
        cashflow_per_door_month=annual_cf / d.units / 12 if d.units else 0.0,
        property_value=value,
        loan_balance=bal,
        equity=equity,
    )


def analyze(d: DealInputs, horizon: int = 10) -> DealAnalysis:
    welcome_tax = quebec_welcome_tax(d.purchase_price)
    other_closing = d.purchase_price * d.other_closing_pct

    vtb = d.purchase_price * d.vtb_pct
    down = d.purchase_price * d.down_payment_pct
    mortgage_before_cmhc = d.purchase_price - down - vtb
    cmhc_rate = cmhc_premium_rate(d.down_payment_pct, d.units)
    cmhc_premium = mortgage_before_cmhc * cmhc_rate
    mortgage = mortgage_before_cmhc + cmhc_premium

    mortgage_payment = _monthly_payment(mortgage, d.mortgage_rate, d.amortization_years) * 12
    vtb_payment = vtb * d.vtb_rate

    cash_invested = down + welcome_tax + other_closing + d.initial_capex

    yearly = [
        _snapshot(d, y, mortgage, mortgage_payment, vtb_payment)
        for y in range(1, horizon + 1)
    ]

    cf_5 = sum(s.annual_cashflow for s in yearly[:5])
    coc_y1 = yearly[0].annual_cashflow / cash_invested if cash_invested else 0.0

    # IRR 5 ans: cash_invested negatif en t=0, cashflow t=1..4, t=5 inclus exit value
    if len(yearly) >= 5:
        exit_snap = yearly[4]
        exit_proceeds = (
            exit_snap.property_value * (1 - d.sell_costs_pct)
            - exit_snap.loan_balance - vtb
        )
        flows = [-cash_invested]
        for i in range(4):
            flows.append(yearly[i].annual_cashflow)
        flows.append(yearly[4].annual_cashflow + exit_proceeds)
        irr5 = irr(flows)
    else:
        irr5 = None

    # Rendement total 5 ans (vue simplifiee)
    equity_gain_5 = yearly[4].equity - (down + d.initial_capex) if len(yearly) >= 5 else 0
    total_return = (cf_5 + equity_gain_5) / cash_invested if cash_invested else 0

    return DealAnalysis(
        inputs=d,
        yearly=yearly,
        cash_invested=cash_invested,
        welcome_tax=welcome_tax,
        cmhc_premium=cmhc_premium,
        mortgage_principal=mortgage,
        annual_mortgage_payment=mortgage_payment,
        annual_vtb_payment=vtb_payment,
        cash_on_cash_year_1=coc_y1,
        five_year_cf_cumulative=cf_5,
        five_year_total_return=total_return,
        irr_5_year=irr5,
    )


def sensitivity(d: DealInputs, prices: list[float], revenues: list[float]) -> list[list[dict]]:
    """Matrice price × revenue, chaque cellule contenant les metriques cles.

    Retourne une liste 2D de dicts {price, revenue, irr, cf_door_mo, tga, mrb}.
    """
    out = []
    for p in prices:
        row = []
        for r in revenues:
            d2 = DealInputs(
                purchase_price=p,
                units=d.units,
                gross_annual_revenue=r,
                annual_expenses=d.annual_expenses,
                expense_ratio=d.expense_ratio,
                vacancy_rate=d.vacancy_rate,
                down_payment_pct=d.down_payment_pct,
                mortgage_rate=d.mortgage_rate,
                amortization_years=d.amortization_years,
                vtb_pct=d.vtb_pct,
                vtb_rate=d.vtb_rate,
                initial_capex=d.initial_capex,
                other_closing_pct=d.other_closing_pct,
                annual_rent_growth=d.annual_rent_growth,
                annual_expense_growth=d.annual_expense_growth,
                annual_appreciation=d.annual_appreciation,
                sell_costs_pct=d.sell_costs_pct,
            )
            a = analyze(d2, horizon=5)
            row.append({
                "price": p,
                "revenue": r,
                "irr": a.irr_5_year,
                "cf_door_mo": a.year_1.cashflow_per_door_month,
                "tga": a.year_1.cap_rate,
                "mrb": a.year_1.mrb,
            })
        out.append(row)
    return out
