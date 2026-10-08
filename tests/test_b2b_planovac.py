"""Testy Fázy 3 — plánovač FVE → jadro B2B kalkulačky (F3-PLANOVAC-SPEC, časť E) — stdlib unittest, bez siete.

Nové voliteľné vstupy `calculate_bom_v2`:
  inverters_override  [{key|code, qty}]        meniče presne podľa plánovača (namiesto automatického výberu)
  konstrukcia_mix     [{typ_strechy, kwp}]     konštrukcia a záťaž V-Z po častiach strechy

Spustenie z koreňa repa:
    python3 -m unittest discover -s tests -p 'test_b2b*.py' -v

DB nahrádza FakeSB z test_b2b_calculator_v2 (pravidlá = f0_rules(), stacky = tests/fixtures/b2b_vendor_stacks.json).

Regresia "bez nových polí = ako pred Fázou 3": tests/fixtures/b2b_planovac_baseline.json je vygenerovaný PÔVODNÝM jadrom
(origin/main f752509) a test TestBezNovychPoli ho porovnáva s aktuálnym jadrom (položky aj súčty). Ak sa správanie bez
nových polí zmení úmyselne, baseline sa vygeneruje znova:
    python3 tests/test_b2b_planovac.py --regen-baseline [cesta/k/b2b_calculator_v2.py] ["popis zdroja"]
(bez cesty = aktuálne jadro).
"""
import importlib.util
import json
import math
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)    # tests/ — pomocné funkcie z test_b2b_calculator_v2

import b2b_calculator_v2 as eng  # noqa: E402
from test_b2b_calculator_v2 import (  # noqa: E402
    BASE, FakeSB, by_rule, calc, f0_rules, kinds, load_stacks, one, read_text, synthetic_stack,
)

BASELINE = os.path.join(HERE, "fixtures", "b2b_planovac_baseline.json")
REGEN_HINT = ("Ak je zmena správania (alebo vstupných dát f0_rules / b2b_vendor_stacks.json) úmyselná, baseline prepíš: "
              "python3 tests/test_b2b_planovac.py --regen-baseline")


def stack_of(stacks, vendor):
    return next(s for s in stacks if s["vendor_key"] == vendor)


def inv_of(stacks, vendor, key):
    return next(i for i in stack_of(stacks, vendor)["inverters"] if i["key"] == key)


def warns(res, kind):
    return [w for w in res["warnings"] if w["kind"] == kind]


def synth_500(ac_kw, hybrid=False):
    """Stack s jediným meničom a panelom 500 Wp (kWp = počet panelov / 2) — presné hranice DC/AC."""
    s = synthetic_stack(ac_kw, hybrid)
    s["preferred_panels"][0].update(sku="P500", name="Panel 500 Wp", wp=500)
    return s


def calc_synt(ac_kw, panels, override, **cfg):
    c = {"vendor_stack": "synt", "panel_sku": "P500", "pocet_panelov": panels, "inverters_override": override}
    c.update(cfg)
    return calc(c, stacks=[synth_500(ac_kw)])


