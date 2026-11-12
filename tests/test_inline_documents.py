"""Testy POST /webhook/generuj-dokumenty-inline (inline_documents.py) — stdlib unittest, bez siete.

Spustenie z koreňa repa (potrebuje flask, python-docx, openpyxl, requests z requirements.txt):
    python3 -m pytest -q -p no:cacheprovider tests/test_inline_documents.py
    # alebo: python3 -m unittest discover -s tests -p 'test_inline_documents.py' -v

Čo sa overuje:
  * FAIL-CLOSED autentifikácia (503 bez WEBHOOK_SECRET, 401 pri chýbajúcej/zlej hlavičke, konštantný čas),
    brána beží PRED parsovaním tela;
  * prísna validácia (whitelist, typy, dĺžky, formáty, enum, rozsahy, 422 s menom poľa, bez opisu hodnôt);
  * každý druh dokumentu vyplní SKUTOČNÚ šablónu bez zvyšnej značky; suma slovom; 4 platobné varianty; záruky;
  * kontrola úplnosti zachytí šablónu s menším/väčším počtom značiek než hodnôt (nikdy zmluva s dierou);
  * po požiadavke ostane prázdny dočasný adresár; žiadne sieťové volanie; payload sa nelogguje;
  * tvar požiadavky/odpovede sedí so zdieľanou fixture tests/fixtures/inline_documents_contract.json.
PDF konverzia je v testoch podstrčená (CI nemá zaručený LibreOffice); jeden test s LibreOffice beží len keď je k dispozícii.
"""
import base64
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import flask  # noqa: F401
    import docx  # noqa: F401
    import openpyxl  # noqa: F401
except ImportError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"chýba závislosť ({_e}) — pip install -r requirements.txt")

from flask import Flask  # noqa: E402

import generuj_dokumenty as gd  # noqa: E402
import inline_documents as inl  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "inline_documents_contract.json").read_text(encoding="utf-8"))
SECRET = "test-secret-nie-je-skutocne"
HDR = {"X-Webhook-Secret": SECRET}
FAKE_PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
CANARY_NAME = "Kanárik Testovacieho-Mena"


def example(kind):
    return copy.deepcopy(FIXTURE["request_examples"][kind])


def make_app():
    app = Flask(__name__)
    inl.register(app)
    return app


def docx_text(docx_bytes):
    return "\n".join(inl._paragraph_texts(docx_bytes))


