"""Testy nočnej synchronizácie cenníka Raynet -> B2B kalkulačka (Fáza 2) — stdlib unittest, bez siete a bez Flasku.

Spustenie z koreňa repa:
    python3 -m unittest discover -s tests -p 'test_raynet*.py' -v

HTTP nahrádza FakeSession (zoznam volaní, povolené je iba GET), Supabase nahrádza FakeSB (in-memory tabuľky,
zapisuje všetky zápisy do `writes`; `delete` neexistuje, takže ho nemožno zavolať). Test registrácie cez Flask sa
preskočí, ak Flask nie je nainštalovaný.
"""
import copy
import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests  # noqa: E402

import raynet_price_sync as rps  # noqa: E402

NOW = datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)
SECRET_KEY = "tajny-api-kluc-123"
SECRET_USER = "sync.bot@example.test"
ENV_OK = {"RAYNET_INSTANCE": "energovision-test", "RAYNET_USER": SECRET_USER, "RAYNET_API_KEY": SECRET_KEY,
          "WEBHOOK_SECRET": "x-webhook"}


# ----------------------------------------------------------------------------------------------------------------
# FakeSB — podmnožina supabase-py používaná modulom + záznam zápisov
# ----------------------------------------------------------------------------------------------------------------
def _eq(a, b):
    if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
        return a == b
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:  # noqa: BLE001
        return a == b


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.cols, self.values, self.rows = "select", "*", None, None
        self.filters, self._order, self._range, self.on_conflict = [], None, None, None

    def select(self, cols="*"):
        self.op, self.cols = "select", cols
        return self

    def update(self, values):
        self.op, self.values = "update", values
        return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.rows, self.on_conflict = "upsert", rows, on_conflict
        return self

    def insert(self, rows):
        self.op, self.rows = "insert", rows
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def is_(self, col, val):          # PostgREST `is.null`
        assert val == "null"
        self.filters.append((col, None))
        return self

    def order(self, col, desc=False, **_k):
        self._order = col
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def _match(self, row):
        return all(_eq(row.get(c), v) for c, v in self.filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "select":
            out = [copy.deepcopy(r) for r in rows if self._match(r)]
            if self._order:
                out.sort(key=lambda r: (r.get(self._order) is None, r.get(self._order)))
            if self._range:
                out = out[self._range[0]: self._range[1] + 1]
            if self.cols != "*":
                keys = [c.strip() for c in self.cols.split(",")]
                out = [{k: r.get(k) for k in keys} for r in out]
            return _Resp(out)
        if self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(copy.deepcopy(self.values))
            self.db.writes.append(("update", self.table, list(self.filters), copy.deepcopy(self.values), len(hit)))
            return _Resp([copy.deepcopy(r) for r in hit])
        if self.op == "upsert":
            batch = self.rows if isinstance(self.rows, list) else [self.rows]
            for new in batch:
                cur = next((r for r in rows if r.get(self.on_conflict) == new.get(self.on_conflict)), None)
                if cur is None:
                    rows.append(copy.deepcopy(new))
                else:
                    cur.update(copy.deepcopy(new))
            self.db.writes.append(("upsert", self.table, self.on_conflict, len(batch)))
            return _Resp(copy.deepcopy(batch))
        if self.op == "insert":
            batch = self.rows if isinstance(self.rows, list) else [self.rows]
            out = []
            for new in batch:
                r = copy.deepcopy(new)
                r.setdefault("id", f"{self.table}-{len(rows) + 1}")
                rows.append(r)
                out.append(copy.deepcopy(r))
            self.db.writes.append(("insert", self.table, len(batch)))
            return _Resp(out)
        raise AssertionError(self.op)


class FakeSB:
    def __init__(self, tables):
        self.tables = copy.deepcopy(tables)
        self.writes = []

    def table(self, name):
        return _Query(self, name)


# ----------------------------------------------------------------------------------------------------------------
# FakeSession — Raynet HTTP (iba GET)
# ----------------------------------------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code, self._payload, self.headers, self.text = status, payload, headers or {}, text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def paged(rows):
    def handler(params):
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 1000))
        return FakeResponse(200, {"success": True, "totalCount": len(rows), "data": copy.deepcopy(rows[off: off + lim])},
                            {"X-Ratelimit-Remaining": "23990"})
    return handler


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, auth=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers or {}),
                           "auth": auth, "timeout": timeout})
        path = url.split("/api/v2", 1)[1]
        handler = self.routes.get(path)
        if handler is None:
            return FakeResponse(404, {"success": False}, text="not found")
        if callable(handler) and not isinstance(handler, FakeResponse):
            return handler(params or {})
        return handler

    def __getattr__(self, name):    # post / put / delete / patch / request — do Raynetu sa nesmie zapisovať
        if name in ("post", "put", "delete", "patch", "request"):
            raise AssertionError(f"pokus o zápis do Raynetu: {name}")
        raise AttributeError(name)


# ----------------------------------------------------------------------------------------------------------------
# Fixtúry
# ----------------------------------------------------------------------------------------------------------------
EUR = {"id": 16, "value": "€"}
PRICELISTS = [
    {"id": 1, "code": "Výchozí", "name": "Výchozí", "primary": True, "currency": EUR},
    {"id": 10, "code": "LZ-HU", "name": "LZ-HU", "primary": False, "currency": EUR},
    {"id": 11, "code": "LZ-SG", "name": "LZ-SG", "primary": False, "currency": EUR},
]


def prod(pid, code, price, cost, unit="ks", valid_till=None, name=None):
    return {"id": pid, "code": code, "name": name or f"Produkt {code}", "unit": unit, "price": price, "cost": cost, "taxRate": 23,
            "validFrom": "2023-12-16", "validTill": valid_till, "category": {"id": 170, "value": "Komponenty"},
            "productLine": {"id": 173, "value": "Striedače"},
            "primaryPriceListItem": {"id": pid * 10, "price": price, "priceList": {"id": 1, "currency": "EUR"}}}


def item(item_id, pid, code, price, cost):
    return {"id": item_id, "product": {"id": pid, "code": code, "name": code}, "name": code, "price": price, "cost": cost, "unit": "ks"}


def raw(rid, code, price, cost):
    return {"raynet_id": rid, "code": code, "name": f"Produkt {code}", "unit": "ks", "price": price, "cost": cost,
            "raw_json": {"id": rid, "code": code}, "fetched_at": "2026-05-24T21:02:46+00:00"}


