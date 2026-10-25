"""Vecné chyby posudku (všetky jazyky vrátane SK), nájdené na Optimetall 2026-10-25:
distribútor natvrdo VSD, surový kód zdroja tarify, záporná arbitráž medzi prínosmi,
diagram tokov bez batérie, prázdna sekcia 7 bez AI."""
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

from aom_i18n import use_lang  # noqa: E402
from aom_i18n_fixtures import _ctx, docx_text, fixture_analyza  # noqa: E402
from posudok_chocosuc.generator import _energy_flow_svg, render_chocosuc_html  # noqa: E402
from posudok_chocosuc.generator_docx import generate_chocosuc_docx  # noqa: E402


def _a(arb=None, **kw):
    a = fixture_analyza()
    if arb is not None:
        a["econ_results"]["full_response"]["variants"][0]["value_streams"]["arbitrage_eur"] = arb
    a.update(kw)
    return a


def _tarif_row(html, label):
    m = re.search(r"<td class=\"cl\">" + re.escape(label) + r"</td><td class=\"cr\">[^<]*</td><td class=\"cl\">([^<]*)</td>", html)
    assert m, label
    return m.group(1)


# 1) distribútor z analyza_om.om_distributor (predtým „VSD tarif" pre každé OM)
def test_distributor_z_analyzy():
    assert _tarif_row(render_chocosuc_html(_ctx(_a(om_distributor="SSE"))), "Variabilná distribúcia") == "tarif SSE"
    assert _tarif_row(render_chocosuc_html(_ctx(_a(om_distributor="zse"))), "Variabilná distribúcia") == "tarif ZSE"
    a = _a(om_distributor=None)
    a["econ_results"]["full_response"]["tariff_detail"]["distribuutor"] = "VSD"
    assert _tarif_row(render_chocosuc_html(_ctx(a)), "Variabilná distribúcia") == "tarif VSD"
    a["econ_results"]["full_response"]["tariff_detail"].pop("distribuutor")
    assert _tarif_row(render_chocosuc_html(_ctx(a)), "Variabilná distribúcia") == "tarif distribútora"
    assert "Variabilná distribúcia | 16,55 €/MWh | tarif SSE" in docx_text(generate_chocosuc_docx(_ctx(_a())))
    with use_lang("en"):
        assert "SSE tariff" in render_chocosuc_html(_ctx(_a()))


def test_distribucia_bez_tarifu_je_orientacna():
    ctx = _ctx(_a(tarif_distribucia_eur_mwh=None))
    assert ctx["dist_tarif_source"] == "orientačné (ÚRSO 2026)"


# 2) zdroj tarify: kód „mcp" (zápis cez CRM nástroj) čitateľne aj v SK
def test_zdroj_tarify_mcp_citatelne_v_sk():
    html = render_chocosuc_html(_ctx(_a(tarif_source="mcp")))
    assert _tarif_row(html, "Silová zložka") == "zadané v CRM"
    assert ">mcp<" not in html and " mcp " not in re.sub(r"<[^>]+>", " ", html)
    assert _ctx(_a(tarif_source="manual"))["tarif_source"] == "zadané ručne"
    assert _ctx(_a())["tarif_source"] == "faktúra"
    with use_lang("de"):
        assert _ctx(_a(tarif_source="mcp"))["tarif_source"] != "mcp"


# 3) záporná arbitráž nie je prínos — riadok nákladu, súčet sedí s ročnou úsporou
def test_zaporna_arbitraz_je_naklad():
    ctx = _ctx(_a(arb=-877.0))
    assert all(v >= 0 for _n, _f, v in ctx["benefit_rows"])
    assert [r[0] for r in ctx["benefit_cost_rows"]] == ["Batéria — nabíjanie zo siete (náklad)"]
    assert ctx["benefit_cost_rows"][0][2] == -877.0
    assert "fixnej tarife" in ctx["arbitrage_reason"] and "odpočítaný" in ctx["arbitrage_reason"]
    html = render_chocosuc_html(ctx)
    assert "BESS arbitráž (spot v BS)" not in html
    assert "Batéria — nabíjanie zo siete (náklad)" in html
    assert "⚡ Nabíjanie batérie zo siete: <b>-877 €</b> ročne" in html and "⚡ Arbitráž batérie" not in html
    d = docx_text(generate_chocosuc_docx(ctx))
    assert "Batéria — nabíjanie zo siete (náklad)" in d and "BESS arbitráž" not in d
    assert ctx["benefit_parts"][3][1] == 0  # graf prínosov: arbitráž sa nekreslí ako prínos
    spot = _ctx(_a(arb=-877.0, om_tarif_typ="spot"))
    assert "spotovej tarife" in spot["arbitrage_reason"]