class Base(unittest.TestCase):
    def setUp(self):
        self.client = make_app().test_client()
        p = mock.patch.dict(os.environ, {"WEBHOOK_SECRET": SECRET})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("GOTENBERG_URL", None)
        # súkromný TMPDIR testu — overíme, že po požiadavke v ňom nič nezostane
        self._tmp = tempfile.mkdtemp(prefix="inl-test-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        p2 = mock.patch.object(tempfile, "tempdir", self._tmp)
        p2.start()
        self.addCleanup(p2.stop)
        self.pdf = mock.patch.object(inl, "convert_to_pdf", return_value=(FAKE_PDF, "libreoffice"))
        self.convert_mock = self.pdf.start()
        self.addCleanup(self.pdf.stop)

    def post(self, body, headers=HDR, raw=None):
        if raw is not None:
            return self.client.post(inl.ROUTE, data=raw, headers=headers, content_type="application/json")
        return self.client.post(inl.ROUTE, json=body, headers=headers)

    def leftovers(self):
        return sorted(os.listdir(self._tmp))


class AuthTests(Base):
    def test_fail_closed_without_secret_configured(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ):
                    if value is None:
                        os.environ.pop("WEBHOOK_SECRET", None)
                    else:
                        os.environ["WEBHOOK_SECRET"] = value
                    r = self.post(example("gdpr"), headers={"X-Webhook-Secret": "cokolvek"})
                    self.assertEqual(r.status_code, 503)
                    self.assertEqual(r.get_json(), {"ok": False, "error": "server misconfigured"})
                    r = self.post(example("gdpr"), headers={})
                    self.assertEqual(r.status_code, 503)

    def test_missing_or_wrong_secret_is_401_and_nothing_runs(self):
        with mock.patch.object(inl, "render_docx") as render:
            for headers in ({}, {"X-Webhook-Secret": ""}, {"X-Webhook-Secret": "zle"},
                            {"X-Webhook-Secret": SECRET + "x"}, {"X-Webhook-Secret": SECRET[:-1]}):
                with self.subTest(headers=headers):
                    r = self.post(example("gdpr"), headers=headers)
                    self.assertEqual(r.status_code, 401)
                    self.assertEqual(r.get_json(), {"ok": False, "error": "unauthorized"})
            render.assert_not_called()

    def test_auth_runs_before_body_parsing(self):
        r = self.post(None, headers={}, raw=b"{toto nie je json")
        self.assertEqual(r.status_code, 401)
        r = self.post(None, headers={}, raw=b"x" * (inl.MAX_BODY_BYTES + 10))
        self.assertEqual(r.status_code, 401)

    def test_constant_time_compare_is_used(self):
        with mock.patch.object(inl.hmac, "compare_digest", wraps=inl.hmac.compare_digest) as cmp:
            self.post(example("gdpr"))
            self.post(example("gdpr"), headers={"X-Webhook-Secret": "zle"})
        self.assertEqual(cmp.call_count, 2)

    def test_correct_secret_passes(self):
        self.assertEqual(self.post(example("gdpr")).status_code, 200)

    def test_only_post_is_routed(self):
        self.assertEqual(self.client.get(inl.ROUTE, headers=HDR).status_code, 405)


class ValidationTests(Base):
    def fields_of(self, response):
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True)[:300])
        j = response.get_json()
        self.assertEqual(j["error"], "validation_failed")
        return j["fields"]

    def with_ld(self, kind, **changes):
        req = example(kind)
        for k, v in changes.items():
            if v is KeyError:
                req["lead_data"].pop(k, None)
            else:
                req["lead_data"][k] = v
        return req

    def test_unknown_and_missing_kind(self):
        for kind in ("faktura", "", None, 5, ["zmluva"]):
            with self.subTest(kind=kind):
                req = example("gdpr")
                req["kind"] = kind
                self.assertIn("kind", self.fields_of(self.post(req)))
        req = example("gdpr")
        del req["kind"]
        self.assertIn("kind", self.fields_of(self.post(req)))

    def test_unknown_top_level_and_lead_data_fields(self):
        req = example("gdpr")
        req["url"] = "https://example.invalid/x.docx"
        self.assertEqual(self.fields_of(self.post(req))["url"], "neznáme pole")
        req = self.with_ld("gdpr", docx_url="https://example.invalid/x")
        self.assertIn("docx_url", self.fields_of(self.post(req)))
        # pole z iného druhu dokumentu sa pre tento druh nepoužíva
        req = self.with_ld("gdpr", cena_eur=100.0)
        self.assertIn("cena_eur", self.fields_of(self.post(req)))
        req = self.with_ld("splnomocnenie", telefon="+421 900 000 000")
        self.assertIn("telefon", self.fields_of(self.post(req)))

    def test_lead_data_must_be_object(self):
        for val in (None, [], "x", 3):
            req = example("gdpr")
            req["lead_data"] = val
            self.assertIn("lead_data", self.fields_of(self.post(req)))

    def test_missing_required_per_kind(self):
        for kind in inl.KINDS:
            for name in inl.KIND_FIELDS[kind]["required"]:
                with self.subTest(kind=kind, name=name):
                    req = example(kind)
                    req["lead_data"].pop(name, None)
                    f = self.fields_of(self.post(req))
                    self.assertIn(name, f)

    def test_types_and_bounds(self):
        cases = [
            ("zmluva", "cena_eur", "15689.17"), ("zmluva", "cena_eur", True), ("zmluva", "cena_eur", 0),
            ("zmluva", "cena_eur", -5), ("zmluva", "cena_eur", 5_000_000.01), ("zmluva", "cena_eur", 100.123),
            ("zmluva", "vykon_kwp", 0), ("zmluva", "vykon_kwp", 1000.5), ("zmluva", "vykon_kwp", "10,7"),
            ("zmluva", "vykon_kwp", 10.123), ("zmluva", "meno_priezvisko", 7), ("zmluva", "meno_priezvisko", "ab"),
            ("zmluva", "meno_priezvisko", "A" * 121), ("zmluva", "telefon", "abc"), ("zmluva", "email", "bez-zavinaca"),
            ("zmluva", "cislo_cp", "má medzery"), ("zmluva", "payment_terms", "70_30"),
            ("zmluva", "payment_terms", "60/30/10"), ("zmluva", "zaruka_panely_produkt", "25"),
            ("zmluva", "zaruka_panely_produkt", 25.5), ("zmluva", "zaruka_panely_produkt", 1),
            ("zmluva", "zaruka_panely_linear", 51), ("zmluva", "zaruka_panely_linear", True),
            ("splnomocnenie", "cislo_op", "12"), ("splnomocnenie", "cislo_op", "AB 12 --"),
        ]
        for kind, name, value in cases:
            with self.subTest(kind=kind, name=name, value=value):
                self.assertIn(name, self.fields_of(self.post(self.with_ld(kind, **{name: value}))))

    def test_dates_strict_format(self):
        for bad in ("2026-11-12", "12.11.26", "1.2.2026", "31.02.2026", "12/11/2026", " 12.11.2026", "12.11.2026 ",
                    "00.01.2026", "12.13.2026", 20261112, "12.11.1999", "12.11.2101"):
            with self.subTest(bad=bad):
                self.assertIn("datum_dnes", self.fields_of(self.post(self.with_ld("gdpr", datum_dnes=bad))))
        for bad in ("01.02.1899", "01.01.2999", "1980-02-01"):
            with self.subTest(bad=bad):
                self.assertIn("datum_narodenia",
                              self.fields_of(self.post(self.with_ld("gdpr", datum_narodenia=bad))))
        self.assertIn("datum_cp", self.fields_of(self.post(self.with_ld("zmluva", datum_cp="05-11-2026"))))

    def test_marker_and_control_characters_rejected(self):
        for bad in ("Jozef XXX Mrkva", "Jozef ____ Mrkva", "Jozef ..... Mrkva", "Jozef\nMrkva", "Jozef​Mrkva",
                    "Jozef‮Mrkva", "Jozef\x00Mrkva", "Jozef\tMrkva"):
            with self.subTest(bad=bad):
                self.assertIn("meno_priezvisko",
                              self.fields_of(self.post(self.with_ld("gdpr", meno_priezvisko=bad))))

    def test_warranty_rules(self):
        req = self.with_ld("zmluva", zaruka_panely_produkt=KeyError, zaruka_panely_linear=KeyError)
        f = self.fields_of(self.post(req))
        self.assertIn("zaruka_panely_produkt", f)
        self.assertIn("zaruka_panely_linear", f)
        req = self.with_ld("zmluva", zaruka_panely_linear=KeyError)
        self.assertIn("zaruka_panely_linear", self.fields_of(self.post(req)))
        req = self.with_ld("zmluva", panel_sku="PAN-001")  # aj explicitné záruky -> oboje je chyba
        self.assertIn("panel_sku", self.fields_of(self.post(req)))
        req = self.with_ld("zmluva", panel_sku="PAN-999", zaruka_panely_produkt=KeyError,
                           zaruka_panely_linear=KeyError)
        self.assertIn("panel_sku", self.fields_of(self.post(req)))

    def test_splnomocnenie_needs_an_address(self):
        req = self.with_ld("splnomocnenie", trvale_bydlisko=KeyError)
        self.assertIn("trvale_bydlisko", self.fields_of(self.post(req)))
        req = self.with_ld("splnomocnenie", trvale_bydlisko=KeyError, adresa="Ukážková 12, 811 01 Bratislava")
        self.assertEqual(self.post(req).status_code, 200)

    def test_body_errors(self):
        r = self.post(None, raw=b"{nie json")
        self.assertIn("_body", self.fields_of(r))
        r = self.post(None, raw=b'{"kind": "gdpr", "lead_data": {"cena_eur": NaN}}')
        self.assertIn("_body", self.fields_of(r))
        r = self.post(None, raw=b"[1,2]")
        self.assertIn("_body", self.fields_of(r))
        r = self.post(None, raw=b"\xff\xfe")
        self.assertIn("_body", self.fields_of(r))

    def test_request_size_limit(self):
        big = example("gdpr")
        big["lead_data"]["meno_priezvisko"] = "A" * (inl.MAX_BODY_BYTES + 100)
        r = self.post(big)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.get_json()["error"], "payload_too_large")

    def test_errors_never_echo_values(self):
        canary = "TAJNE-HODNOTA-12345"
        req = self.with_ld("zmluva", cena_eur=canary, telefon=canary, datum_dnes=canary, payment_terms=canary,
                           cislo_cp=canary + " x", meno_priezvisko=canary + "XXX")
        req["lead_data"]["pole"] = canary
        r = self.post(req)
        self.assertEqual(r.status_code, 422)
        body = r.get_data(as_text=True)
        self.assertNotIn(canary, body)
        self.assertNotIn("TAJNE-HODNOTA-12345", body)

    def test_valid_requests_from_fixture_pass(self):
        for kind in inl.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(self.post(example(kind)).status_code, 200)

    def test_text_values_are_stripped_and_nfc(self):
        req = self.with_ld("gdpr", meno_priezvisko="  Mária Zákazníková  ")
        j = self.post(req).get_json()
        text = docx_text(base64.b64decode(j["docx_base64"]))
        self.assertRegex(text, r"Meno a priezvisko:\s*Mária Zákazníková\s*$|Meno a priezvisko:\s*Mária Zákazníková\n")
        self.assertNotIn("  Mária Zákazníková  ", text)


