"""RTE model — round-trip efficiency závislá od stavu, nie konštanta.

Reálna RTE (AC-AC) klesá pri:
    - Vysokých C-rate (PCS straty)
    - Krajných SoC (cell balancing inefficiency)
    - Extremnyh teplotách (mimo 25 °C)

Default krivka kalibrovaná na Huawei LUNA2000-215 datasheet + field testing reports.
Pre Solinteg E2BR / Sungrow / BYD aplikovať vlastné parametre.
"""
from __future__ import annotations

from math import exp


def rte_simple(base_rte: float = 0.88) -> float:
    """Konštantná RTE — pre rule-based dispatch ako fallback."""
    return base_rte


def rte_curve(
    soc: float,
    c_rate: float,
    temp_c: float = 25.0,
    base_rte_ac_ac: float = 0.88,
) -> float:
    """Vráti RTE pre danú prevádzkovú podmienku.

    Args:
        soc: State of Charge v rozsahu 0–1
        c_rate: Aktuálny C-rate (power_kw / capacity_kwh)
        temp_c: Teplota článku (°C, typicky 20-28 v container s HVAC)
        base_rte_ac_ac: Nominálna RTE pri (SoC=0.5, C=0.3, T=25)

    Returns:
        Korigovaná RTE (typicky 0.82–0.91)
    """
    # SoC stress — krajné hodnoty (0 alebo 1) stratia ~2%
    soc_efficiency = 1.0 - 0.04 * abs(soc - 0.5) * 2

    # C-rate stress — nad 0.3C lineárne klesá
    c_efficiency = 1.0 if c_rate <= 0.3 else 1.0 - 0.05 * (c_rate - 0.3)

    # Temperature stress — Arrhenius-light, optimum 25 °C
    if temp_c < 0:
        temp_efficiency = 0.85  # cold soak penalty
    elif temp_c < 15:
        temp_efficiency = 1.0 - 0.002 * (15 - temp_c)
    elif temp_c <= 35:
        temp_efficiency = 1.0 - 0.0005 * abs(temp_c - 25)
    else:
        temp_efficiency = 1.0 - 0.003 * (temp_c - 35)

    rte = base_rte_ac_ac * soc_efficiency * c_efficiency * temp_efficiency
    return max(0.70, min(0.93, rte))


def split_rte(rte_round_trip: float) -> tuple[float, float]:
    """Rozdelí round-trip RTE na charge a discharge zložku.

    Predpoklad: charge_eff = discharge_eff = sqrt(round_trip_eff).
    Toto je štandardný predpoklad pre LFP/NMC.

    Returns:
        (eta_charge, eta_discharge)
    """
    eta_one_way = rte_round_trip ** 0.5
    return (eta_one_way, eta_one_way)
