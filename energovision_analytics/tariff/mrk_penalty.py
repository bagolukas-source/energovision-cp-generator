"""MRK penalty engine — vrátane novej penalty SSD pri exporte od 1.1.2026.

Slovenská regulácia podľa vyhlášky ÚRSO 154/2024 Z.z. § 24 ods. 11
zavádza od 1.1.2026 penalty za prekročenie MRK pri **dodávke do DS**
(predtým iba pri odbere). Týka sa primárne FVE projektov s exportom prebytkov.

Mechanizmus:
    - Distribútor meria 15-min export do siete
    - Ak export > MRK v 15-min agregáte → penalty na prekročené kWh
    - Sadzba 0.0125 €/kWh = 12.5 €/MWh (SSD 2026 odhad, finalizácia ÚRSO 2026/E)
    - Penalty sa fakturuje mesačne podľa agregátu prekročení

Týmto je systematicky penalizované poddimenzovanie MRK voči FVE inštalácii.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from energovision_analytics.core.models import TariffYearInput


def compute_mrk_export_penalty(
    export_kw_15min: pd.Series | np.ndarray,
    mrk_kw: float,
    tariff: TariffYearInput,
) -> dict[str, float]:
    """Vypočítaj ročnú MRK export penalty.

    Args:
        export_kw_15min: 15-minútový export do siete v kW (8760×4 = 35040 hodnôt
            pre celý rok)
        mrk_kw: Maximálna rezervovaná kapacita
        tariff: Tarif pre daný rok/distribútor (obsahuje mrk_export_penalty_eur_kwh)

    Returns:
        Dict s:
            penalty_eur: ročná penalty v €
            overflow_kwh: celkové prekročené kWh
            overflow_hours: počet 15-min intervalov s prekročením
            max_overflow_kw: maximálne prekročenie v kW
            applicable: True ak tarif má penalty > 0
    """
    if isinstance(export_kw_15min, pd.Series):
        arr = export_kw_15min.to_numpy()
    else:
        arr = np.asarray(export_kw_15min, dtype=float)

    # Prekročenie nad MRK v kW (per 15-min)
    overflow_kw = np.maximum(0, arr - mrk_kw)

    # Konverzia na kWh — 15 min = 0.25 h
    overflow_kwh = overflow_kw * 0.25

    total_overflow_kwh = float(overflow_kwh.sum())
    overflow_hours = int((overflow_kw > 0).sum())
    max_overflow = float(overflow_kw.max()) if len(overflow_kw) > 0 else 0.0

    penalty_rate = tariff.mrk_export_penalty_eur_kwh

    return {
        "penalty_eur": total_overflow_kwh * penalty_rate,
        "overflow_kwh": total_overflow_kwh,
        "overflow_hours": overflow_hours,
        "max_overflow_kw": max_overflow,
        "penalty_rate_eur_kwh": penalty_rate,
        "applicable": penalty_rate > 0,
        "distribuutor": tariff.distribuutor.value,
        "rok": tariff.rok,
    }


def compute_mrk_capacity_charge(
    monthly_max_kw: pd.Series | list[float],
    mrk_kw: float,
    tariff: TariffYearInput,
) -> dict[str, float]:
    """Vypočítaj ročnú MRK kapacitnú fakturáciu (€/rok) pre VN klienta.

    Distribútor fakturuje **mesačné maximum 1/4-h** × `mrk_kapacita_eur_mw_mes`.
    Pre nesplnenú MRK (max < MRK) sa fakturuje plná MRK (nie max).
    Pre prekročenie (max > MRK) sa fakturuje max + penalty (samostatne).
    """
    if isinstance(monthly_max_kw, pd.Series):
        maxes = monthly_max_kw.to_numpy()
    else:
        maxes = np.asarray(monthly_max_kw, dtype=float)

    if tariff.sadzba.value != "VN":
        return {
            "annual_capacity_charge_eur": 0.0,
            "applicable": False,
            "reason": "MRK kapacitná fakturácia iba pre VN",
        }

    # Mesačná fakturácia: max(MRK, mesačný_max) × sadzba
    billable_mw_monthly = np.maximum(mrk_kw, maxes) / 1000  # → MW
    monthly_charges_eur = billable_mw_monthly * tariff.mrk_kapacita_eur_mw_mes

    return {
        "annual_capacity_charge_eur": float(monthly_charges_eur.sum()),
        "monthly_charges_eur": monthly_charges_eur.tolist(),
        "billable_mrk_mw_avg": float(billable_mw_monthly.mean()),
        "applicable": True,
        "rate_eur_mw_mes": tariff.mrk_kapacita_eur_mw_mes,
    }