# ----------------------------------------------------------------------------------------------
class TestInvertersOverride(unittest.TestCase):
    HW = {"vendor_stack": "huawei", "pocet_panelov": 300}   # 160,5 kWp; automat: 40 + 100 kW

    def hw(self, override=None, stacks=None, **cfg):
        c = {**self.HW, **cfg}
        if override is not None:
            c["inverters_override"] = override
        return calc(c, stacks=stacks)

    # --- presný výber ---
    def test_presny_vyber_dvoch_typov_bez_automatu(self):
        ov = [{"key": "sun2000_100ktl", "qty": 1}, {"key": "sun2000_30ktl", "qty": 2}]
        auto = self.hw()
        with mock.patch.object(eng, "_pick_inverters",
                               side_effect=AssertionError("override nesmie volať automatický výber")) as pick:
            res = self.hw(ov)
        pick.assert_not_called()
        self.assertTrue(res["ok"], res)
        i100, i30 = one(res, "menic.huawei.sun2000_100ktl"), one(res, "menic.huawei.sun2000_30ktl")
        self.assertEqual((i100["qty"], i30["qty"]), (1, 2))
        self.assertEqual((i100["category"], i100["sku"], i100["ac_kw"], i100["cost_per_unit"], i100["vendor_stack"]),
                         ("Striedače", "sun2000_100ktl", 100.0, 3488.0, "huawei"))
        self.assertEqual((i30["ac_kw"], i30["cost_per_unit"]), (30.0, 1863.0))
        t = res["totals"]
        self.assertEqual((t["ac_kw_total"], t["pocet_menicov"], t["inverters_source"]), (160.0, 3, "override"))
        self.assertEqual([k for k in kinds(res) if k.startswith("inverter_override")], [])
        # zostava inak než by zvolil automat (40 + 100 kW) a ďalšie riadky sa riadia Σ AC = 160 kW
        self.assertNotEqual({i["rule_id"] for i in by_rule(auto, "menic.")}, {i["rule_id"] for i in by_rule(res, "menic.")})
        for rid in ("rozvadzac.R200", "pd.PD200", "mtp", "dispecing.ZSD"):
            one(res, rid)

    def test_override_zhodny_s_automatom_da_identicky_vystup(self):
        for vendor, n in (("solinteg", 112), ("huawei", 120), ("sungrow", 120), ("goodwe", 56), ("huawei", 300)):
            cfg = {"vendor_stack": vendor, "pocet_panelov": n}
            auto = calc(cfg)
            ov = [{"key": i["rule_id"].split(".", 2)[2], "qty": i["qty"]} for i in by_rule(auto, "menic.")]
            res = calc({**cfg, "inverters_override": ov})
            self.assertEqual(res["totals"].pop("inverters_source"), "override", vendor)
            self.assertEqual(auto["totals"].pop("inverters_source"), "auto", vendor)
            self.assertEqual(res, auto, vendor)    # rovnaké riadky, rule_id, súčty aj varovania

    def test_inverters_source_auto_bez_override(self):
        self.assertEqual(calc()["totals"]["inverters_source"], "auto")

    # --- párovanie ---
    def test_parovanie_key_aj_code_bez_ohladu_na_velkost_pismen(self):
        stacks = load_stacks()
        inv_of(stacks, "huawei", "sun2000_100ktl")["code"] = "02-600367"
        inv_of(stacks, "huawei", "sun2000_30ktl")["code"] = "SUN2000-30KTL-M3"
        for e in ({"key": "SUN2000_100KTL"}, {"key": "  sun2000_100ktl "}, {"code": "02-600367"},
                  {"code": " 02-600367 "}, {"key": "02-600367"}, {"key": "x", "code": "02-600367"}):
            with self.subTest(e=e):
                res = self.hw([{**e, "qty": 1}], stacks=stacks)
                one(res, "menic.huawei.sun2000_100ktl")
                self.assertEqual(len(by_rule(res, "menic.")), 1)
                self.assertEqual(res["totals"]["inverters_source"], "override")
        res = self.hw([{"code": "sun2000-30ktl-m3", "qty": 2}], stacks=stacks)     # kód malými písmenami
        self.assertEqual(one(res, "menic.huawei.sun2000_30ktl")["qty"], 2)

    def test_key_ma_prednost_pred_code(self):
        stacks = load_stacks()
        inv_of(stacks, "huawei", "sun2000_40ktl")["code"] = "sun2000_30ktl"    # kód iného meniča = cudzí key
        res = self.hw([{"key": "sun2000_30ktl", "qty": 1}], stacks=stacks)
        one(res, "menic.huawei.sun2000_30ktl")
        self.assertEqual(by_rule(res, "menic.huawei.sun2000_40ktl"), [])

    def test_qty_cele_cislo_nad_1_inak_1(self):
        for q, exp in ((None, 1), ("", 1), ("abc", 1), (0, 1), (-3, 1), (0.5, 1), (float("nan"), 1), (float("inf"), 1),
                       (True, 1), (1, 1), (2, 2), ("3", 3), (2.7, 2), (4.0, 4)):
            with self.subTest(qty=q):
                res = self.hw([{"key": "sun2000_30ktl", "qty": q}])
                self.assertEqual(one(res, "menic.huawei.sun2000_30ktl")["qty"], exp)
        res = self.hw([{"key": "sun2000_30ktl"}])                              # qty chýba
        self.assertEqual(one(res, "menic.huawei.sun2000_30ktl")["qty"], 1)

    def test_rovnaky_menic_viackrat_sa_scita_do_jedneho_riadku(self):
        ov = [{"key": "sun2000_30ktl", "qty": 1}, {"key": "SUN2000_30KTL", "qty": 2}, {"key": "sun2000_100ktl", "qty": 1}]
        res = self.hw(ov)
        self.assertEqual(one(res, "menic.huawei.sun2000_30ktl")["qty"], 3)
        ids = [i["rule_id"] for i in by_rule(res, "menic.")]
        self.assertEqual(ids, ["menic.huawei.sun2000_30ktl", "menic.huawei.sun2000_100ktl"])   # poradie prvého výskytu
        self.assertEqual(res["totals"]["pocet_menicov"], 4)

    def test_legacy_model_override_vyberie_automat_nie(self):
        stacks = load_stacks()
        old = inv_of(stacks, "solinteg", "solinteg_mht_50")
        old["legacy"] = True
        stack_of(stacks, "solinteg")["inverters"].append(
            {**old, "key": "solinteg_mht_50_v2", "name": "Solinteg M2HT-50K-150 (3f hybrid)", "legacy": None})
        cfg = {"vendor_stack": "solinteg", "pocet_panelov": 112}
        auto = calc(cfg, stacks=stacks)
        self.assertEqual([i["rule_id"] for i in by_rule(auto, "menic.")], ["menic.solinteg.solinteg_mht_50_v2"])
        res = calc({**cfg, "inverters_override": [{"key": "solinteg_mht_50", "qty": 1}]}, stacks=stacks)
        one(res, "menic.solinteg.solinteg_mht_50")
        self.assertEqual(res["totals"]["inverters_source"], "override")
        self.assertEqual([k for k in kinds(res) if k.startswith("inverter_override")], [])

    # --- nespárované ---
    def test_nesparovane_polozky_su_varovanie_so_zoznamom(self):
        ov = [{"key": "sun2000_100ktl", "qty": 1}, {"key": "neexistuje", "qty": 2}, {"code": "XX-1"}, {"qty": 4}, "abc", None]
        res = self.hw(ov)
        self.assertTrue(res["ok"])
        w = warns(res, "inverter_override_unknown")
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["severity"], "warning")
        self.assertEqual(w[0]["items"], ["neexistuje", "XX-1", "(bez key/code)", "abc", "(prázdna položka)"])
        for name in w[0]["items"]:
            self.assertIn(name, w[0]["message"])
        self.assertNotIn("automatický", w[0]["message"])          # spárovaný menič ostal → automat sa nepoužil
        self.assertEqual([i["rule_id"] for i in by_rule(res, "menic.")], ["menic.huawei.sun2000_100ktl"])
        self.assertEqual(res["totals"]["inverters_source"], "override")

    def test_nic_nesparovane_je_automaticky_vyber_s_varovanim(self):
        base = calc({"vendor_stack": "huawei", "pocet_panelov": 120})
        for ov in ([{"key": "neexistuje", "qty": 2}], [{"code": "ZZ"}, {"key": "x"}], [{}], "sun2000_30ktl", 5):
            with self.subTest(ov=ov):
                res = calc({"vendor_stack": "huawei", "pocet_panelov": 120, "inverters_override": ov})
                self.assertEqual(res["items"], base["items"])                     # dnešný automatický výber
                self.assertEqual(res["totals"], base["totals"])
                self.assertEqual(res["totals"]["inverters_source"], "auto")
                w = warns(res, "inverter_override_unknown")
                self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
                self.assertIn("automatický výber", w[0]["message"])
                self.assertEqual([x for x in res["warnings"] if x["kind"] != "inverter_override_unknown"], base["warnings"])

    def test_prazdny_override_nie_je_override(self):
        base = self.hw()
        for ov in ([], None, {}, "", "   ", ()):
            with self.subTest(ov=ov):
                self.assertEqual(calc({**self.HW, "inverters_override": ov}), base)

    def test_dict_ako_jedina_polozka(self):
        res = self.hw({"key": "sun2000_100ktl", "qty": 2})
        self.assertEqual(one(res, "menic.huawei.sun2000_100ktl")["qty"], 2)
        self.assertEqual(res["totals"]["inverters_source"], "override")

    # --- DC/AC ---
    def test_pomer_dc_ac_hranice_su_v_poriadku(self):
        for ac, panels in ((40, 120), (100, 120)):       # 60 kWp / 40 kW = 1,5 ; 60 kWp / 100 kW = 0,6
            with self.subTest(ac=ac):
                res = calc_synt(ac, panels, [{"key": "inv", "qty": 1}])
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["totals"]["kwp"], 60.0)
                self.assertEqual(warns(res, "inverter_override_ratio"), [])
        res = calc_synt(40, 120, [{"key": "inv", "qty": 2}])      # 0,75
        self.assertEqual(warns(res, "inverter_override_ratio"), [])

    def test_pomer_dc_ac_mimo_intervalu_je_varovanie_s_hodnotou(self):
        res = calc_synt(40, 160, [{"key": "inv", "qty": 1}])      # 80 kWp / 40 kW = 2,00
        w = warns(res, "inverter_override_ratio")
        self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
        self.assertEqual(w[0]["ratio"], 2.0)
        self.assertIn("2.00", w[0]["message"])
        self.assertIn("80", w[0]["message"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["totals"]["inverters_source"], "override")
        res = calc_synt(100, 118, [{"key": "inv", "qty": 1}])     # 59 kWp / 100 kW = 0,59
        w = warns(res, "inverter_override_ratio")
        self.assertEqual((len(w), w[0]["ratio"]), (1, 0.59))
        self.assertIn("0.59", w[0]["message"])
        # tesne za hranicou: 61 kWp / 40 kW = 1,525 ; 59,5 kWp / 100 kW = 0,595
        for ac, panels, ratio in ((40, 122, 1.525), (100, 119, 0.595)):
            with self.subTest(ratio=ratio):
                w = warns(calc_synt(ac, panels, [{"key": "inv", "qty": 1}]), "inverter_override_ratio")
                self.assertEqual((len(w), w[0]["ratio"]), (1, ratio))

    def test_pomer_dc_ac_len_pri_override(self):
        # automatický výber s rovnakou zostavou (jediný model) varovanie nehlási — okno si hlieda sám
        res = calc_synt(40, 160, None)
        self.assertEqual(warns(res, "inverter_override_ratio"), [])
        self.assertEqual(res["totals"]["inverters_source"], "auto")

    def test_menic_s_nulovym_vykonom_nepadne(self):
        s = synth_500(40)
        s["inverters"][0]["ac_kw"] = 0
        res = calc({"vendor_stack": "synt", "panel_sku": "P500", "pocet_panelov": 120,
                    "inverters_override": [{"key": "inv", "qty": 1}]}, stacks=[s])
        self.assertTrue(res["ok"], res)
        w = warns(res, "inverter_override_ratio")
        self.assertEqual(len(w), 1)
        self.assertIn("0 kW", w[0]["message"])
        self.assertEqual(res["totals"]["ac_kw_total"], 0)

    def test_fallback_varovanie_sa_pri_override_nehlasi(self):
        res = calc_synt(125, math.ceil(1800 * 1000 / 500), [{"key": "inv", "qty": 5}])
        self.assertNotIn("inverter_fallback", kinds(res))
        self.assertEqual(one(res, "menic.synt.inv")["qty"], 5)

    # --- batéria bez hybridu ---
    BAT_RES = {"has_bess": True, "bess_sku": "luna2000_10", "bess_count": 1, "bess_class": "residential"}

    def test_rezidencna_bateria_bez_hybridu_v_override_je_varovanie(self):
        res = self.hw([{"key": "sun2000_30ktl", "qty": 2}], pocet_panelov=120, **self.BAT_RES)
        w = warns(res, "inverter_override_no_hybrid")
        self.assertEqual((len(w), w[0]["severity"]), (1, "warning"))
        self.assertNotIn("no_hybrid", kinds(res))        # všeobecné varovanie výberu sa pri override nehlási (duplicita)
        self.assertEqual(res["totals"]["inverters_source"], "override")
        one(res, "battery.luna2000_10")

    def test_hybrid_v_override_je_v_poriadku(self):
        for ov in ([{"key": "sun2000_25k_mb0", "qty": 2}],
                   [{"key": "sun2000_25k_mb0", "qty": 1}, {"key": "sun2000_30ktl", "qty": 1}]):   # aj zmes hybrid + string
            with self.subTest(ov=ov):
                res = self.hw(ov, pocet_panelov=120, **self.BAT_RES)
                self.assertEqual(warns(res, "inverter_override_no_hybrid"), [])
                self.assertNotIn("no_hybrid", kinds(res))

    def test_priemyselna_bateria_a_bez_baterie_nehlasi_no_hybrid(self):
        ind = {"has_bess": True, "bess_sku": "luna2000_241_2s1", "bess_count": 1, "bess_class": "industrial"}
        res = self.hw([{"key": "sun2000_30ktl", "qty": 2}], pocet_panelov=120, **ind)
        self.assertEqual(warns(res, "inverter_override_no_hybrid"), [])
        self.assertNotIn("no_hybrid", kinds(res))
        self.assertIn("pcs_required", kinds(res))        # ostatné varovania k C&I batérii ostávajú
        res = self.hw([{"key": "sun2000_30ktl", "qty": 2}], pocet_panelov=120)       # bez batérie
        self.assertEqual(warns(res, "inverter_override_no_hybrid"), [])

    def test_bez_override_ostava_vseobecne_no_hybrid(self):
        res = calc({"vendor_stack": "sungrow", "pocet_panelov": 120, "has_bess": True, "bess_sku": "sungrow_sbh100",
                    "bess_count": 1})
        self.assertIn("no_hybrid", kinds(res))
        self.assertEqual(warns(res, "inverter_override_no_hybrid"), [])

    def test_len_bess_ignoruje_override(self):
        cfg = {"pocet_panelov": 0, "has_bess": True, "bess_class": "industrial", "vzdialenost_doprava": 150,
               "bess_kwh": 482, "bess_kabel_m": 40, "distribucka": "SSD"}
        plain = calc(cfg)
        res = calc({**cfg, "inverters_override": [{"key": "solinteg_mht_50", "qty": 1}, {"key": "nic"}]})
        self.assertEqual(res, plain)
        self.assertEqual(res["totals"]["inverters_source"], "auto")
        self.assertEqual(by_rule(res, "menic."), [])


# ----------------------------------------------------------------------------------------------
class TestKonstrukciaMix(unittest.TestCase):
    """BASE: solinteg, 112 panelov = 59,92 kWp, typ_strechy trapez."""
    KWP = 59.92

    def mix(self, parts, drop=(), rules=None, **cfg):
        return calc({"konstrukcia_mix": [{"typ_strechy": t, "kwp": k} for t, k in parts], **cfg}, rules=rules, drop=drop)

    @staticmethod
    def kons_ids(res):
        return [i["rule_id"] for i in res["items"] if i["category"] == "Konštrukcia"]

    def test_dva_typy_dva_riadky_a_zatiaz_len_z_vz_casti(self):
        res = self.mix([("vychod_zapad", 30.0), ("trapez", 29.92)])
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.kons_ids(res), ["konstrukcia.vychod_zapad", "konstrukcia.trapez", "zatiaz.vz"])
        vz, tr, zat = (one(res, r) for r in ("konstrukcia.vychod_zapad", "konstrukcia.trapez", "zatiaz.vz"))
        self.assertEqual((vz["qty"], vz["unit"], vz["cost_per_unit"], vz["category"]), (30.0, "kWp", 80.0, "Konštrukcia"))
        self.assertEqual((tr["qty"], tr["unit"], tr["cost_per_unit"]), (29.92, "kWp", 34.0))
        self.assertEqual((zat["qty"], zat["unit"], zat["cost_per_unit"]), (30.0, "kWp", 10.0))    # nie 59,92 kWp
        # nákup konštrukcie + záťaže: 30 × 80 + 29,92 × 34 + 30 × 10
        self.assertEqual(round(sum(i["total_cost"] for i in res["items"] if i["category"] == "Konštrukcia"), 2), 3717.28)
        self.assertEqual([k for k in kinds(res) if k.startswith("konstrukcia")], [])
        self.assertEqual(res["config"]["konstrukcia_mix"],
                         [{"typ_strechy": "vychod_zapad", "kwp": 30.0}, {"typ_strechy": "trapez", "kwp": 29.92}])
        self.assertEqual(res["config"]["typ_strechy"], "trapez")                    # vstupný typ_strechy sa neprepisuje

    def test_bez_vz_casti_nie_je_zatiaz(self):
        res = self.mix([("trapez", 30.0), ("juzna", 29.92)])
        self.assertEqual(self.kons_ids(res), ["konstrukcia.trapez", "konstrukcia.juzna"])
        self.assertEqual(by_rule(res, "zatiaz"), [])

    def test_tri_typy(self):
        res = self.mix([("trapez", 20.0), ("vychod_zapad", 20.0), ("juzna", 19.92)], typ_strechy="skridla")
        self.assertEqual(self.kons_ids(res),
                         ["konstrukcia.trapez", "konstrukcia.vychod_zapad", "konstrukcia.juzna", "zatiaz.vz"])
        self.assertEqual(one(res, "zatiaz.vz")["qty"], 20.0)

    def test_ostatne_polozky_nezavisia_od_typu_strechy(self):
        mixed = self.mix([("vychod_zapad", 30.0), ("trapez", 29.92)])
        plain = calc({"typ_strechy": "trapez"})

        def rest(res):
            return [(i["rule_id"], i["qty"], i["cost_per_unit"], i["price_per_unit"]) for i in res["items"]
                    if i["category"] != "Konštrukcia"]
        self.assertEqual(rest(mixed), rest(plain))
        self.assertEqual(mixed["totals"]["ac_kw_total"], plain["totals"]["ac_kw_total"])

    # --- bez poľa / jeden typ = dnešné správanie ---
    def test_jeden_typ_je_identicky_vystup_ako_bez_mixu(self):
        for typ in ("trapez", "vychod_zapad", "juzna", "zemne_skrutky", "skridla"):
            with self.subTest(typ=typ):
                expected = calc({"typ_strechy": typ})
                self.assertEqual(self.mix([(typ, self.KWP)], typ_strechy=typ), expected)
                # typ_strechy z jedinej položky nahradí typ z configu (aj keď chýba: default vychod_zapad)
                self.assertEqual(self.mix([(typ, self.KWP)], typ_strechy="falcovany_plech"), expected)
                self.assertEqual(self.mix([(typ, self.KWP)], drop=("typ_strechy",)), expected)

    def test_jeden_typ_ignoruje_kwp_casti(self):
        res = self.mix([("vychod_zapad", 10.0)])               # 10 kWp, ale ponuka má 59,92 kWp → cenia sa všetky
        self.assertEqual(res, calc({"typ_strechy": "vychod_zapad"}))
        self.assertEqual(one(res, "zatiaz.vz")["qty"], self.KWP)
        self.assertNotIn("konstrukcia_mix_kwp", kinds(res))

    def test_rovnaky_typ_viackrat_sa_zluci_na_jeden_typ(self):
        res = self.mix([("trapez", 30.0), (" TRAPEZ ", 29.92)], typ_strechy="vychod_zapad")
        self.assertEqual(res, calc({"typ_strechy": "trapez"}))

    def test_bez_pola_alebo_s_neplatnym_polom_je_dnesne_spravanie(self):
        expected = calc()
        junk = [None, "x", 5, {"kwp": 10}, {"typ_strechy": "", "kwp": 10}, {"typ_strechy": "trapez"},
                {"typ_strechy": "trapez", "kwp": 0}, {"typ_strechy": "trapez", "kwp": -3},
                {"typ_strechy": "trapez", "kwp": "abc"}, {"typ_strechy": "trapez", "kwp": None},
                {"typ_strechy": "trapez", "kwp": 0.004}]
        for raw in (None, [], {}, "", "trapez", 7, junk):
            with self.subTest(raw=str(raw)[:30]):
                self.assertEqual(calc({"konstrukcia_mix": raw}), expected)

    def test_neplatne_polozky_medzi_platnymi_sa_preskocia(self):
        valid = [{"typ_strechy": "vychod_zapad", "kwp": 30.0}, {"typ_strechy": "trapez", "kwp": 29.92}]
        expected = calc({"konstrukcia_mix": valid})
        self.assertEqual(calc({"konstrukcia_mix": [None, "x", {"kwp": 5}, {"typ_strechy": "trapez", "kwp": 0}] + valid}), expected)
        txt = [{"typ_strechy": "vychod_zapad", "kwp": "30,0"}, {"typ_strechy": "trapez", "kwp": "29.92"}]
        self.assertEqual(calc({"konstrukcia_mix": txt}), expected)
        self.assertEqual(self.mix([("  Vychod_Zapad ", 30.0), ("TRAPEZ", 29.92)]), expected)

    def test_dict_ako_jedina_polozka(self):
        res = calc({"konstrukcia_mix": {"typ_strechy": "vychod_zapad", "kwp": 12}})
        self.assertEqual(res, calc({"typ_strechy": "vychod_zapad"}))

    # --- súčet kWp ---
    def test_sucet_mimo_2_percent_prepocita_pomerne_s_info(self):
        res = self.mix([("vychod_zapad", 50.0), ("trapez", 30.0)])           # 80 kWp vs 59,92 kWp
        vz, tr = one(res, "konstrukcia.vychod_zapad")["qty"], one(res, "konstrukcia.trapez")["qty"]
        self.assertEqual((vz, tr), (37.45, 22.47))
        self.assertEqual(round(vz + tr, 2), self.KWP)
        self.assertEqual(one(res, "zatiaz.vz")["qty"], vz)
        w = warns(res, "konstrukcia_mix_kwp")
        self.assertEqual((len(w), w[0]["severity"], w[0]["kwp_mix"]), (1, "info", 80.0))
        self.assertIn("80", w[0]["message"])
        self.assertIn("59.92", w[0]["message"])
        self.assertEqual(res["config"]["konstrukcia_mix"],
                         [{"typ_strechy": "vychod_zapad", "kwp": 37.45}, {"typ_strechy": "trapez", "kwp": 22.47}])

    def test_zvysok_zaokruhlenia_ide_na_prvu_najvacsiu_cast(self):
        res = self.mix([("trapez", 30.0), ("vychod_zapad", 30.0), ("juzna", 30.0)], typ_strechy="skridla")
        q = [one(res, f"konstrukcia.{t}")["qty"] for t in ("trapez", "vychod_zapad", "juzna")]
        self.assertEqual(q, [19.98, 19.97, 19.97])                            # 3 × 19,97 + zvyšok 0,01
        self.assertEqual(round(sum(q), 2), self.KWP)                          # súčet je presne kWp ponuky
        self.assertEqual(one(res, "zatiaz.vz")["qty"], 19.97)

    def test_hranica_2_percent(self):
        for factor, expect_info in ((1.019, False), (1.021, True), (0.981, False), (0.979, True), (1.0, False)):
            with self.subTest(factor=factor):
                total = round(self.KWP * factor, 2)
                a = round(total / 2, 2)
                res = self.mix([("vychod_zapad", a), ("trapez", round(total - a, 2))])
                self.assertEqual(bool(warns(res, "konstrukcia_mix_kwp")), expect_info)
                qs = [one(res, "konstrukcia.vychod_zapad")["qty"], one(res, "konstrukcia.trapez")["qty"]]
                if expect_info:
                    self.assertEqual(round(sum(qs), 2), self.KWP)
                else:                                                         # v tolerancii sa berie tak, ako prišlo
                    self.assertEqual(qs, [a, round(total - a, 2)])

    # --- neznámy typ ---
    def test_neznamy_typ_sa_ocenuje_ako_typ_strechy_s_varovanim(self):
        res = self.mix([("vychod_zapad", 40.0), ("neexistuje", 19.92)], typ_strechy="trapez")
        self.assertEqual(self.kons_ids(res), ["konstrukcia.vychod_zapad", "konstrukcia.trapez", "zatiaz.vz"])
        self.assertEqual(one(res, "konstrukcia.trapez")["qty"], 19.92)         # kWp neznámeho typu → typ_strechy
        self.assertEqual(one(res, "zatiaz.vz")["qty"], 40.0)
        w = warns(res, "konstrukcia_mix_unknown")
        self.assertEqual((len(w), w[0]["severity"], w[0]["items"]), (1, "warning", ["neexistuje"]))
        self.assertIn("neexistuje", w[0]["message"])
        self.assertIn("'trapez'", w[0]["message"])
        self.assertEqual(warns(res, "konstrukcia_missing"), [])

    def test_neznamy_typ_pripocitany_k_vz_ide_aj_do_zatiaze(self):
        res = self.mix([("trapez", 20.0), ("neexistuje", 39.92)], typ_strechy="vychod_zapad")
        self.assertEqual(one(res, "konstrukcia.vychod_zapad")["qty"], 39.92)
        self.assertEqual(one(res, "zatiaz.vz")["qty"], 39.92)
        self.assertEqual(one(res, "konstrukcia.trapez")["qty"], 20.0)

    def test_neznamy_typ_pripocitany_k_typu_v_mixe(self):
        res = self.mix([("trapez", 20.0), ("juzna", 20.0), ("neexistuje", 19.92)], typ_strechy="trapez")
        self.assertEqual(one(res, "konstrukcia.trapez")["qty"], 39.92)
        self.assertEqual(one(res, "konstrukcia.juzna")["qty"], 20.0)

    def test_vsetky_typy_neznamy_je_jeden_typ_podla_typu_strechy(self):
        res = self.mix([("a", 30.0), ("b", 29.92)], typ_strechy="juzna")
        expected = calc({"typ_strechy": "juzna"})
        self.assertEqual(res["items"], expected["items"])
        self.assertEqual(res["totals"], expected["totals"])
        w = warns(res, "konstrukcia_mix_unknown")
        self.assertEqual((len(w), w[0]["items"]), (1, ["a", "b"]))
        self.assertEqual([x for x in res["warnings"] if x["kind"] != "konstrukcia_mix_unknown"], expected["warnings"])

    def test_corab_bez_pravidla_je_v_mixe_neznamy_typ(self):
        res = self.mix([("corab", 10.0), ("juzna", 49.92)], typ_strechy="trapez")
        self.assertEqual(one(res, "konstrukcia.trapez")["qty"], 10.0)
        self.assertEqual(warns(res, "konstrukcia_mix_unknown")[0]["items"], ["corab"])

    def test_neznamy_typ_aj_neznamy_typ_strechy_chyba_konstrukcia_len_tej_casti(self):
        res = self.mix([("trapez", 30.0), ("abc", 29.92)], typ_strechy="xyz")
        self.assertEqual(self.kons_ids(res), ["konstrukcia.trapez"])
        self.assertEqual(len(warns(res, "konstrukcia_mix_unknown")), 1)
        m = warns(res, "konstrukcia_missing")
        self.assertEqual(len(m), 1)
        self.assertIn("xyz", m[0]["message"])
        self.assertIn("29.92", m[0]["message"])

    def test_neaktivne_pravidlo_typu_je_neznamy_typ(self):
        rules = f0_rules()
        for r in rules:
            if r["rule_type"] == "konstrukcia" and r["rule_key"] == "juzna":
                r["active"] = False
        res = self.mix([("juzna", 30.0), ("vychod_zapad", 29.92)], typ_strechy="trapez", rules=rules)
        self.assertEqual(warns(res, "konstrukcia_mix_unknown")[0]["items"], ["juzna"])
        self.assertEqual(one(res, "konstrukcia.trapez")["qty"], 30.0)

    # --- ostatné ---
    def test_zemna_cast_hlasi_out_of_scope_raz(self):
        def ground(res):
            return [w for w in res["warnings"] if w["kind"] == "out_of_scope" and "Zemná" in w["message"]]
        self.assertEqual(len(ground(self.mix([("zemne_skrutky", 20.0), ("trapez", 39.92)]))), 1)
        # typ_strechy zo configu je zemný, ale žiadna časť strechy nie je → bez varovania
        self.assertEqual(ground(self.mix([("juzna", 20.0), ("trapez", 39.92)], typ_strechy="zemne_skrutky")), [])
        self.assertEqual(len(ground(self.mix([("zemne_skrutky", self.KWP)]))), 1)      # jediný zemný typ = dnešné správanie

    def test_stare_pravidla_na_kusy_rozdelia_panely_pomerne(self):
        rules = f0_rules()
        for r in rules:
            if r["rule_type"] == "konstrukcia":
                r.update(unit="ks", qty_formula="pocet_panelov")
        res = self.mix([("vychod_zapad", 30.0), ("trapez", 29.92)], rules=rules)
        vz, tr = one(res, "konstrukcia.vychod_zapad"), one(res, "konstrukcia.trapez")
        self.assertEqual((vz["unit"], tr["unit"]), ("ks", "ks"))
        self.assertEqual((vz["qty"], tr["qty"]), (56, 56))                    # 112 panelov podľa kWp častí
        self.assertEqual(one(res, "zatiaz.vz")["qty"], 30.0)                  # záťaž ostáva na kWp V-Z časti

    def test_chybajuce_pravidlo_zatiaze_je_varovanie(self):
        rules = [r for r in f0_rules() if r["rule_type"] != "zatiaz"]
        res = self.mix([("vychod_zapad", 30.0), ("trapez", 29.92)], rules=rules)
        self.assertEqual(by_rule(res, "zatiaz"), [])
        self.assertIn("missing_rule", kinds(res))

    def test_len_bess_ignoruje_mix(self):
        cfg = {"pocet_panelov": 0, "has_bess": True, "bess_class": "industrial", "vzdialenost_doprava": 150,
               "bess_kwh": 482, "bess_kabel_m": 40, "distribucka": "SSD"}
        res = calc({**cfg, "konstrukcia_mix": [{"typ_strechy": "trapez", "kwp": 10}, {"typ_strechy": "juzna", "kwp": 5}]})
        self.assertEqual(res, calc(cfg))
        self.assertNotIn("konstrukcia_mix", res["config"])

    def test_mix_s_override_menicov_spolu(self):
        res = calc({"vendor_stack": "huawei", "pocet_panelov": 300,
                    "inverters_override": [{"key": "sun2000_100ktl", "qty": 1}, {"key": "sun2000_30ktl", "qty": 2}],
                    "konstrukcia_mix": [{"typ_strechy": "vychod_zapad", "kwp": 100.5}, {"typ_strechy": "trapez", "kwp": 60}]})
        self.assertEqual(res["totals"]["inverters_source"], "override")
        self.assertEqual(one(res, "zatiaz.vz")["qty"], 100.5)
        self.assertEqual(res["totals"]["kwp"], 160.5)


