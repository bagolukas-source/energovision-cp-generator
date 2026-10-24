"""Regresné testy — audit AOM 2026-10-24, vlna 1 (docs/aom-audit-2026-10-24/SUMMARY.md).

Každý test pred opravou padal (reprodukcia nálezu), po oprave prechádza.
Spúšťanie: `python3 tests/test_aom_vlna1.py` alebo `pytest tests/test_aom_vlna1.py`
(z koreňa repa; ENERGO_SPOT_CSV / ENERGO_TARIFF_YAML sa dopĺňajú automaticky).
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ENERGO_SPOT_CSV", os.path.join(ROOT, "aom_data", "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", os.path.join(ROOT, "aom_data", "tariffs", "2026.yaml"))
try:  # WeasyPrint potrebuje systémové knižnice; HTML render (Jinja) ho nepotrebuje
    import weasyprint  # noqa: F401
except Exception:
    _w = types.ModuleType("weasyprint")
    _w.HTML = _w.CSS = object
    sys.modules["weasyprint"] = _w

import analyza_om_v2 as aom  # noqa: E402
from aom_fakes import FakeSB  # noqa: E402

# delta variant pridanie_bess, ktorý sa nevráti (audit 05: NPV −60 857 €, payback None)
_CF_NEVRATI = [-159000, 14179, 13922, 13671, 13425, 13184, 12948, 7152, 6925, 6703, 6485, 6272,
               -9837, 5859, 5658, 5462, 5269, 5081, 4896, 4715, 4538]
_V_NEVRATI = {"variant_id": "x", "pv_kwp": 300, "bess_kwh": 500, "bess_kw": 250, "capex_total_eur": 159000,
              "dotacia_eur": 0, "saving_y1_eur": 12271, "npv_eur": -60857, "irr_pct": -1.02,
              "payback_simple_y": None, "cashflow_array": _CF_NEVRATI}
_A_BASE = {"id": "t", "name": "T", "consumption_annual_mwh": 1000, "selected_variant_id": "x",
           "scenario_type": "pridanie_bess", "om_sadzba": "NN", "om_mrk_kw": 400, "om_rk_kw": 300}


# ---------- 1) Návratnosť None → „nevráti sa" ----------
def test_01_posudok_navratnost_none():
    c = aom.build_orkestra_context(dict(_A_BASE), [dict(_V_NEVRATI)], "t")
    assert c["payback_years"] is None, c["payback_years"]
    assert all(s["payback_years"] is None for s in c["scenarios"]), [s["payback_years"] for s in c["scenarios"]]
    from posudok_orkestra import render_orkestra_html
    html = render_orkestra_html(c)
    assert "nevráti sa" in html
    assert "vráti v 0.0" not in html and ">0.0<" not in html


def test_01_posudok_sentinel_99():
    v = dict(_V_NEVRATI, payback_simple_y=99.0)
    c = aom.build_orkestra_context(dict(_A_BASE), [v], "t")
    assert c["payback_years"] is None


def _porovnanie(variants, monkey_ai=True):
    import posudok_orkestra
    sb = FakeSB()
    sb.db["analyza_om"] = [dict(_A_BASE, id="A1", customers={"company_name": "Klient s.r.o."})]
    sb.db["analyza_om_variants"] = [dict(v, analyza_id="A1", position=i) for i, v in enumerate(variants)]
    captured = {}
    orig_pdf, orig_ai = posudok_orkestra.generate_porovnanie_pdf, aom._generate_porovnanie_ai
    posudok_orkestra.generate_porovnanie_pdf = lambda ctx: captured.setdefault("ctx", ctx) and b"%PDF"
    aom._generate_porovnanie_ai = lambda inp: captured.setdefault("ai", inp) and {}
    try:
        aom.render_porovnanie(sb, "A1")
    finally:
        posudok_orkestra.generate_porovnanie_pdf, aom._generate_porovnanie_ai = orig_pdf, orig_ai
    return captured


def _row(i, capex, npv, irr, pb, kwp=300.0, kwh=0.0):
    return {"id": f"v{i}", "name": f"Variant {i}", "capex_eur": capex, "fve_kwp": kwp, "bess_kwh": kwh,
            "result_npv_eur_base": npv, "result_irr_pct_base": irr, "result_payback_y_base": pb}


def test_01_porovnanie_nevratny_nie_je_najrychlejsi():
    cap = _porovnanie([_row(1, 150000, -20000, -1.0, None, kwh=500), _row(2, 210000, 90000, 9.0, 8.4)])
    ctx = cap["ctx"]
    assert ctx["pick_payback"]["id"] == "v2", ctx["pick_payback"]
    r1 = next(r for r in ctx["rows"] if r["id"] == "v1")
    assert r1["payback_y"] is None
    from posudok_orkestra import render_porovnanie_html
    assert "nevráti sa" in render_porovnanie_html(ctx)


def test_01_porovnanie_ziadny_sa_nevrati():
    cap = _porovnanie([_row(1, 150000, -20000, -1.0, None, kwh=500), _row(2, 210000, -5000, 1.0, 99.0)])
    assert cap["ctx"]["same_winner"] is True
    assert "nevráti sa" in cap["ctx"]["ai_recommendation"]["detail"]


# ---------- 2) ChocoSuc verdikt podľa NPV/P10 + sadzba z analyzy ----------
def _chocosuc(v, **a_kw):
    from posudok_chocosuc.context import build_chocosuc_context
    return build_chocosuc_context(dict(_A_BASE, **a_kw), [v])


def test_02_chocosuc_zaporne_npv_nie_je_vyhodne():
    ctx = _chocosuc(dict(_V_NEVRATI))
    assert "výhodná vo všetkých" not in ctx["summary_headline"], ctx["summary_headline"]
    assert "zostáva NPV kladné" not in ctx["recommendation_line"], ctx["recommendation_line"]
    assert "nevráti sa" in ctx["recommendation_line"]


def test_02_chocosuc_kladne_npv_ostava_kladny_verdikt():
    cf = [-200000] + [40000] * 20
    v = dict(_V_NEVRATI, capex_total_eur=200000, saving_y1_eur=40000, npv_eur=250000, irr_pct=19.0,
             payback_simple_y=5.0, cashflow_array=cf)
    ctx = _chocosuc(v, scenario_type="nova_fve")
    assert "výhodná vo všetkých" in ctx["summary_headline"]
    assert "5,0" in ctx["recommendation_line"] or "4,5" in ctx["recommendation_line"]


def test_02_chocosuc_sadzba_z_analyzy():
    assert _chocosuc(dict(_V_NEVRATI), om_sadzba="NN")["om_sadzba"] == "NN"
    assert _chocosuc(dict(_V_NEVRATI), om_sadzba="vn")["om_sadzba"] == "VN"


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