class FillTests(Base):
    """Každý druh vyplní skutočnú šablónu (žiadny mock naplnif_*)."""

    def run_kind(self, req):
        r = self.post(req)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        j = r.get_json()
        return j, docx_text(base64.b64decode(j["docx_base64"]))

    def test_zmluva_fills_every_slot(self):
        req = example("zmluva")
        j, t = self.run_kind(req)
        self.assertNotIn("XXX", t)
        self.assertNotRegex(t, r"XX\.XX\.\d{4}")
        self.assertNotIn("Variant A", t)
        self.assertNotIn("Variant B", t)
        ld = req["lead_data"]
        for expected in (ld["meno_priezvisko"], "Adresa: " + ld["adresa"], "Telefón: " + ld["telefon"],
                         "E-mail: " + ld["email"], "výkonom  10,70 kWp", "PP-2026-0001 zo dňa 05.11.2026",
                         "Miesto výkonu predmetu Zmluvy: " + ld["miesto_vykonu"],
                         "dohodou 15 689,17 EUR s DPH",
                         "Slovom: pätnásťtisícšesťstoosemdesiatdeväť Eur a sedemnásť centov.",
                         "V Bratislave, dňa 12.11.2026", "25 rokov produktová záruka na fotovoltické panely",
                         "Minimálne 30 rokov lineárna výkonová záruka"):
            self.assertIn(expected, t)
        self.assertEqual(t.count(ld["meno_priezvisko"]), 2)  # objednávateľ + podpis
        self.assertEqual(j["filename"], "Zmluva_o_dielo.docx")

    def test_splnomocnenie_fills_table_and_signature(self):
        req = example("splnomocnenie")
        j, t = self.run_kind(req)
        doc = docx.Document(BytesIO(base64.b64decode(j["docx_base64"])))
        rows = {r.cells[0].text.strip(): r.cells[1].text.strip() for r in doc.tables[0].rows}
        self.assertEqual(rows, {"Meno a priezvisko": "Mária Zákazníková", "Číslo OP": "ZZ0001234",
                                "Dátum narodenia": "01.02.1980", "Bydlisko": "Ukážková 12, 811 01 Bratislava"})
        self.assertIn("V Bratislave, dňa 12.11.2026", t)
        self.assertEqual(j["filename"], "Splnomocnenie.docx")

    def test_splnomocnenie_falls_back_to_adresa(self):
        req = example("splnomocnenie")
        req["lead_data"].pop("trvale_bydlisko")
        req["lead_data"]["adresa"] = "Iná 5, 040 01 Košice"
        j, t = self.run_kind(req)
        doc = docx.Document(BytesIO(base64.b64decode(j["docx_base64"])))
        self.assertEqual(doc.tables[0].rows[3].cells[1].text.strip(), "Iná 5, 040 01 Košice")

    def test_gdpr_fills_underscores_and_signature(self):
        req = example("gdpr")
        j, t = self.run_kind(req)
        self.assertNotRegex(t, r"_{3,}")
        self.assertRegex(t, r"Meno a priezvisko:\s*Mária Zákazníková")
        self.assertRegex(t, r"Dátum narodenia:\s*01\.02\.1980")
        self.assertIn("V Bratislave, dňa 12.11.2026", t)
        self.assertEqual(j["filename"], "GDPR_suhlas.docx")

    def test_special_characters_are_escaped(self):
        req = example("gdpr")
        req["lead_data"]["meno_priezvisko"] = "Ján <Novák> & Syn"
        j, t = self.run_kind(req)
        self.assertIn("Ján <Novák> & Syn", t)
        req = example("zmluva")
        req["lead_data"]["adresa"] = "Ukážková 12 & <b>, 811 01 Bratislava"
        j, t = self.run_kind(req)
        self.assertIn("Adresa: Ukážková 12 & <b>, 811 01 Bratislava", t)

    def test_payment_variants(self):
        wanted = {
            "60_30_10": ["60% - zálohová", "30% - po nainštalovaní", "10% - po uvedení"],
            "60_40": ["60% - zálohová", "40% - po nainštalovaní"],
            "50_50": ["50% - zálohová", "50% - po nainštalovaní"],
            "30_70": ["30% - zálohová", "70% - po nainštalovaní"],
        }
        for pt, starts in wanted.items():
            with self.subTest(pt=pt):
                req = example("zmluva")
                req["lead_data"]["payment_terms"] = pt
                j, t = self.run_kind(req)
                lines = [l.strip() for l in t.split("\n") if re.match(r"^\s*\d+%\s*-", l)]
                self.assertEqual(len(lines), len(starts), lines)
                for line, start in zip(lines, starts):
                    self.assertTrue(line.startswith(start), (line, start))
                self.assertNotIn("Variant", t)

    def test_amount_in_words_golden_and_all_cents(self):
        golden = {
            15689.17: ("15 689,17", "pätnásťtisícšesťstoosemdesiatdeväť", "sedemnásť"),
            1000.0: ("1 000,00", "jedentisíc", "nula"),
            1234.5: ("1 234,50", "jedentisícdvestotridsaťštyri", "päťdesiat"),
            21.01: ("21,01", "dvadsaťjeden", "jeden"),
            2500.0: ("2 500,00", "dvetisícpäťsto", "nula"),
        }
        for amount, (num, eur_w, cents_w) in golden.items():
            with self.subTest(amount=amount):
                req = example("zmluva")
                req["lead_data"]["cena_eur"] = amount
                j, t = self.run_kind(req)
                self.assertIn(f"dohodou {num} EUR s DPH", t)
                self.assertIn(f"Slovom: {eur_w} Eur a {cents_w} centov.", t)
        # float zaokrúhlenie: žiadna dvojica (eur, centy) sa nesmie posunúť
        for eur in (1, 99, 100, 1999, 15689, 123456, 4_999_999):
            for cents in range(100):
                a = round(eur + cents / 100, 2)
                ew, cw = gd._eur_slovom(a)
                self.assertEqual((ew, cw), (gd._num_to_sk_words(eur), gd._num_to_sk_words(cents)), a)

    def test_warranty_from_sku_and_explicit(self):
        for sku, (zp, zl) in {"PAN-001": (25, 30), "PAN-002": (15, 25)}.items():
            with self.subTest(sku=sku):
                req = example("zmluva")
                req["lead_data"].pop("zaruka_panely_produkt")
                req["lead_data"].pop("zaruka_panely_linear")
                req["lead_data"]["panel_sku"] = sku
                j, t = self.run_kind(req)
                self.assertIn(f"{zp} rokov produktová záruka na fotovoltické panely", t)
                self.assertIn(f"Minimálne {zl} rokov lineárna výkonová záruka", t)
        req = example("zmluva")
        req["lead_data"].update(zaruka_panely_produkt=12, zaruka_panely_linear=28)
        j, t = self.run_kind(req)
        self.assertIn("12 rokov produktová záruka na fotovoltické panely", t)
        self.assertIn("Minimálne 28 rokov lineárna výkonová záruka", t)

    def test_response_hashes_and_shape(self):
        j, _ = self.run_kind(example("zmluva"))
        docx_b = base64.b64decode(j["docx_base64"])
        pdf_b = base64.b64decode(j["pdf_base64"])
        self.assertEqual(hashlib.sha256(docx_b).hexdigest(), j["docx_sha256"])
        self.assertEqual(hashlib.sha256(pdf_b).hexdigest(), j["pdf_sha256"])
        self.assertEqual(pdf_b, FAKE_PDF)
        self.assertEqual(j["converter"], "libreoffice")
        self.assertTrue(zipfile.is_zipfile(BytesIO(docx_b)))
        tpl = (ROOT / "templates_zmluvy" / "Zmluva_o_dielo_template.docx").read_bytes()
        self.assertEqual(j["template_sha256"], hashlib.sha256(tpl).hexdigest())

    def test_response_matches_shared_fixture(self):
        spec = FIXTURE["response_ok"]
        for kind in inl.KINDS:
            with self.subTest(kind=kind):
                j, _ = self.run_kind(example(kind))
                self.assertEqual(set(j), set(spec["keys"]))
                self.assertIs(j["ok"], True)
                self.assertEqual(j["kind"], kind)
                self.assertEqual(j["filename"], spec["filenames"][kind])
                for h in ("docx_sha256", "pdf_sha256", "template_sha256"):
                    self.assertRegex(j[h], r"^[0-9a-f]{64}$")
                self.assertIn(j["converter"], spec["converters"])

    def test_cache_control_no_store(self):
        self.assertEqual(self.post(example("gdpr")).headers.get("Cache-Control"), "no-store")
        self.assertEqual(self.post(example("gdpr"), headers={}).headers.get("Cache-Control"), "no-store")


