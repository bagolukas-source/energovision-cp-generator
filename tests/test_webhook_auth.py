"""Testy zabezpečenia webhookov výpočtového jadra (Fáza 1, 2026-10) — stdlib unittest, bez siete.

Spustenie z koreňa repa (potrebuje Flask z requirements.txt; bez neho sa celý modul preskočí):
    python3 -m unittest discover -s tests -p 'test_webhook_auth.py' -v
    # bez Flasku v systéme:
    #   python3 -m venv .venv && .venv/bin/pip install flask requests
    #   .venv/bin/python -m unittest discover -s tests -p 'test_webhook_auth.py' -v

Čo sa overuje:
  * /webhook/b2b-* a /webhook/raynet-* sú za X-Webhook-Secret a FAIL-CLOSED: bez hlavičky alebo so zlou
    je 401 (handler sa nespustí), so správnou požiadavka prejde, bez env WEBHOOK_SECRET je 503;
  * zrušené cesty (v1 b2b-calc-preview / -save, b2b-generate-pdf, b2b-calc-v2-save) vracajú 410;
  * iné cesty (/health, ostatné webhooky) a CORS preflight brána nezasiahla;
  * prihlasovacie údaje k Raynetu z tela platia len pre jedno volanie (žiadny globál).

app.py sa importuje s mockmi: knižnice anthropic a supabase sa, ak nie sú nainštalované, nahradia prázdnym
MagicMock-om; sieť je v testoch zablokovaná (socket.connect) a DB (`_sb`) sa podstrčí.
"""
import importlib
import logging
import os
import socket
import sys
import threading
import types
import unittest
import warnings
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SECRET = "test-secret-nie-je-skutocne"
HDR = {"X-Webhook-Secret": SECRET}
PREFIXES = ("/webhook/b2b-", "/webhook/raynet-")

AKTIVNE_B2B = {"b2b-calc-v2-preview", "b2b-vendor-stacks", "b2b-ai-configurator", "b2b-vendor-recommender",
               "b2b-compatibility-checker", "b2b-price-sanity", "b2b-bom-validator"}
ZRUSENE_B2B = {"b2b-calc-preview", "b2b-calc-save", "b2b-generate-pdf", "b2b-calc-v2-save"}
RAYNET = {"raynet-whoami", "raynet-discover", "raynet-fetch-items", "raynet-import"}

BODY_401 = {"ok": False, "error": "unauthorized"}
BODY_503 = {"ok": False, "error": "server misconfigured"}
BODY_410 = {"ok": False, "error": "zrušené — použi /b2b/kalkulacka v CRM"}

appmod = None   # app.py (import v setUpModule)
raynet = None   # raynet_discovery
_patches = []
_stubs = []
_old_cwd = None


def setUpModule():
    global appmod, raynet, _old_cwd
    try:
        import flask  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("Flask nie je nainštalovaný (pip install -r requirements.txt) — testy brány sa preskakujú")
    try:
        logging.disable(logging.CRITICAL)
        net = mock.patch.object(socket.socket, "connect", side_effect=OSError("sieť je v testoch zablokovaná"))
        net.start()
        _patches.append(net)
        for name in ("anthropic", "supabase"):  # ťažké/sieťové knižnice, ktoré app.py importuje pri štarte
            try:
                importlib.import_module(name)
            except ImportError:
                stub = mock.MagicMock(name=name)
                stub.__path__ = []
                sys.modules[name] = stub
                _stubs.append(name)
        _old_cwd = os.getcwd()
        os.chdir(ROOT)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with mock.patch.dict(os.environ, {"WEBHOOK_SECRET": SECRET}):
                import app as _app
                import raynet_discovery as _rd
        appmod, raynet = _app, _rd
    except BaseException:
        tearDownModule()
        raise


def tearDownModule():
    for p in reversed(_patches):
        p.stop()
    _patches.clear()
    for name in _stubs:
        sys.modules.pop(name, None)
    _stubs.clear()
    logging.disable(logging.NOTSET)
    if _old_cwd:
        os.chdir(_old_cwd)


