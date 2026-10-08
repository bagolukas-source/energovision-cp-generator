"""Testy výpočtového jadra B2B kalkulačky (Fáza 0, 2026-10) — stdlib unittest, bez inštalácie, bez siete.

Spustenie z koreňa repa:
    python3 -m unittest discover -s tests -p 'test_b2b*.py' -v

DB nahrádza FakeSB (in-memory tabuľky b2b_calc_rules a b2b_vendor_stacks). Pravidlá = cieľové pravidlá
podľa F0-SPEC (nákup/predaj z Raynet cenníka), stacky = tests/fixtures/b2b_vendor_stacks.json
(Supabase b2b_vendor_stacks 2026-10-08 + doplnené Raynet nákupy, viď _meta v súbore).
"""
import ast
import copy
import json
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import b2b_calculator_v2 as eng  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "b2b_vendor_stacks.json")


# ----------------------------------------------------------------------------------------------
# FakeSB — minimálne supabase-py API používané jadrom: table().select().eq().order().limit().execute()
# ----------------------------------------------------------------------------------------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self._eq = []
        self._order = []
        self._limit = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def order(self, col, desc=False, **_k):
        self._order.append((col, desc))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = [copy.deepcopy(r) for r in self._rows if all(r.get(c) == v for c, v in self._eq)]
        for col, desc in reversed(self._order):
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col) or 0), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Resp(rows)


class FakeSB:
    def __init__(self, rules, stacks):
        self.tables = {"b2b_calc_rules": rules, "b2b_vendor_stacks": stacks}

    def table(self, name):
        return _Query(self.tables[name])


# ----------------------------------------------------------------------------------------------
# Fixture: pravidlá podľa F0-SPEC (cieľový stav b2b_calc_rules)
# ----------------------------------------------------------------------------------------------
def R(rule_type, rule_key, name, unit, cost, price, qty="1", lo=None, hi=None, typ=None, active=True, priority=100):
    return {"id": f"{rule_type}/{rule_key}", "rule_type": rule_type, "rule_key": rule_key, "min_kwp": lo, "max_kwp": hi,
            "typ_strechy": typ, "product_name": name, "unit": unit, "cost_per_unit": cost, "price_per_unit": price,
            "qty_formula": qty, "priority": priority, "active": active}


def _bands(rule_type, label, unit, keys, costs, prices, his):
    out, lo = [], 0
    for k, c, p, hi in zip(keys, costs, prices, his):
        out.append(R(rule_type, k, f"{label} {k}", unit, c, p, "1", lo, hi))
        lo = hi + 0.01
    return out


def f0_rules():
    rules = [
        R("vodice", "dc", "Vodiče DC", "kWp", 15.38, 20.00, "kwp"),
        R("vodice", "ac", "Vodiče AC", "kWp", 20.00, 24.00, "kwp"),
        R("spotrebny", "standard", "Spotrebný materiál", "kWp", 24.00, 31.20, "kwp"),
        R("ostatne", "kablove_zlaby", "Káblové žľaby", "kWp", 14.00, 16.80, "kwp"),
        # staré pravidlá žľabov: v cieľovom stave neaktívne
        R("ostatne", "zlab_kryt_50mm", "Žľab + kryt 50 mm", "ks", 12, 18.33, "ceil(kwp * 1.5)", active=False),
        R("ostatne", "chranicka_25mm", "Chránička 25 mm", "ks", None, 0.77, "kwp * 10", active=False),
        R("ostatne", "chranicka_40mm", "Chránička 40 mm", "ks", None, 1.20, "kwp * 7", active=False),
        R("rozvadzac_dc", "r_dc", "Rozvádzač DC", "kWp", 30.00, 38.00, "kwp"),
        R("mtp", "mtp3", "Merací transformátor prúdu", "ks", 80, 104, "3"),
        R("zatiaz", "vz", "Záťaž konštrukcie (V-Z)", "kWp", 10, 13, "kwp"),
        R("statika", "statika", "Statický posudok", "kpl", 500, 700, "1"),
        R("statika", "ppbs", "Projekt požiarnej bezpečnosti", "kpl", 350, 500, "1"),
        R("dispecing", "ZSD", "Dispečerské riadenie ZSDIS", "kpl", 28000, 36400, "1"),
        R("dispecing", "SSD", "Dispečerské riadenie SSD", "kpl", 20000, 26000, "1"),
        R("dispecing", "VSD", "Dispečerské riadenie VSD", "kpl", 21000, 27300, "1"),
        R("batteria", "montaz_baterie", "Montáž batériového úložiska", "ks", 1750, 2000, "1"),
        R("batteria", "montaz_baterie_rez", "Montáž batérie (rezidenčná sada)", "kpl", 300, 500, "1"),
        R("doprava", "km", "Doprava materiálu", "km", 0.80, 1.04, "vzdialenost_doprava"),
        # rapid shutdown: bez nákupu v DB → odhad (testuje cost_estimated)
        R("rapid_shutdown", "bfs12", "Rapid Shutdown BFS-12", "ks", None, 50.7, "ceil(pocet_panelov / 4)"),
        R("rapid_shutdown", "esw12", "Rapid Shutdown ESW-12", "ks", None, 44.2, "ceil(pocet_panelov / 200)"),
        R("rapid_shutdown", "montaz_rs", "Montáž Rapid Shutdown", "ks", None, 3.90, "ceil(pocet_panelov / 4)"),
    ]
    # Rozvádzač AC R10…R1000 — pásma podľa AC kW (nad 1 000: ceil(AC/1000) × R1000)
    rules += _bands("rozvadzac", "Rozvádzač AC", "ks",
                    ["R10", "R20", "R30", "R50", "R100", "R200", "R250", "R500", "R1000"],
                    [600, 800, 1200, 2500, 4000, 6000, 8000, 12500, 25000],
                    [780, 1040, 1560, 3250, 5200, 7800, 10400, 16250, 32500],
                    [10, 20, 30, 50, 100, 200, 250, 500, 1000])
    # PD10…PD2000 — pásma podľa AC kW
    rules += _bands("pd", "Projektová dokumentácia", "kpl",
                    ["PD10", "PD20", "PD30", "PD50", "PD100", "PD200", "PD500", "PD1000", "PD2000"],
                    [600, 800, 1200, 1800, 3040, 4080, 4400, 4680, 5440],
                    [780, 1040, 1560, 2340, 3952, 5304, 5720, 6084, 7072],
                    [10, 20, 30, 50, 100, 200, 500, 1000, 2000])
    # Montáž FVE — 5 pásiem podľa DC kWp (ako doteraz)
    for key, lo, hi in [("do_30", 0, 30), ("do_100", 30.01, 100), ("do_250", 100.01, 250),
                        ("do_500", 250.01, 500), ("nad_500", 500.01, 5000)]:
        rules.append(R("montaz", key, "Montáž", "kWp", 70, 100, "kwp", lo, hi))
    # Konštrukcia na kWp
    for typ, name, cost, price in [("trapez", "Trapéz", 34, 45), ("vychod_zapad", "V-Z", 80, 96),
                                   ("zemne_skrutky", "Zemné skrutky", 130, 156), ("skridla", "Škridla", 81, 105),
                                   ("falcovany_plech", "Falcovaný plech", 45, 54),
                                   ("plech_kombi_skrutka", "Plech kombi skrutka", 62, 81), ("juzna", "Južná", 132, 158)]:
        rules.append(R("konstrukcia", typ, f"Konštrukcia - {name}", "kWp", cost, price, "kwp", typ=typ))
    return rules


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_stacks():
    return copy.deepcopy(json.loads(read_text(FIXTURE))["stacks"])