def rule(rid, rtype, rkey, cost, price, notes, unit="kWp", active=True, pid=None):
    return {"id": rid, "rule_type": rtype, "rule_key": rkey, "raynet_product_id": pid, "unit": unit, "cost_per_unit": cost,
            "price_per_unit": price, "active": active, "notes": notes}


def product_row(pid, sku, purchase, sale, unit="ks", active=True):
    return {"id": pid, "sku": sku, "unit": unit, "purchase_price": purchase, "sale_price": sale, "is_active": active}


def raynet_products():
    return [
        prod(1, "MONT", 100, 75, "kWp"),          # LZ-HU: nákup 74 (iný než produkt 75)
        prod(2, "VODAC", 26, 20, "kWp"),          # cenník 24 -> 26
        prod(3, "R50", 3250, 3500, "ks"),         # nákup 2500 -> 3500 (+40 %)
        prod(4, "HUA30", 2513.5, 1863.3, "ks"),   # LZ-HU: nákup 2010.8 (produkt má starší 1863.3)
        prod(5, "SG33", 1834.1, 1528.42, "ks"),   # starý model; V21 má vlastný nákup 1755
        prod(6, "LONGI535", 100, 72.55, "ks"),
        prod(7, "LUNA241", 66248.75, 55999, "ks"),
        prod(8, "TIGOTS4", 38.5, 29, "ks"),
        prod(9, "PD20", 1040, 800, "kpl"),
        prod(10, "HUASMRTDNGL", 55, 41, "ks"),
        prod(11, "ZEROCOST", 100, 0, "ks"),       # nákup 0 = nezadané
        prod(12, "OLD", 10, 5, "ks", valid_till="2000-01-01"),
        prod(13, "HUA3PRICEODD", 3034.625, 2427.7, "ks"),
    ]


def lzhu_items():
    return [item(101, 1, "MONT", 100, 74), item(104, 4, "HUA30", 2513.5, 2010.8), item(113, 13, "HUA3PRICEODD", 3034.625, 2427.7)]


def vychozi_items():
    out = [item(1000 + p["id"], p["id"], p["code"], p["price"], p["cost"]) for p in raynet_products()]
    return out


def previous_snapshot():
    """Posledný stav katalógu (raynet_raw_products 05/2026) — základ trojcestného zlučovania."""
    return [raw(1, "MONT", 100, 70), raw(2, "VODAC", 24, 20), raw(3, "R50", 3250, 2500), raw(4, "HUA30", 2235.96, 1863.3),
            raw(5, "SG33", 1834.1, 1528.42), raw(6, "LONGI535", 90.69, 72.55), raw(7, "LUNA241", 66248.75, 55999),
            raw(8, "TIGOTS4", 37.7, 29), raw(9, "PD20", 1040, 800), raw(10, "HUASMRTDNGL", 51.25, 41),
            raw(11, "ZEROCOST", 80, 50), raw(13, "HUA3PRICEODD", 3034.63, 2427.7)]


def rules_rows():
    return [
        rule("r1", "montaz", "do_30", 70, 100, "Raynet LZ-HU 10/2026, kód MONT; pásmo podľa kWp"),
        rule("r2", "vodice", "ac", 20, 24, "Raynet 10/2026, kód VODAC"),
        rule("r3", "rozvadzac", "R50", 2500, 3250, "Raynet LZ-HU 10/2026, kód R50; pásmo podľa AC kW (30.01–50 kW)", unit="ks"),
        rule("r4", "montaz", "do_100", 72, 100, "Raynet LZ-HU 10/2026, kód MONT; pásmo podľa kWp"),
        rule("r5", "montaz", "do_250", 70, 100, "kód MONT", active=False),
        rule("r6", "panel", "longi_430", None, 98.93, "430 Wp pre malé/stredné", unit="ks"),
        rule("r7", "montaz", "do_500", 70, 100, "Raynet LZ-HU 10/2026, kód MONT [sync:off]"),
        rule("r8", "montaz", "nad_500", 70, 100, "Raynet LZ-HU 10/2026, kód MONT", unit="ks"),
        rule("r9", "ostatne", "x", 1, 2, "Raynet 10/2026, kód NEEXISTUJE"),
    ]


def stacks_rows():
    return [{
        "id": "s1", "vendor_key": "huawei", "display_name": "Huawei",
        "preferred_panels": [{"sku": "LONGI535", "name": "LONGi 535", "wp": 535, "cost": 72.55, "price": 90.69, "price_per_unit": 90.69}],
        "inverters": [
            {"key": "sun2000_30k_mc0", "code": "HUA30", "cost": 2010.8, "price": 2513.5, "source": "Raynet 10/2026 HUA30"},
            {"key": "sg33cx_p2_v21", "code": "SG33", "cost": 1755, "price": 1834.1, "source": "Raynet 10/2026 SG33V21"},
            {"key": "sun2000_30ktl", "legacy": True, "code": "HUA30", "cost": 1923.1, "price": 2513.5},
        ],
        "optimizers": [{"key": "tigo_ts4", "code": "TIGOTS4", "cost": 29, "price": 37.7, "price_per_panel": 37.7, "panels_per_unit": 1}],
        "batteries": [{"key": "luna2000_241_2s1", "code": "LUNA241", "cost": 40000, "price": 50000, "source": "Raynet 08–10/2026 LUNA241"}],
        "wallboxes": [], "accessories": [],
        "smart_manager": {"key": "huawei_dongle", "code": "HUASMRTDNGL", "cost": 41, "price": 51.25,
                          "price_note": "cenník neznámy → nákup × 1,25", "source": "Raynet raw 05/2026 HUASMRTDNGL"},
        "smart_meter": {"key": "meter_bez_kodu", "cost": 99, "price": 123.75},
        "smart_manager_large": None,
    }]


def products_rows():
    return [product_row("p1", "PD20", 250, 350, "kpl"), product_row("p2", "VODAC", 20, 24, "kWp"),
            product_row("p3", "MONT", 70, 100, "kWp", active=False), product_row("p4", "NIECOVRAYNETE", 1, 2),
            product_row("p5", "ZEROCOST", 50, 80, "ks")]


def make_db():
    return FakeSB({"b2b_calc_rules": rules_rows(), "b2b_vendor_stacks": stacks_rows(), "products": products_rows(),
                   "raynet_raw_products": previous_snapshot(), "raynet_import_log": []})


def make_session(pricelists=None, products=None, lzhu=None, vychozi=None):
    return FakeSession({
        "/priceList/": paged(PRICELISTS if pricelists is None else pricelists),
        "/priceList/10/items/": paged(lzhu_items() if lzhu is None else lzhu),
        "/priceList/1/items/": paged(vychozi_items() if vychozi is None else vychozi),
        "/product/": paged(raynet_products() if products is None else products),
    })


