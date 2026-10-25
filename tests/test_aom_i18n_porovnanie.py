"""Porovnávací súhrn ponúk — zlaté SK výstupy (spec 2026-10-25, „Ochrana SK verzie").
  python tests/test_aom_i18n_porovnanie.py --capture   # prepíše tests/golden/sk/porovnanie*.html (LEN na referenčnom kóde)
  python tests/test_aom_i18n_porovnanie.py --diff      # rozdiel aktuálneho SK výstupu voči zlatým súborom
Pri páde zlatého testu opraviť kód, nie zlatý súbor."""
import difflib
import os
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("ENERGO_SPOT_CSV", str(ROOT / "aom_data" / "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", str(ROOT / "aom_data" / "tariffs" / "2026.yaml"))
try:
    import weasyprint  # noqa: F401
except Exception:
    _w = types.ModuleType("weasyprint"); _w.HTML = _w.CSS = object; sys.modules["weasyprint"] = _w

import analyza_om_v2 as aom  # noqa: E402
import posudok_orkestra  # noqa: E402
from aom_fakes import FakeSB  # noqa: E402
from aom_i18n_fixtures import normalize_html  # noqa: E402

G = ROOT / "tests" / "golden" / "sk"


def _v(i, name, kwp, kwh, capex, npv, irr, pb, samosp=0, vyroba=0, export=0):
    return {"id": f"v{i}", "name": name, "fve_kwp": kwp, "bess_kwh": kwh, "bess_kw": kwh / 2, "capex_eur": capex,
            "result_npv_eur_base": npv, "result_irr_pct_base": irr, "result_payback_y_base": pb,
            "result_samosp_pct": samosp, "result_vyroba_mwh": vyroba, "result_export_mwh": export}


_CLIENT = "Testovacia firma s.r.o."
# A: 3 ponuky, 1 dominovaná, dve rôzne voľby (NPV vs. návratnosť), špička nad MRK, tarify
_A = ({"name": "Vyrobny areal Senec", "om_address": "Priemyselná 12, Senec", "consumption_annual_mwh": 1250.0,
       "consumption_peak_kw_15min": 620.0, "om_mrk_kw": 500.0, "tarif_buy": 0.1684, "tarif_sell": 0.045,
       "selected_variant_id": "v2"},
      [_v(1, f"P-26-322 {_CLIENT} SOLINTEG 535 kWp / 224 kWh exaktná ponuka", 535.0, 224.0, 412000, 268000, 11.8,
          7.9, 81.5, 562.3, 98.4),
       _v(2, "HUAWEI 535 kWp", 535.0, 0, 298000, 241000, 13.6, 6.4, 74.2, 562.3, 145.1),
       _v(3, "SUNGROW 400 kWp", 400.0, 0, 320000, 150000, 9.0, 8.5, 79.0, 420.0, 88.0)])
# B: 4 ponuky, 2 dominované, jedna sa nevráti, jedna voľba, bez tarify/adresy, špička pod MRK
_B = ({"name": "Sklad Nitra", "om_address": None, "consumption_annual_mwh": 1000.0,
       "consumption_peak_kw_15min": 300.0, "om_mrk_kw": 400.0, "selected_variant_id": None},
      [_v(1, "FVE 300 kWp + BESS 500 kWh", 300.0, 500.0, 150000, -20000, -1.0, None),
       _v(2, "FVE 300 kWp", 300.0, 0, 210000, 90000, 9.0, 8.4),
       _v(3, "FVE 280 kWp", 280.0, 0, 220000, 80000, 8.0, 9.1),
       _v(4, "FVE 260 kWp", 260.0, 0, 230000, 70000, 7.0, 99.0)])
# AI odpoveď (bez diakritiky, aby EN/DE kontrola zvyškov slovenčiny ignorovala dáta fixtúry)
_AI = {"ai_executive_summary": "<p>Summary of the site: 1250 MWh, peak 620 kW, MRK 500 kW.</p>",
       "ai_variant_assessments": [{"id": "v1", "verdict": "Best long-term value.", "pros": ["NPV 268 000 EUR"],
                                   "cons": ["CAPEX 412 000 EUR"]},
                                  {"id": "v2", "verdict": "Fastest payback.", "pros": ["6.4 y"], "cons": []}],
       "ai_recommendation": {"title": "", "detail": "<p>Choose SOLINTEG 535 kWp / 224 kWh.</p>"},
       "ai_technical_notes": [{"title": "Peak vs MRK", "detail": "Peak 620 kW exceeds MRK 500 kW."}],
       "ai_open_questions": [{"title": "Grid connection", "detail": "Confirm export limit with the DSO."}]}
CASES = {"porovnanie.html": (_A, {}), "porovnanie_jedna_volba.html": (_B, {}), "porovnanie_ai.html": (_A, _AI)}


def run(case, lang=None, ai=None, customer_lang=None, **render_kw):
    """render_porovnanie nad FakeSB; PDF a AI sú stuby. Vráti (výsledok, zachytené, sb)."""
    (a_kw, variants) = case
    sb = FakeSB()
    cust = {"company_name": _CLIENT}
    if customer_lang:
        cust["lang"] = customer_lang
    sb.db["analyza_om"] = [dict(a_kw, id="A1", customers=cust)]
    sb.db["analyza_om_variants"] = [dict(v, analyza_id="A1", position=i) for i, v in enumerate(variants)]
    seen = {}

    def pdf(ctx):
        from aom_i18n import get_lang
        seen["html"] = posudok_orkestra.render_porovnanie_html(ctx)
        seen["lang_pdf"] = get_lang()
        return b"%PDF"

    def fake_ai(inp):
        from aom_i18n import get_lang
        seen["ai_input"], seen["lang_ai"] = inp, get_lang()
        return dict(ai or {})
    orig = posudok_orkestra.generate_porovnanie_pdf, aom._generate_porovnanie_ai
    posudok_orkestra.generate_porovnanie_pdf, aom._generate_porovnanie_ai = pdf, fake_ai
    try:
        if lang:
            render_kw["lang"] = lang
        r = aom.render_porovnanie(sb, "A1", **render_kw)
    finally:
        posudok_orkestra.generate_porovnanie_pdf, aom._generate_porovnanie_ai = orig
    return r, seen, sb


_SVG = re.compile(r"<\?xml.*?</svg>", re.S)


def _svg_texts(m):
    """Matplotlib SVG (náhodné id, dátum v metadátach) → len jeho texty (komentáre pri glyfoch)."""
    return "<svg>" + " | ".join(re.findall(r"<!-- (.*?) -->", m.group(0))) + "</svg>"


def normalize(html):
    return normalize_html(_SVG.sub(_svg_texts, html))


def outputs(lang=None):
    return {name: normalize(run(case, lang=lang, ai=ai)[1]["html"]) for name, (case, ai) in CASES.items()}


def test_porovnanie_sk_golden():
    outs = outputs()
    bad = [n for n, got in outs.items() if (G / n).read_text(encoding="utf-8") != got]
    assert not bad, f"SK regresia: {bad} — python tests/test_aom_i18n_porovnanie.py --diff"


_FIXTURE_WORDS = {"Lukáš"}  # prepared_by_name — dáta, nie text šablóny


def test_porovnanie_en_de_bez_slovenciny():
    from aom_i18n_fixtures import slovak_leftovers
    for lang in ("en", "de"):
        for name, (case, ai) in CASES.items():
            r, seen, _sb = run(case, lang=lang, ai=ai)
            html = seen["html"]
            assert f'<html lang="{lang}">' in html, (lang, name)
            # grafy (posudok_orkestra/charts.py) majú popisky ako glyfy v SVG — mimo tejto kontroly
            left = [w for w in slovak_leftovers(_SVG.sub("", html)) if w not in _FIXTURE_WORDS]
            assert not left, (lang, name, left)
            assert seen["lang_pdf"] == lang and seen["lang_ai"] == lang
    assert "keine Amortisation" in run(_B, lang="de")[1]["html"]
    en = run(_A, lang="en")[1]["html"]
    assert "7.9 yrs" in en and "268,000 €" in en and "maximum reserved capacity (MRK)" in en


def test_porovnanie_de_zaznam_a_nazov_suboru():
    from aom_i18n import get_lang
    r, seen, sb = run(_A, lang="de")
    assert r["lang"] == "de" and "/Vergleich_" in r["pdf_url"] and "_DE_" in r["pdf_url"]
    assert [d.get("lang") for d in sb.db["analyza_om_documents"]] == ["de"]
    assert get_lang() == "sk"  # kontext sa po renderi vráti


def test_porovnanie_jazyk_zo_zakaznika_a_sk_default():
    r, seen, sb = run(_A, customer_lang="en")
    assert r["lang"] == "en" and "/Comparison_" in r["pdf_url"] and "_EN_" in r["pdf_url"]
    r, seen, sb = run(_A)
    assert r["lang"] == "sk" and "/Porovnanie_" in r["pdf_url"] and "_SK" not in r["pdf_url"]
    assert all("lang" not in d for d in sb.db["analyza_om_documents"])  # sk = DB default, payload bez zmeny
    assert seen["ai_input"]["ponuky"][0]["payback_y"] == 7.9


def _chart_texts(case, lang=None):
    """Texty matplotlib grafov porovnania (osi, legendy, popisky stĺpcov) — v SVG sú to glyfy, nie text."""
    import matplotlib.text
    from posudok_orkestra import charts as C
    seen, orig = [], C._save_svg

    def rec(fig):
        fig.canvas.draw()
        seen.append([t.get_text() for t in fig.findobj(matplotlib.text.Text) if t.get_text().strip()])
        return orig(fig)
    C._save_svg = rec
    try:
        run(case, lang=lang)
    finally:
        C._save_svg = orig
    return seen


def test_porovnanie_grafy_sk_bez_zmeny_en_de_bez_slovenciny():
    from aom_i18n_fixtures import slovak_leftovers
    capex_npv, payback = _chart_texts(_B)
    assert {"CAPEX (vstup)", "NPV 20 r (zisk)"} <= set(capex_npv)
    assert {"8.4 r", "9.1 r", "nevráti sa"} <= set(payback)  # SK: desatinná bodka ako predtým
    assert "Návratnosť (roky) — kratšia je lepšia" in payback
    for lang, pb in (("en", {"8.4 yrs", "9.1 yrs", "no payback", "CAPEX (investment)"}),
                     ("de", {"8,4 J.", "9,1 J.", "keine Amortisation", "CAPEX (Investition)"})):
        for case in (_A, _B):
            texts = _chart_texts(case, lang)
            assert len(texts) == 2
            assert not slovak_leftovers(" ".join(sum(texts, []))), (lang, texts)
        assert pb <= set(sum(_chart_texts(_B, lang), []))


def test_porovnanie_glosar_v_ai_prompte_len_pre_en_de(monkeypatch):
    calls = []

    class _Anth:
        def __init__(self, *a, **k):
            self.messages = types.SimpleNamespace(create=lambda **kw: calls.append(kw["system"]) or types.SimpleNamespace(
                content=[types.SimpleNamespace(type="tool_use", input={"executive_summary": "<p>x</p>"})]))
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Anth))
    from aom_i18n import use_lang
    assert aom._generate_porovnanie_ai({})["ai_executive_summary"] == "<p>x</p>"
    assert calls and "OUTPUT LANGUAGE" not in calls[-1]
    for lang, name in (("en", "English"), ("de", "German")):
        with use_lang(lang):
            aom._generate_porovnanie_ai({})
        assert name in calls[-1] and "Zelená podnikom" in calls[-1] and "MRK" in calls[-1]


if __name__ == "__main__":
    outs = outputs()
    if "--diff" in sys.argv:
        for name, got in outs.items():
            exp = (G / name).read_text(encoding="utf-8")
            sys.stdout.writelines(list(difflib.unified_diff(exp.splitlines(True), got.splitlines(True), name,
                                                            "aktuálne"))[:200])
    elif "--capture" in sys.argv:
        G.mkdir(parents=True, exist_ok=True)
        for name, got in outs.items():
            (G / name).write_text(got, encoding="utf-8")
        print("zapísané:", ", ".join(sorted(outs)))
    else:
        print(__doc__)


def test_porovnanie_ai_mimo_schemy_nepada():
    """AI vrátilo recommendation ako reťazec a hodnotenie ako reťazec → predtým 500 'str' has no attribute 'get'."""
    bad = dict(_AI, ai_recommendation="<p>Choose HUAWEI 535 kWp.</p>",
               ai_variant_assessments=["Best value.", {"id": "v2", "verdict": "Fast.", "pros": [], "cons": []}])
    for lang in (None, "de"):
        r, seen, _ = run(CASES["porovnanie.html"][0], lang=lang, ai=bad)
        assert r.get("ok"), r
        assert "Choose HUAWEI 535 kWp." in seen["html"]
