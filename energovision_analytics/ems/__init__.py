"""EMS Energy Management System — dispatch logika pre BESS.

Implementácie:
    - rule_based: multi-cycle greedy s warranty constraint (master Excel-style)
    - milp: Pyomo + HiGHS perfect foresight (TODO Sprint 3.2)
    - mpc: rolling horizon 48h s forecast chybou (TODO)

Value streams (SK adaptácia):
    1. Solar self-consumption (PV → load priamo)
    2. Solar export (PV → grid)
    3. BESS self-consumption (BAT → load v deficite)
    4. Tariff/wholesale arbitráž (BAT load-shifting)
    5. Peak demand reduction (BAT zníži ¼-h MRK špičku)
    6. MRK export penalty avoidance (nová SSD 2026)
"""
from energovision_analytics.ems.dispatch_state import (
    DispatchInterval,
    DispatchSummary,
    EMSConfig,
    ValueStream,
)
from energovision_analytics.ems.rule_based import RuleBasedEMS

__all__ = [
    "DispatchInterval",
    "DispatchSummary",
    "EMSConfig",
    "ValueStream",
    "RuleBasedEMS",
]
