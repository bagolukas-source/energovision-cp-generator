"""Regresné testy — audit AOM 2026-10-24, vlna 1b: reporty, štítky, lokalita.

Nálezy: A27/F26 (štítky top_picks), A33 (ID variantu), A29 (escapovanie HTML),
A26/V10/V11 (PSČ → distribútor / GPS). Každý test pred opravou padal, po oprave prechádza.
Spúšťanie: `python3 tests/test_aom_vlna1b_report.py` alebo `pytest tests/test_aom_vlna1b_report.py`.
"""
import os
import sys
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ENERGO_SPOT_CSV", os.path.join(ROOT, "aom_data", "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", os.path.join(ROOT, "aom_data", "tariffs", "2026.yaml"))

from energovision_analytics.data.auto_fill import psc_to_distribuutor, psc_to_gps  # noqa: E402
from energovision_analytics.variants.scorer import pick_top_variants  # noqa: E402

XSS = "<script>alert(1)</script> & Co"
XSS_ESC = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co"


# ---------- A27/F26: štítok = skutočný víťaz metriky, variant nie je v zozname dvakrát ----------
def _v(vid, npv, irr, pb, samosp, samostat, capex):
    return NS(variant_id=vid, npv_eur=npv, irr_pct=irr, payback_y=pb,
              samospotreba_pct=samosp, samostatnost_pct=samostat, capex_total_eur=capex)


# F26: max NPV aj max IRR (14,5 %) má PV300_BESS0, štítok IRR predtým dostal PV255
_VARIANTS = [
    _v("PV300_BESS0_rule_based", 120000, 14.5, 6.1, 70, 30, 210000),
    _v("PV255_BESS0_rule_based", 110000, 14.2, 6.2, 75, 27, 185000),
    _v("PV300_BESS500_rule_based", 90000, 9.7, 7.6, 90, 40, 370000),
]
_METRIC = {  # štítok → (kľúč, smer)
    "Najvyššie NPV": (lambda v: v.npv_eur, max),
    "Najvyššie IRR": (lambda v: v.irr_pct, max),
    "Najrýchlejšia návratnosť": (lambda v: v.payback_y, min),
    "Najvyššia samospotreba": (lambda v: v.samospotreba_pct, max),
    "Najvyššia samostatnosť": (lambda v: v.samostatnost_pct, max),
    "Najlacnejšia s NPV > 0": (lambda v: v.capex_total_eur, min),
}


def test_a27_stitok_zodpoveda_poradiu_metriky():
    picks = pick_top_variants(_VARIANTS)
    by_label = {lab: v for combo, v in picks for lab in combo.split(" · ")}
    assert set(by_label) == set(_METRIC), sorted(by_label)
    for lab, (key, best) in _METRIC.items():
        want = best(_VARIANTS, key=key).variant_id
        assert by_label[lab].variant_id == want, (lab, by_label[lab].variant_id, want)


def test_a27_variant_nie_je_pod_dvoma_polozkami():
    for variants in (_VARIANTS, _VARIANTS[:1]):  # 1 variant = predtým fallback 6× ten istý
        ids = [v.variant_id for _, v in pick_top_variants(variants)]
        assert len(ids) == len(set(ids)), ids


# ---------- A33: jedinečné ID aj pre desatinné kWp/kWh, celočíselné bez zmeny ----------
def test_a33_variant_id_desatinne_jedinecne():
    from energovision_analytics.variants.variant_id import make_variant_id
    assert make_variant_id(10.1, 100, "rule_based") != make_variant_id(10.2, 100, "rule_based")
    assert make_variant_id(10, 100.5, "rule_based") != make_variant_id(10, 100.4, "rule_based")
    assert make_variant_id(10.1, 0, "rule_based") == "PV10.1_BESS0_rule_based"


def test_a33_variant_id_celociselne_identicke():
    from energovision_analytics.variants.variant_id import make_variant_id
    for pv, bess in ((300, 0), (300.0, 800.0), (0, 500), (255, 1000)):
        assert make_variant_id(pv, bess, "rule_based") == f"PV{pv:.0f}_BESS{bess:.0f}_rule_based"
    assert make_variant_id(300, 0, "rule_based") == "PV300_BESS0_rule_based"


# ---------- A29: text klienta/scenára vo verejnom HTML je escapovaný ----------
def test_a29_web_posudok_escapuje_klienta_a_scenar():
    from posudok_web import build_web_posudok_html
    html = build_web_posudok_html({"scenarios": [{"name": XSS, "npv_eur": 1}]}, {"client_name": XSS})
    assert "<script>alert(1)" not in html
    assert f"<h1>{XSS_ESC}</h1>" in html
    assert f"<tr><td>{XSS_ESC}</td>" in html


def test_a29_html_dashboard_escapuje_klienta_a_scenar():
    from energovision_analytics.reporting.html_dashboard import HTMLDashboard
    d = HTMLDashboard.__new__(HTMLDashboard)
    d.client_name, d.scenario_name = XSS, XSS
    d.financial = NS(discount_rate=0.06, horizon_years=20)
    d.site = NS(distribuutor=NS(value="ZSD"), sadzba=NS(value="NN"), typ_tarify=NS(value="fix"),
                rk_kw=100, mrk_kw=100, rocna_spotreba_kwh=1000)
    d.pv, d.bess = None, NS(nominal_kwh=100, power_kw_ac=50, vyrobca=NS(value="Huawei"), typ=XSS, rte_ac_ac=0.9)
    html = d._template("", "") + d._card_site_info() + d._card_solution_info()
    assert "<script>alert(1)" not in html
    assert f"<title>{XSS_ESC} — Posudok" in html and f"<h1>{XSS_ESC}</h1>" in html
    assert f"<span>{XSS_ESC}</span>" in html
    assert f"<dd>{XSS_ESC}</dd>" in html and f"Huawei {XSS_ESC}</dd>" in html


# ---------- A26/V10/V11: PSČ → distribútor a GPS (tabuľka 01-vstupy/t02_config_inputs.py) ----------
_T02 = [("010 01", "SSE", "Žilina"), ("040 01", "VSD", "Košice"), ("080 01", "VSD", "Prešov"),
        ("085 01", "VSD", "Bardejov"), ("089 01", "VSD", "Svidník"), ("091 01", "VSD", "Stropkov"),
        ("093 01", "VSD", "Vranov nad Topľou"), ("066 01", "VSD", "Humenné"), ("811 01", "ZSD", "Bratislava"),
        ("911 01", "ZSD", "Trenčín"), ("917 01", "ZSD", "Trnava"), ("902 01", "ZSD", "Pezinok"),
        ("925 21", "ZSD", "Galanta"), ("949 01", "ZSD", "Nitra"),
        ("971 01", "SSE", "Prievidza"), ("974 01", "SSE", "Banská Bystrica"), ("979 01", "SSE", "Rimavská Sobota")]


def test_a26_v10_psc_distributor():
    zle = [(p, psc_to_distribuutor(p).value, d) for p, d, _ in _T02 if psc_to_distribuutor(p).value != d]
    assert not zle, zle


def test_a26_v10_psc_088_099_vsd():
    for p in ("08801", "08901", "09001", "09101", "09301", "09431", "09999"):
        assert psc_to_distribuutor(p).value == "VSD", p


def test_a26_v11_psc_gps_najspecifickejsi_rozsah():
    zle = [(p, psc_to_gps(p)[2], c) for p, _, c in _T02 if psc_to_gps(p)[2] != c]
    assert not zle, zle


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