# ----------------------------------------------------------------------------------------------
class TestPomocneFunkcieF3(unittest.TestCase):
    def test_split_int(self):
        self.assertEqual(eng._split_int(112, [30.0, 29.92]), [56, 56])
        self.assertEqual(eng._split_int(100, [1, 1, 1]), [34, 33, 33])
        self.assertEqual(eng._split_int(7, [1, 0, 1]), [4, 0, 3])
        self.assertEqual(eng._split_int(0, [1, 2]), [0, 0])
        self.assertEqual(eng._split_int(10, [0, 0]), [0, 0])
        for total, w in ((113, [3.3, 4.4, 5.5, 6.6]), (1, [1, 1, 1]), (1000, [0.1, 0.7, 12.0])):
            self.assertEqual(sum(eng._split_int(total, w)), total)

    def test_scale_mix_parts(self):
        out = eng._scale_mix_parts([("a", 50.0), ("b", 30.0)], 59.92)
        self.assertEqual(out, [("a", 37.45), ("b", 22.47)])
        out = eng._scale_mix_parts([("a", 1.0), ("b", 1.0), ("c", 1.0)], 10.0)          # 3 × 3,33 + zvyšok 0,01
        self.assertEqual((out, round(sum(k for _, k in out), 2)), ([("a", 3.34), ("b", 3.33), ("c", 3.33)], 10.0))
        out = eng._scale_mix_parts([("a", 100.0), ("b", 0.001)], 1.0)                    # nulová časť sa vynechá
        self.assertEqual(out, [("a", 1.0)])

    def test_parse_konstrukcia_mix(self):
        raw = [{"typ_strechy": "Trapez", "kwp": "10,5"}, {"typ_strechy": "juzna", "kwp": 5},
               {"typ_strechy": " trapez ", "kwp": 4.25}, {"typ_strechy": "x", "kwp": 0}]
        self.assertEqual(eng._parse_konstrukcia_mix(raw), [("trapez", 14.75), ("juzna", 5.0)])
        self.assertEqual(eng._parse_konstrukcia_mix(None), [])
        self.assertEqual(eng._parse_konstrukcia_mix("trapez"), [])

    def test_match_inverters_override(self):
        invs = [{"key": "A1", "code": "c-1", "name": "A"}, {"key": "b2", "code": None, "name": "B"}, {"name": "bez-key"}]
        picked, unknown, requested = eng._match_inverters_override(
            invs, [{"key": "a1", "qty": 2}, {"code": "C-1", "qty": "x"}, {"key": "B2"}, {"key": "zzz"}, 7])
        self.assertEqual([(p["inverter"]["key"], p["qty"]) for p in picked], [("A1", 3), ("b2", 1)])
        self.assertEqual((unknown, requested), (["zzz", "7"], True))
        self.assertEqual(eng._match_inverters_override(invs, None), ([], [], False))
        self.assertEqual(eng._match_inverters_override(invs, []), ([], [], False))


