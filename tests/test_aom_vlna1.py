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


# ---------- spoločné: request z DB riadku → engine ----------
_A_ENGINE = {"name": "Test OM", "om_psc": "949 01", "om_sadzba": "VN", "consumption_annual_mwh": 600,
             "om_mrk_kw": 400, "om_rk_kw": 300, "om_tarif_typ": "fix"}


def _req(**kw):
    return aom._build_request_from_analyza(dict(_A_ENGINE, **kw))


def _engine(rq, pv, bess, merchant=False):
    from energovision_analytics.api.services.engine_service import (
        run_variants_pipeline, build_run_variants_response)
    rq["variants"]["pv_kwp_options"] = [pv]
    rq["variants"]["bess_kwh_options"] = [bess]
    rq["variants"]["merchant_mode"] = merchant
    return build_run_variants_response(run_variants_pipeline(rq))["variants"][0]


# ---------- 3) Falošné nuly: 0 je platná hodnota, nie „chýba" ----------
def test_03_request_nuly_ostanu_nulou():
    r = _req(tarif_sell=0, merchant_organizer_fee_pct=0, arb_min_spread_eur_mwh=0,
             mrk_export_penalty_eur_kwh=0, om_tarif_typ="spot", vykup_spot_diskont_eur_mwh=0)
    assert r["export_price_eur_kwh"] == 0.0
    assert r["variants"]["merchant_organizer_fee_pct"] == 0.0
    assert r["ems_config"]["arb_min_spread_eur_mwh"] == 0.0
    assert r["ems_config"]["mrk_export_penalty_eur_kwh"] == 0.0
    assert r["export_config"]["spot_diskont_eur_mwh"] == 0.0
    # chýbajúca hodnota → default
    r = _req()
    assert r["export_price_eur_kwh"] == 0.06 and r["variants"]["merchant_organizer_fee_pct"] == 15.0


def test_03_engine_vykup_0_nezarobi():
    v = _engine(_req(tarif_sell=0, max_export_kw=None), 300, 0)
    assert v["value_streams"]["solar_export_eur"] == 0.0, v["value_streams"]


def test_03_pv_only_export_zakazany_mrk_0():
    # max_export_kw = 0 (pretoky zakázané) → PV-only baseline nesmie exportovať (audit E05/GPT A02)
    v = _engine(_req(max_export_kw=0), 300, 0)
    assert v["value_streams"]["solar_export_eur"] == 0.0, v["value_streams"]


def test_03_merchant_export_zakazany_mrk_0():
    # merchant pri zakázanom exporte nesmie predávať do siete (audit M2: 42 357 €/r fiktívne)
    v = _engine(_req(max_export_kw=0, om_tarif_typ="spot"), 0, 500, merchant=True)
    assert float(v.get("merchant_eur") or v["value_streams"].get("merchant_eur") or 0) <= 1.0, v["value_streams"]


# ---------- 4) capex_pv_eur obsahuje fixnú zložku ----------
def test_04_rozpis_capex_sedi_na_celok():
    v = _engine(_req(), 300, 200)
    assert abs(v["capex_pv_eur"] + v["capex_bess_eur"] - v["capex_total_eur"]) < 0.01, (
        v["capex_pv_eur"], v["capex_bess_eur"], v["capex_total_eur"])
    assert v["capex_pv_eur"] > 300 * 574  # vrátane fixu 38 000 €


# ---------- 7) Konfigurácia z cenovej ponuky = cena ponuky aj pod 150 kWp ----------
def test_07_cp_capex_pod_150_kwp():
    for price, kwp, kwh in [(200000, 100, 100), (120000, 120, 0), (95000, 60, 50), (180000, 250, 200)]:
        rq = _req(cp_price_eur=price, cp_kwp=kwp, cp_bess_kwh=kwh)
        v = _engine(rq, kwp, kwh)
        assert abs(v["capex_total_eur"] - price) < 1.0, (price, kwp, kwh, rq["capex"], v["capex_total_eur"])


# ---------- e2e cez FakeSB: run_variants_premium → tabuľka → full_response → posudok ----------
_AID = "aid-1"
_A_E2E = {"id": _AID, "name": "Audit s.r.o.", "om_psc": "94901", "om_sadzba": "VN", "om_tarif_typ": "spot",
          "om_mrk_kw": 400, "om_rk_kw": 300, "consumption_annual_mwh": 900, "scenario_type": "nova_fve",
          "dotacia_enabled": False, "tarif_buy": 0.146, "tarif_sell": 0.06, "selected_variant_id": None,
          "consumption_profile_path": None, "customers": None, "econ_results": None}


def _sb(**kw):
    sb = FakeSB()
    sb.db["analyza_om"] = [dict(_A_E2E, **kw)]
    sb.db["analyza_om_variants"] = []
    return sb