def synthetic_stack(ac_kw, hybrid=False):
    """Stack s jediným modelom meničov — deterministický výber (1 kus pre kWp ≈ 1,2 × AC)."""
    return {
        "vendor_key": "synt", "display_name": "Synt", "raynet_share_pct": 0,
        "preferred_panels": [{"sku": "LONGI535", "name": "LONGi 535 Wp", "wp": 535, "cost": 80, "price_per_unit": 100}],
        "inverters": [{"key": "inv", "name": f"INV {ac_kw:g} kW", "ac_kw": ac_kw, "price": ac_kw * 50, "cost": ac_kw * 40,
                       "max_kwp": ac_kw * 1.5, "min_kwp": 0, "hybrid": hybrid}],
        "smart_manager": None, "smart_manager_large": None, "smart_meter": None,
        "optimizers": [], "batteries": [], "wallboxes": [], "warnings": [], "accessories": [],
    }


BASE = {"typ_strechy": "trapez", "vendor_stack": "solinteg", "panel_sku": "LONGI535", "pocet_panelov": 112,
        "has_bess": False, "bess_kwh": 0, "bess_count": 0, "bess_class": "", "bess_sku": "",
        "has_wallbox": False, "wallbox_pocet": 0, "has_optimizery": False, "has_rapid_shutdown": False,
        "has_dc_rozvadzac": True, "vzdialenost_doprava": 200, "margin_pct": 22}


def calc(overrides=None, rules=None, stacks=None, drop=()):
    cfg = dict(BASE)
    cfg.update(overrides or {})
    for k in drop:
        cfg.pop(k, None)
    sb = FakeSB(rules if rules is not None else f0_rules(), stacks if stacks is not None else load_stacks())
    return eng.calculate_bom_v2(sb, cfg)


def calc_ac(ac_kw, overrides=None, rules=None, hybrid=False):
    """Zostava s presne jedným meničom o výkone ac_kw (kWp ≈ 1,2 × AC)."""
    panels = math.ceil(ac_kw * 1.2 * 1000 / 535)
    cfg = {"vendor_stack": "synt", "pocet_panelov": panels}
    cfg.update(overrides or {})
    return calc(cfg, rules=rules, stacks=[synthetic_stack(ac_kw, hybrid)])


def by_rule(res, prefix):
    return [i for i in res["items"] if i["rule_id"].startswith(prefix)]


def one(res, rule_id):
    found = [i for i in res["items"] if i["rule_id"] == rule_id]
    assert len(found) == 1, f"{rule_id}: nájdených {len(found)} z {[i['rule_id'] for i in res['items']]}"
    return found[0]


def kinds(res):
    return [w["kind"] for w in res["warnings"]]


def cats(res):
    return {i["category"] for i in res["items"]}


# ----------------------------------------------------------------------------------------------
class TestMarza(unittest.TestCase):
    def test_vzorec_ceny_pre_vsetky_polozky(self):
        for m in (22, 25.2, 4, 0, 33.33):
            res = calc({"margin_pct": m, "has_bess": True, "bess_class": "industrial", "bess_sku": "solinteg_e2br_112r",
                        "bess_count": 1, "has_optimizery": True})
            self.assertTrue(res["ok"], res)
            for it in res["items"]:
                self.assertEqual(it["price_per_unit"], round(it["cost_per_unit"] / (1 - m / 100), 2), (m, it["rule_id"]))
                self.assertEqual(it["total_cost"], round(it["cost_per_unit"] * it["qty"], 2))
                self.assertEqual(it["total_price"], round(it["price_per_unit"] * it["qty"], 2))
                self.assertNotIn("price_locked", it)

    def test_default_22_a_totals(self):
        res = calc(drop=("margin_pct",))
        t = res["totals"]
        self.assertEqual(t["margin_pct_input"], 22.0)
        self.assertEqual(t["total_price"], round(sum(i["total_price"] for i in res["items"]), 2))
        self.assertEqual(t["total_cost"], round(sum(i["total_cost"] for i in res["items"]), 2))
        self.assertEqual(t["total_margin_eur"], round(t["total_price"] - t["total_cost"], 2))
        # efektívna marža z PREDAJA ≈ zadaná (zaokrúhľovanie jednotkových cien)
        self.assertAlmostEqual(t["margin_pct_effective"], 22.0, delta=0.1)
        self.assertEqual(t["items_count"], len(res["items"]))

    def test_marza_z_predaja_nie_priraz(self):
        # nákup 100 → predaj 100 / (1 - 0,22) = 128,21 (priraz 22 % by dala 122)
        res = calc()
        km = one(res, "doprava.km")
        self.assertEqual(km["price_per_unit"], round(0.80 / 0.78, 2))

    def test_marza_nula_predaj_rovna_nakup(self):
        res = calc({"margin_pct": 0})
        self.assertTrue(res["ok"])
        for it in res["items"]:
            self.assertEqual(it["price_per_unit"], round(it["cost_per_unit"], 2))
        self.assertEqual(res["totals"]["margin_pct_effective"], 0)

    def test_nizka_marza_sa_neblokuje(self):
        res = calc({"margin_pct": 4})
        self.assertTrue(res["ok"])
        self.assertEqual(res["totals"]["margin_pct_input"], 4.0)
        self.assertAlmostEqual(res["totals"]["margin_pct_effective"], 4.0, delta=0.1)

    def test_neplatna_marza_je_chyba(self):
        for bad in (-0.01, -5, 100, 150, "abc", float("nan")):
            res = calc({"margin_pct": bad})
            self.assertFalse(res["ok"], bad)
            self.assertTrue(res["error"])
            self.assertEqual(res["warnings"][0]["severity"], "error")

    def test_marza_ako_text_s_ciarkou(self):
        res = calc({"margin_pct": "25,2"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["totals"]["margin_pct_input"], 25.2)


class TestNakup(unittest.TestCase):
    def test_nakup_z_db_pravidiel(self):
        res = calc()
        kon = one(res, "konstrukcia.trapez")
        self.assertEqual(kon["cost_per_unit"], 34.0)
        self.assertEqual(kon["unit"], "kWp")
        self.assertEqual(one(res, "vodice.dc")["cost_per_unit"], 15.38)
        self.assertEqual(one(res, "montaz.do_100")["cost_per_unit"], 70.0)
        self.assertEqual(one(res, "panel.LONGI535")["cost_per_unit"], 80.0)

    def test_bez_odhadu_ked_su_vsetky_nakupy(self):
        res = calc({"has_optimizery": True})
        self.assertNotIn("cost_estimated", kinds(res))

    def test_chybajuci_nakup_je_odhad_077_s_varovanim(self):
        # Huawei SUN2000-50KTL-M3 nemá v stacku cost → price × 0,77
        res = calc({"vendor_stack": "huawei", "pocet_panelov": 112})
        inv = [i for i in res["items"] if i["category"] == "Striedače"][0]
        self.assertEqual(inv["sku"], "sun2000_50ktl")
        self.assertEqual(inv["cost_per_unit"], round(5200 * 0.77, 4))
        w = [w for w in res["warnings"] if w["kind"] == "cost_estimated"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "warning")
        self.assertIn("SUN2000-50KTL-M3", w[0]["message"])
        self.assertNotIn("Konštrukcia", w[0]["message"])   # položky s reálnym nákupom v zozname nie sú

    def test_rapid_shutdown_bez_nakupu_je_odhad(self):
        res = calc({"has_rapid_shutdown": True})
        w = [w for w in res["warnings"] if w["kind"] == "cost_estimated"][0]
        self.assertIn("Rapid Shutdown BFS-12", w["message"])
        rs = one(res, "rapid_shutdown.bfs12")
        self.assertEqual(rs["qty"], 28)   # ceil(112 / 4)
        self.assertEqual(rs["cost_per_unit"], round(50.7 * 0.77, 4))


class TestWarningsContract(unittest.TestCase):
    def test_ziadny_dvojity_kluc_v_dict_literali(self):
        src = read_text(os.path.join(ROOT, "b2b_calculator_v2.py"))
        dups = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Dict):
                seen = set()
                for k in node.keys:
                    if isinstance(k, ast.Constant):
                        if k.value in seen:
                            dups.append((node.lineno, k.value))
                        seen.add(k.value)
        self.assertEqual(dups, [])

    def test_jedno_pole_warnings_so_strukturou(self):
        res = calc({"vendor_stack": "sungrow", "has_bess": True, "bess_count": 1, "has_optimizery": True,
                    "typ_strechy": "corab", "vzdialenost_doprava": 0})
        self.assertIsInstance(res["warnings"], list)
        self.assertTrue(res["warnings"])
        for w in res["warnings"]:
            self.assertIn(w["severity"], ("info", "warning", "error"))
            self.assertTrue(w["kind"])
            self.assertIsInstance(w["message"], str)
        # varovania z batérie aj konštrukcie sú v jednom zozname (predtým sa stratili)
        self.assertIn("no_hybrid", kinds(res))
        self.assertIn("konstrukcia_missing", kinds(res))
        self.assertIn("doprava_km", kinds(res))

    def test_sungrow_s_baterkou_bez_hybridu_je_varovanie(self):
        res = calc({"vendor_stack": "sungrow", "pocet_panelov": 112, "has_bess": True, "bess_count": 2,
                    "bess_sku": "sungrow_sbh100"})
        w = [w for w in res["warnings"] if w["kind"] == "no_hybrid"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "warning")

    def test_hybrid_bez_varovania(self):
        res = calc({"has_bess": True, "bess_count": 2, "bess_class": "residential"})
        self.assertNotIn("no_hybrid", kinds(res))
        # Solinteg vyberá hybridné MHT
        self.assertIn("solinteg_mht", [i for i in res["items"] if i["category"] == "Striedače"][0]["sku"])


