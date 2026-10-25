"""B2B prezentácia (generuj_prezentaciu_b2b) — SK výstup sa jazykovými mutáciami nesmie zmeniť;
EN/DE bez slovenských zvyškov; AI prompt dostane glosár len pre en/de.
Zlaté súbory: python tests/test_aom_i18n_prezentacia.py --capture (LEN na kóde, ktorého SK výstup je referencia)."""
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import generuj_prezentaciu as GP  # noqa: E402
from aom_i18n_fixtures import normalize_html  # noqa: E402

G = ROOT / "tests" / "golden" / "sk"
_IMG = "data:image/png;base64,iVBORw0KGgo="


def _variant(name, kwp, kwh, capex, npv, pb, samosp, dot=0):
    return {"name": name, "fve_kwp": kwp, "bess_kwh": kwh, "capex_eur": capex, "result_npv_eur_base": npv,
            "result_irr_pct_base": 14.2, "result_payback_y_base": pb, "result_samosp_pct": samosp,
            "result_samostat_pct": 31.4, "result_dotacia_eur": dot}


def fixture_payload(dotacia=True):
    vs = [_variant("Variant A - FVE", 250.5, 0, 187000, 412000, 5.8, 78.0),
          _variant("Variant B - FVE + BESS", 250.5, 215, 1240000, 1630000.5, 6.4, 91.2, dot=310000),
          _variant("Variant C - Max", 420.0, 430, 2310000, 1495000, 7.1, 88.0)]
    return {"zakaznik": "Testovacia firma s.r.o.", "datum": "25. 10. 2026", "zameranie": "",
            "zobraz_dotaciu": dotacia,
            "om": {"consumption_annual_mwh": 1388.06, "consumption_peak_kw_15min": 726, "consumption_peak_kw_hourly": 719.7,
                   "om_mrk_kw": 840, "consumption_mrk_utilization_pct": 86.4},
            "variant": vs[1], "varianty": vs, "charts": [_IMG], "pvprj_render": _IMG, "pvprj_3d_url": "",
            "kontakt": "obchod@energovision.sk · energovision.sk"}


CASES = {"prezentacia.html": dict(dotacia=True), "prezentacia_bez_dotacie.html": dict(dotacia=False)}


def render(dotacia=True, ai=None):
    orig = GP._ai_prez_texty
    GP._ai_prez_texty = lambda payload: dict(ai or {})  # {} = návrat pri chybe/bez kľúča → statické texty
    try:
        return normalize_html(GP._build_b2b_html(fixture_payload(dotacia)))
    finally:
        GP._ai_prez_texty = orig


def test_prezentacia_sk_golden():
    bad = [n for n, kw in CASES.items() if (G / n).read_text(encoding="utf-8") != render(**kw)]
    assert not bad, f"SK regresia: {bad}"


if __name__ == "__main__" and "--capture" in sys.argv:
    for n, kw in CASES.items():
        (G / n).write_text(render(**kw), encoding="utf-8")
    print("zapísané:", ", ".join(CASES))