def run(args=None, db=None, session=None, env=None, **kw):
    db = db or make_db()
    session = session or make_session()
    status, body = rps.handle_request(args or {}, env=ENV_OK if env is None else env, get_sb=lambda: db, session=session,
                                      sleep=lambda s: None, now=NOW, **kw)
    return status, body, db, session


def find(body, table, ref, field, key="changes"):
    for e in body[key]:
        if e["table"] == table and e["ref"] == ref and e["field"] == field:
            return e
    return None


def row(db, table, rid):
    return next(r for r in db.tables[table] if r["id"] == rid)


# ----------------------------------------------------------------------------------------------------------------
# Pomocné funkcie
# ----------------------------------------------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):
    def test_zaokruhlenie_half_up(self):
        self.assertEqual(rps.money(3034.625), Decimal("3034.63"))
        self.assertEqual(rps.money(2.675), Decimal("2.68"))
        self.assertEqual(rps.money("1 523,455".replace(" ", "")), Decimal("1523.46"))

    def test_nula_a_zaporne_je_nezadane(self):
        self.assertIsNone(rps.positive(0))
        self.assertIsNone(rps.positive("0.0"))
        self.assertIsNone(rps.positive(-5))
        self.assertIsNone(rps.positive(None))
        self.assertEqual(rps.positive(5), Decimal("5.00"))

    def test_kod_z_notes(self):
        f = rps.code_from_notes
        self.assertEqual(f("Raynet LZ-HU 10/2026, kód R50; pásmo podľa AC kW (30.01–50 kW)"), "R50")
        self.assertEqual(f("Raynet 10/2026, kód VODAC"), "VODAC")
        self.assertEqual(f("Raynet 10/2026, kód KONŠ; €/kWp (pôv. na panel)"), "KONŠ")
        self.assertEqual(f("Raynet 10/2026, kód R-DC; podmienka has_dc_rozvadzac"), "R-DC")
        self.assertEqual(f("Raynet 10/2026, kód AXYZSDIS; distribučka ZSD"), "AXYZSDIS")
        self.assertIsNone(f("Pre veľké BESS"))
        self.assertIsNone(f(None))

    def test_jednotky(self):
        self.assertEqual(rps.norm_unit("kWp"), rps.norm_unit("kwp"))
        self.assertEqual(rps.norm_unit("kus"), rps.norm_unit("ks"))
        self.assertEqual(rps.norm_unit("komplet"), rps.norm_unit("kpl"))
        self.assertNotEqual(rps.norm_unit("ks"), rps.norm_unit("kWp"))

    def test_json_cisla(self):
        self.assertIsInstance(rps.num_json(Decimal("40000.00")), int)
        self.assertEqual(rps.num_json(Decimal("2513.50")), 2513.5)
        self.assertIsNone(rps.num_json(None))

    def test_parse_targets(self):
        self.assertEqual(rps.parse_targets(None), rps.ALL_TARGETS)
        self.assertEqual(rps.parse_targets("rules, produkty"), ("rules", "products"))
        with self.assertRaises(ValueError):
            rps.parse_targets("rules,nieco")


# ----------------------------------------------------------------------------------------------------------------
# Konfigurácia a vstupy endpointu
# ----------------------------------------------------------------------------------------------------------------
class TestConfigAndParams(unittest.TestCase):
    def test_chybajuce_env_je_503_so_spravou_co_nastavit(self):
        status, body, db, session = run(env={"WEBHOOK_SECRET": "x"})
        self.assertEqual(status, 503)
        self.assertEqual(body["missing_env"], ["RAYNET_INSTANCE", "RAYNET_USER", "RAYNET_API_KEY"])
        for name in ("RAYNET_INSTANCE", "RAYNET_USER", "RAYNET_API_KEY", "energovision-cp-generator"):
            self.assertIn(name, body["error"])
        self.assertEqual(session.calls, [])
        self.assertEqual(db.writes, [])

    def test_ciastocne_env_vymenuje_len_chybajuce(self):
        status, body, _db, _s = run(env={"RAYNET_INSTANCE": "i", "RAYNET_USER": "u", "WEBHOOK_SECRET": "x"})
        self.assertEqual(status, 503)
        self.assertEqual(body["missing_env"], ["RAYNET_API_KEY"])

    def test_alias_nazvov_premennych(self):
        cfg = rps.load_config({"RAYNET_INSTANCE": "i", "RAYNET_USERNAME": "u", "RAYNET_KEY": "k"})
        self.assertEqual((cfg["instance"], cfg["user"], cfg["key"]), ("i", "u", "k"))

    def test_503_neobsahuje_tajomstva(self):
        status, body, _db, _s = run(env={"RAYNET_INSTANCE": "inst-x", "RAYNET_USER": SECRET_USER, "WEBHOOK_SECRET": "x"})
        self.assertNotIn(SECRET_USER, str(body))

    def test_apply_aj_dry_run_naraz_je_400(self):
        status, body, db, _s = run({"apply": "1", "dry_run": "1"})
        self.assertEqual(status, 400)
        self.assertEqual(db.writes, [])

    def test_dry_run_nula_bez_apply_je_400(self):
        status, body, _db, _s = run({"dry_run": "0"})
        self.assertEqual(status, 400)
        self.assertIn("apply=1", body["error"])

    def test_neplatny_cielovy_zoznam_a_max_changes_je_400(self):
        self.assertEqual(run({"targets": "blabla"})[0], 400)
        self.assertEqual(run({"apply": "1", "max_changes": "0"})[0], 400)
        self.assertEqual(run({"apply": "1", "max_changes": "abc"})[0], 400)

    def test_apply_bez_webhook_secret_je_503_a_nic_sa_nevola(self):
        env = {k: v for k, v in ENV_OK.items() if k != "WEBHOOK_SECRET"}
        status, body, db, session = run({"apply": "1"}, env=env)
        self.assertEqual(status, 503)
        self.assertIn("WEBHOOK_SECRET", body["error"])
        self.assertEqual(session.calls, [])
        self.assertEqual(db.writes, [])

    def test_aj_dry_run_bez_webhook_secret_je_503_lebo_odpoved_nesie_nakupne_ceny(self):
        env = {k: v for k, v in ENV_OK.items() if k != "WEBHOOK_SECRET"}
        status, body, db, session = run({}, env=env)
        self.assertEqual(status, 503)
        self.assertIn("WEBHOOK_SECRET", body["error"])
        self.assertEqual(session.calls, [])

    def test_cli_nepotrebuje_webhook_secret(self):
        env = {k: v for k, v in ENV_OK.items() if k != "WEBHOOK_SECRET"}
        status, _body, _db, _s = run({}, env=env, require_webhook_secret=False)
        self.assertEqual(status, 200)


