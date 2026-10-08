"""Testy výpočtového jadra B2B kalkulačky (Fáza 0 + Fáza 1, 2026-10) — stdlib unittest, bez inštalácie, bez siete.

Spustenie z koreňa repa:
    python3 -m unittest discover -s tests -p 'test_b2b*.py' -v

DB nahrádza FakeSB (in-memory tabuľky b2b_calc_rules a b2b_vendor_stacks). Pravidlá = cieľové pravidlá
podľa F0-SPEC (nákup/predaj z Raynet cenníka) + pravidlá Fázy 1 (ems, kabelaz_bess; docs/b2b-kalkulacka/data/
F1-bess-pravidla.sql), stacky = tests/fixtures/b2b_vendor_stacks.json
(Supabase b2b_vendor_stacks 2026-10-08 + doplnené Raynet nákupy, viď _meta v súbore).
Fáza 1 (F1-SPEC "Jadro BESS"): plný režim len BESS, výber modelu podľa cieľa kWh, varovanie inverter_fallback.
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
        # Fáza 1 (data/F1-bess-pravidla.sql): EMS (EnergoStation compact/full) a AC kabeláž batérie (AYKY-J 3x150+70)
        R("ems", "compact", "EnergoStation EMS compact + licencia", "kpl", 7539.68, 9424.60, "1"),
        R("ems", "full", "EnergoStation EMS full + licencia", "kpl", 21825.40, 27281.76, "1"),
        R("kabelaz_bess", "ayky_3x150_70", "Kabeláž AC batérie AYKY-J 3x150+70", "m", 8.78, 10.54, "bess_kabel_m"),
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
                   "Dispečerské riadenie", "Optimizéry", "Rapid Shutdown", "Batéria", "Wallbox", "Montáž", "Doprava",
                   "EMS"}   # EMS: nová kategória Fázy 1 (len pri has_ems / len BESS od 100 kWh)
        self.assertLessEqual(cats(res), allowed)
        self.assertNotIn("EMS", cats(res))   # FVE + BESS bez výslovného has_ems: žiadny EMS
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
        # cieľ 130 kWh: žiadny model nie je v tolerancii 5 % → najmenšie prekročenie cieľa = 2 × E2BR-80 (160 kWh, +23,1 %)
        res = calc({**self.IND, "bess_kwh": 130})
        batt = one(res, "battery.solinteg_e2br_80r")
        self.assertEqual(batt["qty"], 2)   # ceil(130 / 80)
        self.assertEqual(res["totals"]["bess_kwh_effective"], 160)
        w = [w for w in res["warnings"] if w["kind"] == "bess_kwh_deviation"]
        self.assertEqual(len(w), 1)
        self.assertIn("+23.1 %", w[0]["message"])
        self.assertEqual(one(res, "battery.montaz")["qty"], 2)   # montáž za skriňu

    def test_kwh_482_nemodularna_presne_2_x_241(self):
        # F0 vybralo "najbližšiu kapacitu" 2 × L261 = 522 kWh (+8,3 %); kombinácia jedného modelu 2 × L241 = presne 482
        res = calc({**self.IND, "bess_kwh": 482})
        self.assertEqual(one(res, "battery.sunwoda_oasis_l241")["qty"], 2)
        self.assertEqual(by_rule(res, "battery.sunwoda_oasis_l261"), [])
        self.assertEqual(res["totals"]["bess_kwh_effective"], 482)
        self.assertNotIn("bess_kwh_deviation", kinds(res))
        self.assertEqual(one(res, "battery.montaz")["qty"], 2)

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

    def test_len_bess_plna_skladba_cez_model(self):
        # F1: 2 × E2BR-112R (224 kWh, 2 × 50 kW = 100 kW) → batéria, montáž, AC rozvádzač, kabeláž, PD, EMS, statika/PBS,
        # dispečing (AC >= 100 kW), doprava — bez panelov, meničov FVE, konštrukcie, vodičov DC a montáže FVE
        res = calc(self.CFG)
        self.assertTrue(res["ok"], res)
        self.assertEqual(cats(res), {"Batéria", "Rozvádzač", "Vodiče", "Projektová dokumentácia", "EMS", "Statika a PBS",
                                     "Dispečerské riadenie", "Doprava"})
        ids = [i["rule_id"] for i in res["items"]]
        self.assertEqual(ids, ["battery.solinteg_e2br_112r", "battery.montaz", "rozvadzac.R100", "kabelaz_bess",
                               "pd.PD100", "ems", "statika", "ppbs", "dispecing.ZSD", "doprava.km"])
        self.assertEqual(one(res, "battery.montaz")["qty"], 2)
        t = res["totals"]
        self.assertEqual((t["kwp"], t["pocet_panelov"], t["pocet_menicov"], t["ac_kw_total"], t["requires_asdr"]),
                         (0, 0, 0, 100, True))
        self.assertEqual(t["bess_kwh_effective"], 224)
        w = [w for w in res["warnings"] if w["kind"] == "bess_only"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "info")
        self.assertIn("PCS", w[0]["message"])   # menič/PCS kalkulačka v tomto režime nepridáva

    def test_len_bess_cez_kwh(self):
        res = calc({"pocet_panelov": 0, "has_bess": True, "bess_class": "industrial", "bess_kwh": 112})
        self.assertTrue(res["ok"])
        self.assertIn("bess_only", kinds(res))
        self.assertEqual(res["totals"]["bess_kwh_effective"], 112)
        self.assertEqual(one(res, "battery.montaz")["qty"], 1)

    def test_len_bess_bez_zemneho_varovania(self):
        res = calc({**self.CFG, "typ_strechy": "zemne_skrutky"})
        self.assertTrue(res["ok"])
        self.assertNotIn("out_of_scope", [w["kind"] for w in res["warnings"] if "Zemná" in w["message"]])

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


# ----------------------------------------------------------------------------------------------
# Fáza 1 (2026-10): plný režim len BESS, výber modelu podľa cieľa kWh (N-19), fallback meničov (E-12)
# ----------------------------------------------------------------------------------------------
class TestLenBessF1(unittest.TestCase):
    """F1-SPEC "Jadro BESS": pocet_panelov 0, bez kwp, has_bess → batéria, montáž, AC rozvádzač, kabeláž, PD, EMS,
    statika + PBS, dispečing, doprava; bez panelov, meničov FVE, konštrukcie, vodičov DC, R-DC a montáže FVE."""
    BESS = {"pocet_panelov": 0, "has_bess": True, "bess_class": "industrial", "vzdialenost_doprava": 150}
    FVE_PREFIXES = ("panel.", "menic.", "smart_manager", "smart_meter", "accessory.", "konstrukcia.", "zatiaz",
                    "rozvadzac_dc", "vodice.", "spotrebny", "kablove_zlaby", "mtp", "montaz.", "optimizer", "tigo",
                    "rapid_shutdown", "wallbox")

    def ids(self, res):
        return [i["rule_id"] for i in res["items"]]

    def test_482_kwh_solinteg_sunwoda_plna_skladba(self):
        res = calc({**self.BESS, "bess_kwh": 482, "bess_kabel_m": 40, "distribucka": "SSD"})
        self.assertTrue(res["ok"], res)
        ids = self.ids(res)
        self.assertEqual(ids, ["battery.sunwoda_oasis_l241", "battery.montaz", "rozvadzac.R250", "kabelaz_bess",
                               "pd.PD500", "ems", "statika", "ppbs", "dispecing.SSD", "doprava.km"])
        for rid in ids:   # "battery.montaz" je montáž batérie, nie "montaz.*" (FVE)
            self.assertFalse(rid.startswith(self.FVE_PREFIXES), rid)
        self.assertEqual(cats(res) & {"Panely", "Striedače", "Konštrukcia", "Monitoring", "Montáž", "Optimizéry"}, set())
        self.assertEqual((one(res, "battery.sunwoda_oasis_l241")["qty"], one(res, "battery.montaz")["qty"]), (2, 2))
        kab = one(res, "kabelaz_bess")
        self.assertEqual((kab["qty"], kab["unit"], kab["category"], kab["cost_per_unit"]), (40, "m", "Vodiče", 8.78))
        ems = one(res, "ems")
        self.assertEqual((ems["category"], ems["unit"], ems["cost_per_unit"]), ("EMS", "kpl", 7539.68))
        self.assertEqual(one(res, "dispecing.SSD")["category"], "Dispečerské riadenie")
        t = res["totals"]
        self.assertEqual((t["kwp"], t["pocet_panelov"], t["pocet_menicov"], t["panel_wp"]), (0, 0, 0, 0))
        self.assertEqual((t["ac_kw_total"], t["requires_asdr"], t["bess_kwh_effective"]), (250, True, 482))
        # nákup: 2 × 36 584 + 2 × 1 750 + R250 8 000 + 40 m × 8,78 + PD500 4 400 + EMS 7 539,68 + 500 + 350 + SSD 20 000 + 150 km × 0,8
        self.assertEqual(t["total_cost"], 117928.88)
        self.assertAlmostEqual(t["margin_pct_effective"], 22.0, delta=0.1)
        # model v tolerancii, kabeláž aj distribučka zadané → jediné hlásenie je informácia o režime
        self.assertEqual(kinds(res), ["bess_only"])
        self.assertEqual(res["warnings"][0]["severity"], "info")

    def test_defaulty_kabelaz_30_m_a_distribucka_zsd_s_varovanim(self):
        res = calc({**self.BESS, "bess_kwh": 482})
        self.assertEqual(one(res, "kabelaz_bess")["qty"], 30)
        w = [w for w in res["warnings"] if w["kind"] == "bess_kabel_default"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "warning")
        self.assertIn("30 m", w[0]["message"])
        self.assertIn("dispecing.ZSD", self.ids(res))
        self.assertIn("distribucka", kinds(res))
        for m in (0, -5, None, ""):   # nulová / záporná / prázdna dĺžka = nezadaná
            res = calc({**self.BESS, "bess_kwh": 482, "bess_kabel_m": m})
            self.assertEqual(one(res, "kabelaz_bess")["qty"], 30, m)
            self.assertIn("bess_kabel_default", kinds(res), m)
        res = calc({**self.BESS, "bess_kwh": 482, "bess_kabel_m": 45.5})
        self.assertEqual(one(res, "kabelaz_bess")["qty"], 45.5)
        self.assertNotIn("bess_kabel_default", kinds(res))

    def test_723_kwh_sungrow(self):
        res = calc({**self.BESS, "vendor_stack": "sungrow", "bess_kwh": 723, "bess_kabel_m": 60, "distribucka": "ZSD"})
        self.assertTrue(res["ok"], res)
        # 3 × PowerKeeper 250 kWh = 750 kWh (+3,7 %) je jediný model v tolerancii 5 % (3 × ST255 = 765 → +5,8 %; 2 × ST510 = 1 024)
        b = one(res, "battery.sungrow_powerkeeper_250")
        self.assertEqual(b["qty"], 3)
        self.assertEqual(res["totals"]["bess_kwh_effective"], 750)
        self.assertNotIn("bess_kwh_deviation", kinds(res))
        self.assertEqual(one(res, "battery.montaz")["qty"], 3)
        self.assertEqual(res["totals"]["ac_kw_total"], 375)   # 3 × 125 kW zo stacku
        for rid in ("rozvadzac.R500", "kabelaz_bess", "pd.PD500", "ems", "statika", "ppbs", "dispecing.ZSD", "doprava.km"):
            one(res, rid)
        self.assertEqual(by_rule(res, "menic"), [])
        self.assertNotIn("no_hybrid", kinds(res))   # menič FVE sa v režime len BESS nepridáva

    def test_luna_241_x3_tauris(self):
        res = calc({**self.BESS, "vendor_stack": "huawei", "bess_sku": "luna2000_241_2s1", "bess_count": 3,
                    "bess_kw": 324, "bess_kabel_m": 150, "distribucka": "ZSD"})
        self.assertTrue(res["ok"], res)
        b = one(res, "battery.luna2000_241_2s1")
        self.assertEqual((b["qty"], b["cost_per_unit"]), (3, 40000.0))
        self.assertEqual(one(res, "battery.montaz")["qty"], 3)
        t = res["totals"]
        self.assertEqual((t["ac_kw_total"], t["bess_kwh_effective"], t["requires_asdr"]), (324, 723, True))
        self.assertEqual(self.ids(res), ["battery.luna2000_241_2s1", "battery.montaz", "rozvadzac.R500", "kabelaz_bess",
                                         "pd.PD500", "ems", "statika", "ppbs", "dispecing.ZSD", "doprava.km"])
        self.assertEqual(one(res, "kabelaz_bess")["qty"], 150)
        pcs = [w for w in res["warnings"] if w["kind"] == "pcs_required"]
        self.assertEqual((len(pcs), pcs[0]["severity"]), (1, "info"))   # otvorený bod R4 ostáva len informáciou
        self.assertNotIn("out_of_scope", kinds(res))   # C&I batéria je v režime len BESS bežný rozsah
        self.assertNotIn("bess_kw_below", kinds(res))   # 324 kW vs 3 × 100 kW v dátach: do +10 % bez varovania
        # batéria je zásadná časť nákupu (Raynet TAURIS: batéria 65–70 % ceny)
        self.assertGreater(b["total_cost"] / t["total_cost"], 0.60)

    def test_ems_default_od_100_kwh_a_prebitie(self):
        res = calc({**self.BESS, "bess_kwh": 482})
        ems = one(res, "ems")
        self.assertEqual((ems["category"], ems["cost_per_unit"], ems["qty"]), ("EMS", 7539.68, 1))
        self.assertIn("compact", ems["product_name"])
        for off in (False, "false", "0", "nie"):
            self.assertEqual(by_rule(calc({**self.BESS, "bess_kwh": 482, "has_ems": off}), "ems"), [], off)
        self.assertEqual(len(by_rule(calc({**self.BESS, "bess_kwh": 482, "has_ems": True}), "ems")), 1)
        # pod 100 kWh (1 × E2BR-64R, 64 kWh / 50 kW): bez EMS, statiky, PBS a dispečingu
        small = {**self.BESS, "bess_sku": "solinteg_e2br_64r", "bess_count": 1}
        res = calc(small)
        self.assertEqual(res["totals"]["bess_kwh_effective"], 64)
        self.assertEqual(by_rule(res, "ems") + by_rule(res, "statika") + by_rule(res, "ppbs") + by_rule(res, "dispecing"), [])
        self.assertEqual(self.ids(res), ["battery.solinteg_e2br_64r", "battery.montaz", "rozvadzac.R50", "kabelaz_bess",
                                         "pd.PD50", "doprava.km"])
        self.assertEqual(one(calc({**small, "has_ems": True}), "ems")["cost_per_unit"], 7539.68)
        full = one(calc({**small, "has_ems": True, "ems_typ": "full"}), "ems.full")
        self.assertEqual((full["cost_per_unit"], full["category"]), (21825.40, "EMS"))
        self.assertIn("full", full["product_name"])
        self.assertEqual(by_rule(calc({**small, "has_ems": True, "ems_typ": "full"}), "ems."), [full])   # len jeden riadok EMS
        self.assertEqual(one(calc({**small, "has_ems": True, "ems_typ": "xyz"}), "ems")["cost_per_unit"], 7539.68)

    def test_ems_pri_fve_bess_len_na_vyslovne_has_ems(self):
        cfg = {"has_bess": True, "bess_class": "industrial", "bess_sku": "solinteg_e2br_112r", "bess_count": 1}
        self.assertEqual(by_rule(calc(cfg), "ems"), [])   # FVE + BESS: EMS nie je predvolený
        res = calc({**cfg, "has_ems": True})
        ids = self.ids(res)
        self.assertEqual(one(res, "ems")["category"], "EMS")
        self.assertLess(ids.index("battery.solinteg_e2br_112r"), ids.index("ems"))
        self.assertEqual(by_rule(calc({"has_ems": True}), "ems"), [])   # bez batérie has_ems nič nerobí

    def test_chybajuce_pravidla_ems_a_kabelaz_su_varovanie(self):
        rules = [r for r in f0_rules() if r["rule_type"] not in ("ems", "kabelaz_bess")]
        res = calc({**self.BESS, "bess_kwh": 482}, rules=rules)   # dáta pred F1 (SQL ešte nenasadený)
        self.assertTrue(res["ok"], res)
        msgs = " ".join(w["message"] for w in res["warnings"] if w["kind"] == "missing_rule")
        self.assertIn("ems/compact", msgs)
        self.assertIn("kabelaz_bess/ayky_3x150_70", msgs)
        self.assertEqual(by_rule(res, "ems") + by_rule(res, "kabelaz_bess"), [])
        for rid in ("battery.sunwoda_oasis_l241", "rozvadzac.R250", "pd.PD500", "dispecing.ZSD", "doprava.km"):
            one(res, rid)

    def test_bess_kw_vstup_urcuje_rozvadzac_pd_a_dispecing(self):
        # (bess_kw, rozvádzač, PD, dispečing) — PD podľa max(kW, 50) = najmenej PD50; dispečing od 100 kW AC
        cases = [(8, "R10", "PD50", False), (20, "R20", "PD50", False), (50, "R50", "PD50", False),
                 (80, "R100", "PD100", False), (99, "R100", "PD100", False), (100, "R100", "PD100", True),
                 (150, "R200", "PD200", True), (250, "R250", "PD500", True), (400, "R500", "PD500", True),
                 (800, "R1000", "PD1000", True)]
        for kw, r_key, pd_key, disp in cases:
            with self.subTest(bess_kw=kw):
                res = calc({**self.BESS, "bess_kwh": 482, "bess_kw": kw, "distribucka": "ZSD"})
                self.assertEqual(res["totals"]["ac_kw_total"], kw)
                self.assertEqual(res["totals"]["requires_asdr"], disp)
                self.assertEqual([i["rule_id"] for i in by_rule(res, "rozvadzac.")], [f"rozvadzac.{r_key}"])
                self.assertEqual([i["rule_id"] for i in by_rule(res, "pd.")], [f"pd.{pd_key}"])
                self.assertEqual([i["rule_id"] for i in by_rule(res, "dispecing.")], ["dispecing.ZSD"] if disp else [])
                self.assertNotIn("bess_kw_odhad", kinds(res))
        # nad 1 000 kW: ceil(AC / 1000) × R1000 a PD2000 s varovaniami
        res = calc({**self.BESS, "bess_kwh": 482, "bess_kw": 1500, "distribucka": "ZSD"})
        self.assertEqual(one(res, "rozvadzac.R1000")["qty"], 2)
        self.assertIn("rozvadzac_over_range", kinds(res))
        one(res, "pd.PD2000")

    def test_dispecing_podla_distribucky_od_100_kw(self):
        base = {**self.BESS, "bess_kwh": 482}
        self.assertEqual(by_rule(calc({**base, "bess_kw": 99}), "dispecing."), [])
        self.assertNotIn("distribucka", kinds(calc({**base, "bess_kw": 99})))
        for dist in ("ZSD", "SSD", "VSD"):
            res = calc({**base, "bess_kw": 100, "distribucka": dist})
            self.assertEqual(one(res, f"dispecing.{dist}")["category"], "Dispečerské riadenie")
            self.assertNotIn("distribucka", kinds(res))
        res = calc({**base, "bess_kw": 100})   # bez distribučky: ZSD + varovanie
        one(res, "dispecing.ZSD")
        self.assertIn("distribucka", kinds(res))

    def test_vykon_zo_stacku_a_odhad_kwh_2(self):
        res = calc({**self.BESS, "bess_kwh": 482})
        self.assertEqual(res["totals"]["ac_kw_total"], 250)   # 2 × Oasis L241 po 125 kW
        self.assertNotIn("bess_kw_odhad", kinds(res))
        stacks = load_stacks()
        for s in stacks:
            for b in s["batteries"]:
                b.pop("max_power_kw", None)
        res = calc({**self.BESS, "bess_kwh": 482}, stacks=stacks)   # výkon nikde → kWh / 2 + varovanie
        self.assertEqual(res["totals"]["ac_kw_total"], 241)
        w = [w for w in res["warnings"] if w["kind"] == "bess_kw_odhad"]
        self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
        self.assertIn("241", w[0]["message"])
        one(res, "rozvadzac.R250")
        one(res, "pd.PD500")
        res = calc({**self.BESS, "bess_kwh": 482, "bess_kw": 100}, stacks=stacks)   # bess_kw odhad prebije
        self.assertEqual(res["totals"]["ac_kw_total"], 100)
        self.assertNotIn("bess_kw_odhad", kinds(res))

    def test_bess_kw_nad_vykonom_baterii_je_varovanie(self):
        res = calc({**self.BESS, "bess_kwh": 482, "bess_kw": 500, "distribucka": "ZSD"})   # v dátach 2 × 125 = 250 kW
        w = [w for w in res["warnings"] if w["kind"] == "bess_kw_below"]
        self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
        self.assertEqual(res["totals"]["ac_kw_total"], 500)   # počíta sa požadovaný výkon
        self.assertNotIn("bess_kw_below", kinds(calc({**self.BESS, "bess_kwh": 482, "bess_kw": 270})))   # do +10 % bez varovania

    def test_statika_a_pbs_od_100_kwh(self):
        res = calc({**self.BESS, "bess_sku": "solinteg_e2br_96r", "bess_count": 1})   # 96 kWh
        self.assertEqual(by_rule(res, "statika") + by_rule(res, "ppbs"), [])
        res = calc({**self.BESS, "bess_sku": "solinteg_e2br_112r", "bess_count": 1})   # 112 kWh
        st, pb = one(res, "statika"), one(res, "ppbs")
        self.assertEqual((st["category"], pb["category"], st["qty"], pb["qty"]), ("Statika a PBS", "Statika a PBS", 1, 1))
        # hranica presne 100 kWh (GoodWe Dyness BF100): statika, PBS aj EMS už áno, dispečing (50 kW) nie
        res = calc({**self.BESS, "vendor_stack": "goodwe", "bess_sku": "dyness_bf100", "bess_count": 1})
        self.assertEqual(res["totals"]["bess_kwh_effective"], 100)
        for rid in ("statika", "ppbs", "ems", "rozvadzac.R50", "pd.PD50"):
            one(res, rid)
        self.assertEqual(by_rule(res, "dispecing."), [])

    def test_len_bess_s_wallboxom_a_bez_dopravy(self):
        res = calc({**self.BESS, "bess_kwh": 112, "has_wallbox": True, "wallbox_pocet": 2, "vzdialenost_doprava": 0})
        wb = [i for i in res["items"] if i["category"] == "Wallbox"]
        self.assertEqual([i["qty"] for i in wb], [2])
        self.assertEqual(by_rule(res, "doprava"), [])
        self.assertIn("doprava_km", kinds(res))

    def test_len_bess_nema_fve_polozky_pri_ziadnom_vstupe_fve(self):
        # typ strechy, panely, optimizéry či rapid shutdown v režime len BESS nemajú vplyv
        res = calc({**self.BESS, "bess_kwh": 112, "typ_strechy": "vychod_zapad", "has_optimizery": True,
                    "has_rapid_shutdown": True, "has_dc_rozvadzac": True, "panel_sku": "LONGI535"})
        for rid in self.ids(res):
            self.assertFalse(rid.startswith(self.FVE_PREFIXES), rid)
        self.assertNotIn("rs_recommendation", kinds(res))
        self.assertNotIn("vendor_match", kinds(res))

    def test_fve_rezim_ignoruje_vstupy_len_bess(self):
        # s panelmi sú bess_kw / bess_kabel_m / ems_typ bezvýznamné: AC výkon určujú meniče, kabeláž ide per kWp
        base = calc()
        res = calc({"bess_kw": 500, "bess_kabel_m": 100, "ems_typ": "full"})
        self.assertEqual(self.ids(res), self.ids(base))
        self.assertEqual(res["totals"], base["totals"])
        self.assertEqual(by_rule(res, "kabelaz_bess") + by_rule(res, "ems"), [])

    def test_vyrobca_bez_baterie_v_katalogu_je_chyba(self):
        res = calc({"pocet_panelov": 0, "has_bess": True, "bess_count": 1, "vendor_stack": "synt"},
                   stacks=[synthetic_stack(10)])
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])

    def test_nulovy_vyber_je_chyba(self):
        for cfg in ({"pocet_panelov": 0, "has_bess": True}, {"pocet_panelov": 0, "has_bess": False, "bess_kwh": 482}):
            res = calc(cfg)
            self.assertFalse(res["ok"], cfg)

    def test_save_bundle_v2_odstranena(self):
        self.assertFalse(hasattr(eng, "save_bundle_v2"))


class TestVyberBaterie(unittest.TestCase):
    """Kombinácia skríň jedného modelu a N-19 (priemyselná batéria bez zvoleného modelu)."""
    IND = {"has_bess": True, "bess_class": "industrial"}

    def pick(self, res):
        return [(i["rule_id"], i["qty"]) for i in res["items"] if i["rule_id"].startswith("battery.") and i["rule_id"] != "battery.montaz"]

    def test_kwh_kombinacia_jedneho_modelu(self):
        # (cieľ kWh, model, počet, efektívna kapacita) pre Solinteg industrial
        cases = [(482, "sunwoda_oasis_l241", 2, 482), (300, "sunwoda_oasis_60", 5, 300), (112, "solinteg_e2br_112r", 1, 112),
                 (250, "sunwoda_oasis_l261", 1, 261), (64, "solinteg_e2br_64r", 1, 64), (241, "sunwoda_oasis_l241", 1, 241)]
        for kwh, key, qty, eff in cases:
            with self.subTest(kwh=kwh):
                res = calc({**self.IND, "pocet_panelov": 0, "bess_kwh": kwh})
                self.assertEqual(self.pick(res), [(f"battery.{key}", qty)])
                self.assertEqual(res["totals"]["bess_kwh_effective"], eff)
                self.assertNotIn("bess_kwh_deviation", kinds(res))

    def test_kwh_ziadny_model_v_tolerancii_najmensie_prekrocenie(self):
        res = calc({**self.IND, "pocet_panelov": 0, "bess_kwh": 100})   # 112 kWh = +12 % (112R aj 112C → lacnejší 112R)
        self.assertEqual(self.pick(res), [("battery.solinteg_e2br_112r", 1)])
        self.assertIn("bess_kwh_deviation", kinds(res))

    def test_kwh_modularna_rezidencna_bez_modelu(self):
        # Huawei 15 kWh: 3 × LUNA2000-5 = 15 kWh presne (F0: 2 × LUNA2000-10 = 20 kWh, +33 %)
        res = calc({"vendor_stack": "huawei", "has_bess": True, "bess_class": "residential", "bess_kwh": 15})
        self.assertEqual(self.pick(res), [("battery.luna2000_5", 3)])
        self.assertNotIn("bess_kwh_deviation", kinds(res))
        # 20 kWh: 2 × LUNA2000-10 (menej kusov ako 4 × LUNA2000-5)
        res = calc({"vendor_stack": "huawei", "has_bess": True, "bess_class": "residential", "bess_kwh": 20})
        self.assertEqual(self.pick(res), [("battery.luna2000_10", 2)])

    def test_trieda_nezvolena_od_60_kwh_vyberie_priemyselne_skrine(self):
        # Huawei 200 kWh bez triedy: 1 × LUNA2000-241 (+20,5 % = varovanie), nie 20 × rezidenčný LUNA2000-10
        res = calc({"vendor_stack": "huawei", "pocet_panelov": 0, "has_bess": True, "bess_kwh": 200})
        self.assertEqual(self.pick(res), [("battery.luna2000_241_2s1", 1)])
        self.assertIn("bess_kwh_deviation", kinds(res))
        # Sungrow 225 kWh bez triedy: PowerKeeper 250 kWh (+11 %), nie 15 × SBH150
        res = calc({"vendor_stack": "sungrow", "pocet_panelov": 0, "has_bess": True, "bess_kwh": 225})
        self.assertEqual(self.pick(res), [("battery.sungrow_powerkeeper_250", 1)])
        # S02c z auditu: Solinteg 112 kWh bez triedy → 1 × E2BR-112R (ako v F0)
        res = calc({"has_bess": True, "bess_kwh": 112})
        self.assertEqual(self.pick(res), [("battery.solinteg_e2br_112r", 1)])
        # pod 60 kWh sa trieda neodvodzuje: Solinteg 50 kWh → 1 × Dyness Stack100 51,2 kWh
        res = calc({"has_bess": True, "bess_kwh": 50})
        self.assertEqual(self.pick(res), [("battery.dyness_stack100_51", 1)])
        # výslovne zvolená trieda sa neprepisuje (ani keď je skladba nezmyselná) a zvolený model sa nestratí
        res = calc({"vendor_stack": "huawei", "has_bess": True, "bess_class": "residential", "bess_kwh": 200})
        self.assertEqual(self.pick(res), [("battery.luna2000_10", 20)])
        w = [w for w in res["warnings"] if w["kind"] == "bess_many_units"]   # 20 kusov pre 200 kWh = podozrivá skladba
        self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
        self.assertIn("20×", w[0]["message"])
        self.assertNotIn("bess_many_units", kinds(calc({**self.IND, "pocet_panelov": 0, "bess_kwh": 482})))
        res = calc({"has_bess": True, "bess_sku": "dyness_stack100_51", "bess_kwh": 100})
        self.assertEqual(self.pick(res), [("battery.dyness_stack100_51", 2)])
        self.assertNotIn("bess_sku_unknown", kinds(res))

    def test_best_bess_for_kwh_pravidla_vyberu(self):
        a = {"key": "a", "capacity_kwh": 100, "cost": 10000}
        b = {"key": "b", "capacity_kwh": 50, "cost": 6000}
        c = {"key": "c", "capacity_kwh": 25, "cost": 2000}
        best = lambda bats, kwh: (lambda r: (r["battery"]["key"], r["qty"]))(eng._best_bess_for_kwh(bats, kwh))
        self.assertEqual(best([a, b, c], 100), ("a", 1))    # všetky presne 100 → najmenej kusov
        self.assertEqual(best([a, b, c], 150), ("b", 3))    # a × 2 = 200 mimo tolerancie; b × 3 aj c × 6 presne → menej kusov
        self.assertEqual(best([a, b, c], 90), ("a", 1))     # nikto v tolerancii (+11,1 %) → rovnaká odchýlka → menej kusov
        d = {"key": "d", "capacity_kwh": 100, "cost": 8000}
        self.assertEqual(best([a, d], 100), ("d", 1))       # rovnaký počet kusov → lacnejší
        e = {"key": "e", "capacity_kwh": 10, "cost": 500, "max_units": 2}
        self.assertEqual(best([e, b], 50), ("b", 1))        # e by potreboval 5 ks > max_units 2 → preskočený
        self.assertEqual(best([e], 50), ("e", 5))           # jediný model: limit orezáva až calculate_bom_v2 (bess_limit)
        self.assertEqual(best([{"key": "x"}], 50), ("x", 1))   # bez údajov o kapacite nepadne
        self.assertEqual(best([{"key": "m", "capacity_kwh": 10.24}], 20.48), ("m", 2))   # bez šumu pohyblivej rádovej čiarky (nie 3 ks)

    def test_n19_pocet_a_kwh_bez_modelu_cielom_je_kwh(self):
        # UI posiela bess_count (predvolené 1–2) aj bess_kwh: model aj počet vyberie jadro podľa cieľa (nie "batteries[0]")
        res = calc({**self.IND, "bess_count": 1, "bess_kwh": 112})
        self.assertEqual(self.pick(res), [("battery.solinteg_e2br_112r", 1)])
        self.assertNotIn("bess_model_missing", kinds(res))
        res = calc({**self.IND, "pocet_panelov": 0, "bess_count": 3, "bess_kwh": 723})
        self.assertEqual(self.pick(res), [("battery.sunwoda_oasis_l241", 3)])
        res = calc({"vendor_stack": "huawei", **self.IND, "bess_count": 2, "bess_kwh": 482})
        self.assertEqual(self.pick(res), [("battery.luna2000_241_2s1", 2)])

    def test_n19_zvoleny_model_aj_s_kwh_pocet_je_pocet(self):
        # model + počet zadané výslovne → počet platí, rozpor s cieľom kWh sa len nahlási; kWh bez počtu → ceil(kWh / kapacita)
        res = calc({**self.IND, "bess_sku": "solinteg_e2br_112r", "bess_count": 3, "bess_kwh": 500})
        self.assertEqual(self.pick(res), [("battery.solinteg_e2br_112r", 3)])
        w = [w for w in res["warnings"] if w["kind"] == "bess_kwh_deviation"]
        self.assertEqual(len(w), 1)
        self.assertIn("-32.8 %", w[0]["message"])   # 336 kWh vs 500 kWh
        res = calc({**self.IND, "bess_sku": "solinteg_e2br_112r", "bess_count": 3, "bess_kwh": 336})
        self.assertNotIn("bess_kwh_deviation", kinds(res))   # sedí → bez varovania
        res = calc({**self.IND, "bess_sku": "solinteg_e2br_112r", "bess_kwh": 500})
        self.assertEqual(self.pick(res), [("battery.solinteg_e2br_112r", 5)])   # ceil(500 / 112) = 5 → 560 kWh (+12 %)
        self.assertIn("bess_kwh_deviation", kinds(res))

    def test_n19_bez_modelu_a_bez_ciela_je_varovanie_vyber_model(self):
        res = calc({**self.IND, "bess_count": 1})   # S02b z auditu: UI "Priemyselná", 1 ks, bez modelu
        w = [w for w in res["warnings"] if w["kind"] == "bess_model_missing"]
        self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
        self.assertIn("vyber model", w[0]["message"])
        self.assertIn("E2BR-64K-R", w[0]["message"])   # menuje, čo sa použilo
        self.assertEqual(self.pick(res), [("battery.solinteg_e2br_64r", 1)])   # batéria ostáva (poradie stacku), len s varovaním
        # rezidenčná sada bez modelu (UI predvolené bess_count = 2): tiež varovanie
        res = calc({"vendor_stack": "huawei", "has_bess": True, "bess_class": "residential", "bess_count": 2})
        self.assertIn("bess_model_missing", kinds(res))
        self.assertEqual(self.pick(res), [("battery.luna2000_5", 2)])

    def test_n19_bez_varovania_ked_je_model_alebo_ciel(self):
        for cfg in ({**self.IND, "bess_sku": "solinteg_e2br_112r", "bess_count": 1},
                    {**self.IND, "bess_sku": "solinteg_e2br_112r"},
                    {**self.IND, "bess_kwh": 112},
                    {"vendor_stack": "huawei", **self.IND, "bess_count": 1}):   # Huawei industrial = jediný model → nie je čo vyberať
            with self.subTest(cfg=cfg):
                self.assertNotIn("bess_model_missing", kinds(calc(cfg)))

    def test_nezname_sku_nema_dvojite_varovanie(self):
        res = calc({**self.IND, "bess_sku": "neexistuje", "bess_count": 1})
        self.assertIn("bess_sku_unknown", kinds(res))
        self.assertNotIn("bess_model_missing", kinds(res))


class TestInverterFallback(unittest.TestCase):
    """E-12: n × najväčší menič (žiadna kombinácia do MAX_INVERTER_UNITS ks) musí mať varovanie."""

    def test_fallback_ma_varovanie(self):
        res = calc_ac(125, {"pocet_panelov": math.ceil(1800 * 1000 / 535)})   # ~1 800 kWp, jediný model 125 kW
        n = one(res, "menic.synt.inv")["qty"]
        self.assertGreater(n, eng.MAX_INVERTER_UNITS)
        w = [w for w in res["warnings"] if w["kind"] == "inverter_fallback"]
        self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
        self.assertIn(f"{n}×", w[0]["message"])
        self.assertIn("INV 125 kW", w[0]["message"])
        self.assertIn(f"{res['totals']['ac_kw_total']:g} kW AC", w[0]["message"])

    def test_bez_fallbacku_bez_varovania(self):
        for res in (calc(), calc_ac(125), calc({"vendor_stack": "huawei", "pocet_panelov": 300})):
            self.assertNotIn("inverter_fallback", kinds(res))

    def test_pick_inverters_oznacuje_fallback(self):
        invs = [{"key": "x", "name": "X", "ac_kw": 10, "max_kwp": 15, "price": 1000, "cost": 800}]
        far = eng._pick_inverters(invs, 500)
        self.assertEqual((len(far), far[0]["qty"] > eng.MAX_INVERTER_UNITS, far[0].get("fallback")), (1, True, True))
        near = eng._pick_inverters(invs, 10)
        self.assertTrue(near)
        self.assertTrue(all("fallback" not in p for p in near))


class TestKompatibilitaLenBess(unittest.TestCase):
    def test_checker_len_bess_nehlasi_no_hybrid(self):
        sb = FakeSB(f0_rules(), load_stacks())
        cfg = {"vendor_stack": "sungrow", "has_bess": True, "bess_class": "industrial", "bess_count": 1}
        out = eng.ai_compatibility_checker(sb, {**cfg, "pocet_panelov": 0})   # výslovne 0 panelov = len BESS
        self.assertNotIn("no_hybrid", [i["kind"] for i in out["issues"]])
        out = eng.ai_compatibility_checker(sb, {**cfg, "pocet_panelov": 112})   # FVE + BESS: Sungrow bez hybridu hlási
        self.assertIn("no_hybrid", [i["kind"] for i in out["issues"]])
        out = eng.ai_compatibility_checker(sb, {**cfg, "pocet_panelov": 0, "kwp": 30})   # kWp zadané = FVE
        self.assertIn("no_hybrid", [i["kind"] for i in out["issues"]])


class TestLegacyInverters(unittest.TestCase):
    def test_legacy_model_not_auto_picked(self):
        from b2b_calculator_v2 import _pick_inverters
        invs = [
            {"key": "old50", "name": "Old 50K", "ac_kw": 50, "max_kwp": 75, "price": 4000, "cost": 3200, "hybrid": True, "legacy": True},
            {"key": "new50", "name": "New 50K", "ac_kw": 50, "max_kwp": 75, "price": 4400, "cost": 3545, "hybrid": True},
        ]
        picked = _pick_inverters(invs, 59.92 / 1.10, require_hybrid=True)
        self.assertTrue(picked)
        self.assertTrue(all(p["inverter"]["key"] == "new50" for p in picked))

    def test_only_legacy_still_works(self):
        from b2b_calculator_v2 import _pick_inverters
        invs = [{"key": "old50", "name": "Old 50K", "ac_kw": 50, "max_kwp": 75, "price": 4000, "cost": 3200, "hybrid": True, "legacy": True}]
        self.assertTrue(_pick_inverters(invs, 59.92 / 1.10, require_hybrid=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
