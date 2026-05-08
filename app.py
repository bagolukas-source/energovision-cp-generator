"""
Energovision B2C cenovka — Webhook server pre Notion → PDF generovanie.

Endpointy:
- POST /webhook/prepocet — prerátá ceny pre lead, updatne Notion polia
- POST /webhook/generate-pdf — vyrobí PDF + uploaduje do Notion stránky
- GET /health — healthcheck pre Render

Deployovať na Render.com (alebo iný cloud s Python 3.11+).
"""
import os
import sys
import json
import logging
import tempfile
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify
import requests

# Import existujúcich modulov z generátora
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_cp import (
    load_cennik, vyrataj_konfig, vyrataj_ceny,
    vyrataj_navratnost, vyrob_grafy
)
from generate_cp_html import vyrob_html_pdf
from generate_from_notion import (
    lead_from_notion, predpocitaj_ceny_pre_record,
    check_compatibility, INVERTOR_BATTERY_COMPAT, safe_filename,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("evo")

app = Flask(__name__)

# === ENV ===
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "ba7a1d6c-63a9-43da-b66d-2b1c7e8660da")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def require_secret(f):
    """Dekorátor — kontroluje X-Webhook-Secret header."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if WEBHOOK_SECRET:
            received = request.headers.get("X-Webhook-Secret", "")
            if received != WEBHOOK_SECRET:
                return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def notion_get_page(page_id):
    """Stiahne Notion page properties."""
    r = requests.get(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def notion_props_to_flat(page):
    """Z Notion API response (page properties) urob flat dict {prop_name: value}."""
    out = {}
    props = page.get("properties", {})
    for name, prop in props.items():
        ptype = prop.get("type")
        if ptype == "title":
            out[name] = "".join(t.get("plain_text", "") for t in prop["title"])
        elif ptype == "rich_text":
            out[name] = "".join(t.get("plain_text", "") for t in prop["rich_text"])
        elif ptype == "select":
            out[name] = (prop["select"] or {}).get("name", "")
        elif ptype == "multi_select":
            out[name] = json.dumps([s["name"] for s in prop["multi_select"]], ensure_ascii=False)
        elif ptype == "number":
            out[name] = prop["number"]
        elif ptype == "checkbox":
            out[name] = "__YES__" if prop["checkbox"] else "__NO__"
        elif ptype == "url":
            out[name] = prop["url"]
        elif ptype == "email":
            out[name] = prop["email"]
        elif ptype == "phone_number":
            out[name] = prop["phone_number"]
        elif ptype == "unique_id":
            uid = prop["unique_id"]
            out[name] = f"{uid.get('prefix','')}-{uid['number']}" if uid.get('prefix') else str(uid["number"])
    return out


def notion_update_page(page_id, properties):
    """Update Notion stránku s novými properties (Notion API v2022-06-28 formát)."""
    payload = {"properties": properties}
    r = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def notion_set_number(prop_name, value):
    return {prop_name: {"number": float(value) if value is not None else None}}


def notion_set_text(prop_name, value):
    return {prop_name: {"rich_text": [{"text": {"content": str(value)[:2000]}}]}}


def notion_set_select(prop_name, value):
    return {prop_name: {"select": {"name": value}} if value else {prop_name: {"select": None}}}


def notion_set_url(prop_name, value):
    return {prop_name: {"url": value or None}}


# ============================================================
# HEALTHCHECK
# ============================================================
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "energovision-cp-generator",
        "notion_token_set": bool(NOTION_TOKEN),
    })


# ============================================================
# WEBHOOK 1: PREPOČET CIEN
# Trigger: Notion Button "🔄 Prepočítaj cenu"
# Vstup: { "page_id": "..." }
# ============================================================
@app.route("/webhook/prepocet", methods=["POST"])
@require_secret
def prepocet():
    body = request.get_json(silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info(f"Prepočet pre page {page_id}")
    page = notion_get_page(page_id)
    notion_props = notion_props_to_flat(page)

    # Vyrátá ceny pre A/B/C
    ceny = predpocitaj_ceny_pre_record(notion_props)

    # Update Notion polí
    update = {}
    a = ceny.get("A", {})
    b = ceny.get("B", {})
    c = ceny.get("C", {})

    if a.get("cena_s_dph"):
        update.update(notion_set_number("Cena A s DPH", round(a["cena_s_dph"], 2)))
        update.update(notion_set_number("Nákupná cena A €", round(a["nakupna"], 2)))
        update.update(notion_set_number("Zisk A €", round(a["zisk"], 2)))
    if b.get("cena_s_dph"):
        update.update(notion_set_number("Cena B s DPH", round(b["cena_s_dph"], 2)))
        update.update(notion_set_number("Nákupná cena B €", round(b["nakupna"], 2)))
        update.update(notion_set_number("Zisk B €", round(b["zisk"], 2)))
    if c.get("cena_s_dph"):
        update.update(notion_set_number("Cena C s DPH", round(c["cena_s_dph"], 2)))
        update.update(notion_set_number("Nákupná cena C €", round(c["nakupna"], 2)))
        update.update(notion_set_number("Zisk C €", round(c["zisk"], 2)))

    suma = (b.get("cena_s_dph") or a.get("cena_s_dph") or c.get("cena_s_dph"))
    if suma:
        update.update(notion_set_number("Suma CP s DPH", round(suma, 2)))

    # Veľkosť (auto label)
    if a:
        from generate_from_notion import lead_from_notion
        lead = lead_from_notion(notion_props, "A")
        velkost = f"{lead['vykon_kwp']:.2f} kWp / {lead.get('panel_pocet', '?')}× LONGi"
        if notion_props.get("Batéria (kWh)"):
            velkost += f" + {notion_props['Batéria (kWh)']} kWh"
        update.update(notion_set_text("Veľkosť", velkost))

    if update:
        notion_update_page(page_id, update)
        log.info(f"Updatnuté {len(update)} polí")

    return jsonify({"success": True, "ceny": ceny, "fields_updated": len(update)})


# ============================================================
# WEBHOOK 2: GENERATE PDF
# Trigger: Notion Button "🖨 Vytlač ponuku"
# Vstup: { "page_id": "...", "variant": "A" | "B" | "C" }
# ============================================================
@app.route("/webhook/generate-pdf", methods=["POST"])
@require_secret
def generate_pdf():
    body = request.get_json(silent=True) or {}
    page_id = body.get("page_id")
    variant = body.get("variant", "A")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info(f"Generate PDF pre page {page_id}, variant {variant}")

    page = notion_get_page(page_id)
    notion_props = notion_props_to_flat(page)
    lead = lead_from_notion(notion_props, variant)

    # Kompatibilita
    if variant in ("B", "C"):
        ok, msg = check_compatibility(lead["invertor_kod"], lead.get("bateria_kod"))
        if not ok:
            return jsonify({"error": f"incompatible: {msg}"}), 400

    cennik = load_cennik()
    konfig = vyrataj_konfig(lead, cennik)
    ceny = vyrataj_ceny(konfig, lead)
    navratnost = vyrataj_navratnost(konfig, ceny, lead)

    # Vyrob v dočasnom adresári
    with tempfile.TemporaryDirectory() as tmpdir:
        priezvisko = safe_filename(lead["meno"].split()[-1])
        ev_id = lead.get("cislo_ponuky", "EV-XX-001-A")
        base = f"{ev_id}_{priezvisko}"

        # Grafy
        grafy = vyrob_grafy(navratnost, lead, tmpdir, base)

        # PDF
        pdf_path = os.path.join(tmpdir, f"{base}.pdf")
        vyrob_html_pdf(lead, konfig, ceny, navratnost, grafy, pdf_path)

        # TODO: Upload PDF do Notion stránky ako file attachment
        # Notion API neumožňuje priame upload — treba cez S3 alebo
        # cez file URL property. Najjednoduchšie: pridať PDF na S3
        # alebo Render's persistent disk + URL do Notion.
        pdf_size = os.path.getsize(pdf_path)

        # Provisional: vrátime PDF ako base64 (Make.com to dokáže uložiť)
        import base64
        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode("ascii")

    return jsonify({
        "success": True,
        "ev_id": ev_id,
        "filename": f"{base}.pdf",
        "size_bytes": pdf_size,
        "cena_s_dph": ceny["cena_s_dph"],
        "cena_finalna": ceny["cena_finalna"],
        "pdf_base64": pdf_b64,
    })


# ============================================================
# ROOT — info
# ============================================================
@app.route("/")
def root():
    return jsonify({
        "service": "Energovision B2C cenovka generator",
        "version": "1.0.0",
        "endpoints": [
            "GET /health",
            "POST /webhook/prepocet",
            "POST /webhook/generate-pdf",
        ],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