# ----------------------------------------------------------------------------------------------------------------
# Raynet klient
# ----------------------------------------------------------------------------------------------------------------
class TestClient(unittest.TestCase):
    CFG = {"instance": "energovision-test", "user": SECRET_USER, "key": SECRET_KEY}

    def client(self, session, **kw):
        return rps.RaynetClient(self.CFG, session=session, sleep=lambda s: None, **kw)

    def test_len_get_s_basic_autentifikaciou_a_hlavickou_instancie(self):
        s = make_session()
        c = self.client(s)
        c.get("product", {"limit": 5})
        call = s.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://app.raynet.cz/api/v2/product/")
        self.assertEqual(call["auth"], (SECRET_USER, SECRET_KEY))
        self.assertEqual(call["headers"]["X-Instance-Name"], "energovision-test")
        self.assertEqual(c.rate_remaining, 23990)
        for forbidden in ("post", "put", "delete", "patch"):
            self.assertFalse(hasattr(rps.RaynetClient, forbidden))

    def test_strankovanie(self):
        rows = [prod(i, f"C{i}", 1, 1) for i in range(1, 6)]
        s = FakeSession({"/product/": paged(rows)})
        out = self.client(s).pages("product", page_size=2)
        self.assertEqual([r["id"] for r in out], [1, 2, 3, 4, 5])
        self.assertEqual([c["params"]["offset"] for c in s.calls], [0, 2, 4])
        self.assertTrue(all(c["params"]["limit"] == 2 for c in s.calls))

    def test_429_sa_zopakuje_raz(self):
        answers = [FakeResponse(429, {}, text="limit"), FakeResponse(200, {"success": True, "data": [], "totalCount": 0})]
        s = FakeSession({"/product/": lambda p: answers.pop(0)})
        self.assertEqual(self.client(s).pages("product"), [])
        self.assertEqual(len(s.calls), 2)

    def test_dvakrat_429_je_chyba(self):
        s = FakeSession({"/product/": lambda p: FakeResponse(429, {}, text="limit")})
        with self.assertRaises(rps.RaynetError) as cm:
            self.client(s).pages("product")
        self.assertEqual(cm.exception.status, 429)

    def test_401_sprava_bez_tajomstiev(self):
        s = FakeSession({"/product/": lambda p: FakeResponse(401, {"message": SECRET_KEY}, text=SECRET_KEY)})
        with self.assertRaises(rps.RaynetError) as cm:
            self.client(s).pages("product")
        self.assertNotIn(SECRET_KEY, str(cm.exception))
        self.assertNotIn(SECRET_USER, str(cm.exception))
        self.assertIn("RAYNET_API_KEY", str(cm.exception))

    def test_5xx_sa_opakuje_a_potom_chyba(self):
        s = FakeSession({"/product/": lambda p: FakeResponse(503, {}, text="down")})
        with self.assertRaises(rps.RaynetError):
            self.client(s).pages("product")
        self.assertEqual(len(s.calls), 3)

    def test_sietova_chyba(self):
        class Boom:
            def get(self, *a, **k):
                raise requests.ConnectionError(f"spojenie s {SECRET_KEY} zlyhalo")
        with self.assertRaises(rps.RaynetError) as cm:
            self.client(Boom()).pages("product")
        self.assertNotIn(SECRET_KEY, str(cm.exception))

    def test_neplatny_tvar_odpovede(self):
        s = FakeSession({"/product/": lambda p: FakeResponse(200, {"success": True})})
        with self.assertRaises(rps.RaynetError):
            self.client(s).pages("product")