def test_kladna_arbitraz_ostava_prinosom():
    ctx = _ctx(_a(arb=640.0, om_tarif_typ="spot"))
    assert ("BESS arbitráž (spot v BS)", "nabíjanie lacno / vybíjanie draho", 640.0) in ctx["benefit_rows"]
    assert ctx["benefit_cost_rows"] == []
    assert "⚡ Arbitráž batérie: <b>640 €</b>" in render_chocosuc_html(ctx)


# 4) diagram tokov: batéria má šípky z FVE, zo siete a do spotreby; Sieť→Spotreba = len import do odberu
def _labels(svg):
    return [(int(x), int(y), t) for x, y, t in re.findall(r'<text x="(\d+)" y="(\d+)" text-anchor="middle" font-size="13" font-weight="700" fill="#[0-9A-F]+">([^<]+)<', svg)]


def test_diagram_tokov_sedi_s_vysledkami():
    ctx = _ctx(_a())
    svg = _energy_flow_svg(ctx)
    lab = {(x, y): int(t) for x, y, t in _labels(svg)}
    assert lab[(350, 305)] == round(ctx["grid_to_load_mwh"])        # Sieť → Spotreba (bez nabíjania batérie)
    assert lab[(764, 263)] == round(ctx["pv_to_bat_mwh"])           # FVE → batéria
    assert lab[(470, 437)] == round(ctx["grid_to_bat_mwh"])         # sieť → batéria
    assert lab[(604, 357)] == round(ctx["bat_to_load_mwh"])         # batéria → spotreba
    assert svg.count("marker-end") == 6
    # bilancia výroby: priamo + do batérie + export + nevyužité = výroba
    assert abs(ctx["pv_to_load_mwh"] + ctx["pv_to_bat_mwh"] + ctx["export_mwh"] + ctx["loss_mwh"] - ctx["fve_prod_mwh"]) < 0.01
    assert "MWh · {} nevyužité".format(round(ctx["loss_mwh"])) in svg


def test_diagram_bez_baterie_bez_sipok_baterie():
    a = fixture_analyza()
    a["selected_variant_id"] = "V2"
    svg = _energy_flow_svg(_ctx(a))
    assert svg.count("marker-end") == 3 and "#5E8E2A\" stroke-width" not in svg


# 5) bez AI sekcia 7 nemá prázdne nadpisy (deterministický text aj kroky)
def test_sekcia7_bez_ai_ma_obsah():
    ctx = _ctx(_a())
    html = render_chocosuc_html(ctx)
    sec7 = html.split("7 — Expert posúdenie a odporúčania", 1)[1].split("<section", 1)[0]
    assert '<div class="narr"></div>' not in sec7 and "Z technického hľadiska" in sec7
    assert sec7.count('class="rec"') >= 3
    d = docx_text(generate_chocosuc_docx(ctx)).split("7 — EXPERT POSÚDENIE A ODPORÚČANIA", 1)[1].split("8 — ZÁVER", 1)[0]
    assert "Citlivosť:" in d and "01  Odporúčanie k navrhnutému variantu" in d
    for lang in ("en", "de"):
        with use_lang(lang):
            sec7 = render_chocosuc_html(_ctx(_a())).split('<div class="kick">7 — ', 1)[1].split("<section", 1)[0]
        assert '<div class="narr"></div>' not in sec7 and sec7.count('class="rec"') >= 3, lang