class TestLoadRule(unittest.TestCase):
    def test_load_rule_len_aktivne_a_zoradene(self):
        rules = [
            R("pd", "b", "B", "kpl", 1, 2, lo=20, hi=30, priority=200),
            R("pd", "a", "A", "kpl", 1, 2, lo=10, hi=20, priority=100),
            R("pd", "off", "OFF", "kpl", 1, 2, lo=0, hi=10, active=False, priority=1),
            R("pd", "c", "C", "kpl", 1, 2, lo=0, hi=10, priority=100),
        ]
        sb = FakeSB(rules, [])
        got = [r["rule_key"] for r in eng._load_rule(sb, "pd")]
        self.assertEqual(got, ["c", "a", "b"])   # priority, potom min_kwp; neaktívne preč
        self.assertEqual([r["rule_key"] for r in eng._load_rule(sb, "pd", "a")], ["a"])

    def test_load_konstrukcia_len_aktivne(self):
        rules = [R("konstrukcia", "t1", "T1", "kWp", 1, 2, typ="trapez", priority=200),
                 R("konstrukcia", "t0", "T0", "kWp", 1, 2, typ="trapez", priority=100),
                 R("konstrukcia", "tx", "TX", "kWp", 1, 2, typ="trapez", active=False),
                 R("konstrukcia", "s", "S", "kWp", 1, 2, typ="skridla")]
        sb = FakeSB(rules, [])
        self.assertEqual([r["rule_key"] for r in eng._load_konstrukcia_rule(sb, "trapez")], ["t0", "t1"])

    def test_neaktivne_pravidlo_sa_nepouzije_v_kalkulacii(self):
        rules = f0_rules()
        for r in rules:
            if r["rule_key"] == "trapez":
                r["active"] = False
        res = calc(rules=rules)
        self.assertEqual(by_rule(res, "konstrukcia."), [])
        self.assertIn("konstrukcia_missing", kinds(res))


class TestAcVykon(unittest.TestCase):
    def test_ac_kw_total_a_polozky_menicov(self):
        res = calc({"vendor_stack": "sungrow", "pocet_panelov": 468})   # 250,38 kWp
        invs = [i for i in res["items"] if i["category"] == "Striedače"]
        self.assertTrue(invs)
        for i in invs:
            self.assertTrue(i["sku"])
            self.assertGreater(i["ac_kw"], 0)
        self.assertEqual(res["totals"]["ac_kw_total"], round(sum(i["ac_kw"] * i["qty"] for i in invs), 2))
        self.assertEqual(res["totals"]["pocet_menicov"], sum(i["qty"] for i in invs))

    def test_pasma_rozvadzaca_a_pd_podla_ac(self):
        # (AC kW, rozvádzač, PD)
        cases = [(8, "R10", "PD10"), (10, "R10", "PD10"), (15, "R20", "PD20"), (25, "R30", "PD30"),
                 (30, "R30", "PD30"), (33, "R50", "PD50"), (50, "R50", "PD50"), (60, "R100", "PD100"),
                 (100, "R100", "PD100"), (110, "R200", "PD200"), (125, "R200", "PD200"),
                 (220, "R250", "PD500"), (300, "R500", "PD500"), (600, "R1000", "PD1000")]
        for ac, r_key, pd_key in cases:
            with self.subTest(ac=ac):
                res = calc_ac(ac)
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["totals"]["ac_kw_total"], ac)
                self.assertEqual(len(by_rule(res, "rozvadzac.")), 1)
                self.assertEqual(by_rule(res, "rozvadzac.")[0]["rule_id"], f"rozvadzac.{r_key}")
                self.assertEqual(by_rule(res, "pd.")[0]["rule_id"], f"pd.{pd_key}")
                self.assertNotIn("rozvadzac_over_range", kinds(res))
                self.assertNotIn("missing_rule", kinds(res))

    def test_pasmo_podla_ac_nie_dc(self):
        # 59,92 kWp DC, jeden 50 kW menič → R50/PD50 (podľa DC 59,92 by to bolo R100/PD100)
        res = calc()
        self.assertEqual(res["totals"]["ac_kw_total"], 50)
        self.assertIn("rozvadzac.R50", [i["rule_id"] for i in res["items"]])
        self.assertIn("pd.PD50", [i["rule_id"] for i in res["items"]])

    def test_rozvadzac_nad_najvyssim_pasmom(self):
        res = calc_ac(125, {"pocet_panelov": math.ceil(1800 * 1000 / 535)})   # ~1 800 kWp → fallback n × 125 kW
        ac = res["totals"]["ac_kw_total"]
        self.assertGreater(ac, 1000)
        rozv = by_rule(res, "rozvadzac.")
        self.assertEqual(len(rozv), 1)
        self.assertEqual(rozv[0]["rule_id"], "rozvadzac.R1000")
        self.assertEqual(rozv[0]["qty"], math.ceil(ac / 1000))
        w = [w for w in res["warnings"] if w["kind"] == "rozvadzac_over_range"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "warning")

    def test_pd_nad_najvyssim_pasmom(self):
        res = calc_ac(125, {"pocet_panelov": math.ceil(3300 * 1000 / 535)})   # ~3 300 kWp
        self.assertGreater(res["totals"]["ac_kw_total"], 2000)
        self.assertEqual(one(res, "pd.PD2000")["qty"], 1)
        self.assertIn("pd_over_range", kinds(res))

    def test_nad_2000_kwp_ma_rozvadzac_aj_pd(self):
        res = calc({"vendor_stack": "huawei", "pocet_panelov": math.ceil(2100 * 1000 / 535), "typ_strechy": "zemne_skrutky"})
        self.assertGreater(res["totals"]["kwp"], 2000)
        self.assertEqual(len(by_rule(res, "rozvadzac.")), 1)
        self.assertEqual(len(by_rule(res, "pd.")), 1)
        self.assertIn("rozvadzac_over_range", kinds(res))
        self.assertIn("out_of_scope", kinds(res))   # > 500 kWp + zemné: len varovanie, výpočet prebehne
        self.assertTrue(res["ok"])

    def test_bez_pravidiel_je_varovanie_nie_ticho(self):
        rules = [r for r in f0_rules() if r["rule_type"] not in ("rozvadzac", "pd")]
        res = calc(rules=rules)
        self.assertTrue(res["ok"])
        msgs = " ".join(w["message"] for w in res["warnings"] if w["kind"] == "missing_rule")
        self.assertIn("Rozvádzač AC", msgs)
        self.assertIn("Projektová dokumentácia", msgs)