# ----------------------------------------------------------------------------------------------------------------
# Cenníky a efektívne hodnoty
# ----------------------------------------------------------------------------------------------------------------
class TestCatalog(unittest.TestCase):
    def test_vyber_cennikov_podla_kodu_nazvu_a_primarneho(self):
        sel, warn = rps.resolve_pricelists(PRICELISTS, ("LZ-HU", "Výchozí"))
        self.assertEqual([s["id"] for s in sel], [10, 1])
        self.assertEqual(warn, [])
        lists = [{"id": 5, "code": "X", "name": "Základný cenník", "primary": True, "currency": EUR},
                 {"id": 6, "code": "L1", "name": "Cenník LZ-HU (Huawei)", "primary": False, "currency": EUR}]
        sel, warn = rps.resolve_pricelists(lists, ("lz-hu", "Vychozi"))
        self.assertEqual([s["id"] for s in sel], [6, 5])     # názov obsahuje token; Výchozí -> primárny

    def test_ine_id_cennika_ako_v_ponukach_je_varovanie(self):
        lists = [dict(PRICELISTS[0], id=7), dict(PRICELISTS[1], id=10)]
        sel, warn = rps.resolve_pricelists(lists, ("LZ-HU", "Výchozí"))
        self.assertEqual([s["id"] for s in sel], [10, 7])
        self.assertEqual(len(warn), 1)
        self.assertIn("id 7", warn[0])
        self.assertEqual(rps.resolve_pricelists(PRICELISTS, ("LZ-HU", "Výchozí"))[1], [])

    def test_chybajuci_cennik_je_varovanie_a_nie_eur_sa_vynecha(self):
        sel, warn = rps.resolve_pricelists([PRICELISTS[0], dict(PRICELISTS[1], currency="CZK")], ("LZ-HU", "Výchozí", "NEEXISTUJE"))
        self.assertEqual([s["id"] for s in sel], [1])
        self.assertEqual(len(warn), 2)

    def test_efektivna_hodnota_lzhu_potom_vychozi_potom_produkt(self):
        sel, _ = rps.resolve_pricelists(PRICELISTS, ("LZ-HU", "Výchozí"))
        cat = rps.build_catalog(raynet_products(), sel, {"LZ-HU": [rps_item for rps_item in lzhu_items()],
                                                         "Výchozí": [i for i in vychozi_items() if i["product"]["code"] != "VODAC"]})
        mont = cat.by_code["MONT"]
        self.assertEqual((mont.cost, mont.cost_src), (Decimal("74.00"), "LZ-HU"))      # LZ-HU má prednosť pred produktom (75)
        self.assertEqual((mont.price, mont.price_src), (Decimal("100.00"), "LZ-HU"))
        hua = cat.by_code["HUA30"]
        self.assertEqual(hua.cost, Decimal("2010.80"))                                 # nie 1863.3 z produktu
        r50 = cat.by_code["R50"]
        self.assertEqual((r50.cost_src, r50.price_src), ("Výchozí", "Výchozí"))
        vodac = cat.by_code["VODAC"]                                                   # nie je v žiadnom zozname -> produkt
        self.assertEqual((vodac.price, vodac.price_src), (Decimal("26.00"), "produkt"))

    def test_nulovy_nakup_sa_ignoruje(self):
        sel, _ = rps.resolve_pricelists(PRICELISTS, ("LZ-HU", "Výchozí"))
        cat = rps.build_catalog(raynet_products(), sel, {"LZ-HU": [], "Výchozí": vychozi_items()})
        z = cat.by_code["ZEROCOST"]
        self.assertIsNone(z.cost)
        self.assertEqual(z.price, Decimal("100.00"))

    def test_konflikty_medzi_cennikmi(self):
        sel, _ = rps.resolve_pricelists(PRICELISTS, ("LZ-HU", "Výchozí"))
        cat = rps.build_catalog(raynet_products(), sel, {"LZ-HU": lzhu_items(), "Výchozí": vychozi_items()})
        self.assertEqual(cat.by_code["MONT"].conflicts(), {"cost": {"LZ-HU": 74, "Výchozí": 75}})
        self.assertEqual(cat.by_code["HUA30"].conflicts(), {"cost": {"LZ-HU": 2010.8, "Výchozí": 1863.3}})
        self.assertEqual(cat.by_code["HUA3PRICEODD"].conflicts(), {})      # 3034,625 vs 3034,63 po zaokrúhlení rovnaké

    def test_neplatny_produkt_a_veľkost_pismen(self):
        sel, _ = rps.resolve_pricelists(PRICELISTS, ("LZ-HU", "Výchozí"))
        cat = rps.build_catalog(raynet_products(), sel, {"LZ-HU": [], "Výchozí": []})
        self.assertIn("neplatný", cat.by_code["OLD"].invalid_reason)
        self.assertIs(cat.lookup("longi535")[0], cat.by_code["LONGI535"])
        self.assertIsNone(cat.lookup("NIEJE")[0])

    def test_duplicitny_kod_je_nejednoznacny(self):
        sel, _ = rps.resolve_pricelists(PRICELISTS, ("LZ-HU",))
        cat = rps.build_catalog([prod(1, "DUP", 1, 1), prod(2, "DUP", 2, 1)], sel, {"LZ-HU": []})
        self.assertEqual(cat.lookup("DUP"), (None, "ambiguous_code"))