# ----------------------------------------------------------------------------------------------
# Regresia: bez nových polí je výstup rovnaký ako v origin/main pred Fázou 3
# ----------------------------------------------------------------------------------------------
# názov → (varianta pravidiel, prepis BASE). "ks" = staré pravidlá konštrukcie na kusy (podľa počtu panelov).
BASELINE_CASES = {
    "solinteg_trapez_112": ("f0", {}),
    "pon_26_1404_bess_112kwh": ("f0", {"has_bess": True, "bess_kwh": 112, "bess_count": 1, "bess_class": "industrial",
                                        "bess_sku": "solinteg_e2br_112r", "vzdialenost_doprava": 200, "margin_pct": 25.2}),
    "huawei_vz_optimizery_rs_wallbox": ("f0", {"vendor_stack": "huawei", "typ_strechy": "vychod_zapad", "pocet_panelov": 120,
                                               "has_optimizery": True, "has_rapid_shutdown": True, "has_wallbox": True,
                                               "wallbox_pocet": 2}),
    "sungrow_juzna_160kwp_ssd": ("f0", {"vendor_stack": "sungrow", "typ_strechy": "juzna", "pocet_panelov": 300,
                                        "distribucka": "SSD"}),
    "goodwe_skridla_bateria_rez": ("f0", {"vendor_stack": "goodwe", "typ_strechy": "skridla", "pocet_panelov": 56,
                                          "has_bess": True, "bess_kwh": 20, "bess_class": "residential"}),
    "huawei_falcovany_bateria_pocet": ("f0", {"vendor_stack": "huawei", "typ_strechy": "falcovany_plech", "pocet_panelov": 80,
                                              "has_bess": True, "bess_sku": "luna2000_5", "bess_count": 3,
                                              "bess_class": "residential"}),
    "solinteg_ci_482kwh": ("f0", {"has_bess": True, "bess_kwh": 482, "bess_class": "industrial", "distribucka": "ZSD"}),
    "sungrow_zemne_skrutky_500kwp": ("f0", {"vendor_stack": "sungrow", "typ_strechy": "zemne_skrutky", "pocet_panelov": 934,
                                            "distribucka": "VSD"}),
    "corab_bez_pravidla": ("f0", {"typ_strechy": "corab"}),
    "huawei_kwp_bez_typu_strechy": ("f0", {"vendor_stack": "huawei", "typ_strechy": None, "pocet_panelov": 0, "kwp": 59.92}),
    "len_bess_482": ("f0", {"pocet_panelov": 0, "has_bess": True, "bess_class": "industrial", "bess_kwh": 482,
                            "bess_kabel_m": 40, "distribucka": "SSD"}),
    "huawei_marza_4_bez_dopravy": ("f0", {"vendor_stack": "huawei", "pocet_panelov": 200, "margin_pct": 4,
                                          "vzdialenost_doprava": 0}),
    "sungrow_4000_panelov_fallback": ("f0", {"vendor_stack": "sungrow", "pocet_panelov": 4000}),
    "stare_pravidla_konstrukcia_na_kusy": ("ks", {"typ_strechy": "vychod_zapad", "pocet_panelov": 100}),
}