class TestDispecing(unittest.TestCase):
    def test_pod_100_kw_bez_dispecingu(self):
        res = calc_ac(99)
        self.assertEqual(by_rule(res, "dispecing."), [])
        self.assertFalse(res["totals"]["requires_asdr"])
        self.assertNotIn("distribucka", kinds(res))

    def test_od_100_kw_dispecing_podla_distribucky(self):
        for dist, rid in (("ZSD", "dispecing.ZSD"), ("SSD", "dispecing.SSD"), ("VSD", "dispecing.VSD"), ("ssd", "dispecing.SSD")):
            with self.subTest(dist=dist):
                res = calc_ac(100, {"distribucka": dist})
                self.assertTrue(res["totals"]["requires_asdr"])
                it = one(res, rid)
                self.assertEqual(it["category"], "Dispečerské riadenie")
                self.assertNotIn("distribucka", kinds(res))

    def test_bez_distribucky_zsd_a_varovanie(self):
        for dist in (None, "", "XYZ"):
            res = calc_ac(110, {"distribucka": dist})
            self.assertEqual(len(by_rule(res, "dispecing.")), 1)
            self.assertEqual(by_rule(res, "dispecing.")[0]["rule_id"], "dispecing.ZSD")
            self.assertIn("distribucka", kinds(res))

    def test_ceny_dispecingu(self):
        res = calc_ac(125, {"distribucka": "SSD", "margin_pct": 25})
        it = one(res, "dispecing.SSD")
        self.assertEqual(it["cost_per_unit"], 20000)
        self.assertEqual(it["price_per_unit"], round(20000 / 0.75, 2))


class TestNoveRiadky(unittest.TestCase):
    def test_rozvadzac_dc_default_a_vypnutie(self):
        res = calc()
        it = one(res, "rozvadzac_dc")
        self.assertEqual(it["category"], "Rozvádzač")
        self.assertEqual(it["qty"], res["totals"]["kwp"])
        self.assertEqual(it["cost_per_unit"], 30.0)
        res2 = calc({"has_dc_rozvadzac": False})
        self.assertEqual(by_rule(res2, "rozvadzac_dc"), [])
        self.assertNotIn("missing_rule", kinds(res2))

    def test_mtp_nad_30_kw_ac(self):
        self.assertEqual(by_rule(calc_ac(30), "mtp"), [])
        res = calc_ac(33)
        mtp = one(res, "mtp")
        self.assertEqual((mtp["qty"], mtp["category"], mtp["cost_per_unit"]), (3, "Meranie", 80.0))

    def test_zatiaz_len_pri_vychod_zapad(self):
        res = calc({"typ_strechy": "vychod_zapad"})
        z = one(res, "zatiaz.vz")
        self.assertEqual((z["qty"], z["cost_per_unit"]), (res["totals"]["kwp"], 10.0))
        self.assertIn("konstrukcia.vychod_zapad", [i["rule_id"] for i in res["items"]])
        self.assertEqual(by_rule(calc({"typ_strechy": "trapez"}), "zatiaz"), [])

    def test_statika_a_ppbs_od_100_kwp(self):
        res = calc_ac(50, {"pocet_panelov": 186})   # 99,51 kWp
        self.assertLess(res["totals"]["kwp"], 100)
        self.assertEqual(by_rule(res, "statika") + by_rule(res, "ppbs"), [])
        res = calc_ac(50, {"pocet_panelov": 187})   # 100,05 kWp
        self.assertGreaterEqual(res["totals"]["kwp"], 100)
        st, pb = one(res, "statika"), one(res, "ppbs")
        self.assertEqual((st["category"], pb["category"]), ("Statika a PBS", "Statika a PBS"))
        self.assertEqual((st["qty"], pb["qty"]), (1, 1))

    def test_kablove_zlaby_na_kwp(self):
        res = calc()
        z = one(res, "kablove_zlaby")
        self.assertEqual((z["category"], z["unit"], z["qty"]), ("Káblové žľaby", "kWp", res["totals"]["kwp"]))
        self.assertEqual(by_rule(res, "ostatne."), [])

    def test_kategorie_podla_spec(self):
        res = calc({"vendor_stack": "huawei", "typ_strechy": "vychod_zapad", "has_optimizery": True,
                    "has_rapid_shutdown": True, "has_bess": True, "bess_count": 2, "has_wallbox": True,
                    "wallbox_pocet": 1, "pocet_panelov": 112})
        allowed = {"Panely", "Striedače", "Monitoring", "Diagnostika siete", "Konštrukcia", "Rozvádzač", "Vodiče",
                   "Káblové žľaby", "Spotrebný materiál", "Projektová dokumentácia", "Statika a PBS", "Meranie",
                   "Dispečerské riadenie", "Optimizéry", "Rapid Shutdown", "Batéria", "Wallbox", "Montáž", "Doprava"}
        self.assertLessEqual(cats(res), allowed)
        self.assertIn("Spotrebný materiál", cats(res))
        # rule_id sú unikátne (stabilný kľúč riadku)
        ids = [i["rule_id"] for i in res["items"]]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_stare_data_bez_novych_pravidiel_dava_varovania_a_pokracuje(self):
        # dáta pred F0: ani rozvadzac_dc, mtp, zatiaz, statika, dispecing, montaz_baterie_rez, kablove_zlaby
        new_types = {"rozvadzac_dc", "mtp", "zatiaz", "statika", "dispecing"}
        rules = [r for r in f0_rules() if r["rule_type"] not in new_types and r["rule_key"] not in ("kablove_zlaby", "montaz_baterie_rez")]
        for r in rules:
            if r["rule_key"] in ("zlab_kryt_50mm", "chranicka_25mm", "chranicka_40mm"):
                r["active"] = True   # staré pravidlá žľabov ešte aktívne
        res = calc_ac(125, {"typ_strechy": "vychod_zapad", "pocet_panelov": 400, "has_bess": True, "bess_count": 2,
                            "bess_class": "residential"}, rules=rules, hybrid=True)
        self.assertTrue(res["ok"])
        missing = " ".join(w["message"] for w in res["warnings"] if w["kind"] == "missing_rule")
        for what in ("rozvadzac_dc", "mtp", "zatiaz", "statika", "dispecing"):
            self.assertIn(what, missing)
        self.assertEqual(by_rule(res, "kablove_zlaby"), [])
        self.assertEqual(len(by_rule(res, "ostatne.")), 3)   # legacy žľab + 2 chráničky
        # synt stack nemá batérie → bess_unavailable (nie ticho)
        self.assertIn("bess_unavailable", kinds(res))


