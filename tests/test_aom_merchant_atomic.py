"""Merchant s chronologickým SoC (denný MILP) a atómové nahradenie variantov (RPC + fallback).

Audit AOM 2026-10-24: M1 (merchant párovanie bez chronológie), A10/F14 (neatómové nahradenie
variantov), A23 (zlyhanie všetkých variantov = completed). Spúšťanie: `pytest tests/test_aom_merchant_atomic.py`.
"""
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp
from scipy.optimize import linprog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ENERGO_SPOT_CSV", os.path.join(ROOT, "aom_data", "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", os.path.join(ROOT, "aom_data", "tariffs", "2026.yaml"))
try:
    import weasyprint  # noqa: F401
except Exception:
    _w = types.ModuleType("weasyprint")
    _w.HTML = _w.CSS = object
    sys.modules["weasyprint"] = _w

import analyza_om_v2 as aom  # noqa: E402
from aom_fakes import FakeSB  # noqa: E402
from energovision_analytics.financial.merchant_arbitrage import (  # noqa: E402
    _solve_day, compute_merchant_arbitrage, degradation_reserve_eur_mwh)

SPOT = pd.read_csv(os.environ["ENERGO_SPOT_CSV"])["price_eur_per_mwh"].to_numpy()[:8760]


def _lp(price, kwh, p_kw, rk_kw, exp_kw, fee, degr=0.0, cycles=1.0, load=None, rte=0.88, window=24):
    """Nezávislá referencia (docs/aom-audit-2026-10-24/04-tarify-merchant/merchant_lp.py): LP s
    chronologickým SoC, deň začína prázdny, strop DC energie = usable × cykly/deň."""
    n = len(price); sr = rte ** 0.5; usable = kwh * 0.9
    head = np.full(n, rk_kw, float) if load is None else np.maximum(0.0, rk_kw - np.asarray(load, float))
    cmax = np.minimum(p_kw, head); dmax = min(p_kw, exp_kw)
    nv = 3 * n + 1
    obj = np.zeros(nv)
    obj[:n] = (1 - fee) * price / 1000.0
    obj[n:2 * n] = -((1 - fee) * price / 1000.0 - degr / 1000.0 / sr)
    rows = np.repeat(np.arange(n), 4)
    cols = np.column_stack([2 * n + np.arange(1, n + 1), 2 * n + np.arange(n), np.arange(n), n + np.arange(n)]).ravel()
    vals = np.tile([1.0, -1.0, -sr, 1.0 / sr], n)
    starts = list(range(0, n, window))
    reset = sp.coo_matrix((np.ones(len(starts)), (np.arange(len(starts)), 2 * n + np.array(starts))), shape=(len(starts), nv))
    a_eq = sp.vstack([sp.coo_matrix((vals, (rows, cols)), shape=(n, nv)), reset]).tocsr()
    b_eq = np.zeros(n + len(starts))
    a_ub = b_ub = None
    if cycles is not None:
        r = [k for k, st in enumerate(starts) for _ in range(st, min(st + window, n))]
        c = [n + t for st in starts for t in range(st, min(st + window, n))]
        a_ub = sp.coo_matrix((np.full(len(r), 1.0 / sr), (r, c)), shape=(len(starts), nv)).tocsr()
        b_ub = np.full(len(starts), usable * cycles)
    bounds = [(0, cmax[t]) for t in range(n)] + [(0, dmax)] * n + [(0, usable)] * (n + 1)
    res = linprog(obj, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    assert res.status == 0, res.message
    return -res.fun


def _merchant(**kw):
    base = dict(spot_eur_mwh=SPOT, dt_h=1.0, bess_kwh=1000, power_kw_ac=500, rk_kw=5000, export_kw=5000,
                organizer_fee_pct=15.0, window=24)
    base.update(kw)
    return compute_merchant_arbitrage(**base)


# ---------- merchant = LP s chronologickým SoC ----------
def test_merchant_1_cyklus_rovna_sa_lp():
    e = _merchant(max_cycles_per_day=1.0)
    lp = _lp(SPOT, 1000, 500, 5000, 5000, 0.15, cycles=1.0)
    assert abs(e["merchant_net_eur"] - lp) <= 1.0, (e["merchant_net_eur"], lp)
    assert abs(e["merchant_net_eur"] - 33622) <= 1.0, e["merchant_net_eur"]   # rc1 = LP
    assert e["model"] == "denny_milp_chronologicky_soc"


def test_merchant_viac_cyklov_s_degradaciou_a_mrk_rovna_sa_lp():
    degr = degradation_reserve_eur_mwh(318, 6000, 0.90)
    load = 4700.0 - 300.0 * np.sin(np.arange(len(SPOT)) / 24 * 2 * np.pi)   # odber blízko RK
    for cyc, ld in ((1000 / 365, None), (2.0, load)):
        e = _merchant(max_cycles_per_day=cyc, degradation_cost_eur_mwh=degr, load_kw=ld)
        lp = _lp(SPOT, 1000, 500, 5000, 5000, 0.15, degr=degr, cycles=cyc, load=ld)
        # dispatch = LP optimum s opotrebením v prahu; opotrebenie sa od výnosu neodpočítava
        assert abs(e["merchant_net_eur"] - e["degradation_eur"] - lp) <= 2.0, (cyc, e["merchant_net_eur"], lp)


def test_merchant_nepreda_pred_nabitim():
    # drahé ráno, lacné poobede: prázdna batéria o polnoci nemá čo predať (greedy párovanie predávalo)
    rev = np.array([200.0] * 8 + [100.0] * 8 + [20.0] * 8)
    e = compute_merchant_arbitrage(rev, 1.0, 1000, 500, 2000, 2000, organizer_fee_pct=0.0, window=24,
                                   max_cycles_per_day=1.0)
    assert e["merchant_net_eur"] == 0.0 and e["dc_throughput_mwh"] == 0.0, e


def test_merchant_zaporny_spot_nenabija_a_nevybija_naraz():
    w = np.array([-80.0] * 12 + [50.0] * 12)
    c, d = _solve_day(w, np.full(24, 500.0), 500.0, 900.0, None, 0.88 ** 0.5, 0.15, 0.0, 0.0)
    assert float(np.max(c * d)) <= 1e-6, (c, d)
    assert c.sum() > 0


# ---------- atómové nahradenie variantov ----------
_AID = "aid-1"
_A = {"id": _AID, "name": "Audit s.r.o.", "om_psc": "94901", "om_sadzba": "VN", "om_tarif_typ": "spot",
      "om_mrk_kw": 400, "om_rk_kw": 300, "consumption_annual_mwh": 900, "scenario_type": "nova_fve",
      "dotacia_enabled": False, "tarif_buy": 0.146, "tarif_sell": 0.06, "selected_variant_id": None,
      "consumption_profile_path": None, "customers": None, "econ_results": None, "updated_at": "v1"}
_OLD = {"id": "old-1", "analyza_id": _AID, "name": "starý", "position": 1, "fve_kwp": 50, "bess_kwh": 0,
        "capex_source": "engine_v095_quick"}


def _sb(rpc):
    sb = FakeSB(aom_atomic_rpc=rpc)
    sb.db["analyza_om"] = [dict(_A)]
    sb.db["analyza_om_variants"] = [dict(_OLD)]
    return sb


def _analyza(sb):
    return sb.db["analyza_om"][0]


def _assert_db_ids_sedia(sb):
    ids = {str(r["id"]) for r in sb.db["analyza_om_variants"]}
    fr = _analyza(sb)["econ_results"]["full_response"]["variants"]
    assert fr and all(str(v.get("db_id")) in ids for v in fr), ([v.get("db_id") for v in fr], ids)


@pytest.mark.parametrize("rpc", [True, False], ids=["rpc", "fallback"])
def test_prepocet_nahradi_varianty_a_rucny_prepocita(rpc):
    sb = _sb(rpc)
    sb.db["analyza_om_variants"].append({"id": "man-1", "analyza_id": _AID, "name": "CP 280 kWp", "position": 2,
                                         "fve_kwp": 280, "bess_kwh": 200, "bess_kw": 100, "capex_eur": 252000,
                                         "capex_source": "manual", "inverter_kw": 250})
    _analyza(sb)["selected_variant_id"] = "man-1"
    aom.run_variants_premium(sb, _AID)
    assert sb.rpc_calls == ["aom_replace_engine_result"]
    a = _analyza(sb)
    assert a["status"] == "completed"
    rows = sb.db["analyza_om_variants"]
    assert all(r["id"] not in ("old-1", "man-1") for r in rows)          # staré nahradené
    man = [r for r in rows if r.get("capex_source") == "manual"]
    assert len(man) == 1 and abs(man[0]["capex_eur"] - 252000) < 1.0, man  # ručný prepočítaný enginom
    assert man[0].get("inverter_kw") == 250 and man[0].get("result_npv_eur_base") is not None
    rich = [v for v in a["econ_results"]["full_response"]["variants"] if v.get("capex_source") == "manual"]
    assert rich and str(rich[0]["db_id"]) == str(man[0]["id"])
    assert a["selected_variant_id"] == man[0]["id"]                          # výber premapovaný
    _assert_db_ids_sedia(sb)


def test_prepocet_rucneho_fve_bess_rozdeli_capex_podla_suroden_fve():
    # Röchling vzor: ponuka FVE + ponuka FVE+BESS s rovnakým kWp → BESS = rozdiel cien,
    # nie natvrdo 480 €/kWh (predtým 200 kWh × 480 = 96 000 € a FVE 156 000 €)
    sb = _sb(True)
    for vid, kwh, cap in (("man-fve", 0, 182000), ("man-bess", 200, 252000)):
        sb.db["analyza_om_variants"].append({"id": vid, "analyza_id": _AID, "name": vid, "position": 2,
                                             "fve_kwp": 280, "bess_kwh": kwh, "bess_kw": 100 if kwh else 0,
                                             "capex_eur": cap, "capex_source": "manual"})
    aom.run_variants_premium(sb, _AID)
    rich = [v for v in _analyza(sb)["econ_results"]["full_response"]["variants"]
            if v.get("capex_source") == "manual" and v.get("bess_kwh")]
    assert rich and abs(rich[0]["capex_bess_eur"] - 70000) < 1.0, rich[0].get("capex_bess_eur")
    assert abs(rich[0]["capex_total_eur"] - 252000) < 1.0


def test_rpc_zlyhanie_zapisu_nenecha_analyzu_bez_variantov():
    sb = _sb(True)
    sb.db["_fail_insert"] = {"analyza_om_variants": True}
    with pytest.raises(RuntimeError):
        aom.run_variants_premium(sb, _AID)
    assert [r["id"] for r in sb.db["analyza_om_variants"]] == ["old-1"]    # rollback


def test_rpc_zmena_pocas_vypoctu_nezapise_zastarany_vysledok(monkeypatch):
    sb = _sb(True)
    _orig = aom._generate_ai_narrative

    def _edit_pocas_behu(*a, **k):
        _analyza(sb)["updated_at"] = "v2"                                     # používateľ uložil zmenu
        return _orig(*a, **k)
    monkeypatch.setattr(aom, "_generate_ai_narrative", _edit_pocas_behu)
    with pytest.raises(Exception, match="zmenila"):
        aom.run_variants_premium(sb, _AID)
    a = _analyza(sb)
    assert a["status"] == "failed" and a["updated_at"] == "v2"
    assert [r["id"] for r in sb.db["analyza_om_variants"]] == ["old-1"]


@pytest.mark.parametrize("rpc", [True, False], ids=["rpc", "fallback"])
def test_a23_ziadny_variant_je_failed(monkeypatch, rpc):
    from energovision_analytics.api.services import engine_service as es
    monkeypatch.setattr(es, "run_variants_pipeline", lambda req, *a, **k: {})
    monkeypatch.setattr(es, "build_run_variants_response",
                        lambda raw, *a, **k: {"success": True, "variants": [], "top_picks": []})
    sb = _sb(rpc)
    with pytest.raises(RuntimeError):
        aom.run_variants_premium(sb, _AID)
    assert _analyza(sb)["status"] == "failed"
    assert sb.rpc_calls == []
    assert [r["id"] for r in sb.db["analyza_om_variants"]] == ["old-1"]    # nič sa nezmazalo


def test_chraneny_variant_prezije_a_ostane_vybrany():
    sb = _sb(True)
    sb.db["analyza_om_variants"][0]["is_protected"] = True
    _analyza(sb)["selected_variant_id"] = "old-1"
    aom.run_variants_premium(sb, _AID)
    assert any(r["id"] == "old-1" for r in sb.db["analyza_om_variants"])
    assert _analyza(sb)["selected_variant_id"] == "old-1"