class ContractFixtureTests(unittest.TestCase):
    """Fixture zdieľaná s portálom nesmie ujsť od skutočných whitelistov a limitov."""

    def test_fixture_matches_module(self):
        self.assertEqual(FIXTURE["route"], inl.ROUTE)
        self.assertEqual(set(FIXTURE["top_level_keys"]), inl.TOP_LEVEL_KEYS)
        self.assertEqual(set(FIXTURE["kinds"]), set(inl.KINDS))
        for kind in inl.KINDS:
            self.assertEqual(FIXTURE["kinds"][kind]["required"], inl.KIND_FIELDS[kind]["required"])
            self.assertEqual(FIXTURE["kinds"][kind]["optional"], inl.KIND_FIELDS[kind]["optional"])
        self.assertEqual(set(FIXTURE["fields"]), set(inl.FIELD_SPECS))
        for name, spec in inl.FIELD_SPECS.items():
            f = FIXTURE["fields"][name]
            if spec["type"] == "text":
                self.assertEqual((f["type"], f["min"], f["max"]), ("text", spec["min"], spec["max"]), name)
            elif spec["type"] in ("date", "birthdate"):
                self.assertEqual((f["type"], f["format"]), ("date", "DD.MM.YYYY"), name)
            elif spec["type"] == "number":
                self.assertEqual((f["type"], f["min"], f["max"]), ("number", spec["min"], spec["max"]), name)
            elif spec["type"] == "int":
                self.assertEqual((f["type"], f["min"], f["max"]), ("int", spec["min"], spec["max"]), name)
            elif spec["type"] == "enum":
                self.assertEqual((f["type"], tuple(f["values"])), ("enum", tuple(spec["values"])), name)
        self.assertEqual(FIXTURE["response_errors"]["413"]["max_bytes"], inl.MAX_BODY_BYTES)

    def test_every_field_is_read_by_a_filler(self):
        """Každé pole whitelistu sa naozaj číta v generuj_dokumenty (nepridávame pole, ktoré nič nerobí)."""
        src = (ROOT / "generuj_dokumenty.py").read_text(encoding="utf-8")
        for name in inl.FIELD_SPECS:
            if name == "panel_sku":
                continue  # ide cez panel_params_from_sku
            self.assertTrue(f"'{name}'" in src or f'"{name}"' in src, name)


