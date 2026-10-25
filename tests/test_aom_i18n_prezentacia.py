"""B2B prezentácia (generuj_prezentaciu_b2b) — SK výstup sa jazykovými mutáciami nesmie zmeniť;
EN/DE bez slovenských zvyškov; AI prompt dostane glosár len pre en/de.
Zlaté súbory: python tests/test_aom_i18n_prezentacia.py --capture (LEN na kóde, ktorého SK výstup je referencia)."""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import generuj_prezentaciu as GP  # noqa: E402
from aom_i18n import use_lang  # noqa: E402
from aom_i18n.glossary import glossary_prompt  # noqa: E402
from aom_i18n_fixtures import normalize_html, slovak_leftovers  # noqa: E402

G = ROOT / "tests" / "golden" / "sk"
_IMG = "data:image/png;base64,iVBORw0KGgo="
_REF_NAMES = {"Žarnovica", "Partizánske", "Vlkanová"}  # mená miest referencií (vlastné mená)


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


def _fake_segno(monkeypatch):
    """QR blok (pvprj_3d_url) — segno nemusí byť nainštalované; stačí, že niečo zapíše do bufferu."""
    seg = types.ModuleType("segno")
    seg.make = lambda *a, **k: types.SimpleNamespace(save=lambda buf, **kw: buf.write(b"qr"))
    monkeypatch.setitem(sys.modules, "segno", seg)


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("dotacia", [True, False])
def test_prezentacia_bez_slovenciny(lang, dotacia, monkeypatch):
    _fake_segno(monkeypatch)
    p = fixture_payload(dotacia)
    p["pvprj_3d_url"] = "https://example.test/3d"
    p.pop("zakaznik")  # fallback „Vážený klient" sa musí tiež preložiť
    monkeypatch.setattr(GP, "_ai_prez_texty", lambda payload: {})
    with use_lang(lang):
        html = GP._build_b2b_html(p)
    assert f"<html lang='{lang}'>" in html
    assert "elektráreň v 3D" not in html and "<img src='data:image/png;base64," in html  # QR blok sa vykreslil
    left = [w for w in slovak_leftovers(html) if w not in _REF_NAMES]
    assert not left, f"{lang}: slovenské zvyšky {left}"


def test_prezentacia_formaty():
    with use_lang("en"):
        en = render()
    with use_lang("de"):
        de = render()
    assert "1,388.1 MWh" in en and "1.388,1 MWh" in de           # num
    assert "1.24 M €" in en and "1,24 Mio. €" in de               # _eur_c
    assert "187,000\u00a0€" in en and "187.000\u00a0€" in de      # _eur (NBSP pred €)
    assert "6.4 yrs" in en and "6,4 J." in de                     # návratnosť
    assert "1.56 MWp" in en and "1,56 MWp" in de                  # referencie
    assert "Zelená podnikom (Slovak state subsidy scheme for businesses)" in en
    assert "Maximal reservierte Leistung (MRK)" in de


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"content": [{"text": '{"podtitul": "x", "prinosy": []}'}]}


def _ai_system(lang, monkeypatch):
    import requests
    seen = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", lambda url, headers=None, json=None, timeout=None: seen.update(json) or _Resp())
    with use_lang(lang):
        out = GP._ai_prez_texty({"zameranie": "", "zobraz_dotaciu": True})
    assert out.get("podtitul") == "x"
    return seen["system"]


def test_ai_glosar_len_pre_en_de(monkeypatch):
    sk = _ai_system("sk", monkeypatch)
    assert "OUTPUT LANGUAGE" not in sk and sk.endswith("}\n")
    for lang in ("en", "de"):
        t = _ai_system(lang, monkeypatch)
        assert t == sk + glossary_prompt(lang) and glossary_prompt(lang)


if __name__ == "__main__" and "--capture" in sys.argv:
    for n, kw in CASES.items():
        (G / n).write_text(render(**kw), encoding="utf-8")
    print("zapísané:", ", ".join(CASES))
