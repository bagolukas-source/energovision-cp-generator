"""CI wrapper pre property testy batérie (tests/audit_bess/t1–t7) a merchant sanity test.

Skripty vypisujú „VERDIKT: OK/CHYBA" a končia kódom 0 — tu sa verdikt vyhodnotí.
t4/t5b/t5c ukazujú známu nemonotónnosť greedy dispatchu (audit 2026-10-24 E02) — rieši
rolling-horizon LP vo vlne 2, preto xfail (nie strict). t4b je informatívny rozklad.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "tests", "audit_bess")
ENV = dict(os.environ,
           PYTHONPATH=ROOT,
           ENERGO_SPOT_CSV=os.environ.get("ENERGO_SPOT_CSV") or os.path.join(ROOT, "aom_data", "sk_spot_2025_hourly.csv"),
           ENERGO_TARIFF_YAML=os.environ.get("ENERGO_TARIFF_YAML") or os.path.join(ROOT, "aom_data", "tariffs", "2026.yaml"))

ZNAMA_NEMONOTONNOST = pytest.mark.xfail(reason="greedy dispatch nemonotónny (audit E02) — vlna 2: rolling LP", strict=False)


def _run(path):
    p = subprocess.run([sys.executable, "-W", "ignore", path], cwd=os.path.dirname(path), env=ENV,
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    return p.stdout


@pytest.mark.parametrize("name", [
    "t1_null", "t2_arbitrage", "t3_pv_shift", "t5_more_pv", "t6_cycle_stress", "t7_conservation",
    pytest.param("t4_monotonicity", marks=ZNAMA_NEMONOTONNOST),
    pytest.param("t5b_curtail_isolation", marks=ZNAMA_NEMONOTONNOST),
])
def test_audit_bess_verdikt(name):
    out = _run(os.path.join(AUDIT, f"{name}.py"))
    assert "CHYBA" not in out and "POKLES" not in out, out[-2000:]


@pytest.mark.parametrize("name", ["t4b_rte_attribution", pytest.param("t5c_decompose", marks=ZNAMA_NEMONOTONNOST)])
def test_audit_bess_rozklad_bezi(name):
    out = _run(os.path.join(AUDIT, f"{name}.py"))
    assert "POKLES" not in out, out[-2000:]


def test_merchant_arbitrage():
    _run(os.path.join(ROOT, "tests", "test_merchant_arbitrage.py"))