def gated_rules():
    """Všetky registrované routes pod prefixmi /webhook/b2b- a /webhook/raynet-."""
    return sorted((r for r in appmod.app.url_map.iter_rules() if r.rule.startswith(PREFIXES)),
                  key=lambda r: r.rule)


def method_for(rule):
    return sorted(rule.methods - {"HEAD", "OPTIONS"})[0]


class _Base(unittest.TestCase):
    def setUp(self):
        wcm = warnings.catch_warnings()   # app.py používa datetime.utcnow() (DeprecationWarning) — v teste šum
        wcm.__enter__()
        self.addCleanup(wcm.__exit__, None, None, None)
        warnings.simplefilter("ignore", DeprecationWarning)
        self.client = appmod.app.test_client()
        self.set_env(WEBHOOK_SECRET=SECRET)

    def set_env(self, **values):
        """Nastaví premenné prostredia na čas testu; hodnota None = premennú odstráni."""
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def stub_views(self, rules):
        """Namiesto skutočných handlerov podstrčí počítadlo — dokáže, či požiadavka prešla bránou."""
        calls = []

        def view(*args, **kwargs):
            calls.append(1)
            return appmod.jsonify({"stub": True}), 200

        p = mock.patch.dict(appmod.app.view_functions, {r.endpoint: view for r in rules})
        p.start()
        self.addCleanup(p.stop)
        return calls

    def call(self, rule, headers=None, **kwargs):
        return self.client.open(rule.rule, method=method_for(rule), headers=headers or {}, **kwargs)