def _case_rules(variant):
    rules = f0_rules()
    if variant == "ks":
        for r in rules:
            if r["rule_type"] == "konstrukcia":
                r.update(unit="ks", qty_formula="pocet_panelov")
    return rules


def _run_case(module, variant, overrides):
    cfg = dict(BASE)
    cfg.update(overrides)
    return module.calculate_bom_v2(FakeSB(_case_rules(variant), load_stacks()), cfg)


def _project(res):
    """Výsledok v porovnateľnom JSON tvare (položky, súčty, varovania); nové pole totals.inverters_source sa vynecháva."""
    if not res.get("ok"):
        out = {"ok": False, "error": res.get("error"), "warnings": res["warnings"]}
    else:
        out = {"ok": True, "config": res["config"],
               "totals": {k: v for k, v in res["totals"].items() if k != "inverters_source"},
               "items": res["items"], "warnings": res["warnings"]}
    return json.loads(json.dumps(out))


def _dump_baseline(data):
    """JSON s jednou položkou na riadok (prehľadný diff)."""
    def j(v):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    out = ["{", f'  "_meta": {j(data["_meta"])},', '  "cases": {']
    names = list(data["cases"])
    for ci, name in enumerate(names):
        case = data["cases"][name]
        out.append(f"    {j(name)}: {{")
        out.append(f'      "variant": {j(case["variant"])},')
        out.append(f'      "overrides": {j(case["overrides"])},')
        out.append('      "result": {')
        res = case["result"]
        for ki, key in enumerate(res):
            comma = "," if ki < len(res) - 1 else ""
            if isinstance(res[key], list):
                out.append(f'        "{key}": [')
                out += [f"          {j(row)}" + ("," if ri < len(res[key]) - 1 else "") for ri, row in enumerate(res[key])]
                out.append("        ]" + comma)
            else:
                out.append(f'        "{key}": {j(res[key])}{comma}')
        out.append("      }")
        out.append("    }" + ("," if ci < len(names) - 1 else ""))
    out += ["  }", "}"]
    return "\n".join(out) + "\n"


