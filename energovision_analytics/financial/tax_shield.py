"""SK daňový odpis + Zelená podnikom dotácia logika."""
from __future__ import annotations


def sk_tax_shield_schedule(
    net_capex_eur: float,
    dppo_pct: float = 0.21,
    depr_years: int = 6,
    has_sufficient_profit: bool = True,
) -> list[float]:
    """Slovenský daňový odpis pre FVE+BESS (6 rokov, DPPO 21 %).

    Vráti list ročných tax shield benefitov (€/rok) od roku 1 po depr_years.

    Args:
        net_capex_eur: CAPEX po dotácii (zdaňovaný základ)
        dppo_pct: Sadzba DPPO (15 % malé firmy, 21 % do 5 mil, 24 % nad 5 mil)
        depr_years: Roky odpisu (6 pre FVE+BESS v SK)
        has_sufficient_profit: Či firma má dostatočný zisk na uplatnenie shield-u

    Returns:
        List rocnych tax shield amounts (€/rok)
    """
    if not has_sufficient_profit:
        return [0.0] * depr_years
    annual_shield = net_capex_eur * dppo_pct / depr_years
    return [annual_shield] * depr_years


def sk_dotacia_zelena_podnikom(
    capex_eur: float,
    samospotreba_pct: float,
    max_dotacia_eur: float = 50_000,
    intenzita_pct: float = 0.40,
    min_samospotreba_pct: float = 50,
) -> dict:
    """Vyhodnotenie dotácie Zelená podnikom (2024-2026).

    Pravidlá:
    - Max 50 000 € absolútne
    - Intenzita 45 % z CAPEX
    - Splnené samospotreba ≥ 80 % (kritérium)
    - Suma = MIN(CAPEX × 0.45, 50k€) ak splnené, inak 0

    Returns:
        Dict s eligibility a sumou
    """
    eligible = samospotreba_pct >= min_samospotreba_pct
    if not eligible:
        return {
            "eligible": False,
            "amount_eur": 0.0,
            "reason": f"Samospotreba {samospotreba_pct:.1f} % < {min_samospotreba_pct} %",
            "max_possible": min(capex_eur * intenzita_pct, max_dotacia_eur),
        }
    eff_int = 0.45 if samospotreba_pct > 80 else intenzita_pct
    amount = min(capex_eur * eff_int, max_dotacia_eur)
    return {
        "eligible": True,
        "amount_eur": amount,
        "intenzita_used_pct": amount / capex_eur * 100,
        "reason": f"Splnené (samospotreba {samospotreba_pct:.1f} % >= 80 %)",
    }