class TestBranaB2bRaynet(_Base):
    def test_ocakavane_routes_existuju(self):
        paths = {r.rule for r in gated_rules()}
        ocakavane = {"/webhook/" + n for n in AKTIVNE_B2B | ZRUSENE_B2B | RAYNET}
        self.assertEqual(ocakavane - paths, set(), "chýbajú očakávané routes (brána by nemala čo kryť)")

    def test_bez_hlavicky_je_401_a_handler_nebezi(self):
        rules = gated_rules()
        calls = self.stub_views(rules)
        for rule in rules:
            r = self.call(rule, json={})
            self.assertEqual(r.status_code, 401, rule.rule)
            self.assertEqual(r.get_json(), BODY_401, rule.rule)
        self.assertEqual(calls, [], "handler sa nesmie spustiť bez tajomstva")

    def test_head_bez_hlavicky_je_401(self):
        r = self.client.head("/webhook/b2b-vendor-stacks")
        self.assertEqual(r.status_code, 401)

    def test_zle_tajomstvo_je_401(self):
        rules = gated_rules()
        calls = self.stub_views(rules)
        zle = ["zle", SECRET + "x", SECRET[:-1], SECRET.upper(), " " + SECRET, "", "heslo-é"]
        for value in zle:
            for rule in rules:
                r = self.call(rule, headers={"X-Webhook-Secret": value}, json={})
                self.assertEqual(r.status_code, 401, f"{rule.rule} s hodnotou {value!r}")
                self.assertEqual(r.get_json(), BODY_401)
        self.assertEqual(calls, [])

    def test_tajomstvo_v_query_sa_neberie(self):
        calls = self.stub_views(gated_rules())
        for q in ("secret", "key", "X-Webhook-Secret"):
            r = self.client.get(f"/webhook/b2b-vendor-stacks?{q}={SECRET}")
            self.assertEqual(r.status_code, 401, q)
        self.assertEqual(calls, [])

    def test_spravne_tajomstvo_pusti_do_handlera(self):
        rules = gated_rules()
        calls = self.stub_views(rules)
        for rule in rules:
            r = self.call(rule, headers=HDR, json={})
            self.assertEqual(r.status_code, 200, rule.rule)
            self.assertEqual(r.get_json(), {"stub": True}, rule.rule)
        self.assertEqual(len(calls), len(rules))

    def test_chybajuci_env_je_503_fail_closed(self):
        self.set_env(WEBHOOK_SECRET=None)
        rules = gated_rules()
        calls = self.stub_views(rules)
        for rule in rules:
            for headers in ({}, HDR, {"X-Webhook-Secret": ""}):
                r = self.call(rule, headers=headers, json={})
                self.assertEqual(r.status_code, 503, rule.rule)
                self.assertEqual(r.get_json(), BODY_503, rule.rule)
        self.assertEqual(calls, [], "bez nastaveného tajomstva sa handler nikdy nesmie spustiť")

    def test_prazdny_alebo_medzerovy_env_je_503(self):
        calls = self.stub_views(gated_rules())
        for value in ("", "   "):
            self.set_env(WEBHOOK_SECRET=value)
            for headers in ({}, {"X-Webhook-Secret": value}, HDR):
                r = self.client.get("/webhook/b2b-vendor-stacks", headers=headers)
                self.assertEqual(r.status_code, 503, repr(value))
                self.assertEqual(r.get_json(), BODY_503)
        self.assertEqual(calls, [])

    def test_neznama_cesta_pod_prefixom_je_401_a_az_so_secretom_404(self):
        r = self.client.post("/webhook/b2b-neexistuje", json={})
        self.assertEqual(r.status_code, 401, "bez tajomstva sa nemá dať zisťovať, ktoré cesty existujú")
        r = self.client.post("/webhook/b2b-neexistuje", json={}, headers=HDR)
        self.assertEqual(r.status_code, 404)

    def test_netypicke_tvary_cesty_nepustia_handler_bez_tajomstva(self):
        """Percent-encoding, bodkočiarka, query, trojité lomítko, dvojité lomítko v strede — handler nesmie bežať."""
        calls = self.stub_views(gated_rules())
        cesty = ["/webhook/%62%32%62-vendor-stacks", "/webhook/b2b%2Dvendor-stacks", "/webhook/b2b-vendor-stacks;a=1",
                 "/webhook/b2b-vendor-stacks?x=1", "///webhook/b2b-vendor-stacks", "/webhook//b2b-vendor-stacks",
                 "/webhook/raynet-%69mport", "/webhook/raynet-import/"]
        for cesta in cesty:
            r = self.client.open(cesta, method="POST", json={})
            self.assertNotEqual(r.status_code, 200, cesta)
        self.assertEqual(calls, [], "bez tajomstva sa nesmie spustiť žiadny handler")

    def test_brana_kryje_aj_neskor_pridane_routes(self):
        """Napr. budúce /webhook/raynet-price-sync: brána je podľa prefixu, nie podľa dekorátora na route."""
        import flask
        probe = flask.Flask("probe_brany")
        probe.before_request(appmod._secret_gate_b2b_raynet)

        @probe.route("/webhook/raynet-price-sync", methods=["POST"])
        def _sync():
            return "ran"

        @probe.route("/webhook/ine-webhook", methods=["POST"])
        def _ine():
            return "ran"

        c = probe.test_client()
        self.assertEqual(c.post("/webhook/raynet-price-sync").status_code, 401)
        self.assertEqual(c.post("/webhook/raynet-price-sync", headers=HDR).status_code, 200)
        self.assertEqual(c.post("/webhook/ine-webhook").status_code, 200)
        self.set_env(WEBHOOK_SECRET=None)
        self.assertEqual(c.post("/webhook/raynet-price-sync", headers=HDR).status_code, 503)
        self.assertEqual(c.post("/webhook/ine-webhook").status_code, 200)

    def test_preflight_options_ostava_bez_zmeny(self):
        """CORS sa nemení: preflight (OPTIONS) sa nekontroluje a dostáva CORS hlavičky ako doteraz."""
        for secret in (SECRET, None):
            self.set_env(WEBHOOK_SECRET=secret)
            origin = "https://crm.energovision.sk"
            r = self.client.options("/webhook/b2b-calc-v2-preview", headers={"Origin": origin})
            self.assertEqual(r.status_code, 204)
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), origin)
            self.assertIn("X-Webhook-Secret", r.headers.get("Access-Control-Allow-Headers", ""))

    def test_odpoved_401_ma_cors_hlavicky(self):
        """Chybová odpoveď brány prechádza cez existujúce after_request (CORS), prehliadač ju teda prečíta."""
        origin = "https://crm.energovision.sk"
        r = self.client.get("/webhook/b2b-vendor-stacks", headers={"Origin": origin})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), origin)


