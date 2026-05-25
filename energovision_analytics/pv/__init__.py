"""PV engine — analytický model FVE výroby kalibrovaný na PVGIS pre SK lokality.

MVP bez pvlib (príliš veľká závislosť). Plný pvlib integrácia v ďalšej iterácii.
Cieľ: ±5–10 % presnosť vs PVGIS pre slovenské lokality.
"""
from energovision_analytics.pv.analytical import (
    hourly_clear_sky_factor,
    monthly_yield_kwh_per_kwp,
    nearest_sk_location,
    sk_typical_monthly_yields,
    synthesize_hourly_profile,
)
from energovision_analytics.pv.degradation import pv_capacity_factor
from energovision_analytics.pv.losses import (
    apply_all_losses,
    apply_inverter_clipping,
    total_loss_factor,
)
from energovision_analytics.pv.system import PVSystemSim

__all__ = [
    "PVSystemSim",
    "hourly_clear_sky_factor",
    "monthly_yield_kwh_per_kwp",
    "sk_typical_monthly_yields",
    "synthesize_hourly_profile",
    "nearest_sk_location",
    "apply_all_losses",
    "apply_inverter_clipping",
    "total_loss_factor",
    "pv_capacity_factor",
]
