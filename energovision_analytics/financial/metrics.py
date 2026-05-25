"""Finančné metriky — NPV, IRR (robust), Payback, LCOS, LCOE."""
from __future__ import annotations

from typing import Optional

import numpy as np


def compute_npv(cashflows: list[float] | np.ndarray, discount_rate: float) -> float:
    """Net Present Value z cashflow streamu.

    cashflows[0] je investícia (typicky záporná).
    cashflows[1..n] sú ročné cash flowy.
    """
    cf = np.asarray(cashflows, dtype=float)
    years = np.arange(len(cf))
    discounts = (1 + discount_rate) ** years
    return float((cf / discounts).sum())


def compute_irr_robust(
    cashflows: list[float] | np.ndarray,
    initial_guess: float = 0.10,
    max_iter: int = 100,
    tolerance: float = 1e-7,
) -> Optional[float]:
    """Robust IRR computation cez Newton-Raphson s viacerými reštartmi.

    Pracuje pre IRR od -50 % po +500 % (dokáže riešiť aj RATUFA s IRR > 200 %).
    Vráti None ak neexistuje real IRR (NPV stále kladné/záporné).
    """
    cf = np.asarray(cashflows, dtype=float)
    if len(cf) < 2:
        return None
    # Sanity: ak žiadny záporný flow (= žiadna investícia), IRR neexistuje
    if (cf < 0).sum() == 0 or (cf > 0).sum() == 0:
        return None

    # Pokús sa cez scipy brentq s adaptívnym bracketom
    try:
        from scipy.optimize import brentq

        def npv_fn(r):
            return compute_npv(cf, r)

        # Pokús sa viac brackets
        brackets = [
            (-0.50, 0.50),
            (0.50, 2.00),
            (2.00, 5.00),
            (-0.99, 5.00),
        ]
        for lo, hi in brackets:
            try:
                f_lo = npv_fn(lo)
                f_hi = npv_fn(hi)
                if f_lo * f_hi < 0:
                    return float(brentq(npv_fn, lo, hi, xtol=tolerance))
            except Exception:
                continue
    except ImportError:
        pass

    # Fallback: Newton-Raphson manual
    r = initial_guess
    for _ in range(max_iter):
        years = np.arange(len(cf))
        discounts = (1 + r) ** years
        npv = (cf / discounts).sum()
        # Derivácia: d/dr NPV = -sum(i * cf[i] / (1+r)^(i+1))
        deriv = -(years * cf / discounts / (1 + r)).sum()
        if abs(deriv) < 1e-12:
            break
        r_new = r - npv / deriv
        if abs(r_new - r) < tolerance:
            return float(r_new)
        r = max(-0.99, r_new)
    return None  # nekonverguje


def compute_payback(
    cashflows: list[float] | np.ndarray,
    discount_rate: float = 0.0,
) -> float:
    """Discounted (alebo simple ak discount=0) payback period v rokoch.

    Vráti rok keď cumulative cashflow prejde do kladu.
    Interpolácia medzi rokmi pre presnejšiu hodnotu.
    """
    cf = np.asarray(cashflows, dtype=float)
    discounts = (1 + discount_rate) ** np.arange(len(cf))
    discounted = cf / discounts
    cumulative = np.cumsum(discounted)
    # Nájdi prvý rok kde cumulative >= 0
    for i, v in enumerate(cumulative):
        if v >= 0:
            if i == 0:
                return 0.0
            # Interpolácia: kde presne sa to prechádza
            prev = cumulative[i - 1]
            fraction = -prev / (v - prev) if (v - prev) > 0 else 0
            return float(i - 1 + fraction)
    return 99.0  # nenavráti sa


def compute_lcoe(
    capex_eur: float,
    opex_per_year_eur: float | list[float],
    generation_per_year_kwh: float | list[float],
    horizon_years: int,
    discount_rate: float = 0.06,
    degradation_pct_per_year: float = 0.5,
) -> float:
    """Levelized Cost of Energy (EUR/MWh) — pre PV časť."""
    discounted_costs = capex_eur
    discounted_gen = 0.0
    for y in range(1, horizon_years + 1):
        opex_y = opex_per_year_eur if isinstance(opex_per_year_eur, (int, float)) else opex_per_year_eur[y - 1]
        gen_y_kwh = (generation_per_year_kwh if isinstance(generation_per_year_kwh, (int, float))
                     else generation_per_year_kwh[y - 1])
        deg_factor = (1 - degradation_pct_per_year / 100) ** (y - 1)
        gen_y = gen_y_kwh * deg_factor
        discount = (1 + discount_rate) ** y
        discounted_costs += opex_y / discount
        discounted_gen += gen_y / discount
    if discounted_gen <= 0:
        return float("inf")
    return discounted_costs / discounted_gen * 1000  # €/MWh


def compute_lcos(
    bess_capex_eur: float,
    bess_opex_per_year_eur: float,
    discharge_per_year_kwh: float | list[float],
    charge_cost_per_year_eur: float | list[float],
    horizon_years: int,
    discount_rate: float = 0.06,
    replacement_year: Optional[int] = 12,
    replacement_cost_pct: float = 0.5,
) -> float:
    """Levelized Cost of Storage (EUR/MWh discharged) — NREL metodika.

    LCOS = (CAPEX + Σ OPEX_y/(1+r)^y + Σ CHARGE_COST_y/(1+r)^y + replacement)
           / Σ DISCHARGE_y/(1+r)^y
    """
    discounted_costs = bess_capex_eur

    # Replacement event
    if replacement_year and replacement_year < horizon_years:
        repl = bess_capex_eur * replacement_cost_pct
        discounted_costs += repl / (1 + discount_rate) ** replacement_year

    discounted_discharge = 0.0
    for y in range(1, horizon_years + 1):
        opex = bess_opex_per_year_eur
        charge_cost = (charge_cost_per_year_eur if isinstance(charge_cost_per_year_eur, (int, float))
                       else charge_cost_per_year_eur[y - 1])
        disch_kwh = (discharge_per_year_kwh if isinstance(discharge_per_year_kwh, (int, float))
                     else discharge_per_year_kwh[y - 1])
        discount = (1 + discount_rate) ** y
        discounted_costs += (opex + charge_cost) / discount
        discounted_discharge += disch_kwh / discount

    if discounted_discharge <= 0:
        return float("inf")
    return discounted_costs / discounted_discharge * 1000  # €/MWh