class TestSkutocneHandleryZaBranou(_Base):
    """Bez hlavičky 401 je pokryté vyššie; tu správne tajomstvo + skutočný handler → 200/400 podľa tela."""

    def test_ai_configurator_prazdne_telo_je_400(self):
        r = self.client.post("/webhook/b2b-ai-configurator", json={}, headers=HDR)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json(), {"ok": False, "error": "text required"})

    def test_calc_v2_preview_je_200(self):
        with mock.patch.object(appmod, "_sb", return_value=object()), \
                mock.patch.object(appmod._b2b_v2, "calculate_bom_v2", return_value={"ok": True, "items": []}) as calc:
            r = self.client.post("/webhook/b2b-calc-v2-preview", json={"kwp": 10}, headers=HDR)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "items": []})
        calc.assert_called_once()

    def test_vendor_stacks_je_200(self):
        sb = _FakeSB({"b2b_vendor_stacks": [{"vendor_key": "huawei"}]})
        with mock.patch.object(appmod, "_sb", return_value=sb):
            r = self.client.get("/webhook/b2b-vendor-stacks", headers=HDR)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "stacks": [{"vendor_key": "huawei"}]})

    def test_raynet_whoami_je_200(self):
        with mock.patch.object(raynet, "whoami", return_value={"user": "test"}):
            r = self.client.post("/webhook/raynet-whoami", json={}, headers=HDR)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "whoami": {"user": "test"}})


class TestZruseneRoutes(_Base):
    def test_zrusene_vracaju_410_pre_vsetky_metody_a_nedotknu_sa_db(self):
        sb = mock.Mock(side_effect=AssertionError("zrušený endpoint sa nesmie dotknúť DB"))
        with mock.patch.object(appmod, "_sb", sb):
            for name in sorted(ZRUSENE_B2B):
                for method in ("POST", "GET", "PUT", "PATCH", "DELETE"):
                    r = self.client.open("/webhook/" + name, method=method, json={"quote_id": "x"}, headers=HDR)
                    self.assertEqual(r.status_code, 410, f"{method} {name}")
                    self.assertEqual(r.get_json(), BODY_410, f"{method} {name}")
        sb.assert_not_called()

    def test_zrusene_bez_tajomstva_ostavaju_401(self):
        for name in sorted(ZRUSENE_B2B):
            r = self.client.post("/webhook/" + name, json={})
            self.assertEqual(r.status_code, 401, name)

    def test_implementacia_je_odstranena(self):
        for atr in ("_b2b_calc", "_b2b_pdf", "webhook_b2b_calc_preview", "webhook_b2b_calc_save",
                    "webhook_b2b_generate_pdf", "webhook_b2b_calc_v2_save"):
            self.assertFalse(hasattr(appmod, atr), atr)
        self.assertTrue(hasattr(appmod, "webhook_b2b_zrusene"))