def _rows(sb):
    return sorted(sb.db["analyza_om_variants"], key=lambda r: r.get("position") or 0)


def _render_capture(sb, which):
    """Spustí render_posudok_* s vypnutým PDF/DOCX a AI; vráti kontext, ktorý by išiel do PDF."""
    import posudok_orkestra
    import posudok_chocosuc.generator as cg
    import posudok_chocosuc.generator_docx as cd
    cap = {}
    saved = (posudok_orkestra.generate_orkestra_pdf, cg.generate_chocosuc_pdf, cd.generate_chocosuc_docx)
    posudok_orkestra.generate_orkestra_pdf = lambda ctx: cap.setdefault("ctx", ctx) and b"%PDF"
    cg.generate_chocosuc_pdf = lambda ctx: cap.setdefault("ctx", ctx) and b"%PDF"
    cd.generate_chocosuc_docx = lambda ctx: b"DOCX"
    _key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        getattr(aom, f"render_posudok_{which}")(sb, _AID)
    finally:
        posudok_orkestra.generate_orkestra_pdf, cg.generate_chocosuc_pdf, cd.generate_chocosuc_docx = saved
        if _key:
            os.environ["ANTHROPIC_API_KEY"] = _key
    return cap["ctx"]


# ---------- 5) Variant z cenovej ponuky / aom-custom-variant prežije prepočet ----------
def test_05_custom_variant_webhook_je_manual():
    import ast
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "webhook_aom_custom_variant")
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "insert_variant_via_engine")
    kw = {k.arg: k.value for k in call.keywords}
    assert "capex_source" in kw and getattr(kw["capex_source"], "value", None) == "manual"


def test_05_cp_variant_prezije_prepocet():
    sb = _sb()
    aom.run_variants_premium(sb, _AID)
    # CP→Analýza: 280 kWp + 200 kWh za 252 000 € (audit F03), ako ho volá webhook
    aom.insert_variant_via_engine(sb, _AID, "CP 280 kWp", 280, 200, 100, capex_per_kwp=(252000 - 200 * 318) / 280,
                                  capex_per_kwh_bess=318, capex_source="manual")
    aom.run_variants_premium(sb, _AID)
    cp = [r for r in _rows(sb) if r["name"] == "CP 280 kWp"]
    assert cp and abs(cp[0]["capex_eur"] - 252000) < 1.0, [(r["name"], r["capex_eur"]) for r in _rows(sb)]


# ---------- 8) Posudok páruje variant podľa DB id; chat/UI varianty sú vo full_response ----------
def test_08_posudok_paruje_podla_id_nie_konfiguracie():
    sb = _sb()
    aom.run_variants_premium(sb, _AID)
    # chat „s/bez optimizérov": 2 varianty rovnakej konfigurácie, iná cena (audit F01/T3)
    aom.insert_variant_via_engine(sb, _AID, "300 kWp — s optimizérmi", 300, 0, capex_per_kwp=700, capex_source="manual")
    aom.insert_variant_via_engine(sb, _AID, "300 kWp — bez optimizérov", 300, 0, capex_per_kwp=650, capex_source="manual")
    sel = next(r for r in _rows(sb) if r["name"] == "300 kWp — bez optimizérov")
    sb.db["analyza_om"][0]["selected_variant_id"] = sel["id"]
    for which in ("chocosuc", "orkestra"):
        ctx = _render_capture(sb, which)
        assert abs(float(ctx["capex_total_eur"]) - sel["capex_eur"]) < 1.0, (which, ctx["capex_total_eur"], sel["capex_eur"])


def test_08_chat_variant_je_vo_full_response():
    sb = _sb()
    aom.run_variants_premium(sb, _AID)
    r = aom.insert_variant_via_engine(sb, _AID, "310 kWp — podľa zadania", 310, 0, capex_per_kwp=620, capex_source="manual")
    fr = sb.db["analyza_om"][0]["econ_results"]["full_response"]["variants"]
    assert any(str(v.get("db_id")) == str(r["id"]) for v in fr), [v.get("db_id") for v in fr]
    # Orkestra/Web vezme presne tento variant, nie najbližší maticový 300 kWp (audit F12/T14)
    sb.db["analyza_om"][0]["selected_variant_id"] = r["id"]
    ctx = _render_capture(sb, "orkestra")
    assert abs(float(ctx["pv_kwp"]) - 310) < 0.01 and abs(float(ctx["capex_total_eur"]) - 310 * 620) < 1.0, (
        ctx["pv_kwp"], ctx["capex_total_eur"])


# ---------- 6) Intake: posledných 12 mesiacov, diery nie sú 0 kWh ----------
def _local_15min(start, end_excl):
    import pandas as pd
    tz = "Europe/Bratislava"
    utc = pd.date_range(pd.Timestamp(start, tz=tz).tz_convert("UTC"), pd.Timestamp(end_excl, tz=tz).tz_convert("UTC"),
                        freq="15min", inclusive="left")
    return utc.tz_convert(tz).tz_localize(None)  # lokálny čas ako export distribútora (DST)