def _regen_baseline(module_path=None, source=None):
    if module_path:
        spec = importlib.util.spec_from_file_location("b2b_calculator_v2_baseline_src", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = eng
    data = {"_meta": {"zdroj": source or ("aktuálne jadro" if not module_path else module_path),
                      "popis": "Výstup calculate_bom_v2 BEZ nových polí (FakeSB: f0_rules + b2b_vendor_stacks.json); "
                               "totals.inverters_source sa neporovnáva. Generuje: python3 tests/test_b2b_planovac.py --regen-baseline"},
            "cases": {}}
    for name, (variant, overrides) in BASELINE_CASES.items():
        data["cases"][name] = {"variant": variant, "overrides": overrides,
                               "result": _project(_run_case(module, variant, overrides))}
    text = _dump_baseline(data)
    assert json.loads(text) == json.loads(json.dumps(data)), "baseline sa nedá načítať späť rovnako"
    with open(BASELINE, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"Zapísané: {BASELINE} ({len(text)} B, {len(data['cases'])} prípadov, zdroj: {data['_meta']['zdroj']})")


class TestBezNovychPoli(unittest.TestCase):
    """Bez nových polí je výstup rovnaký ako v origin/main (f752509) pred Fázou 3 — položky aj súčty."""

    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(read_text(BASELINE))

    def test_baseline_pokryva_vsetky_pripady(self):
        self.assertEqual(set(self.base["cases"]), set(BASELINE_CASES))
        for name, (variant, overrides) in BASELINE_CASES.items():
            self.assertEqual((self.base["cases"][name]["variant"], self.base["cases"][name]["overrides"]),
                             (variant, json.loads(json.dumps(overrides))), name)

    def test_vystup_je_rovnaky_ako_v_origin_main(self):
        for name, (variant, overrides) in BASELINE_CASES.items():
            with self.subTest(name):
                got = _project(_run_case(eng, variant, overrides))
                want = self.base["cases"][name]["result"]
                self.assertEqual(got.get("ok"), want.get("ok"), REGEN_HINT)
                self.assertEqual(got.get("totals"), want.get("totals"), "súčty. " + REGEN_HINT)
                self.assertEqual(got.get("items"), want.get("items"), "položky. " + REGEN_HINT)
                self.assertEqual(got.get("warnings"), want.get("warnings"), "varovania. " + REGEN_HINT)
                self.assertEqual(got, want, REGEN_HINT)

    def test_baseline_ma_zmysluplny_obsah(self):
        ok = [c for c in self.base["cases"].values() if c["result"]["ok"]]
        self.assertGreaterEqual(len(ok), 10)
        self.assertGreaterEqual(sum(len(c["result"]["items"]) for c in ok), 200)
        self.assertTrue(any(c["result"]["totals"]["total_price"] > 100000 for c in ok))

    def test_neutralne_nove_polia_nic_nemenia(self):
        neutral = ({"inverters_override": []}, {"konstrukcia_mix": []}, {"inverters_override": None, "konstrukcia_mix": None},
                   {"inverters_override": [], "konstrukcia_mix": {}})
        for name, (variant, overrides) in BASELINE_CASES.items():
            expected = _run_case(eng, variant, overrides)
            for extra in neutral:
                with self.subTest(name=name, extra=extra):
                    self.assertEqual(_run_case(eng, variant, {**overrides, **extra}), expected)

    def test_totals_ma_len_jedno_nove_pole(self):
        for name, (variant, overrides) in BASELINE_CASES.items():
            res = _run_case(eng, variant, overrides)
            if res["ok"]:
                self.assertEqual(res["totals"]["inverters_source"], "auto", name)
                self.assertEqual(set(res["totals"]) - set(self.base["cases"][name]["result"]["totals"]),
                                 {"inverters_source"}, name)
                self.assertNotIn("konstrukcia_mix", res["config"], name)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--regen-baseline":
        _regen_baseline(sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        unittest.main(verbosity=2)
