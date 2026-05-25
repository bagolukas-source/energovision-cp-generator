"""PV losses model — kumulatívne straty aplikované na ideálnu výrobu.

Komponenty:
    - Soiling (znečistenie panelov)
    - Snow (sneh — zimné mesiace)
    - Mismatch (rozdielnosť reťazcov)
    - Wiring (DC + AC káble)
    - Inverter efficiency
    - System availability (downtime)
"""
from __future__ import annotations


def total_loss_factor(
    soiling_pct: float = 2.0,
    snow_pct: float = 1.5,
    mismatch_pct: float = 1.0,
    wiring_pct: float = 1.5,
    inverter_eff_pct: float = 1.5,    # 100 - 98.5
    availability_pct: float = 1.0,
    other_pct: float = 0.5,
) -> float:
    """Vráti kumulatívny multiplikatívny faktor (0-1) pre PV výrobu.

    Defaulty kalibrované pre slovenské podmienky:
    - Soiling 2 % (priemer SK, čistené 1×/rok)
    - Snow 1.5 % (juh nižšie, sever vyššie)
    - Mismatch 1 % (well-binned moderné moduly)
    - Wiring 1.5 % (DC + AC)
    - Inverter 1.5 % (EU eff 98.5 %)
    - Availability 1 % (downtime, údržba)
    - Other 0.5 % (LID, PID, tienenie)

    Defaultný total: cca 14 % strát = factor 0.86 (zodpovedá PVGIS 14% default)
    """
    losses = [soiling_pct, snow_pct, mismatch_pct, wiring_pct,
              inverter_eff_pct, availability_pct, other_pct]
    factor = 1.0
    for loss in losses:
        factor *= (1 - loss / 100)
    return factor


def apply_all_losses(
    ideal_kwh: float,
    soiling_pct: float = 2.0,
    snow_pct: float = 1.5,
    mismatch_pct: float = 1.0,
    wiring_pct: float = 1.5,
    inverter_eff_pct: float = 1.5,
    availability_pct: float = 1.0,
    other_pct: float = 0.5,
) -> float:
    """Aplikuj všetky losses na ideálnu (clear-sky) výrobu."""
    return ideal_kwh * total_loss_factor(
        soiling_pct, snow_pct, mismatch_pct, wiring_pct,
        inverter_eff_pct, availability_pct, other_pct,
    )


def apply_inverter_clipping(
    dc_kw: float,
    inverter_kw_ac: float,
) -> tuple[float, float]:
    """Aplikuj AC clipping na DC výrobu.

    Returns:
        (ac_kw, clipped_kw) — ac_kw je po clippingu, clipped_kw je strata
    """
    if dc_kw <= inverter_kw_ac:
        return dc_kw, 0.0
    clipped = dc_kw - inverter_kw_ac
    return inverter_kw_ac, clipped
