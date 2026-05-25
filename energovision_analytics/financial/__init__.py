"""Financial engine — ekonomická analýza pre FVE+BESS posudky.

Komponenty:
    - NPV / IRR (robust) / Payback / LCOS / LCOE
    - SK 6-r daňový odpis (DPPO 21 %)
    - Zelená podnikom dotácia (max 50 k€, 45 % intenzita)
    - Monte Carlo sensitivity (P10/P50/P90)
    - Tornado sensitivity (top 5 premenných)
    - Replacement schedule (BESS inverter 12r, cells 15-20r)
"""
from energovision_analytics.financial.cashflow import CashflowBuilder
from energovision_analytics.financial.metrics import (
    compute_irr_robust,
    compute_lcoe,
    compute_lcos,
    compute_npv,
    compute_payback,
)
from energovision_analytics.financial.monte_carlo import (
    MonteCarloConfig,
    monte_carlo_npv,
)
from energovision_analytics.financial.tax_shield import (
    sk_dotacia_zelena_podnikom,
    sk_tax_shield_schedule,
)

__all__ = [
    "CashflowBuilder",
    "compute_npv",
    "compute_irr_robust",
    "compute_payback",
    "compute_lcoe",
    "compute_lcos",
    "monte_carlo_npv",
    "MonteCarloConfig",
    "sk_tax_shield_schedule",
    "sk_dotacia_zelena_podnikom",
]