# ----------------------------------------------------------------------------------------------------------------
# Beh: dry-run, apply, poistky
# ----------------------------------------------------------------------------------------------------------------
class TestDryRun(unittest.TestCase):
    def test_dry_run_nic_nezapise(self):
        before = make_db()
        status, body, db, session = run({})
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "dry_run")
        self.assertEqual(db.writes, [])
        self.assertEqual(db.tables, before.tables)                 # ani raynet_raw_products, ani log
        self.assertEqual(db.tables["raynet_import_log"], [])
        self.assertTrue(all(c["method"] == "GET" for c in session.calls))
        self.assertGreater(body["would_apply"], 0)
        self.assertEqual(body["applied"], 0)

    def test_odpoved_je_json_serializovatelna(self):
        _status, body, _db, _s = run({})
        json.dumps(body)

    def test_align_v_dry_run_je_simulacia_bez_zapisu(self):
        status, body, db, _s = run({"align": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(db.writes, [])
        e = find(body, "b2b_calc_rules", "montaz/do_100", "cost")          # v bežnom dry-run je diverged
        self.assertEqual((e["current"], e["raynet"]), (72, 74))
        self.assertGreater(body["would_apply"], run({})[1]["would_apply"])

    def test_dry_run_plan_obsahuje_zmeny_aj_zablokovane(self):
        _status, body, _db, _s = run({})
        mont = find(body, "b2b_calc_rules", "montaz/do_30", "cost")
        self.assertEqual((mont["current"], mont["raynet"], mont["change_pct"], mont["source"]), (70, 74, 5.71, "LZ-HU"))
        self.assertIsNone(find(body, "b2b_calc_rules", "montaz/do_30", "price"))        # cenník sedí
        r50 = find(body, "b2b_calc_rules", "rozvadzac/R50", "cost", key="blocked")
        self.assertEqual((r50["code_reason"], r50["change_pct"]), ("over_limit", 40.0))
        self.assertEqual(body["blocked_by_reason"].get("over_limit"), 5)   # R50 + LUNA241 ×2 + PD20 ×2

    def test_raynet_meta_a_pocty(self):
        _status, body, _db, _s = run({})
        self.assertEqual([l["label"] for l in body["raynet"]["price_lists_used"]], ["LZ-HU", "Výchozí"])
        self.assertEqual(body["raynet"]["products"], len(raynet_products()))
        self.assertEqual(body["raynet"]["api_calls"], 4)         # cenníky + 2× položky + produkty
        self.assertEqual(body["raw_products"]["written"], 0)
        self.assertGreater(body["conflicts_total"], 0)


class TestApply(unittest.TestCase):
    def setUp(self):
        self.status, self.body, self.db, self.session = run({"apply": "1"})

    def test_status_ok(self):
        self.assertEqual(self.status, 200, self.body)
        self.assertEqual(self.body["mode"], "apply")
        json.dumps(self.body)

    def test_pravidla_zmenene_len_v_limite(self):
        r1 = row(self.db, "b2b_calc_rules", "r1")
        self.assertEqual((r1["cost_per_unit"], r1["price_per_unit"]), (74, 100))
        self.assertEqual(r1["updated_at"], NOW.isoformat())
        self.assertEqual(row(self.db, "b2b_calc_rules", "r2")["price_per_unit"], 26)
        r3 = row(self.db, "b2b_calc_rules", "r3")                               # +40 % -> nezmenené
        self.assertEqual((r3["cost_per_unit"], r3["price_per_unit"]), (2500, 3250))
        r3b = find(self.body, "b2b_calc_rules", "rozvadzac/R50", "cost", key="blocked")
        self.assertEqual(r3b["code_reason"], "over_limit")

    def test_kuratovana_hodnota_sa_neprepise_diverged(self):
        r4 = row(self.db, "b2b_calc_rules", "r4")
        self.assertEqual(r4["cost_per_unit"], 72)
        e = find(self.body, "b2b_calc_rules", "montaz/do_100", "cost", key="blocked")
        self.assertEqual(e["code_reason"], "diverged")

    def test_neaktivne_zamknute_a_bez_kodu_sa_nemenia(self):
        for rid in ("r5", "r6", "r7"):
            orig = next(r for r in rules_rows() if r["id"] == rid)
            self.assertEqual(row(self.db, "b2b_calc_rules", rid), orig)
        stats = self.body["stats"]["b2b_calc_rules"]
        self.assertEqual((stats["inactive"], stats["no_code"], stats["locked"]), (1, 1, 1))

    def test_nezhoda_jednotky_je_zablokovana(self):
        self.assertEqual(row(self.db, "b2b_calc_rules", "r8")["cost_per_unit"], 70)
        e = find(self.body, "b2b_calc_rules", "montaz/nad_500", "cost", key="blocked")
        self.assertEqual(e["code_reason"], "unit_mismatch")

    def test_nenajdeny_kod_je_v_unmapped(self):
        self.assertIn("NEEXISTUJE", [u["code"] for u in self.body["unmapped"]])

    def test_stack_panel_cena_a_zrkadlo(self):
        st = row(self.db, "b2b_vendor_stacks", "s1")
        panel = st["preferred_panels"][0]
        self.assertEqual((panel["price"], panel["price_per_unit"], panel["cost"]), (100, 100, 72.55))

    def test_stack_optimizer_zrkadlo_price_per_panel(self):
        opt = row(self.db, "b2b_vendor_stacks", "s1")["optimizers"][0]
        self.assertEqual((opt["price"], opt["price_per_panel"], opt["cost"]), (38.5, 38.5, 29))

    def test_stack_smart_manager_poznamka_a_zdroj(self):
        sm = row(self.db, "b2b_vendor_stacks", "s1")["smart_manager"]
        self.assertEqual(sm["price"], 55)
        self.assertNotIn("price_note", sm)
        self.assertEqual(sm["source"], "Raynet sync 2026-10-09 (Výchozí)")

    def test_stack_v21_a_luna_ostavaju_ako_kuratovane(self):
        inv = row(self.db, "b2b_vendor_stacks", "s1")["inverters"]
        v21 = next(i for i in inv if i["key"] == "sg33cx_p2_v21")
        self.assertEqual((v21["cost"], v21["source"]), (1755, "Raynet 10/2026 SG33V21"))
        luna = row(self.db, "b2b_vendor_stacks", "s1")["batteries"][0]
        self.assertEqual((luna["cost"], luna["price"]), (40000, 50000))
        e = find(self.body, "b2b_vendor_stacks", "huawei.inverters.sg33cx_p2_v21", "cost", key="blocked")
        self.assertEqual(e["code_reason"], "diverged")

    def test_stack_efektivny_lzhu_nakup_nie_je_zmena(self):
        hua = next(i for i in row(self.db, "b2b_vendor_stacks", "s1")["inverters"] if i["key"] == "sun2000_30k_mc0")
        self.assertEqual((hua["cost"], hua["price"], hua["source"]), (2010.8, 2513.5, "Raynet 10/2026 HUA30"))

    def test_legacy_a_prvok_bez_kodu_sa_nemenia(self):
        st = row(self.db, "b2b_vendor_stacks", "s1")
        self.assertEqual(st["inverters"][2], stacks_rows()[0]["inverters"][2])
        self.assertEqual(st["smart_meter"], stacks_rows()[0]["smart_meter"])
        self.assertEqual(self.body["stats"]["b2b_vendor_stacks"]["legacy_skipped"], 1)

    def test_products(self):
        self.assertEqual(row(self.db, "products", "p2")["sale_price"], 26)
        p1 = row(self.db, "products", "p1")                                 # PD20: +220 % -> nezmenené
        self.assertEqual((p1["purchase_price"], p1["sale_price"]), (250, 350))
        self.assertEqual(row(self.db, "products", "p3"), products_rows()[2])  # neaktívny
        p5 = row(self.db, "products", "p5")                                 # Raynet nákup 0 = nezadané -> nákup ostáva
        self.assertEqual((p5["purchase_price"], p5["sale_price"]), (50, 100))

    def test_b2c_viditelny_produkt_sa_preskakuje(self):
        db = make_db()
        db.tables["products"].append(dict(product_row("p6", "VODAC", 20, 24, "kWp"), b2c_visible=True))
        db.tables["products"][-1]["sku"] = "VODAC"
        _s, body, db, _x = run({"apply": "1"}, db=db)
        self.assertEqual(row(db, "products", "p6")["sale_price"], 24)
        self.assertEqual(body["stats"]["products"]["b2c_skipped"], 1)

    def test_neplatny_produkt_sa_nepise(self):
        self.assertNotIn("OLD", [e["code"] for e in self.body["changes"]])

    def test_raw_products_upsert_s_fetched_at_a_sync_metadatami(self):
        raws = {r["code"]: r for r in self.db.tables["raynet_raw_products"]}
        self.assertEqual(len(raws), len(raynet_products()) - 0)
        mont = raws["MONT"]
        self.assertEqual(mont["fetched_at"], NOW.isoformat())
        self.assertEqual(mont["raw_json"]["_sync"]["effective"], {"price": 100, "cost": 74, "price_src": "LZ-HU", "cost_src": "LZ-HU"})
        self.assertEqual(mont["raw_json"]["_sync"]["pricelists"]["Výchozí"]["cost"], 75)
        self.assertEqual(mont["category"], '{"id": 170, "value": "Komponenty"}')
        self.assertEqual((mont["price"], mont["cost"]), (100, 75))            # stĺpce = hodnoty produktu ako ich vrátil Raynet
        self.assertEqual(self.body["raw_products"]["written"], len(raynet_products()))

    def test_zaznam_behu_v_raynet_import_log(self):
        logs = self.db.tables["raynet_import_log"]
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0]["entity_types"], logs[0]["dry_run"], logs[0]["error"]), (["price_sync"], False, None))
        self.assertEqual(logs[0]["result"]["mode"], "apply")
        self.assertIn("log_id", self.body)
        applied = [e for e in logs[0]["result"]["changes"] if e["applied"]]
        self.assertTrue(applied)

    def test_nic_sa_nemaze_a_raynet_sa_len_cita(self):
        self.assertTrue(all(w[0] in ("update", "upsert", "insert") for w in self.db.writes))
        self.assertTrue(all(c["method"] == "GET" for c in self.session.calls))
        self.assertFalse(hasattr(self.db, "delete"))

    def test_druhy_beh_je_idempotentny(self):
        status, body, db2, _s = run({"apply": "1"}, db=self.db)
        self.assertEqual(status, 200)
        self.assertEqual(body["applied"], 0)
        self.assertEqual(body["planned_rows"], 0)
        updates = [w for w in db2.writes if w[0] == "update"]
        self.assertEqual(len(updates), len([w for w in self.db.writes if w[0] == "update"]))    # žiadne nové UPDATE