class TestKonstrukcia(unittest.TestCase):
    def test_konstrukcia_na_kwp_presne(self):
        res = calc()
        k = one(res, "konstrukcia.trapez")
        self.assertEqual(k["unit"], "kWp")
        self.assertEqual(k["qty"], 59.92)   # nie ceil → 60
        self.assertEqual(k["category"], "Konštrukcia")

    def test_corab_bez_pravidla_je_varovanie(self):
        res = calc({"typ_strechy": "corab"})
        self.assertTrue(res["ok"])
        self.assertEqual(by_rule(res, "konstrukcia."), [])
        w = [w for w in res["warnings"] if w["kind"] == "konstrukcia_missing"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "warning")
        self.assertIn("corab", w[0]["message"])

    def test_typ_strechy_null_je_varovanie(self):
        res = calc({"typ_strechy": None})
        self.assertIn("konstrukcia_missing", kinds(res))

    def test_stara_konstrukcia_na_kusy_sa_respektuje(self):
        rules = f0_rules()
        for r in rules:
            if r["rule_type"] == "konstrukcia" and r["rule_key"] == "trapez":
                r.update(unit="ks", qty_formula="pocet_panelov", cost_per_unit=18.19, price_per_unit=24.08)
        res = calc(rules=rules)
        k = one(res, "konstrukcia.trapez")
        self.assertEqual((k["unit"], k["qty"], k["cost_per_unit"]), ("ks", 112, 18.19))


class TestOptimizery(unittest.TestCase):
    def test_huawei_merc_bez_montaze_a_bez_cca(self):
        res = calc({"vendor_stack": "huawei", "has_optimizery": True})
        merc = [i for i in res["items"] if i["category"] == "Optimizéry"]
        self.assertEqual(len(merc), 1)
        self.assertEqual(merc[0]["qty"], 56)   # 112 / 2
        self.assertEqual([i for i in res["items"] if "optimizér" in i["product_name"].lower() and i["category"] == "Montáž"], [])
        self.assertNotIn("tigo_cca_missing", kinds(res))

    def test_tigo_cca_kit_a_tap(self):
        res = calc({"has_optimizery": True})   # solinteg → Tigo, 112 optimizérov
        self.assertEqual([i for i in res["items"] if "Montáž optimizér" in i["product_name"]], [])
        tigo = [i for i in res["items"] if i["category"] == "Optimizéry"]
        self.assertEqual(len(tigo), 2)
        cca = one(res, "tigo_cca")
        self.assertEqual((cca["qty"], cca["cost_per_unit"]), (1, 134.0))
        self.assertNotIn("tigo_cca_multi", kinds(res))

    def test_tigo_cca_hranica_150(self):
        res = calc({"has_optimizery": True, "pocet_panelov": 150})
        self.assertEqual(one(res, "tigo_cca")["qty"], 1)
        self.assertNotIn("tigo_cca_multi", kinds(res))
        res = calc({"has_optimizery": True, "pocet_panelov": 151})
        self.assertEqual(one(res, "tigo_cca")["qty"], 2)
        self.assertIn("tigo_cca_multi", kinds(res))
        res = calc({"has_optimizery": True, "pocet_panelov": 420})   # PON-26-1366: 420 ks
        self.assertEqual(one(res, "tigo_cca")["qty"], 3)

    def test_tigo_bez_cca_v_datach_je_varovanie(self):
        stacks = load_stacks()
        for s in stacks:
            s["accessories"] = [a for a in s["accessories"] if a.get("category") != "tigo_cca"]
        res = calc({"has_optimizery": True}, stacks=stacks)
        self.assertTrue(res["ok"])
        self.assertIn("tigo_cca_missing", kinds(res))
        self.assertEqual(by_rule(res, "tigo_cca"), [])


class TestJanitza(unittest.TestCase):
    def jan(self, res):
        return [i for i in res["items"] if i["category"] == "Diagnostika siete"]

    def test_huawei_nad_30_kwp_model_103(self):
        res = calc({"vendor_stack": "huawei", "pocet_panelov": 102})   # 54,57 kWp
        j = self.jan(res)
        self.assertEqual(len(j), 1)
        self.assertIn("103", j[0]["product_name"])
        self.assertEqual(j[0]["rule_id"], "accessory.janitza_umg103cbm")

    def test_pod_prahom_nie(self):
        self.assertEqual(self.jan(calc({"vendor_stack": "huawei", "pocet_panelov": 56})), [])   # 29,96 kWp
        self.assertEqual(len(self.jan(calc({"vendor_stack": "huawei", "pocet_panelov": 57}))), 1)   # 30,5 kWp

    def test_len_huawei(self):
        for v in ("solinteg", "sungrow", "goodwe"):
            self.assertEqual(self.jan(calc({"vendor_stack": v, "pocet_panelov": 112})), [], v)

    def test_vypnutie_a_vyber_modelu(self):
        self.assertEqual(self.jan(calc({"vendor_stack": "huawei", "has_janitza": False})), [])
        res = calc({"vendor_stack": "huawei", "janitza_key": "janitza_umg104"})
        self.assertEqual(self.jan(res)[0]["rule_id"], "accessory.janitza_umg104")

    def test_prah_default_30_ak_stack_nema_offer_above_kw(self):
        stacks = load_stacks()
        for s in stacks:
            for a in s["accessories"]:
                a.pop("offer_above_kw", None)
        self.assertEqual(self.jan(calc({"vendor_stack": "huawei", "pocet_panelov": 56}, stacks=stacks)), [])
        self.assertEqual(len(self.jan(calc({"vendor_stack": "huawei", "pocet_panelov": 57}, stacks=stacks))), 1)