class TemplateIntegrityTests(Base):
    def test_zmluva_template_has_exactly_12_placeholders_and_all_are_filled(self):
        tpl = ROOT / "templates_zmluvy" / "Zmluva_o_dielo_template.docx"
        xml = zipfile.ZipFile(tpl).read("word/document.xml").decode("utf-8")
        self.assertEqual(xml.count("XXX"), 12)
        ld = {"meno_priezvisko": "Meno-1", "adresa": "Adresa-2", "telefon": "+421 900 000 003",
              "email": "e4@example.test", "vykon_kwp": 5.5, "cislo_cp": "CP-6", "datum_cp": "07.01.2026",
              "miesto_vykonu": "Miesto-8", "cena_eur": 9999.99, "datum_dnes": "10.01.2026",
              "payment_terms": "60_30_10", "zaruka_panely_produkt": 15, "zaruka_panely_linear": 25}
        data = inl.render_docx("zmluva", ld)
        self.assertEqual(inl.verify_filled("zmluva", ld, data), [])
        t = docx_text(data)
        self.assertNotIn("XXX", t)
        for token in ("Meno-1", "Adresa-2", "Miesto-8", "CP-6", "07.01.2026", "5,50", "9 999,99",
                      "deväťtisícdeväťstodeväťdesiatdeväť", "deväťdesiatdeväť centov"):
            self.assertIn(token, t)

    def _with_modified_template(self, mutate):
        """Podstrčí kópiu šablóny zmluvy so zmenenou document.xml (priamo cez TEMPLATES_DIR)."""
        d = Path(tempfile.mkdtemp(prefix="inl-tpl-"))
        self.addCleanup(shutil.rmtree, d, True)
        for f in (ROOT / "templates_zmluvy").iterdir():
            shutil.copy(f, d / f.name)
        target = d / "Zmluva_o_dielo_template.docx"
        with zipfile.ZipFile(target) as z:
            members = {n: z.read(n) for n in z.namelist()}
        members["word/document.xml"] = mutate(members["word/document.xml"].decode("utf-8")).encode("utf-8")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in members.items():
                z.writestr(n, b)
        p = mock.patch.object(gd, "TEMPLATES_DIR", d)
        p.start()
        self.addCleanup(p.stop)

    def test_template_with_fewer_placeholders_is_rejected_not_silently_truncated(self):
        # posledné XXX (podpis) zmizne -> naplnif_zmluvu by hodnotu potichu zahodila
        def drop_last(xml):
            i = xml.rfind("XXX")
            return xml[:i] + "xxx" + xml[i + 3:]
        self._with_modified_template(drop_last)
        r = self.post(example("zmluva"))
        self.assertEqual(r.status_code, 422)
        j = r.get_json()
        self.assertEqual(j["error"], "document_incomplete")
        self.assertTrue(any("podpis_meno" in p for p in j["problems"]), j)
        self.assertNotIn("Zákazníková", r.get_data(as_text=True))

    def test_template_with_more_placeholders_is_rejected(self):
        self._with_modified_template(lambda xml: xml.replace("Lukáš Bago", "Lukáš Bago XXX", 1))
        r = self.post(example("zmluva"))
        self.assertEqual(r.status_code, 422)
        self.assertTrue(any("XXX" in p for p in r.get_json()["problems"]))

    def test_template_without_payment_variants_markers_is_rejected(self):
        # ak sa riadky platieb v šablóne zmenia tak, že výber variantu nezaberie, dokument sa nevydá
        self._with_modified_template(lambda xml: xml.replace(">Variant B:<", ">Variant Q:<"))
        r = self.post(example("zmluva"))
        self.assertEqual(r.status_code, 422)
        self.assertTrue(any("platobné podmienky" in p for p in r.get_json()["problems"]))

    def test_unfilled_gdpr_underscore_is_reported(self):
        d = Path(tempfile.mkdtemp(prefix="inl-tpl-"))
        self.addCleanup(shutil.rmtree, d, True)
        for f in (ROOT / "templates_zmluvy").iterdir():
            shutil.copy(f, d / f.name)
        with mock.patch.object(gd, "TEMPLATES_DIR", d), \
                mock.patch.object(gd, "_replace_underscores_in_para", return_value=False):
            r = self.post(example("gdpr"))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["error"], "document_incomplete")