class TestAlign(unittest.TestCase):
    def test_align_prijme_kuratovane_v_limite_ale_nie_nad_limit_ani_zamknute(self):
        status, body, db, _s = run({"apply": "1", "align": "1"})
        self.assertEqual(status, 200, body)
        self.assertEqual(row(db, "b2b_calc_rules", "r4")["cost_per_unit"], 74)        # diverged -> prijaté
        self.assertEqual(row(db, "b2b_calc_rules", "r3")["cost_per_unit"], 2500)      # nad 30 % ostáva
        self.assertEqual(row(db, "b2b_calc_rules", "r7"), rules_rows()[6])             # [sync:off]
        inv = row(db, "b2b_vendor_stacks", "s1")["inverters"]
        self.assertEqual(next(i for i in inv if i["key"] == "sg33cx_p2_v21")["cost"], 1528.42)   # explicitne zosúladené
        luna = row(db, "b2b_vendor_stacks", "s1")["batteries"][0]
        self.assertEqual(luna["cost"], 40000)                                          # +40 % -> limit

    def test_align_dopln_chybajucu_hodnotu(self):
        db = make_db()
        row(db, "b2b_calc_rules", "r2")["cost_per_unit"] = None
        _s, body, db, _x = run({"apply": "1"}, db=db)
        self.assertEqual(row(db, "b2b_calc_rules", "r2")["cost_per_unit"], None)       # bez align sa nedopĺňa
        e = find(body, "b2b_calc_rules", "vodice/ac", "cost", key="blocked")
        self.assertEqual(e["code_reason"], "missing_current")
        _s, body2, db2, _x = run({"apply": "1", "align": "1"}, db=db)
        self.assertEqual(row(db2, "b2b_calc_rules", "r2")["cost_per_unit"], 20)


class TestMapovanieRaynetId(unittest.TestCase):
    def test_raynet_product_id_ma_prednost_pred_kodom_v_notes(self):
        db = make_db()
        # pravidlo bez "kód" v notes, namapované len cez raynet_product_id = 2 (VODAC); cena 24 == základ -> 26
        db.tables["b2b_calc_rules"].append(rule("r11", "vodice", "dc2", 20, 24, "ručne pridané pravidlo", pid=2))
        # pravidlo s nesprávnym kódom v notes, ale správnym id -> použije sa id
        db.tables["b2b_calc_rules"].append(rule("r12", "vodice", "dc3", 20, 24, "Raynet 10/2026, kód NEEXISTUJE", pid=2))
        _s, body, db, _x = run({"apply": "1"}, db=db)
        self.assertEqual(row(db, "b2b_calc_rules", "r11")["price_per_unit"], 26)
        self.assertEqual(row(db, "b2b_calc_rules", "r12")["price_per_unit"], 26)

    def test_neznamy_raynet_product_id_je_unmapped(self):
        db = make_db()
        db.tables["b2b_calc_rules"].append(rule("r13", "vodice", "dc4", 20, 24, "kód VODAC", pid=99999))
        _s, body, db, _x = run({"apply": "1"}, db=db)
        self.assertEqual(row(db, "b2b_calc_rules", "r13")["price_per_unit"], 24)
        self.assertIn("id:99999", [u["code"] for u in body["unmapped"]])


class TestNovyProdukt(unittest.TestCase):
    def test_kod_bez_predosleho_stavu_sa_bez_align_nezapise(self):
        db = make_db()
        db.tables["b2b_calc_rules"].append(rule("r10", "ostatne", "novy", 10, 15, "Raynet 10/2026, kód NOVY", unit="ks"))
        sess = make_session(products=raynet_products() + [prod(20, "NOVY", 16, 11, "ks")],
                            vychozi=vychozi_items() + [item(1020, 20, "NOVY", 16, 11)])
        _s, body, db, _x = run({"apply": "1"}, db=db, session=sess)
        self.assertEqual(row(db, "b2b_calc_rules", "r10")["cost_per_unit"], 10)
        e = find(body, "b2b_calc_rules", "ostatne/novy", "cost", key="blocked")
        self.assertEqual(e["code_reason"], "no_base")
        _s, body, db, _x = run({"apply": "1", "align": "1"}, db=db, session=sess)
        self.assertEqual(row(db, "b2b_calc_rules", "r10")["cost_per_unit"], 11)