class TestBateria(unittest.TestCase):
    IND = {"has_bess": True, "bess_class": "industrial"}

    def test_nemodularna_kwh_ceil_a_odchylka(self):
        res = calc({**self.IND, "bess_kwh": 482})
        batt = one(res, "battery.sunwoda_oasis_l261")   # najbližšia kapacita k 482 kWh = 261 kWh
        self.assertEqual(batt["qty"], 2)   # ceil(482 / 261)
        self.assertEqual(res["totals"]["bess_kwh_effective"], 522)
        w = [w for w in res["warnings"] if w["kind"] == "bess_kwh_deviation"]
        self.assertEqual(len(w), 1)
        self.assertEqual(one(res, "battery.montaz")["qty"], 2)   # montáž za skriňu

    def test_kwh_bez_odchylky(self):
        res = calc({**self.IND, "bess_kwh": 112})
        b = one(res, "battery.solinteg_e2br_112r")
        self.assertEqual(b["qty"], 1)
        self.assertEqual(res["totals"]["bess_kwh_effective"], 112)
        self.assertNotIn("bess_kwh_deviation", kinds(res))

    def test_odchylka_do_5_percent_bez_varovania(self):
        # 3 × SBH400 (40 kWh) = 120 kWh pre cieľ 115 kWh → +4,3 % → bez varovania
        res = calc({"vendor_stack": "sungrow", "has_bess": True, "bess_class": "residential", "bess_kwh": 115,
                    "bess_sku": "sungrow_sbh400"})
        self.assertEqual(one(res, "battery.sungrow_sbh400")["qty"], 3)
        self.assertEqual(res["totals"]["bess_kwh_effective"], 120)
        self.assertNotIn("bess_kwh_deviation", kinds(res))
        # cieľ 112 kWh → 120 kWh = +7,1 % → varovanie
        res = calc({"vendor_stack": "sungrow", "has_bess": True, "bess_class": "residential", "bess_kwh": 112,
                    "bess_sku": "sungrow_sbh400"})
        self.assertIn("bess_kwh_deviation", kinds(res))

    def test_modularna_kwh_zaokruhlenie(self):
        res = calc({"has_bess": True, "bess_class": "residential", "bess_kwh": 20.48, "bess_sku": "dyness_tower_ts10"})
        self.assertEqual(one(res, "battery.dyness_tower_ts10")["qty"], 3)   # ceil(20,48 / 10,13)
        self.assertGreater(res["totals"]["bess_kwh_effective"], 20.48)

    def test_pocet_kusov_montaz_industrial_x_pocet(self):
        res = calc({**self.IND, "bess_count": 3, "bess_sku": "solinteg_e2br_112r"})
        self.assertEqual(one(res, "battery.solinteg_e2br_112r")["qty"], 3)
        m = one(res, "battery.montaz")
        self.assertEqual((m["qty"], m["cost_per_unit"], m["category"]), (3, 1750.0, "Batéria"))
        self.assertEqual(res["totals"]["bess_kwh_effective"], 336)
        self.assertNotIn("bess_kwh_deviation", kinds(res))   # počet kusov zadaný priamo → bez cieľa

    def test_montaz_rezidencna_x1(self):
        res = calc({"has_bess": True, "bess_class": "residential", "bess_count": 3, "bess_sku": "dyness_tower_ts10"})
        m = one(res, "battery.montaz")
        self.assertEqual((m["qty"], m["unit"], m["cost_per_unit"]), (1, "kpl", 300.0))
        self.assertEqual(m["product_name"], "Montáž batérie (rezidenčná sada)")

    def test_ziadny_pausal_5000(self):
        res = calc({**self.IND, "bess_count": 1, "bess_sku": "solinteg_e2br_112r"})
        for it in res["items"]:
            self.assertNotEqual(it["cost_per_unit"], 3850.0)
            self.assertNotEqual(it["price_per_unit"], 5000)
        self.assertEqual(one(res, "battery.montaz")["cost_per_unit"], 1750.0)

    def test_chybajuce_pravidlo_montaze_ma_zalozne_ceny_a_varovanie(self):
        rules = [r for r in f0_rules() if r["rule_type"] != "batteria"]
        res = calc({**self.IND, "bess_count": 2, "bess_sku": "solinteg_e2br_112r"}, rules=rules)
        m = one(res, "battery.montaz")
        self.assertEqual((m["qty"], m["cost_per_unit"]), (2, 1750.0))
        self.assertIn("missing_rule", kinds(res))
        res = calc({"has_bess": True, "bess_class": "residential", "bess_count": 2, "bess_sku": "dyness_tower_ts10"},
                   rules=rules)
        m = one(res, "battery.montaz")
        self.assertEqual((m["qty"], m["cost_per_unit"]), (1, 300.0))

    def test_luna_pcs_je_info(self):
        res = calc({"vendor_stack": "huawei", "has_bess": True, "bess_class": "industrial", "bess_count": 1,
                    "bess_sku": "luna2000_241_2s1", "pocet_panelov": 112})
        w = [w for w in res["warnings"] if w["kind"] == "pcs_required"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "info")
        self.assertIn("Raynet PCS doteraz nepridával — over u dodávateľa", w[0]["message"])
        self.assertEqual(one(res, "battery.luna2000_241_2s1")["cost_per_unit"], 40000.0)
        self.assertIn("out_of_scope", kinds(res))   # C&I batéria: len varovanie

    def test_ci_baterie_nad_250_kwh_je_mimo_rozsahu(self):
        res = calc({**self.IND, "bess_kwh": 300})   # nemodulárne skrine → efektívna kapacita > 250 kWh
        self.assertGreater(res["totals"]["bess_kwh_effective"], 250)
        self.assertIn("out_of_scope", kinds(res))
        res = calc({**self.IND, "bess_kwh": 112})
        self.assertNotIn("out_of_scope", kinds(res))

    def test_zapnuta_bateria_bez_poctu_je_varovanie(self):
        res = calc({"has_bess": True})
        self.assertTrue(res["ok"])
        self.assertIn("bess_missing_qty", kinds(res))
        self.assertEqual([i for i in res["items"] if i["category"] == "Batéria"], [])

    def test_vypnuta_bateria_s_poctom_sa_ignoruje(self):
        # UI posiela bess_count=2 aj pri has_bess=false
        res = calc({"has_bess": False, "bess_count": 2, "bess_class": "residential"})
        self.assertEqual([i for i in res["items"] if i["category"] == "Batéria"], [])
        self.assertNotIn("bess_missing_qty", kinds(res))

    def test_neznamy_model_baterie(self):
        res = calc({"has_bess": True, "bess_class": "residential", "bess_count": 1, "bess_sku": "neexistuje"})
        self.assertIn("bess_sku_unknown", kinds(res))

    def test_limit_max_units(self):
        res = calc({"has_bess": True, "bess_class": "residential", "bess_count": 5, "bess_sku": "solinteg_eba_b5k1"})
        self.assertEqual(one(res, "battery.solinteg_eba_b5k1")["qty"], 2)
        self.assertIn("bess_limit", kinds(res))
        self.assertEqual(res["totals"]["bess_kwh_effective"], round(2 * 5.12, 2))


