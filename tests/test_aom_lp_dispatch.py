"""Regresné testy — LP plán batérie v site režime (audit AOM 2026-10-24 E02, vlna 2).

Pravidlá P2b–P4 dosahovali 39–66 % optima a väčšia batéria vedela zarobiť menej (t4).
Spúšťanie: `pytest tests/test_aom_lp_dispatch.py` (z koreňa repa).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "audit_bess"))
os.environ.setdefault("ENERGO_SPOT_CSV", os.path.join(ROOT, "aom_data", "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", os.path.join(ROOT, "aom_data", "tariffs", "2026.yaml"))

import numpy as np  # noqa: E402

from harness import actual_cost, real_spot, run_ems  # noqa: E402

N = 24 * 60                                   # 60 dní reálneho spotu 2025 — rýchle, ale pestré ceny
_h = np.arange(N) % 24
_LOAD = np.where((_h >= 6) & (_h <= 21), 40.0, 15.0)
_PV = np.clip(np.sin(np.pi * (_h - 6) / 13.0), 0, None) * 60.0
_SPOT = real_spot()[:N]


def _cost(kwh, dispatch):
    iv, s, bat, _ = run_ems(_LOAD, _PV, _SPOT, float(kwh), initial_soc_pct=0.08, dispatch=dispatch)
    return actual_cost(iv), iv


def test_lp_zarobi_aspon_tolko_ako_pravidla():
    for kwh in (100, 300):
        c_lp, _ = _cost(kwh, "lp")
        c_rules, _ = _cost(kwh, "rules")
        assert c_lp <= c_rules + 1.0, (kwh, c_lp, c_rules)


def test_lp_vacsia_bateria_nezarobi_menej():
    costs = [_cost(k, "lp")[0] for k in (50, 100, 200, 400)]
    assert all(b <= a + 1.0 for a, b in zip(costs, costs[1:])), costs


def test_lp_plan_sa_vykona_aj_ked_lp_da_pv_do_baterie_a_siet_do_zataze():
    # LP môže zvoliť „PV→bat + sieť→záťaž"; vykonanie nesmie plánované nabitie zahodiť
    # (predtým sa z plánovaných 151 MWh PV→bat nabilo len 26 MWh)
    _, iv = _cost(200, "lp")
    nabite = sum(i.pv_to_bat_kwh + i.grid_to_bat_kwh for i in iv)
    vybite = sum(i.bat_to_load_kwh for i in iv)
    assert nabite > 0 and vybite > 0.7 * nabite, (nabite, vybite)