class WarrantyHelperTests(unittest.TestCase):
    def test_rule_table(self):
        self.assertEqual(gd.panel_params_from_sku("PAN-001"),
                         {"panel_sku": "PAN-001", "panel_wp": 470, "zaruka_panely_produkt": 25, "zaruka_panely_linear": 30})
        self.assertEqual(gd.panel_params_from_sku("PAN-002"),
                         {"panel_sku": "PAN-002", "panel_wp": 535, "zaruka_panely_produkt": 15, "zaruka_panely_linear": 25})
        for sku in (None, ""):
            self.assertEqual(gd.panel_params_from_sku(sku)["panel_sku"], "PAN-001")
            self.assertEqual(gd.panel_params_from_sku(sku)["zaruka_panely_produkt"], 25)
        other = gd.panel_params_from_sku("PAN-777")  # staré správanie: 470 Wp a 15/25
        self.assertEqual((other["panel_wp"], other["zaruka_panely_produkt"], other["zaruka_panely_linear"]), (470, 15, 25))


class ConverterTests(Base):
    def setUp(self):
        super().setUp()
        self.pdf.stop()  # tieto testy skúšajú skutočné convert_to_pdf s podstrčenými backendmi
        self.addCleanup(self.pdf.start)

    def test_gotenberg_used_when_configured(self):
        ok = mock.Mock(ok=True, status_code=200, content=FAKE_PDF)
        with mock.patch.dict(os.environ, {"GOTENBERG_URL": "https://gotenberg.example.test/"}), \
                mock.patch.object(inl.requests, "post", return_value=ok) as post:
            j = self.post(example("gdpr")).get_json()
        self.assertEqual(j["converter"], "gotenberg-libreoffice")
        self.assertEqual(base64.b64decode(j["pdf_base64"]), FAKE_PDF)
        self.assertEqual(post.call_args[0][0], "https://gotenberg.example.test/forms/libreoffice/convert")

    def test_converters_get_a_meaningful_input_file_name(self):
        """Päta šablóny tlačí názov súboru — nesmie to byť `in.docx`/`document.docx`."""
        ok = mock.Mock(ok=True, status_code=200, content=FAKE_PDF)
        with mock.patch.dict(os.environ, {"GOTENBERG_URL": "https://gotenberg.example.test"}), \
                mock.patch.object(inl.requests, "post", return_value=ok) as post:
            self.post(example("zmluva"))
        self.assertEqual(post.call_args[1]["files"]["files"][0], "Zmluva_o_dielo.docx")
        seen = []

        class FakeProc:
            def __init__(self, cmd, **kw):
                seen.append(cmd)
                outdir = cmd[cmd.index("--outdir") + 1]
                src = cmd[-1]
                with open(os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf"), "wb") as fh:
                    fh.write(FAKE_PDF)

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(inl, "_soffice_bin", return_value="/usr/bin/soffice"), \
                mock.patch.object(inl.subprocess, "Popen", FakeProc):
            j = self.post(example("splnomocnenie")).get_json()
        self.assertEqual(j["converter"], "libreoffice")
        self.assertEqual(os.path.basename(seen[0][-1]), "Splnomocnenie.docx")

    def test_gotenberg_failure_falls_back_to_libreoffice(self):
        bad = mock.Mock(ok=False, status_code=500, content=b"err")
        with mock.patch.dict(os.environ, {"GOTENBERG_URL": "https://gotenberg.example.test"}), \
                mock.patch.object(inl.requests, "post", return_value=bad), \
                mock.patch.object(inl, "_convert_libreoffice", return_value=FAKE_PDF) as lo:
            j = self.post(example("gdpr")).get_json()
        self.assertEqual(j["converter"], "libreoffice")
        lo.assert_called_once()

    def test_gotenberg_non_pdf_is_not_accepted(self):
        html = mock.Mock(ok=True, status_code=200, content=b"<html>chyba</html>")
        with mock.patch.dict(os.environ, {"GOTENBERG_URL": "https://gotenberg.example.test"}), \
                mock.patch.object(inl.requests, "post", return_value=html), \
                mock.patch.object(inl, "_soffice_bin", return_value=None):
            r = self.post(example("gdpr"))
        self.assertEqual(r.status_code, 503)

    def test_no_converter_is_503_unless_docx_only_allowed(self):
        with mock.patch.object(inl, "_soffice_bin", return_value=None):
            r = self.post(example("zmluva"))
            self.assertEqual(r.status_code, 503)
            j = r.get_json()
            self.assertEqual(j["error"], "pdf_converter_unavailable")
            self.assertNotIn("docx_base64", j)
            req = example("zmluva")
            req["allow_docx_only"] = True
            r = self.post(req)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertIsNone(j["pdf_base64"])
            self.assertIsNone(j["pdf_sha256"])
            self.assertIsNone(j["converter"])
            self.assertTrue(j["docx_base64"])
            req["allow_docx_only"] = "yes"
            self.assertIn("allow_docx_only", self.post(req).get_json()["fields"])

    def test_lossy_renderers_are_never_used(self):
        import ast
        tree = ast.parse((ROOT / "inline_documents.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"mammoth", "weasyprint"}, imported)
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertNotIn("branded-fallback", strings)

    @unittest.skipUnless(shutil.which("soffice") or shutil.which("libreoffice"), "LibreOffice nie je k dispozícii")
    def test_real_libreoffice_produces_a_pdf(self):
        j = self.post(example("gdpr")).get_json()
        pdf = base64.b64decode(j["pdf_base64"])
        self.assertEqual(j["converter"], "libreoffice")
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(hashlib.sha256(pdf).hexdigest(), j["pdf_sha256"])
        self.assertEqual(self.leftovers(), [])
        try:
            import fitz  # PyMuPDF — voliteľné
        except ImportError:
            return
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            text = "".join(p.get_text() for p in doc)
        self.assertIn("Zákazníková", text)
        self.assertNotRegex(text, r"_{3,}")
        # päta (pole „názov súboru") nenesie technický názov vstupu
        self.assertNotIn("in.docx", text)
        self.assertNotIn("document.docx", text)
        # zmluva má v päte pole s názvom súboru: musí to byť názov dokumentu
        j = self.post(example("zmluva")).get_json()
        with fitz.open(stream=base64.b64decode(j["pdf_base64"]), filetype="pdf") as doc:
            zml = "".join(p.get_text() for p in doc)
        self.assertNotIn("in.docx", zml)
        self.assertIn("Zmluva_o_dielo.docx", zml)


class HygieneTests(Base):
    def test_no_temp_files_left_behind(self):
        for kind in inl.KINDS:
            self.assertEqual(self.post(example(kind)).status_code, 200)
        self.assertEqual(self.leftovers(), [])
        # aj pri chybe plnenia
        with mock.patch.object(gd, "naplnif_gdpr", side_effect=RuntimeError("zlyhalo")):
            self.assertEqual(self.post(example("gdpr")).status_code, 500)
        self.assertEqual(self.leftovers(), [])
        # aj pri neúplnom dokumente
        with mock.patch.object(inl, "verify_filled", return_value=["x"]):
            self.assertEqual(self.post(example("gdpr")).status_code, 422)
        self.assertEqual(self.leftovers(), [])

    def test_temp_dir_is_private(self):
        seen = []
        real = tempfile.mkdtemp

        def spy(*a, **k):
            d = real(*a, **k)
            seen.append(oct(os.stat(d).st_mode & 0o777))
            return d
        with mock.patch.object(inl.tempfile, "mkdtemp", side_effect=spy):
            self.post(example("gdpr"))
        self.assertEqual(seen, ["0o700"])

    def test_render_failure_does_not_leak_details(self):
        with mock.patch.object(gd, "naplnif_gdpr", side_effect=RuntimeError(CANARY_NAME)):
            r = self.post(example("gdpr"))
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json(), {"ok": False, "error": "render_failed"})
        self.assertNotIn(CANARY_NAME, r.get_data(as_text=True))

    def test_no_network_and_no_outbound_calls(self):
        with mock.patch.object(socket.socket, "connect", side_effect=OSError("sieť je v testoch zablokovaná")), \
                mock.patch.object(inl.requests, "get", side_effect=AssertionError("outbound GET")), \
                mock.patch.object(inl.requests, "post", side_effect=AssertionError("outbound POST")), \
                mock.patch.object(inl.requests, "patch", side_effect=AssertionError("outbound PATCH")):
            for kind in inl.KINDS:
                self.assertEqual(self.post(example(kind)).status_code, 200)
            req = example("gdpr")
            req["lead_data"]["meno_priezvisko"] = "https://example.invalid/steal.docx"
            self.assertEqual(self.post(req).status_code, 200)  # URL v hodnote je len text, nič sa nesťahuje

    def test_payload_is_never_logged(self):
        records = []

        class Cap(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
                if record.exc_text:
                    records.append(record.exc_text)

        h = Cap(level=logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(h)
        gen_log = logging.getLogger("generuj_dokumenty")
        old_prop = gen_log.propagate
        try:
            for kind in inl.KINDS:
                req = example(kind)
                req["lead_data"]["meno_priezvisko"] = CANARY_NAME
                if kind == "splnomocnenie":
                    req["lead_data"]["cislo_op"] = "TAJNEOP99"
                self.assertEqual(self.post(req).status_code, 200)
            bad = example("zmluva")
            bad["lead_data"]["meno_priezvisko"] = CANARY_NAME
            bad["lead_data"]["cena_eur"] = "zle"
            self.post(bad)
            with mock.patch.object(gd, "naplnif_gdpr", side_effect=RuntimeError(CANARY_NAME)):
                req = example("gdpr")
                req["lead_data"]["meno_priezvisko"] = CANARY_NAME
                self.post(req)
        finally:
            root.removeHandler(h)
            root.setLevel(old_level)
            gen_log.propagate = old_prop
        text = "\n".join(records)
        self.assertTrue(records, "očakával som aspoň riadok s druhom/veľkosťou")
        for secret in (CANARY_NAME, "TAJNEOP99", "Zákazníková", "15689", "15 689", "maria.zakaznikova", "Ukážková"):
            self.assertNotIn(secret, text)
        self.assertIn("kind=gdpr", text)

    def test_logging_of_other_callers_unchanged(self):
        """Filter potláča naplnif_* logy len počas inline požiadavky; mimo nej sa loguje ako predtým."""
        messages = []

        class Cap(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        h = Cap(level=logging.INFO)
        lg = logging.getLogger("generuj_dokumenty")
        old = lg.level
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
        try:
            out = Path(self._tmp) / "x.docx"
            gd.naplnif_gdpr({"meno_priezvisko": "Mária Zákazníková", "datum_narodenia": "01.02.1980",
                             "datum_dnes": "12.11.2026"}, str(out))
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        self.assertTrue(any("gdpr" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