class TestLenBess(unittest.TestCase):
    CFG = {"pocet_panelov": 0, "has_bess": True, "bess_class": "industrial", "bess_sku": "solinteg_e2br_112r", "bess_count": 2}

    def test_len_bess_zjednodusena_vetva(self):
        res = calc(self.CFG)
        self.assertTrue(res["ok"], res)
        self.assertEqual(cats(res), {"Batéria", "Projektová dokumentácia", "Doprava"})
        ids = [i["rule_id"] for i in res["items"]]
        self.assertEqual(ids, ["battery.solinteg_e2br_112r", "battery.montaz", "pd.PD50", "doprava.km"])
        self.assertEqual(one(res, "battery.montaz")["qty"], 2)
        t = res["totals"]
        self.assertEqual((t["kwp"], t["pocet_panelov"], t["pocet_menicov"], t["ac_kw_total"], t["requires_asdr"]),
                         (0, 0, 0, 0, False))
        self.assertEqual(t["bess_kwh_effective"], 224)
        w = [w for w in res["warnings"] if w["kind"] == "bess_only"]
        self.assertEqual(len(w), 1)
        for word in ("PCS", "AC rozvádzač", "kabeláž", "EMS", "dispečing"):
            self.assertIn(word, w[0]["message"])

    def test_len_bess_cez_kwh(self):
        res = calc({"pocet_panelov": 0, "has_bess": True, "bess_class": "industrial", "bess_kwh": 112})
        self.assertTrue(res["ok"])
        self.assertIn("bess_only", kinds(res))
        self.assertEqual(res["totals"]["bess_kwh_effective"], 112)
        self.assertEqual(one(res, "battery.montaz")["qty"], 1)

    def test_bez_panelov_a_bez_baterie_je_chyba(self):
        for cfg in ({"pocet_panelov": 0}, {"pocet_panelov": 0, "has_bess": False, "bess_count": 2, "bess_class": "residential"},
                    {"pocet_panelov": 0, "has_bess": True}):
            res = calc(cfg)
            self.assertFalse(res["ok"], cfg)
            self.assertTrue(res["error"])
            self.assertEqual(res["warnings"][0]["severity"], "error")

    def test_kwp_bez_panelov_je_normalna_fve(self):
        res = calc({"pocet_panelov": 0, "kwp": 59.92})
        self.assertEqual(res["totals"]["pocet_panelov"], 112)
        self.assertEqual(res["totals"]["kwp"], 59.92)
        self.assertNotIn("bess_only", kinds(res))


class TestVstupy(unittest.TestCase):
    def test_neznamy_panel_je_varovanie(self):
        res = calc({"panel_sku": "NEEXISTUJE-999"})
        self.assertTrue(res["ok"])
        self.assertIn("panel_unknown", kinds(res))
        self.assertEqual(res["totals"]["panel_wp"], 535)   # predvolený (prvý) panel
        self.assertNotIn("panel_unknown", kinds(calc()))

    def test_neznamy_vendor_je_chyba(self):
        res = calc({"vendor_stack": "neexistuje"})
        self.assertFalse(res["ok"])
        self.assertIn("neexistuje", res["error"])
        self.assertEqual(res["warnings"][0]["kind"], "unknown_vendor")

    def test_doprava_km_nula_chyba_zaporna(self):
        for km in (0, None, "", -5):
            res = calc({"vzdialenost_doprava": km})
            self.assertEqual(by_rule(res, "doprava"), [], km)
            self.assertIn("doprava_km", kinds(res), km)
        res = calc(drop=("vzdialenost_doprava",))
        self.assertEqual(by_rule(res, "doprava"), [])
        self.assertIn("doprava_km", kinds(res))

    def test_doprava_so_zadanymi_km(self):
        res = calc({"vzdialenost_doprava": 200})
        d = one(res, "doprava.km")
        self.assertEqual((d["qty"], d["unit"], d["category"]), (200, "km", "Doprava"))
        self.assertNotIn("doprava_km", kinds(res))

    def test_montaz_fve_a_polozky_fve(self):
        res = calc()
        self.assertEqual(one(res, "montaz.do_100")["qty"], 59.92)
        self.assertEqual(one(res, "spotrebny.standard")["category"], "Spotrebný materiál")
        self.assertEqual([i["category"] for i in res["items"]][:2], ["Panely", "Striedače"])
        self.assertEqual([i["position"] for i in res["items"]], list(range(1, len(res["items"]) + 1)))


class TestPomocneFunkcie(unittest.TestCase):
    BANDS = [R("x", "a", "A", "ks", 1, 2, lo=0, hi=10), R("x", "b", "B", "ks", 1, 2, lo=10.01, hi=20),
             R("x", "c", "C", "ks", 1, 2, lo=20.01, hi=30)]

    def test_pick_band_bez_medzier(self):
        key = lambda v: (eng._pick_band(self.BANDS, v) or {}).get("rule_key")
        self.assertEqual([key(0), key(10), key(10.005), key(10.01), key(20), key(30)], ["a", "a", "b", "b", "b", "c"])
        self.assertIsNone(key(30.01))
        self.assertIsNone(key(-1))

    def test_select_band_nad_rozsahom(self):
        band, mult, over = eng._select_band(self.BANDS, 95)
        self.assertEqual((band["rule_key"], mult, over), ("c", 4, True))   # ceil(95 / 30)
        self.assertEqual(eng._select_band([], 5), (None, 1, False))

    def test_eval_qty_formula_exact_a_celociselne(self):
        self.assertEqual(eng._eval_qty_formula("kwp", 59.92, 112), 60)
        self.assertEqual(eng._eval_qty_formula("kwp", 59.92, 112, exact=True), 59.92)
        self.assertEqual(eng._eval_qty_formula("ceil(kwp * 1.5)", 59.92, 112), 90)
        self.assertEqual(eng._eval_qty_formula("pocet_panelov", 59.92, 112), 112)
        self.assertEqual(eng._eval_qty_formula("__import__('os')", 1, 1), 1)   # mimo whitelistu → 1

    def test_pick_bess_rezimy(self):
        bats = [{"key": "a", "capacity_kwh": 40, "modular": False}, {"key": "b", "capacity_kwh": 10.24, "modular": True}]
        self.assertEqual(eng._pick_bess(bats, 0, 3)[0]["battery"]["key"], "b")   # počet kusov → modulárny
        self.assertEqual(eng._pick_bess(bats, 0, 3)[0]["qty"], 3)
        got = eng._pick_bess([bats[0]], 112, 0)[0]   # nemodulárne: ceil(112 / 40) = 3
        self.assertEqual((got["battery"]["key"], got["qty"]), ("a", 3))
        self.assertEqual(eng._pick_bess([bats[1]], 20.48, 0)[0]["qty"], 2)   # bez šumu pohyblivej rádovej čiarky
        self.assertEqual(eng._pick_bess(bats, 0, 0), [])
        self.assertEqual(eng._pick_bess([], 100, 0), [])

    def test_prazdny_config_je_chyba(self):
        res = eng.calculate_bom_v2(FakeSB(f0_rules(), load_stacks()), {})
        self.assertFalse(res["ok"])
        res = eng.calculate_bom_v2(FakeSB(f0_rules(), load_stacks()), None)
        self.assertFalse(res["ok"])


