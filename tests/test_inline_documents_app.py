"""Integrácia inline route do app.py + zhoda pravidla SKU→záruky s /webhook/generuj-dokumenty-supabase.

stdlib unittest, bez siete (socket.connect zablokovaný, requests podstrčené). app.py sa importuje s mockmi
rovnako ako v tests/test_webhook_auth.py.

  * /webhook/generuj-dokumenty-inline je v app.py zaregistrovaná raz a platí pre ňu vlastná fail-closed brána;
  * správanie Supabase route sa extrakciou pravidla panel→záruky NEZMENILO: tabuľka očakávaní je zapísaná
    natvrdo podľa pôvodného kódu (PAN-001 → 25/30 a 470 Wp, PAN-002 → 15/25 a 535 Wp, ostatné/chýba → pôvodné
    hodnoty) a test beží rovnako proti pôvodnému aj novému app.py.
"""
import importlib
import logging
import os
import socket
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
SECRET = "test-secret-nie-je-skutocne"
HDR = {"X-Webhook-Secret": SECRET}

appmod = None
_patches = []
_stubs = []
_old_cwd = None


def setUpModule():
    global appmod, _old_cwd
    try:
        import flask  # noqa: F401
        import docx  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("Flask/python-docx nie sú nainštalované")
    try:
        logging.disable(logging.CRITICAL)
        net = mock.patch.object(socket.socket, "connect", side_effect=OSError("sieť je v testoch zablokovaná"))
        net.start()
        _patches.append(net)
        for name in ("anthropic", "supabase"):
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
        appmod = _app
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


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.ok, self.text = data, status, status < 400, ""

    def json(self):
        return self._d


class SupabasePanelParity(unittest.TestCase):
    """Pôvodné pravidlo (z app.py pred extrakciou), zapísané natvrdo."""
    GOLDEN = {
        "PAN-001": (25, 30, 470),
        "PAN-002": (15, 25, 535),
        None: (25, 30, 470),        # chýbajúce SKU → default PAN-001
        "": (25, 30, 470),
        "PAN-777": (15, 25, 470),   # neznáme SKU: 470 Wp a záruky 15/25 (zachované správanie)
    }

    def setUp(self):
        warnings.simplefilter("ignore", DeprecationWarning)
        p = mock.patch.dict(os.environ, {"WEBHOOK_SECRET": SECRET, "SUPABASE_SERVICE_ROLE_KEY": "x",
                                         "SUPABASE_URL": "https://supabase.example.test"})
        p.start()
        self.addCleanup(p.stop)
        self.client = appmod.app.test_client()

    def run_route(self, sku, pocet=20):
        order = {"id": "o1", "lead_id": "l1", "bundle_id": "b1", "total_with_vat": 1000.0, "accepted_variant": "A",
                 "order_number": "ORD-1",
                 "customers": {"first_name": "Mária", "last_name": "Zákazníková", "street": "Ukážková 12",
                               "postal_code": "811 01", "city": "Bratislava", "phone": "+421 900 000 000",
                               "email": "m@example.test"},
                 "leads": {"ev_id": "EV-26-001", "distribucka": "ZSD", "users": {}}}
        bundle = {"pocet_panelov": pocet, "payment_terms": "60_40"}
        if sku is not None:
            bundle["panel_sku"] = sku

        def fake_get(url, **kw):
            return _Resp([order] if url.endswith("/orders") else [bundle])

        captured = {}

        def fake_fill(lead_data, out_path):
            captured.update(lead_data)
            Path(out_path).write_bytes(b"x")

        with mock.patch.object(appmod.requests, "get", side_effect=fake_get), \
                mock.patch.object(appmod.requests, "post", return_value=_Resp({}, 200)), \
                mock.patch.object(appmod.requests, "patch", return_value=_Resp({}, 200)), \
                mock.patch("generuj_dokumenty.naplnif_zmluvu", side_effect=fake_fill):
            r = self.client.post("/webhook/generuj-dokumenty-supabase", json={"order_id": "o1", "kind": "zmluva"},
                                 headers=HDR)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        return captured

    def test_supabase_route_behaviour_unchanged(self):
        for sku, (zp, zl, wp) in self.GOLDEN.items():
            with self.subTest(sku=sku):
                ld = self.run_route(sku)
                self.assertEqual((ld["zaruka_panely_produkt"], ld["zaruka_panely_linear"]), (zp, zl))
                self.assertEqual(ld["vykon_kwp"], round(20 * wp / 1000, 2))
                self.assertEqual(ld["payment_terms"], "60_40")

    def test_supabase_route_zero_panels(self):
        self.assertEqual(self.run_route("PAN-002", pocet=0)["vykon_kwp"], 0)


@unittest.skipUnless(os.path.exists(os.path.join(ROOT, "inline_documents.py")), "inline route v tejto verzii nie je")
class InlineRouteInApp(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", DeprecationWarning)
        self.client = appmod.app.test_client()
        p = mock.patch.dict(os.environ, {"WEBHOOK_SECRET": SECRET})
        p.start()
        self.addCleanup(p.stop)
        self._tmp = tempfile.mkdtemp(prefix="inl-app-")
        self.addCleanup(__import__("shutil").rmtree, self._tmp, True)

    BODY = {"kind": "gdpr", "lead_data": {"meno_priezvisko": "Mária Zákazníková", "datum_narodenia": "01.02.1980",
                                          "datum_dnes": "12.11.2026"}}

    def test_registered_once(self):
        rules = [r for r in appmod.app.url_map.iter_rules() if r.rule == "/webhook/generuj-dokumenty-inline"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(sorted(rules[0].methods - {"HEAD", "OPTIONS"}), ["POST"])

    def test_fail_closed_through_the_app(self):
        url = "/webhook/generuj-dokumenty-inline"
        with mock.patch.dict(os.environ):
            os.environ.pop("WEBHOOK_SECRET", None)
            self.assertEqual(self.client.post(url, json=self.BODY, headers=HDR).status_code, 503)
        self.assertEqual(self.client.post(url, json=self.BODY).status_code, 401)
        self.assertEqual(self.client.post(url, json=self.BODY, headers={"X-Webhook-Secret": "zle"}).status_code, 401)

    def test_ok_through_the_app_with_stubbed_converter(self):
        import inline_documents
        with mock.patch.object(inline_documents, "convert_to_pdf",
                               return_value=(b"%PDF-1.7\n%%EOF\n", "libreoffice")):
            r = self.client.post("/webhook/generuj-dokumenty-inline", json=self.BODY, headers=HDR)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")


if __name__ == "__main__":
    unittest.main()