class TestPoistky(unittest.TestCase):
    def test_cena_musi_zostat_nad_nakupom(self):
        db = make_db()
        # Raynet: nákup MONT 74 (LZ-HU); pravidlo má cenu 70 < 74 po zmene nákupu -> riadok sa nemení
        row(db, "b2b_calc_rules", "r1")["price_per_unit"] = 73
        _s, body, db, _x = run({"apply": "1"}, db=db)
        r1 = row(db, "b2b_calc_rules", "r1")
        self.assertEqual((r1["cost_per_unit"], r1["price_per_unit"]), (70, 73))
        e = find(body, "b2b_calc_rules", "montaz/do_30", "cost", key="blocked")
        self.assertEqual(e["code_reason"], "price_not_above_cost")

    def test_limit_zmien_zastavi_zapis(self):
        before = make_db()
        status, body, db, _s = run({"apply": "1", "max_changes": "2"})
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "limit_zmien")
        for t in ("b2b_calc_rules", "b2b_vendor_stacks", "products", "raynet_raw_products"):
            self.assertEqual(db.tables[t], before.tables[t])
        self.assertEqual([w for w in db.writes if w[0] != "insert"], [])
        self.assertEqual(len(db.tables["raynet_import_log"]), 1)       # záznam o zastavení

    def test_zmenene_medzitym_sa_preskoci(self):
        db = make_db()

        class Racing(FakeSB):
            def table(self, name):
                q = super().table(name)
                if name == "b2b_calc_rules":
                    orig = q.execute

                    def execute():
                        if q.op == "update":
                            for r in self.tables[name]:
                                if r["id"] == "r1":
                                    r["cost_per_unit"] = 71       # niekto zapísal tesne pred nami
                        return orig()
                    q.execute = execute
                return q
        racing = Racing(db.tables)
        status, body, racing, _s = run({"apply": "1"}, db=racing)
        self.assertEqual(status, 200)
        self.assertEqual(row(racing, "b2b_calc_rules", "r1")["cost_per_unit"], 71)
        self.assertIn("changed_meanwhile", [s["code_reason"] for s in body["skipped"]])

    def test_chyba_zapisu_neposuva_zaklad_a_vrati_500(self):
        class Failing(FakeSB):
            def table(self, name):
                q = super().table(name)
                if name == "b2b_calc_rules":
                    def execute():
                        if q.op == "update":
                            raise RuntimeError("DB nedostupná")
                        return _Query.execute(q)
                    q.execute = execute
                return q
        db = Failing(make_db().tables)
        status, body, db, _x = run({"apply": "1"}, db=db)
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])
        self.assertTrue(body["errors"])
        self.assertEqual(body["raw_products"]["written"], 0)
        self.assertEqual(db.tables["raynet_raw_products"], previous_snapshot())        # základ sa neposunul
        self.assertEqual(len(db.tables["raynet_import_log"]), 1)                      # beh je zalogovaný
        self.assertEqual(db.tables["raynet_import_log"][0]["result"]["ok"], False)

    def test_zlyhanie_raynetu_apply_nic_nezapise(self):
        s = FakeSession({"/priceList/": lambda p: FakeResponse(401, {}, text=SECRET_KEY)})
        status, body, db, _x = run({"apply": "1"}, session=s)
        self.assertEqual(status, 502)
        self.assertNotIn(SECRET_KEY, str(body))
        self.assertNotIn(SECRET_USER, str(body))
        self.assertEqual(db.writes, [])

    def test_chyba_429_vracia_429(self):
        s = FakeSession({"/priceList/": lambda p: FakeResponse(429, {}, text="limit")})
        self.assertEqual(run({}, session=s)[0], 429)

    def test_apply_bez_preferovaneho_cennika_sa_zastavi_dry_run_len_varuje(self):
        no_lzhu = [PRICELISTS[0], PRICELISTS[2]]
        status, body, db, _s = run({"apply": "1"}, session=make_session(pricelists=no_lzhu))
        self.assertEqual(status, 502)
        self.assertIn("LZ-HU", body["error"])
        self.assertEqual(db.writes, [])
        status, body, db, _s = run({}, session=make_session(pricelists=no_lzhu))
        self.assertEqual(status, 200)
        self.assertTrue(body["raynet"]["warnings"])

    def test_cielovy_zoznam_obmedzuje_zapis(self):
        status, body, db, _s = run({"apply": "1", "targets": "rules"})
        self.assertEqual(status, 200)
        self.assertEqual(list(body["stats"]), ["b2b_calc_rules"])
        self.assertEqual(db.tables["products"], products_rows())
        self.assertEqual(db.tables["b2b_vendor_stacks"], stacks_rows())

    def test_zaokruhlenie_pri_zapise(self):
        db = make_db()
        db.tables["products"].append(product_row("p9", "HUA3PRICEODD", 2427.7, 3034.60, "ks"))
        db.tables["raynet_raw_products"][-1]["price"] = 3034.6
        # kód nemá žiadny cieľ okrem produktu p9; raw základ 3034.6 == cieľ 3034.60
        _s, body, db, _x = run({"apply": "1", "targets": "products"}, db=db)
        self.assertEqual(row(db, "products", "p9")["sale_price"], 3034.63)

    def test_pouzije_iba_get_na_raynet(self):
        _s, _b, _db, session = run({"apply": "1"})
        self.assertEqual({c["method"] for c in session.calls}, {"GET"})
        self.assertTrue(all(c["url"].startswith("https://app.raynet.cz/api/v2/") for c in session.calls))

    def test_tajomstva_nie_su_v_odpovedi_ani_v_logu(self):
        status, body, db, _s = run({"apply": "1"})
        blob = repr(body) + repr(db.tables["raynet_import_log"])
        self.assertNotIn(SECRET_KEY, blob)
        self.assertNotIn(SECRET_USER, blob)


# ----------------------------------------------------------------------------------------------------------------
# Registrácia v Flasku (preskočí sa bez Flasku)
# ----------------------------------------------------------------------------------------------------------------
@unittest.skipUnless(importlib.util.find_spec("flask"), "Flask nie je nainštalovaný")
class TestRegister(unittest.TestCase):
    def make_app(self):
        from functools import wraps

        from flask import Flask, jsonify, request

        app = Flask(__name__)

        def require_secret(f):          # rovnaká sémantika ako v app.py
            @wraps(f)
            def wrapper(*a, **k):
                if request.headers.get("X-Webhook-Secret", "") != "x-webhook":
                    return jsonify({"error": "unauthorized"}), 401
                return f(*a, **k)
            return wrapper
        rps.register(app, require_secret, get_sb=lambda: make_db())
        return app

    def test_trasa_je_len_post_za_require_secret(self):
        app = self.make_app()
        c = app.test_client()
        self.assertEqual(c.post("/cron/raynet-price-sync").status_code, 401)
        self.assertEqual(c.get("/cron/raynet-price-sync", headers={"X-Webhook-Secret": "x-webhook"}).status_code, 405)
        rules = [r.rule for r in app.url_map.iter_rules() if r.endpoint == "raynet_price_sync"]
        self.assertEqual(rules, ["/cron/raynet-price-sync"])

    def _post(self, query, secret_env="x-webhook"):
        app = self.make_app()
        names = ("RAYNET_INSTANCE", "RAYNET_USER", "RAYNET_USERNAME", "RAYNET_API_KEY", "RAYNET_KEY", "WEBHOOK_SECRET")
        old = {k: os.environ.pop(k, None) for k in names}
        if secret_env:
            os.environ["WEBHOOK_SECRET"] = secret_env
        try:
            return app.test_client().post("/cron/raynet-price-sync" + query, headers={"X-Webhook-Secret": "x-webhook"})
        finally:
            os.environ.pop("WEBHOOK_SECRET", None)
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_bez_raynet_env_je_503_so_spravou(self):
        r = self._post("")
        self.assertEqual(r.status_code, 503)
        self.assertIn("RAYNET_API_KEY", r.get_json()["error"])
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertEqual(self._post("?apply=1").status_code, 503)

    def test_bez_webhook_secret_v_env_je_503(self):
        r = self._post("", secret_env=None)
        self.assertEqual(r.status_code, 503)
        self.assertIn("WEBHOOK_SECRET", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