class TestGolden1404(unittest.TestCase):
    """PON-26-1404 (STAVIVO IBV): 112 × LONGi 535 Wp, Solinteg, trapéz, batéria 112 kWh, doprava 200 km.
    Raynet: 59 736,68 € bez DPH, marža z predaja 25,2 %."""

    def test_sucet_do_1_percenta(self):
        res = calc({"has_bess": True, "bess_kwh": 112, "bess_count": 1, "bess_class": "industrial",
                    "bess_sku": "solinteg_e2br_112r", "vzdialenost_doprava": 200, "margin_pct": 25.2})
        self.assertTrue(res["ok"], res)
        total = res["totals"]["total_price"]
        self.assertAlmostEqual(total, 59736.68, delta=59736.68 * 0.01, msg=f"súčet {total}")
        self.assertAlmostEqual(res["totals"]["margin_pct_effective"], 25.2, delta=0.1)
        self.assertEqual(res["totals"]["kwp"], 59.92)
        self.assertEqual(res["totals"]["ac_kw_total"], 50)
        for rid in ("panel.LONGI535", "rozvadzac_dc", "rozvadzac.R50", "pd.PD50", "battery.montaz", "doprava.km",
                    "kablove_zlaby", "vodice.dc", "vodice.ac", "spotrebny.standard", "montaz.do_100",
                    "konstrukcia.trapez", "mtp"):
            self.assertTrue(any(i["rule_id"] == rid for i in res["items"]), rid)
        self.assertNotIn("cost_estimated", kinds(res))   # nákupy z Raynetu
        self.assertEqual([w for w in res["warnings"] if w["severity"] == "error"], [])

    def test_nakup_riadkov_voci_raynetu(self):
        res = calc({"has_bess": True, "bess_kwh": 112, "bess_count": 1, "bess_class": "industrial",
                    "bess_sku": "solinteg_e2br_112r", "vzdialenost_doprava": 200, "margin_pct": 25.2})
        # (rule_id, nákup za jednotku v Raynete) — odchýlka do 3 %
        ref = {"panel.LONGI535": 80.0, "menic.solinteg.solinteg_mht_50": 3545.0, "smart_manager.solinteg": 43.0,
               "smart_meter.solinteg": 99.0, "battery.solinteg_e2br_112r": 13399.0, "battery.montaz": 1750.0,
               "konstrukcia.trapez": 34.0, "rozvadzac_dc": 30.0, "vodice.dc": 15.38, "vodice.ac": 20.0,
               "rozvadzac.R50": 2500.0, "kablove_zlaby": 14.0, "spotrebny.standard": 24.0, "montaz.do_100": 70.0,
               "pd.PD50": 1800.0, "doprava.km": 0.8}
        for rid, raynet in ref.items():
            got = one(res, rid)["cost_per_unit"]
            self.assertAlmostEqual(got, raynet, delta=raynet * 0.03, msg=f"{rid}: {got} vs {raynet}")
        # marža z predaja každého riadku ≈ 25,2 % (jednotná cenotvorba)
        for it in res["items"]:
            if it["price_per_unit"] >= 5:   # drobné ceny zaokrúhlenie na 2 desatinné skresľuje
                self.assertAlmostEqual((it["price_per_unit"] - it["cost_per_unit"]) / it["price_per_unit"] * 100, 25.2,
                                       delta=0.3, msg=it["rule_id"])
        self.assertEqual([w for w in res["warnings"] if w["kind"] == "out_of_scope"], [])   # 112 kWh Solinteg je bežný rozsah


class TestAiFunkcie(unittest.TestCase):
    def sb(self):
        return FakeSB(f0_rules(), load_stacks())

    def test_checker_bess_count_nehlasi_missing_kwh(self):
        out = eng.ai_compatibility_checker(self.sb(), {"vendor_stack": "solinteg", "has_bess": True, "bess_kwh": 0, "bess_count": 2})
        self.assertNotIn("bess_missing_kwh", [i["kind"] for i in out["issues"]])

    def test_checker_bez_poctu_stale_hlasi(self):
        out = eng.ai_compatibility_checker(self.sb(), {"vendor_stack": "solinteg", "has_bess": True})
        self.assertIn("bess_missing_kwh", [i["kind"] for i in out["issues"]])

    def test_checker_baterie_s_nehybridnym_vendorom(self):
        out = eng.ai_compatibility_checker(self.sb(), {"vendor_stack": "sungrow", "has_bess": True, "bess_count": 2})
        no_h = [i for i in out["issues"] if i["kind"] == "no_hybrid"]
        self.assertEqual(len(no_h), 1)
        self.assertEqual(no_h[0]["severity"], "warning")
        for v in ("huawei", "solinteg", "goodwe"):
            out = eng.ai_compatibility_checker(self.sb(), {"vendor_stack": v, "has_bess": True, "bess_count": 2})
            self.assertNotIn("no_hybrid", [i["kind"] for i in out["issues"]], v)

    def test_checker_bez_baterie_bez_no_hybrid(self):
        out = eng.ai_compatibility_checker(self.sb(), {"vendor_stack": "sungrow", "has_bess": False, "bess_count": 2})
        self.assertNotIn("no_hybrid", [i["kind"] for i in out["issues"]])

    def test_validator_pozna_kategorie_enginu(self):
        res = calc()
        out = eng.ai_bom_validator(self.sb(), res["items"], {"pocet_panelov": 112})
        self.assertEqual(out["missing"], [])
        res = calc({"vendor_stack": "huawei", "has_bess": True, "bess_count": 2})
        out = eng.ai_bom_validator(self.sb(), res["items"], {"pocet_panelov": 112, "has_bess": True})
        self.assertEqual(out["missing"], [])
        self.assertEqual([w for w in out["warnings"] if w.get("kind") in ("bess_in_config_not_bom", "no_inverter")], [])

    def test_validator_stale_hlasi_chybajuce(self):
        items = [i for i in calc()["items"] if i["category"] not in ("Konštrukcia", "Montáž")]
        out = eng.ai_bom_validator(self.sb(), items, {"pocet_panelov": 112})
        self.assertIn("Konštrukcia", out["missing"])
        self.assertIn("Práca - montáž", out["missing"])

    def test_raynet_konstanty(self):
        self.assertEqual(eng.RAYNET_AVG_EUR_PER_KWP, {"do_30": 790.0, "30_60": 745.0, "60_100": 680.0, "nad_100": 700.0})
        self.assertEqual(eng.RAYNET_VENDOR_DISTRIBUTION, {"huawei": 0.5, "solinteg": 0.33, "sungrow": 0.17, "goodwe": 0})
        out = eng.ai_vendor_recommender(self.sb(), 60.0)
        self.assertTrue(out["ok"])


class TestEvaVypnuta(unittest.TestCase):
    def test_priprav_ponuku_b2b_nevola_jadro(self):
        src = read_text(os.path.join(ROOT, "app.py"))
        fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_eva_tool_priprav_ponuku_b2b")
        first = fn.body[0]
        self.assertIsInstance(first, ast.Return)   # hneď prvý príkaz je return (jadro sa nevolá)
        d = ast.literal_eval(first.value)
        self.assertEqual(d, {"ok": False, "message": "B2B ponuky sa zostavujú v kalkulačke CRM (/b2b/kalkulacka)."})


if __name__ == "__main__":
    unittest.main(verbosity=2)
