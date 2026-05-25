"""Battery engine — fyzikálne korektný BESS model s degradáciou.

Modeluje:
    - RTE per (SoC, C-rate, T) — nie konštanta
    - Calendar aging (Naumann-Schimpe square-root law)
    - Cycle aging (per EFC, závisí od DoD, C-rate, T)
    - Thermal stress factor (Arrhenius)
    - Cycle counter (Rainflow-light)
    - Replacement event detekcia (SoH < warranty EOL)
"""
from energovision_analytics.battery.degradation import (
    BatteryDegradationModel,
    NaumannSchimpeParams,
    estimate_lifetime_years,
)
from energovision_analytics.battery.efficiency import (
    rte_curve,
    rte_simple,
)
from energovision_analytics.battery.pack_model import (
    BatteryPack,
    BatteryState,
    DispatchResult,
)

__all__ = [
    "BatteryPack",
    "BatteryState",
    "DispatchResult",
    "BatteryDegradationModel",
    "NaumannSchimpeParams",
    "estimate_lifetime_years",
    "rte_curve",
    "rte_simple",
]
