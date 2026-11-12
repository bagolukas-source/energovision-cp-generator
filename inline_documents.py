"""Inline generovanie zmluvných dokumentov (zmluva o dielo, splnomocnenie, GDPR súhlas).

Route `POST /webhook/generuj-dokumenty-inline` — pre server-to-server volajúceho (partnerský portál),
ktorý má dáta u seba a nechce, aby generátor siahal do CRM, Notionu ani do verejného úložiska.

Princíp:
  * ROVNAKÉ šablóny a ROVNAKÉ funkcie `naplnif_*` z `generuj_dokumenty.py` ako v CRM — žiadna kópia logiky šablón;
  * PDF sa vyrába v rámci požiadavky rovnakými verným konvertormi ako `/webhook/docx-to-pdf`
    (Gotenberg, inak LibreOffice). Stratový fallback (mammoth/WeasyPrint) sa pre právne dokumenty NEPOUŽÍVA;
  * FAIL-CLOSED autentifikácia (503 bez WEBHOOK_SECRET, 401 pri zlej/chýbajúcej hlavičke, konštantný čas);
  * prísna validácia vstupu (whitelist polí podľa druhu dokumentu, typy, dĺžky, formáty, rozsahy);
  * kontrola úplnosti — po vyplnení sa v DOCX hľadajú zvyšné značky a overuje sa, že každá odoslaná hodnota
    je v dokumente na svojom mieste; inak 422 s menom nevyplneného poľa (nikdy zmluva s dierou);
  * žiadne ukladanie, žiadny upload, žiadne volanie DB/Notionu, žiadne sťahovanie URL od volajúceho;
    dočasné súbory len v súkromnom adresári, vždy zmazané vo `finally`;
  * payload sa nikdy nelogguje (len druh, veľkosti, trvanie, skrátené hashe).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request
from lxml import etree

import generuj_dokumenty as gd

log = logging.getLogger("inline_documents")

ROUTE = "/webhook/generuj-dokumenty-inline"
MAX_BODY_BYTES = 16 * 1024            # reálny payload má < 2 kB
CONVERT_TIMEOUT_S = 90
TMP_PREFIX = "inlinedoc_"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

KINDS = ("zmluva", "splnomocnenie", "gdpr")
PAYMENT_TERMS = ("60_30_10", "60_40", "50_50", "30_70")
PANEL_SKUS = ("PAN-001", "PAN-002")

TEMPLATE_FILES = {
    "zmluva": "Zmluva_o_dielo_template.docx",
    "splnomocnenie": "Splnomocnenie_template.docx",
    "gdpr": "GDPR_suhlas_template.docx",
}
DOCX_FILENAMES = {
    "zmluva": "Zmluva_o_dielo.docx",
    "splnomocnenie": "Splnomocnenie.docx",
    "gdpr": "GDPR_suhlas.docx",
}

# ----------------------------------------------------------------------------
# Špecifikácia polí — presne tie, ktoré naplnif_* čítajú z lead_data
# ----------------------------------------------------------------------------
_SAFE_TEXT_BAD = re.compile(r"[\u0000-\u001f\u007f-\u009f​-‏‪-‮  ⁠﻿]")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()/.\-]{5,28}[0-9]$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")
_CP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{2,39}$")
_OP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{3,18}[A-Za-z0-9]$")
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def _text(min_len, max_len, pattern=None):
    return {"type": "text", "min": min_len, "max": max_len, "re": pattern}


FIELD_SPECS = {
    "meno_priezvisko": _text(3, 120),
    "adresa": _text(5, 200),
    "trvale_bydlisko": _text(5, 200),
    "miesto_vykonu": _text(5, 200),
    "telefon": _text(7, 30, _PHONE_RE),
    "email": _text(6, 254, _EMAIL_RE),
    "cislo_cp": _text(3, 40, _CP_RE),
    "cislo_op": _text(5, 20, _OP_RE),
    "datum_cp": {"type": "date", "min_year": 2020, "max_year": 2100},
    "datum_dnes": {"type": "date", "min_year": 2020, "max_year": 2100},
    "datum_narodenia": {"type": "birthdate"},
    "vykon_kwp": {"type": "number", "min": 0.1, "max": 1000.0},
    "cena_eur": {"type": "number", "min": 1.0, "max": 5_000_000.0},
    "payment_terms": {"type": "enum", "values": PAYMENT_TERMS},
    "panel_sku": {"type": "enum", "values": PANEL_SKUS},
    "zaruka_panely_produkt": {"type": "int", "min": 2, "max": 40},
    "zaruka_panely_linear": {"type": "int", "min": 2, "max": 50},
}

# Čo kto smie / musí poslať. (Záruky a sú-/ne-povinné skupiny rieši _cross_checks.)
KIND_FIELDS = {
    "zmluva": {
        "required": ["meno_priezvisko", "adresa", "telefon", "email", "vykon_kwp", "cislo_cp", "datum_cp",
                     "miesto_vykonu", "cena_eur", "datum_dnes", "payment_terms"],
        "optional": ["panel_sku", "zaruka_panely_produkt", "zaruka_panely_linear"],
    },
    "splnomocnenie": {
        "required": ["meno_priezvisko", "cislo_op", "datum_narodenia", "datum_dnes"],
        "optional": ["trvale_bydlisko", "adresa"],
    },
    "gdpr": {
        "required": ["meno_priezvisko", "datum_narodenia", "datum_dnes"],
        "optional": [],
    },
}

TOP_LEVEL_KEYS = {"kind", "lead_data", "allow_docx_only"}


class ValidationError(Exception):
    def __init__(self, fields):
        super().__init__("validation_failed")
        self.fields = fields


def _check_text(name, value, spec):
    if not isinstance(value, str):
        return None, "musí byť text"
    v = unicodedata.normalize("NFC", value).strip()
    if len(v) < spec["min"]:
        return None, f"príliš krátke (min. {spec['min']} znakov)" if v else "nesmie byť prázdne"
    if len(v) > spec["max"]:
        return None, f"príliš dlhé (max. {spec['max']} znakov)"
    if _SAFE_TEXT_BAD.search(v):
        return None, "obsahuje nepovolené riadiace alebo neviditeľné znaky"
    if "XXX" in v or re.search(r"_{3,}", v) or re.search(r"\.{4,}", v):
        return None, "obsahuje znaky, ktoré sa v šablóne používajú ako značka (XXX, ___, ....)"
    if spec["re"] is not None and not spec["re"].match(v):
        return None, "má neplatný formát"
    return v, None


def _check_date(name, value, spec):
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None, "dátum musí byť vo formáte DD.MM.RRRR"
    try:
        d = datetime.strptime(value, "%d.%m.%Y")
    except ValueError:
        return None, "neplatný kalendárny dátum"
    if spec["type"] == "birthdate":
        if d.year < 1900 or d > datetime.now():
            return None, "dátum narodenia mimo povoleného rozsahu"
    elif not (spec["min_year"] <= d.year <= spec["max_year"]):
        return None, f"rok mimo rozsahu {spec['min_year']}–{spec['max_year']}"
    return value, None


def _check_number(name, value, spec):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "musí byť číslo"
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        return None, "musí byť konečné číslo"
    if not (spec["min"] <= f <= spec["max"]):
        return None, f"mimo povoleného rozsahu ({spec['min']}–{spec['max']})"
    if abs(round(f, 2) - f) > 1e-9:
        return None, "najviac 2 desatinné miesta"
    return round(f, 2), None


def _check_int(name, value, spec):
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "musí byť celé číslo"
    if not (spec["min"] <= value <= spec["max"]):
        return None, f"mimo povoleného rozsahu ({spec['min']}–{spec['max']})"
    return value, None


def _check_enum(name, value, spec):
    if not isinstance(value, str) or value not in spec["values"]:
        return None, "nepovolená hodnota (povolené: " + ", ".join(spec["values"]) + ")"
    return value, None


_CHECKERS = {"text": _check_text, "date": _check_date, "birthdate": _check_date, "number": _check_number,
             "int": _check_int, "enum": _check_enum}


def _safe_key(key):
    s = re.sub(r"[^A-Za-z0-9_.\-]", "?", str(key))[:40]
    return s or "?"


def validate_request(body):
    """Vráti (kind, lead_data, allow_docx_only) alebo vyhodí ValidationError s menami polí a správami
    (hodnoty sa do správ NIKDY neopisujú)."""
    errors = {}
    if not isinstance(body, dict):
        raise ValidationError({"_body": "telo musí byť JSON objekt"})
    for k in body:
        if k not in TOP_LEVEL_KEYS:
            errors[_safe_key(k)] = "neznáme pole"
    kind = body.get("kind")
    if kind not in KINDS:
        errors["kind"] = "povolené: " + ", ".join(KINDS)
    allow = body.get("allow_docx_only", False)
    if not isinstance(allow, bool):
        errors["allow_docx_only"] = "musí byť true/false"
    data = body.get("lead_data")
    if not isinstance(data, dict):
        errors["lead_data"] = "musí byť objekt"
    if errors or kind not in KINDS:
        raise ValidationError(errors)

    fields = KIND_FIELDS[kind]
    allowed = set(fields["required"]) | set(fields["optional"])
    clean = {}
    for k in data:
        if k not in allowed:
            errors[_safe_key(k)] = "pole sa pre tento druh dokumentu nepoužíva"
    for name in fields["required"]:
        if name not in data or data[name] is None:
            errors[name] = "povinné pole"
    for name in allowed:
        if name in data and data[name] is not None and name not in errors:
            spec = FIELD_SPECS[name]
            v, err = _CHECKERS[spec["type"]](name, data[name], spec)
            if err:
                errors[name] = err
            else:
                clean[name] = v
    errors.update(_cross_checks(kind, data, clean, errors))
    if errors:
        raise ValidationError(errors)
    return kind, clean, allow


def _cross_checks(kind, data, clean, errors):
    out = {}
    if kind == "zmluva":
        explicit = [n for n in ("zaruka_panely_produkt", "zaruka_panely_linear") if data.get(n) is not None]
        has_sku = data.get("panel_sku") is not None
        if has_sku and explicit:
            out["panel_sku"] = "použi buď panel_sku, alebo explicitné záruky, nie oboje"
        elif not has_sku and len(explicit) != 2:
            for n in ("zaruka_panely_produkt", "zaruka_panely_linear"):
                if data.get(n) is None:
                    out[n] = "povinné (alebo pošli panel_sku)"
    if kind == "splnomocnenie":
        if "trvale_bydlisko" not in clean and "adresa" not in clean \
                and "trvale_bydlisko" not in errors and "adresa" not in errors:
            out["trvale_bydlisko"] = "povinné (trvalé bydlisko alebo adresa)"
    return out


def resolve_lead_data(kind, clean):
    """Zloží lead_data presne v tvare, v akom ho čítajú naplnif_* (záruky podľa SKU cez zdieľaný helper)."""
    ld = dict(clean)
    if kind == "zmluva":
        if "panel_sku" in ld:
            params = gd.panel_params_from_sku(ld.pop("panel_sku"))
            ld["zaruka_panely_produkt"] = params["zaruka_panely_produkt"]
            ld["zaruka_panely_linear"] = params["zaruka_panely_linear"]
    return ld


# ----------------------------------------------------------------------------
# Plnenie šablóny + kontrola úplnosti
# ----------------------------------------------------------------------------
_FILLERS = {"zmluva": "naplnif_zmluvu", "splnomocnenie": "naplnif_splnomocnenie", "gdpr": "naplnif_gdpr"}


def _paragraph_texts(docx_bytes):
    """Texty všetkých odsekov (telo, tabuľky, textové polia, hlavičky, päty) — priamo z XML."""
    out = []
    with zipfile.ZipFile(BytesIO(docx_bytes)) as z:
        names = [n for n in z.namelist()
                 if n == "word/document.xml" or re.match(r"word/(header|footer|footnotes|endnotes)\d*\.xml$", n)]
        for n in names:
            root = etree.fromstring(z.read(n))
            for p in root.iter(f"{{{W_NS}}}p"):
                parts = []
                for el in p.iter():
                    if el.tag == f"{{{W_NS}}}t":
                        parts.append(el.text or "")
                    elif el.tag == f"{{{W_NS}}}tab":
                        parts.append("\t")
                out.append(gd._norm("".join(parts)))
    return out


def _price_text(cena):
    return f"{cena:,.2f}".replace(",", " ").replace(".", ",")


def _expected_zmluva(ld):
    kwp = gd._sk_num(ld["vykon_kwp"])
    cena = ld["cena_eur"]
    eur_w, cents_w = gd._eur_slovom(cena)
    zp, zl = ld["zaruka_panely_produkt"], ld["zaruka_panely_linear"]
    return {
        "objednavatel_meno": ("exact", ld["meno_priezvisko"]),
        "adresa": ("exact", f"Adresa: {ld['adresa']}"),
        "telefon": ("exact", f"Telefón: {ld['telefon']}"),
        "email": ("exact", f"E-mail: {ld['email']}"),
        "vykon_kwp": ("regex", rf"výkonom\s+{re.escape(kwp)} kWp"),
        "cislo_cp": ("regex", rf"Cenovej ponuky dodávateľa {re.escape(ld['cislo_cp'])} zo dňa "
                              rf"{re.escape(ld['datum_cp'])}"),
        "miesto_vykonu": ("exact", f"Miesto výkonu predmetu Zmluvy: {ld['miesto_vykonu']}"),
        "cena_eur": ("regex", rf"dohodou {re.escape(_price_text(cena))} EUR s DPH"),
        "cena_slovom": ("regex", rf"^Slovom: {re.escape(eur_w)} Eur a {re.escape(cents_w)} centov\.?\s*$"),
        "datum_dnes": ("exact", f"V Bratislave, dňa {ld['datum_dnes']}"),
        "podpis_meno": ("regex", rf"^Lukáš Bago\s+{re.escape(ld['meno_priezvisko'])}$"),
        "zaruka_panely_produkt": ("regex", rf"^{zp} rokov produktová záruka na fotovoltické panely"),
        "zaruka_panely_linear": ("regex", rf"^Minimálne {zl} rokov lineárna výkonová záruka"),
    }


_PAYMENT_EXPECT = {
    "60_30_10": [r"^60% - ", r"^30% - ", r"^10% - "],
    "60_40": [r"^60% - ", r"^40% - "],
    "50_50": [r"^50% - ", r"^50% - "],
    "30_70": [r"^30% - ", r"^70% - "],
}


def verify_filled(kind, ld, docx_bytes):
    """Zoznam problémov (prázdny = dokument je úplný). Správy neobsahujú hodnoty, len mená polí/značky."""
    paras = _paragraph_texts(docx_bytes)
    joined = "\n".join(paras)
    problems = []

    # 1) zvyšné značky šablóny
    leftovers = {
        "zmluva": [("XXX", r"XXX"), ("XX.XX.RRRR", r"XX\.XX\.\d{4}"), ("Variant A/B", r"Variant [AB]:"),
                   ("____", r"_{3,}")],
        "splnomocnenie": [("XXX", r"XXX"), ("____", r"_{3,}")],
        "gdpr": [("XXX", r"XXX"), ("____", r"_{3,}")],
    }[kind]
    for label, pat in leftovers:
        n = len(re.findall(pat, joined))
        if n:
            problems.append(f"nevyplnená značka {label} ({n}x)")

    # 2) každá odoslaná hodnota je v dokumente na svojom mieste
    def has_exact(text):
        return any(p.strip() == text for p in paras)

    def has_regex(pat):
        return any(re.search(pat, p.strip()) for p in paras)

    if kind == "zmluva":
        for name, (mode, arg) in _expected_zmluva(ld).items():
            ok = has_exact(arg) if mode == "exact" else has_regex(arg)
            if not ok:
                problems.append(f"hodnota poľa {name} sa v dokumente nenachádza")
        pay_lines = [p.strip() for p in paras if re.match(r"^\d+%\s*-", p.strip())]
        want = _PAYMENT_EXPECT[ld["payment_terms"]]
        if len(pay_lines) != len(want) or not all(re.match(w, l) for w, l in zip(want, pay_lines)):
            problems.append("platobné podmienky (payment_terms) nezodpovedajú zvolenému variantu")
    elif kind == "splnomocnenie":
        bydlisko = ld.get("trvale_bydlisko") or ld.get("adresa")
        rows = _table_rows(docx_bytes, 0)
        want = {"Meno a priezvisko": ld["meno_priezvisko"], "Číslo OP": ld["cislo_op"],
                "Dátum narodenia": ld["datum_narodenia"], "Bydlisko": bydlisko}
        for label, value in want.items():
            if rows.get(label) != value:
                problems.append(f"pole „{label}“ v tabuľke údajov nie je vyplnené")
        if not has_exact(f"V Bratislave, dňa {ld['datum_dnes']}"):
            problems.append("hodnota poľa datum_dnes sa v dokumente nenachádza")
        if not has_exact(ld["meno_priezvisko"]):
            problems.append("meno v podpisovom bloku nie je vyplnené")
    elif kind == "gdpr":
        if not has_regex(rf"^Meno a priezvisko:\s*{re.escape(ld['meno_priezvisko'])}$"):
            problems.append("hodnota poľa meno_priezvisko sa v dokumente nenachádza")
        if not has_regex(rf"^Dátum narodenia:\s*{re.escape(ld['datum_narodenia'])}$"):
            problems.append("hodnota poľa datum_narodenia sa v dokumente nenachádza")
        if not has_exact(f"V Bratislave, dňa {ld['datum_dnes']}"):
            problems.append("hodnota poľa datum_dnes sa v dokumente nenachádza")
        if not has_exact(ld["meno_priezvisko"]):
            problems.append("meno v podpisovom bloku nie je vyplnené")
    return problems


def _table_rows(docx_bytes, table_index):
    """{label: hodnota} z dvojstĺpcovej tabuľky (pre splnomocnenie — údaje splnomocniteľa)."""
    from docx import Document
    doc = Document(BytesIO(docx_bytes))
    out = {}
    if len(doc.tables) > table_index:
        for row in doc.tables[table_index].rows:
            cells = row.cells
            if len(cells) >= 2:
                out[gd._norm(cells[0].text).strip()] = gd._norm(cells[1].text).strip()
    return out


class _PiiLogFilter(logging.Filter):
    """naplnif_* logujú meno klienta (INFO). Počas inline požiadavky (flag v thread-local) ich zahodíme —
    správanie ostatných route sa nemení."""
    _local = threading.local()

    def filter(self, record):
        return not getattr(self._local, "active", False)


_PII_FILTER = _PiiLogFilter()
logging.getLogger("generuj_dokumenty").addFilter(_PII_FILTER)


def render_docx(kind, ld):
    """Vyplní skutočnú šablónu cez naplnif_* a vráti bajty DOCX. Dočasný súbor v súkromnom adresári, vždy zmazaný."""
    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    _PiiLogFilter._local.active = True
    try:
        out = Path(tmpdir) / "doc.docx"
        getattr(gd, _FILLERS[kind])(ld, str(out))
        return out.read_bytes()
    finally:
        _PiiLogFilter._local.active = False
        shutil.rmtree(tmpdir, ignore_errors=True)


# ----------------------------------------------------------------------------
# DOCX -> PDF (len verné konvertory)
# ----------------------------------------------------------------------------
def _is_pdf(b):
    return bool(b) and b[:5] == b"%PDF-" and b"%%EOF" in b[-2048:]


def _convert_gotenberg(docx_bytes):
    url = os.environ.get("GOTENBERG_URL", "").rstrip("/")
    if not url:
        return None
    user = os.environ.get("GOTENBERG_USER", "")
    pw = os.environ.get("GOTENBERG_PASS", "")
    auth = (user, pw) if user else None
    files = {"files": ("document.docx", docx_bytes,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    try:
        r = requests.post(f"{url}/forms/libreoffice/convert", files=files, auth=auth, timeout=CONVERT_TIMEOUT_S)
        if r.ok and _is_pdf(r.content):
            return r.content
        log.warning("[inline] gotenberg rc=%s len=%s", r.status_code, len(r.content or b""))
    except Exception as e:  # noqa: BLE001 — konverzia je best-effort, spadne na ďalší konvertor
        log.warning("[inline] gotenberg výnimka %s", type(e).__name__)
    return None


def _soffice_bin():
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_libreoffice(docx_bytes):
    soffice = _soffice_bin()
    if not soffice:
        return None
    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    proc = None
    try:
        in_path = os.path.join(tmpdir, "in.docx")
        with open(in_path, "wb") as fh:
            fh.write(docx_bytes)
        env = dict(os.environ, HOME=tmpdir)
        proc = subprocess.Popen(
            [soffice, "--headless", "--norestore", f"-env:UserInstallation=file://{tmpdir}/loprofile",
             "--convert-to", "pdf:writer_pdf_Export", "--outdir", tmpdir, in_path],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            rc = proc.wait(timeout=CONVERT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            log.warning("[inline] libreoffice timeout")
            return None
        out_pdf = os.path.join(tmpdir, "in.pdf")
        if rc == 0 and os.path.exists(out_pdf):
            with open(out_pdf, "rb") as fh:
                pdf = fh.read()
            if _is_pdf(pdf):
                return pdf
        log.warning("[inline] libreoffice rc=%s", rc)
    except Exception as e:  # noqa: BLE001
        log.warning("[inline] libreoffice výnimka %s", type(e).__name__)
        if proc is not None and proc.poll() is None:
            _kill_group(proc)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def convert_to_pdf(docx_bytes):
    """(pdf_bytes, converter) alebo (None, None). Poradie rovnaké ako /webhook/docx-to-pdf: Gotenberg, LibreOffice."""
    pdf = _convert_gotenberg(docx_bytes)
    if pdf:
        return pdf, "gotenberg-libreoffice"
    pdf = _convert_libreoffice(docx_bytes)
    if pdf:
        return pdf, "libreoffice"
    return None, None


# ----------------------------------------------------------------------------
# Route
# ----------------------------------------------------------------------------
bp = Blueprint("inline_documents", __name__)


def _json(payload, status):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _authorize():
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if not expected.strip():
        log.error("[inline] WEBHOOK_SECRET nie je nastavený — odmietnuté (503)")
        return _json({"ok": False, "error": "server misconfigured"}, 503)
    received = request.headers.get("X-Webhook-Secret", "")
    if not hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
        log.warning("[inline] chýbajúce/neplatné X-Webhook-Secret (401)")
        return _json({"ok": False, "error": "unauthorized"}, 401)
    return None


def _read_body():
    """Telo najviac MAX_BODY_BYTES; vráti (obj, None) alebo (None, odpoveď)."""
    cl = request.content_length
    if cl is not None and cl > MAX_BODY_BYTES:
        return None, _json({"ok": False, "error": "payload_too_large", "max_bytes": MAX_BODY_BYTES}, 413)
    raw = request.stream.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return None, _json({"ok": False, "error": "payload_too_large", "max_bytes": MAX_BODY_BYTES}, 413)

    def _bad_const(_):
        raise ValueError("nan")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_bad_const), None
    except (ValueError, UnicodeDecodeError):
        return None, _json({"ok": False, "error": "validation_failed",
                            "fields": {"_body": "telo nie je platný JSON"}}, 422)


@bp.route(ROUTE, methods=["POST"])
def generuj_dokumenty_inline():
    denied = _authorize()
    if denied is not None:
        return denied
    t0 = time.monotonic()
    body, err = _read_body()
    if err is not None:
        return err
    try:
        kind, clean, allow_docx_only = validate_request(body)
    except ValidationError as e:
        return _json({"ok": False, "error": "validation_failed", "fields": e.fields}, 422)

    ld = resolve_lead_data(kind, clean)
    try:
        docx = render_docx(kind, ld)
    except Exception as e:  # noqa: BLE001 — nikdy nevraciaj text výnimky (môže obsahovať údaje)
        log.error("[inline] %s: plnenie šablóny zlyhalo (%s)", kind, type(e).__name__)
        return _json({"ok": False, "error": "render_failed"}, 500)

    problems = verify_filled(kind, ld, docx)
    if problems:
        log.error("[inline] %s: dokument neúplný (%d problémov)", kind, len(problems))
        return _json({"ok": False, "error": "document_incomplete", "problems": problems}, 422)

    pdf, converter = convert_to_pdf(docx)
    if pdf is None and not allow_docx_only:
        log.error("[inline] %s: žiadny verný konvertor DOCX→PDF", kind)
        return _json({"ok": False, "error": "pdf_converter_unavailable",
                      "message": "Verný konvertor DOCX→PDF (Gotenberg/LibreOffice) nie je dostupný."}, 503)

    docx_sha = hashlib.sha256(docx).hexdigest()
    pdf_sha = hashlib.sha256(pdf).hexdigest() if pdf else None
    tpl_sha = hashlib.sha256((gd.TEMPLATES_DIR / TEMPLATE_FILES[kind]).read_bytes()).hexdigest()
    log.info("[inline] kind=%s docx=%dB pdf=%sB converter=%s ms=%d docx_sha=%s pdf_sha=%s", kind, len(docx),
             len(pdf) if pdf else "-", converter, int((time.monotonic() - t0) * 1000), docx_sha[:12],
             pdf_sha[:12] if pdf_sha else "-")
    return _json({
        "ok": True,
        "kind": kind,
        "filename": DOCX_FILENAMES[kind],
        "docx_base64": base64.b64encode(docx).decode("ascii"),
        "pdf_base64": base64.b64encode(pdf).decode("ascii") if pdf else None,
        "docx_sha256": docx_sha,
        "pdf_sha256": pdf_sha,
        "converter": converter,
        "template_sha256": tpl_sha,
    }, 200)


def register(app):
    app.register_blueprint(bp)
