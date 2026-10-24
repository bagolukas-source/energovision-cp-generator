"""Regresné testy — audit AOM 2026-10-24, vlna 1b: výstupy engine_service (A13/G2, A07, A24, F24).

Každý test pred opravou padal (reprodukcia nálezu), po oprave prechádza.
Spúšťanie: `python3 tests/test_aom_vlna1b_engine.py` alebo `pytest tests/test_aom_vlna1b_engine.py`
(z koreňa repa; ENERGO_SPOT_CSV / ENERGO_TARIFF_YAML dopĺňa test_aom_vlna1).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_aom_vlna1 import _engine, _req  # noqa: E402  (nastaví sys.path + env)

from energovision_analytics.api.services import engine_service as es  # noqa: E402

_EUR_KEYS = ("solar_to_load_eur", "solar_export_eur", "bess_self_consumption_eur", "arbitrage_eur",
             "peak_shaving_eur", "mrk_penalty_avoided_eur", "merchant_eur")
# mesačný kľúč → ročný stream vo value_streams
_STREAM = {"solar_to_load_eur": "solar_self_consumption_eur"}


def _assert_mesiace_sedia(v):
    ms, vs = v["monthly_summary"], v["value_streams"]
    assert len(ms) == 12 and [m["month"] for m in ms] == list(range(1, 13))
    total = sum(m["total_eur"] for m in ms)
    assert abs(total - vs["total_eur"]) <= 1.0, (round(total), round(vs["total_eur"]))
    for k in _EUR_KEYS:
        s = sum(m[k] for m in ms)
        assert abs(s - vs[_STREAM.get(k, k)]) <= 1.0, (k, round(s), round(vs[_STREAM.get(k, k)]))
    for m in ms:  # mesiac sa sčíta zo svojich streamov
        assert abs(m["total_eur"] - sum(m[k] for k in _EUR_KEYS)) <= 1e-6, m


# ---------- A13/G2: Σ 12 mesiacov == ročné value_streams ----------
def test_a13_mesiace_bess_self():
    # predtým PV200/BESS200: Σ mesiacov o bess_self (+ mrk penále) menej než ročne
    v = _engine(_req(), 200, 200)
    assert v["value_streams"]["bess_self_consumption_eur"] > 0, v["value_streams"]
    _assert_mesiace_sedia(v)
    assert v["monthly_summary_basis"]["flows"].startswith("monthly_flows")


def test_a13_mesiace_merchant():
    # predtým merchant PV200/BESS500: mesačný súhrn bez merchantu (Σ = len PV časť)
    v = _engine(_req(om_tarif_typ="spot"), 200, 500, merchant=True)
    assert v["value_streams"]["merchant_eur"] > 0, v["value_streams"]
    _assert_mesiace_sedia(v)
    assert v["monthly_summary_basis"]["merchant_eur"] == "rovnomerne /12"


def test_a13_mesiace_pv_only():
    # PV-only (bez EMS behu): total sa doteraz počítal zo sav_total, nie zo streamov mesiaca
    v = _engine(_req(), 200, 0)
    _assert_mesiace_sedia(v)


def test_a13_merchant_mesacny_rozpad_z_detailu():
    w, basis = es._merchant_monthly_weights({"monthly_eur": [0, 0, 0, 0, 0, 2, 2, 0, 0, 0, 0, 0]})
    assert basis == "merchant_detail.monthly_eur" and w[5] == w[6] == 0.5 and sum(w) == 1.0
    w, basis = es._merchant_monthly_weights({})
    assert basis == "rovnomerne /12" and abs(sum(w) - 1.0) < 1e-12


# ---------- A07: cashflow_array = presne roky 0..horizon ----------
def _cf(horizon):
    rq = _req()
    rq["financial"]["horizon_years"] = horizon
    return _engine(rq, 200, 200)


def _npv(cf, r):
    return sum(x / (1 + r) ** t for t, x in enumerate(cf))


def test_a07_cashflow_horizont_5():
    v = _cf(5)  # predtým 21 hodnôt (roky 6..20 vymyslené opakovaním posledného roka)
    assert len(v["cashflow_array"]) == 6, len(v["cashflow_array"])
    assert abs(_npv(v["cashflow_array"], 0.06) - v["npv_eur"]) <= 1.0, (_npv(v["cashflow_array"], 0.06), v["npv_eur"])


def test_a07_cashflow_horizont_25():
    v = _cf(25)  # predtým orezané na 25 (chýbal rok 25)
    assert len(v["cashflow_array"]) == 26, len(v["cashflow_array"])
    assert abs(_npv(v["cashflow_array"], 0.06) - v["npv_eur"]) <= 1.0, (_npv(v["cashflow_array"], 0.06), v["npv_eur"])


# ---------- A24: export intervalov pre PV-only nepadá ----------
def test_a24_export_intervalov_pv_only():
    out = es.export_variant_intervals(_req(), 200, 0)
    assert out["n"] >= 8000 and len(out["pv_kw"]) == out["n"]
    assert sum(out["pv_kw"]) > 150_000 and max(out["battery_kw"]) == 0.0


# ---------- F24: intervaly používajú rovnaké tariff_overrides ako hlavný beh ----------
def test_f24_intervaly_obchodnik_override():
    captured = {}
    orig = es.VariantGenerator

    class _Spy(orig):
        def run_single(self, *a, **kw):
            r = super().run_single(*a, **kw)
            if r.intervals:
                captured["intervals"] = r.intervals
            return r

    rq = _req(tarif_obchodnik_eur_mwh=55.0)  # FIX tarifa → retail cena je v každej hodine rovnaká
    main = es.run_variants_pipeline(dict(rq, variants=dict(rq["variants"], pv_kwp_options=[200],
                                                            bess_kwh_options=[200])))
    td = main["tariff_detail"]
    assert td["obchodnik_eur_mwh"] == 55.0, td
    es.VariantGenerator = _Spy
    try:
        es.export_variant_intervals(_req(tarif_obchodnik_eur_mwh=55.0), 200, 200)
    finally:
        es.VariantGenerator = orig
    ivs = captured["intervals"]
    buy = {round(iv.tarif_buy_eur_kwh * 1000, 2) for iv in ivs}
    assert buy == {td["retail_total_eur_mwh"]}, (sorted(buy)[:5], td["retail_total_eur_mwh"])


if __name__ == "__main__":
    import traceback
    fails = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except Exception:
            fails += 1
            print(f"FAIL {n}")
            traceback.print_exc()
    print(f"{len(tests) - fails}/{len(tests)} OK")
    sys.exit(1 if fails else 0)