class TestNedotknuteCesty(_Base):
    def test_health_je_200_bez_hlavicky_aj_bez_env(self):
        for secret in (SECRET, None):
            self.set_env(WEBHOOK_SECRET=secret)
            r = self.client.get("/health")
            self.assertEqual(r.status_code, 200, f"env={secret!r}")
            self.assertEqual(r.get_json()["status"], "ok")

    def test_podobne_prefixy_brana_nezachytava(self):
        """generate-b2b-*, prezentacia-b2b a ostatné webhooky majú vlastné mechanizmy — brána do nich nezasahuje."""
        self.set_env(WEBHOOK_SECRET=None)   # pri 503 by sa ukázalo, že ich brána omylom zachytila
        adapter = appmod.app.url_map.bind("localhost")
        endpoints = {}
        for path in ("/webhook/generate-b2b-zod", "/webhook/generate-b2b-faktura", "/webhook/prezentacia-b2b",
                     "/webhook/prepocet", "/webhook/generate-pdf"):
            endpoints[path] = adapter.match(path, method="POST")[0]
        calls = []

        def view(*args, **kwargs):
            calls.append(1)
            return appmod.jsonify({"stub": True}), 200

        with mock.patch.dict(appmod.app.view_functions, {ep: view for ep in endpoints.values()}):
            for path in endpoints:
                r = self.client.post(path, json={})
                self.assertEqual(r.status_code, 200, path)
        self.assertEqual(len(calls), len(endpoints))

    def test_gate_prefixy_su_presne_b2b_a_raynet(self):
        self.assertEqual(tuple(appmod._SECRET_GATE_PREFIXES), PREFIXES)