def _kw_shape(idx):
    import numpy as np
    h, wd, m = idx.hour.values, idx.dayofweek.values, idx.month.values
    kw = np.full(len(idx), 200.0)
    kw[(h >= 6) & (h < 18) & (wd < 5)] += 300.0
    kw[(m <= 2) | (m >= 11)] *= 1.10
    return kw


def _sse_csv(idx, kw):
    lines = ["Odberné miesto:;X", "Názov:;TEST", "Dátum od:;x", "Dátum do:;y", ";", "Čas;Spotreba [MWh]"]
    for t, v in zip(idx, kw * 0.25 / 1000.0):
        lines.append(f"{t.day}.{t.month}.{t.year} {t.hour}:{t.minute:02d};{str(round(v, 6)).replace('.', ',')}")
    return "\n".join(lines).encode("utf-8")


def _intake(files):
    from ingestion import intake_agent
    _key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        res = intake_agent.run_agent([{"filename": f"f{i}.csv", "bytes": b} for i, b in enumerate(files)], {}, year=2025)
    finally:
        if _key:
            os.environ["ANTHROPIC_API_KEY"] = _key
    assert res.get("ok"), res.get("reason")
    return res


def _true_mwh(idx):
    return float((_kw_shape(idx) * 0.25).sum() / 1000.0)


def test_06_dva_roky_nie_su_dvojnasobok():
    i1, i2 = _local_15min("2023-01-01", "2024-01-01"), _local_15min("2024-01-01", "2025-01-01")
    res = _intake([_sse_csv(i1, _kw_shape(i1)), _sse_csv(i2, _kw_shape(i2))])
    assert abs(res["annual_mwh"] / _true_mwh(i2) - 1) < 0.01, (res["annual_mwh"], _true_mwh(i2))


def test_06_trinast_mesiacov_je_rok():
    i = _local_15min("2024-01-01", "2025-02-01")
    res = _intake([_sse_csv(i, _kw_shape(i))])
    ref = _true_mwh(_local_15min("2024-02-01", "2025-02-01"))
    assert abs(res["annual_mwh"] / ref - 1) < 0.01, (res["annual_mwh"], ref)


def test_06_diera_v_merani_nie_je_nula():
    i = _local_15min("2025-01-01", "2026-01-01")
    mask = ~((i >= "2025-06-01") & (i < "2025-06-15"))
    res = _intake([_sse_csv(i[mask], _kw_shape(i[mask]))])
    assert abs(res["annual_mwh"] / _true_mwh(i) - 1) < 0.01, (res["annual_mwh"], _true_mwh(i))
    gap = res["hourly"][(res["hourly"].index >= "2025-06-01") & (res["hourly"].index < "2025-06-15")]
    assert len(gap) and float(gap.min()) > 0, "diera v meraní nesmie byť 0 kWh"


def test_06_kalendarny_rok_bez_zmeny():
    i = _local_15min("2025-01-01", "2026-01-01")
    res = _intake([_sse_csv(i, _kw_shape(i))])
    assert abs(res["annual_mwh"] / _true_mwh(i) - 1) < 0.001, (res["annual_mwh"], _true_mwh(i))


def test_06_hodinove_data_diera_sa_nevyplni_konstantou():
    import pandas as pd
    from ingestion.normalizer import _kw_to_15min_mwh
    idx = pd.date_range("2025-03-01", periods=24 * 10, freq="1h")
    kw = pd.Series(100.0, index=idx)
    kw = kw[(kw.index < "2025-03-03") | (kw.index >= "2025-03-07")]  # 4 dni výpadok
    out = _kw_to_15min_mwh(kw)
    assert not ((out.index >= "2025-03-03 01:00") & (out.index < "2025-03-07")).any()


# ---------- 9) FIX tarifa: batéria neobchoduje podľa spotu ----------
def test_09_fix_bateria_nearbitrazuje_spot():
    # kancelária 500 MWh, FVE 300 kWp, FIX tarifa (audit E01/T1: BESS 500 kWh −595 €/r)
    rq = lambda: _req(consumption_annual_mwh=500, om_tarif_typ="fix", om_mrk_kw=300, om_rk_kw=300)
    pv_only = _engine(rq(), 300, 0)
    with_bess = _engine(rq(), 300, 500)
    assert with_bess["energy_flow"].get("grid_to_bat_mwh", 0) < 0.5, with_bess["energy_flow"]
    hodnota = with_bess["saving_y1_eur"] - pv_only["saving_y1_eur"]
    assert hodnota > 6000, hodnota  # pred opravou 3 174 €/r (spot arbitráž s RTE stratou), po 8 130 €/r

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