# ----------------------------------------------------------------------------------------------
# Raynet: prihlasovacie údaje z tela len pre jedno volanie
# ----------------------------------------------------------------------------------------------
class _FakeSB:
    """Minimálne supabase-py API: table().select().execute() → .data."""

    def __init__(self, rows_by_table=None):
        self.rows = rows_by_table or {}
        self._t = None

    def table(self, name):
        self._t = name
        return self

    def select(self, *args, **kwargs):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rows.get(self._t, []))


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeRaynetGet:
    """Náhrada requests.get v raynet_discovery: zapamätá si s akými údajmi sa volal Raynet."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, auth=None, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "auth": auth, "inst": (headers or {}).get("X-Instance-Name")})
        if "/whoami/" in url:
            return _FakeResp({"data": {"user": "test"}})
        if "/offer/" in url:
            return _FakeResp({"data": {"items": []}})
        return _FakeResp({"data": [], "totalCount": 0})


class TestRaynetPrihlasovacieUdaje(_Base):
    def setUp(self):
        super().setUp()
        self.set_env(RAYNET_USERNAME=None, RAYNET_API_KEY=None, RAYNET_INSTANCE=None)
        self.fake_get = _FakeRaynetGet()
        for p in (mock.patch.object(raynet.requests, "get", self.fake_get),
                  mock.patch.object(raynet.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    BODY = {"raynet_user": "u1", "raynet_key": "k1", "raynet_instance": "inst1"}

    def test_ziadny_globalny_stav_pre_udaje(self):
        self.assertFalse(hasattr(raynet, "_RUNTIME_CREDS"))
        self.assertFalse(hasattr(raynet, "set_creds"))

    def test_use_creds_plati_len_vnutri_with(self):
        with self.assertRaises(RuntimeError):
            raynet._creds()
        with raynet.use_creds("u", "k", "i"):
            self.assertEqual(raynet._creds(), ("u", "k", "i"))
        with self.assertRaises(RuntimeError):
            raynet._creds()
        self.assertIsNone(raynet._CALL_CREDS.get())

    def test_use_creds_sa_zahodi_aj_pri_vynimke(self):
        with self.assertRaises(ValueError):
            with raynet.use_creds("u", "k"):
                raise ValueError("boom")
        self.assertIsNone(raynet._CALL_CREDS.get())

    def test_use_creds_vnorene_vrati_povodne(self):
        with raynet.use_creds("u1", "k1", "i1"):
            with raynet.use_creds("u2", "k2", "i2"):
                self.assertEqual(raynet._creds(), ("u2", "k2", "i2"))
            self.assertEqual(raynet._creds(), ("u1", "k1", "i1"))

    def test_use_creds_bez_hodnot_nic_nemeni_a_plati_env(self):
        self.set_env(RAYNET_USERNAME="envu", RAYNET_API_KEY="envk")
        for args in ((None, None), ("u", None), (None, "k"), ("", "")):
            with raynet.use_creds(*args):
                self.assertEqual(raynet._creds(), ("envu", "envk", "energovision"), args)

    def test_udaje_nie_su_viditelne_v_inom_vlakne(self):
        videl = []

        def worker():
            try:
                raynet._creds()
                videl.append(True)
            except RuntimeError:
                videl.append(False)

        with raynet.use_creds("u", "k"):
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        self.assertEqual(videl, [False], "údaje z jedného volania sa nesmú dostať do iného vlákna/požiadavky")

    def test_whoami_pouzije_udaje_z_tela_a_dalsie_volanie_uz_nie(self):
        r1 = self.client.post("/webhook/raynet-whoami", json=self.BODY, headers=HDR)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(self.fake_get.calls[0]["auth"], ("u1", "k1"))
        self.assertEqual(self.fake_get.calls[0]["inst"], "inst1")
        self.assertIsNone(raynet._CALL_CREDS.get())
        r2 = self.client.post("/webhook/raynet-whoami", json={}, headers=HDR)
        self.assertEqual(r2.status_code, 500)
        self.assertIn("Missing RAYNET", r2.get_json()["error"])
        self.assertEqual(len(self.fake_get.calls), 1, "druhé volanie nesmie ísť do Raynetu s cudzími údajmi")

    def test_whoami_get_bez_tela_pouzije_env(self):
        self.set_env(RAYNET_USERNAME="envu", RAYNET_API_KEY="envk", RAYNET_INSTANCE="envinst")
        r = self.client.get("/webhook/raynet-whoami", headers=HDR)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake_get.calls[0]["auth"], ("envu", "envk"))
        self.assertEqual(self.fake_get.calls[0]["inst"], "envinst")

    def test_udaje_z_tela_prebiju_env_len_pre_jedno_volanie(self):
        self.set_env(RAYNET_USERNAME="envu", RAYNET_API_KEY="envk")
        self.client.post("/webhook/raynet-whoami", json=self.BODY, headers=HDR)
        self.client.post("/webhook/raynet-whoami", json={}, headers=HDR)
        self.assertEqual([c["auth"] for c in self.fake_get.calls], [("u1", "k1"), ("envu", "envk")])

    def test_neuplne_udaje_v_tele_sa_ignoruju(self):
        self.set_env(RAYNET_USERNAME="envu", RAYNET_API_KEY="envk")
        self.client.post("/webhook/raynet-whoami", json={"raynet_user": "u1"}, headers=HDR)
        self.assertEqual(self.fake_get.calls[0]["auth"], ("envu", "envk"))

    def test_discover_pouzije_udaje_z_tela_a_dalsie_volanie_uz_nie(self):
        with mock.patch.object(appmod, "_sb", return_value=_FakeSB()):
            r1 = self.client.post("/webhook/raynet-discover", json={"only": "products", **self.BODY}, headers=HDR)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(self.fake_get.calls[0]["auth"], ("u1", "k1"))
            self.assertIsNone(raynet._CALL_CREDS.get())
            r2 = self.client.post("/webhook/raynet-discover", json={"only": "products"}, headers=HDR)
        self.assertEqual(r2.status_code, 500)
        self.assertIn("Missing RAYNET", r2.get_json()["error"])
        self.assertEqual(len(self.fake_get.calls), 1)

    def test_fetch_items_pouzije_udaje_z_tela_a_dalsie_volanie_uz_nie(self):
        sb = _FakeSB({"raynet_raw_quotations": [{"raynet_id": 7}], "raynet_raw_quotation_items": []})
        with mock.patch.object(appmod, "_sb", return_value=sb):
            r1 = self.client.post("/webhook/raynet-fetch-items", json=self.BODY, headers=HDR)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.get_json()["processed_offers"], 1)
            self.assertEqual(self.fake_get.calls[0]["auth"], ("u1", "k1"))
            self.assertIsNone(raynet._CALL_CREDS.get())
            r2 = self.client.post("/webhook/raynet-fetch-items", json={}, headers=HDR)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["processed_offers"], 0)
        self.assertEqual(r2.get_json()["failed"], 1)
        self.assertEqual(len(self.fake_get.calls), 1, "druhé volanie nesmie ísť do Raynetu s cudzími údajmi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
