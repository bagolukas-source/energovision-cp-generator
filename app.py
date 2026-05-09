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
import re
import math
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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

# Default spotreba ak nezadaná zákazníkom (priemer SK domácnosti)
DEFAULT_SPOTREBA_KWH = 8000

# Panel default pre auto-konfiguráciu
AUTO_PANEL = "LONGi 535 Wp"
AUTO_PANEL_WP = 535
# DC oversize: panely sa dimenzujú s prebytkom voči AC menicu (typicky 1.2-1.35x)
AUTO_DC_OVERSIZE = 1.28
# Zaokrúhliť počet panelov nahor na párne číslo (kvôli stringom + symetrii inštalácie)
AUTO_ROUND_TO_EVEN = True

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

def notion_create_page_in_db(database_id, properties):
    """Vytvor novú page v Notion DB."""
    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    r = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


# ===== AUTO-SIZING LOGIKA =====
def menic_huawei_pre_kwp(kwp):
    """Mapovanie kWp -> Huawei menic (Variant A FVE-only)."""
    if kwp <= 5:
        return "Huawei SUN2000-5K"
    elif kwp <= 6:
        return "Huawei SUN2000-6K"
    elif kwp <= 8:
        return "Huawei SUN2000-8K"
    else:
        return "Huawei SUN2000-10K"


def menic_solinteg_default():
    """Solinteg MHT-10K-25 zvlada 5-10 kWp s bateriou."""
    return "Solinteg MHT-10K-25"


def auto_sizing_from_spotreba(spotreba_kwh, ma_bateriu=False):
    """Mapping spotreby na vykon FVE s DC oversize.
    
    Vzorec: pocet_panelov = ceil(spotreba * 1.28 / 535), zaokruhlene hore na parne.
    Mernicu vyberame podla AC kWp (= round(spotreba/1000), clamp 3-12).
    
    Priklady:
      Spotreba 10 000 kWh -> 24 panelov LONGi 535 Wp = 12.84 kWp DC, 10K menic AC
      Spotreba  8 000 kWh -> 20 panelov = 10.7 kWp DC, 8K menic
      Spotreba  5 000 kWh -> 12 panelov = 6.42 kWp DC, 5K menic
    """
    # AC velkost menica (1:1 so spotrebou, clamp 3-12 kWp B2C strop)
    target_kwp = max(3, min(12, round(spotreba_kwh / 1000)))

    # Pocet panelov: spotreba * DC oversize / Wp panelu
    pocet_raw = math.ceil(spotreba_kwh * AUTO_DC_OVERSIZE / AUTO_PANEL_WP)

    # Zaokruhlit nahor na parne cislo (pre symetricke 2-row stringy)
    if AUTO_ROUND_TO_EVEN and pocet_raw % 2 != 0:
        pocet_raw += 1

    # Min 6 panelov, max 30 (kvoli B2C limit + stringom)
    pocet_panelov = max(6, min(30, pocet_raw))

    if ma_bateriu:
        menic = menic_solinteg_default()
    else:
        menic = menic_huawei_pre_kwp(target_kwp)

    return {
        "target_kwp": target_kwp,
        "pocet_panelov": pocet_panelov,
        "panel": AUTO_PANEL,
        "menic": menic,
    }


def claude_extract_leads(raw_text):
    """Zavola Claude API a vrati pole leadov ako Python list."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY nie je nastaveny v env vars")

    system_prompt = (
        "Si parser pre slovenskú energetickú firmu Energovision (FVE, batérie, wallbox, trafostanice, revízie).\n\n"
        "Z neformálneho textu (email, formulár, tabuľka, voicemail prepis, OCR) extrahuješ údaje o ZÁKAZNÍKOCH.\n\n"
        "JEDEN INPUT MÔŽE OBSAHOVAŤ VIAC LEADOV. Vráť ich VŠETKY ako pole.\n\n"
        "Vráť IBA platný JSON v presnej štruktúre:\n"
        "{\n"
        '  "leads": [\n'
        "    {\n"
        '      "meno": "iba krstné meno (napr. Peter), nie celé meno + priezvisko, alebo null",\n'
        '      "priezvisko": "iba priezvisko (napr. Novák), bez krstného mena, alebo null",\n'
        '      "telefon": "+421... alebo null",\n'
        '      "email": "email@... alebo null",\n'
        '      "ulica_cislo": "ulica + číslo alebo null",\n'
        '      "mesto": "iba mesto alebo null",\n'
        '      "psc": "PSČ alebo null",\n'
        '      "spotreba_kwh_rok": "číslo (ak zákazník zadal) alebo null",\n'
        '      "rocna_faktura_eur": null alebo číslo,\n'
        '      "typ_dopytu": ["FVE", "Batéria", "Wallbox", "Revízia", "Bleskozvod", "Iné"],\n'
        '      "typ_strechy": "Škridla / Plech kombivrut / Falcový plech / Plochá strecha — J 13° / Plochá strecha — V/Z 10° / null",\n'
        '      "orientacia": "J / V-Z / J-V / J-Z / null",\n'
        '      "bateria_odporucana": "Solinteg EBA B5K1 — 5.12 kWh / Solinteg EBA B5K1 — 10.24 kWh / Pylontech Force H3 — 5.12 kWh / Huawei LUNA2000 — 5 kWh / Huawei LUNA2000 — 7 kWh / null",\n'
        '      "wallbox_odporucany": "Solinteg 7 kW (1F) / Solinteg 11 kW (3F) / Huawei AC Smart 22 kW / Huawei AC Smart 7 kW / GoodWe 11 kW / GoodWe 22 kW / null",\n'
        '      "variant_odporucany": "A" alebo "B" alebo "C",\n'
        '      "priorita": "Vysoká / Stredná / Nízka",\n'
        '      "poznamky": "krátke zhrnutie kontextu, max 200 znakov"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Pravidlá:\n"
        "- Variant A = iba FVE, B = FVE+batéria, C = FVE+batéria+wallbox\n"
        "- Ak EV/elektromobil/wallbox -> C; ostrov/výpadky/akumulácia -> minimálne B; iba znižovanie účtu -> A\n"
        "- Slovenské diakritiky zachovaj. Telefón normalizuj na +421 formát.\n"
        "- DÔLEŽITÉ: spotreba_kwh_rok dávaj LEN ak je v texte explicitne uvedená. Ak nie, daj null. NEVYMÝŠĽAJ z faktúry.\n"
        "- Batéria default: Solinteg EBA B5K1 — 10.24 kWh ak spotreba >5000 kWh, inak 5.12 kWh.\n"
        "- Ak chýba údaj, daj null. ZIADNE markdown bloky, IBA surový JSON {...}."
    )

    user_prompt = f"Tu je raw lead text \u2014 moze obsahovat 1 alebo viac leadov:\n\n{raw_text[:15000]}"

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "temperature": 0.1,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90)
    r.raise_for_status()
    resp = r.json()
    text = resp["content"][0]["text"].strip()

    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Claude vratil ne-JSON: %s", text[:500])
        raise RuntimeError(f"Claude vratil neplatny JSON: {e}")

    return data.get("leads", [])


KONSTRUKCIA_OPTIONS = {
    "Škridla", "Plech kombivrut", "Falcový plech",
    "Plochá strecha — J 13°", "Plochá strecha — V/Z 10°",
}
BATERIA_OPTIONS = {
    "Pylontech Force H3 — 5.12 kWh",
    "Solinteg EBA B5K1 — 5.12 kWh", "Solinteg EBA B5K1 — 10.24 kWh",
    "Huawei LUNA2000 — 5 kWh", "Huawei LUNA2000 — 7 kWh",
}
WALLBOX_OPTIONS = {
    "Solinteg 7 kW (1F)", "Solinteg 11 kW (3F)",
    "Huawei AC Smart 22 kW", "Huawei AC Smart 7 kW",
    "GoodWe 11 kW", "GoodWe 22 kW",
}
TYP_DOPYTU_OPTIONS = {"FVE", "Batéria", "Wallbox", "Revízia", "Bleskozvod", "Iné"}
PRIORITA_OPTIONS = {"Vysoká", "Stredná", "Nízka"}


def _select_or_none(value, allowed_set):
    if value and value in allowed_set:
        return {"select": {"name": value}}
    return None


def lead_to_notion_properties(lead):
    """Z parsed lead dict vyrob Notion API properties payload pre Zakaznici B2C DB."""
    props = {}

    # Title = Meno Priezvisko (mesto ide do separatneho Mesto property, NIE do title)
    meno = (lead.get("meno") or "").strip()
    priezvisko = (lead.get("priezvisko") or "").strip()
    # Ak meno uz obsahuje priezvisko (full name), pouzi iba meno
    if meno and priezvisko and priezvisko.lower() in meno.lower():
        title_text = meno
    elif meno and priezvisko:
        title_text = f"{meno} {priezvisko}"
    elif meno:
        title_text = meno
    elif priezvisko:
        title_text = priezvisko
    else:
        title_text = "Nový lead (bez mena)"
    props["Zákazník"] = {"title": [{"text": {"content": title_text[:200]}}]}

    if lead.get("telefon"):
        props["Telefón"] = {"phone_number": str(lead["telefon"])[:50]}
    if lead.get("email"):
        props["Email"] = {"email": str(lead["email"])[:200]}

    mesto_parts = []
    if lead.get("ulica_cislo"):
        mesto_parts.append(str(lead["ulica_cislo"]))
    if lead.get("mesto"):
        mesto_parts.append(str(lead["mesto"]))
    if mesto_parts:
        props["Mesto"] = {"rich_text": [{"text": {"content": ", ".join(mesto_parts)[:500]}}]}

    variant = (lead.get("variant_odporucany") or "A").upper()
    props["Variant A — FVE"] = {"checkbox": variant == "A"}
    props["Variant B — FVE + BESS"] = {"checkbox": variant == "B"}
    props["Variant C — FVE + BESS + Wallbox"] = {"checkbox": variant == "C"}

    # SPOTREBA + AUTO-SIZING
    spotreba_raw = lead.get("spotreba_kwh_rok")
    if spotreba_raw is not None:
        try:
            spotreba_val = float(spotreba_raw)
            spotreba_zdroj = "Zákazník zadal"
        except (ValueError, TypeError):
            spotreba_val = float(DEFAULT_SPOTREBA_KWH)
            spotreba_zdroj = "Default 8000 (SK priemer)"
    else:
        spotreba_val = float(DEFAULT_SPOTREBA_KWH)
        spotreba_zdroj = "Default 8000 (SK priemer)"

    props["Spotreba"] = {"number": spotreba_val}
    props["Spotreba zdroj"] = {"select": {"name": spotreba_zdroj}}

    sizing = auto_sizing_from_spotreba(spotreba_val, ma_bateriu=variant in ("B", "C"))
    props["Panel"] = {"select": {"name": sizing["panel"]}}
    props["Počet panelov"] = {"number": sizing["pocet_panelov"]}
    props["Menič"] = {"select": {"name": sizing["menic"]}}

    konstr_sel = _select_or_none(lead.get("typ_strechy"), KONSTRUKCIA_OPTIONS)
    if not konstr_sel:
        konstr_sel = {"select": {"name": "Škridla"}}  # default ak AI nezistila
    props["Konštrukcia (typ)"] = konstr_sel

    if variant in ("B", "C"):
        bat_sel = _select_or_none(lead.get("bateria_odporucana"), BATERIA_OPTIONS)
        if not bat_sel:
            default_bat = "Solinteg EBA B5K1 — 10.24 kWh" if spotreba_val > 5000 else "Solinteg EBA B5K1 — 5.12 kWh"
            bat_sel = {"select": {"name": default_bat}}
        props["Batéria (typ)"] = bat_sel
        props["Batéria počet"] = {"select": {"name": "1"}}
    else:
        props["Batéria počet"] = {"select": {"name": "0"}}

    if variant == "C":
        wb_sel = _select_or_none(lead.get("wallbox_odporucany"), WALLBOX_OPTIONS)
        if not wb_sel:
            wb_sel = {"select": {"name": "Solinteg 11 kW (3F)"}}
        props["Wallbox (typ)"] = wb_sel

    typ_dopytu_raw = lead.get("typ_dopytu") or []
    if isinstance(typ_dopytu_raw, str):
        typ_dopytu_raw = [typ_dopytu_raw]
    typ_dopytu_clean = [t for t in typ_dopytu_raw if t in TYP_DOPYTU_OPTIONS]
    if not typ_dopytu_clean:
        typ_dopytu_clean = ["FVE"]
    props["Typ dopytu"] = {"multi_select": [{"name": t} for t in typ_dopytu_clean]}

    prio_sel = _select_or_none(lead.get("priorita"), PRIORITA_OPTIONS)
    if prio_sel:
        props["Priorita"] = prio_sel

    poznamky_parts = []
    if spotreba_zdroj == "Zákazník zadal":
        poznamky_parts.append(f"Spotreba: {int(spotreba_val)} kWh/rok (zadaná)")
    else:
        poznamky_parts.append(f"Spotreba: {int(spotreba_val)} kWh/rok (default SK priemer)")
    if lead.get("rocna_faktura_eur"):
        poznamky_parts.append(f"Faktúra: {lead['rocna_faktura_eur']} €/rok")
    if lead.get("orientacia"):
        poznamky_parts.append(f"Orientácia: {lead['orientacia']}")
    if lead.get("psc"):
        poznamky_parts.append(f"PSČ: {lead['psc']}")
    if lead.get("poznamky"):
        poznamky_parts.append(str(lead["poznamky"]))
    poznamky_parts.append(f"[AI parsed → auto-sizing: {sizing['target_kwp']} kWp / {sizing['pocet_panelov']}× panel]")
    props["Poznámky"] = {"rich_text": [{"text": {"content": " | ".join(poznamky_parts)[:1900]}}]}

    return props



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
# WEBHOOK 0: PARSUJ LEADY (Multi-lead intake parser)
# Trigger: Notion Button "🔍 Parsuj leady" v Lead Inbox DB
# ============================================================
@app.route("/webhook/parsuj-leady", methods=["POST"])
@require_secret
def parsuj_leady():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[parsuj-leady] page_id=%s", page_id)

    try:
        source_page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(source_page)
    raw_text = (flat.get("Surový lead") or "").strip()

    if not raw_text:
        try:
            notion_update_page(page_id, {"Status": {"select": {"name": "🔴 Chyba"}}})
        except Exception:
            pass
        return jsonify({"error": "Surový lead je prázdny"}), 400

    log.info("[parsuj-leady] raw_text dlzka=%d znakov", len(raw_text))

    try:
        leads = claude_extract_leads(raw_text)
    except Exception as e:
        log.exception("Claude extraction zlyhala")
        try:
            notion_update_page(page_id, {
                "Status": {"select": {"name": "🔴 Chyba"}},
                "Surový lead": {"rich_text": [{"text": {"content": f"Claude error: {e}"[:1900]}}]},
            })
        except Exception:
            pass
        return jsonify({"error": f"claude failed: {e}"}), 500

    if not leads:
        try:
            notion_update_page(page_id, {"Status": {"select": {"name": "🔴 Chyba"}}})
        except Exception:
            pass
        return jsonify({"error": "Claude nenasiel ziadne leady"}), 400

    log.info("[parsuj-leady] extrahovanych leadov: %d", len(leads))

    created = []
    failed = []
    for i, lead in enumerate(leads):
        try:
            props = lead_to_notion_properties(lead)
            new_page = notion_create_page_in_db(NOTION_DATABASE_ID, props)
            created.append({
                "id": new_page.get("id"),
                "url": new_page.get("url"),
                "title": (lead.get("priezvisko") or lead.get("meno") or f"Lead #{i+1}"),
            })
        except Exception as e:
            log.exception("Vytvorenie page #%d zlyhalo", i + 1)
            failed.append({"index": i + 1, "error": str(e), "lead_preview": str(lead)[:200]})

    try:
        notion_update_page(page_id, {
            "Surový lead": {"rich_text": []},
            "Počet vyparsovaných": {"number": len(created)},
            "Status": {"select": {"name": "🟢 Spracované"}},
        })
    except Exception as e:
        log.warning("Cleanup source page zlyhal: %s", e)

    return jsonify({"ok": True, "created": len(created), "failed": len(failed), "leads": created, "errors": failed})


# ============================================================
# WEBHOOK 0b: AUTO-KONFIG (sizing zo Spotreby)
# Trigger: Notion Button "🎯 Auto-konfig" v Zákazníci B2C
# ============================================================
@app.route("/webhook/auto-konfig", methods=["POST"])
@require_secret
def auto_konfig():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[auto-konfig] page_id=%s", page_id)

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    spotreba_raw = flat.get("Spotreba")
    if spotreba_raw is not None:
        try:
            spotreba_val = float(spotreba_raw)
            spotreba_zdroj = "Zákazník zadal"
        except (ValueError, TypeError):
            spotreba_val = float(DEFAULT_SPOTREBA_KWH)
            spotreba_zdroj = "Default 8000 (SK priemer)"
    else:
        spotreba_val = float(DEFAULT_SPOTREBA_KWH)
        spotreba_zdroj = "Default 8000 (SK priemer)"

    var_b = flat.get("Variant B — FVE + BESS") == "__YES__"
    var_c = flat.get("Variant C — FVE + BESS + Wallbox") == "__YES__"
    ma_bateriu = var_b or var_c

    sizing = auto_sizing_from_spotreba(spotreba_val, ma_bateriu=ma_bateriu)

    update_props = {
        "Spotreba": {"number": spotreba_val},
        "Spotreba zdroj": {"select": {"name": spotreba_zdroj}},
        "Panel": {"select": {"name": sizing["panel"]}},
        "Počet panelov": {"number": sizing["pocet_panelov"]},
        "Menič": {"select": {"name": sizing["menic"]}},
    }

    bat_aktualna = flat.get("Batéria (typ)")
    if ma_bateriu and not bat_aktualna:
        default_bat = "Solinteg EBA B5K1 — 10.24 kWh" if spotreba_val > 5000 else "Solinteg EBA B5K1 — 5.12 kWh"
        update_props["Batéria (typ)"] = {"select": {"name": default_bat}}
        update_props["Batéria počet"] = {"select": {"name": "1"}}
    elif not ma_bateriu:
        update_props["Batéria počet"] = {"select": {"name": "0"}}

    # Default konstrukcia "Skridla" ak este nie je nastavena
    konstr_aktualna = flat.get("Konštrukcia (typ)")
    if not konstr_aktualna:
        update_props["Konštrukcia (typ)"] = {"select": {"name": "Škridla"}}

    try:
        notion_update_page(page_id, update_props)
    except Exception as e:
        log.exception("Notion update zlyhal")
        return jsonify({"error": f"notion_update failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "spotreba": spotreba_val,
        "spotreba_zdroj": spotreba_zdroj,
        "sizing": sizing,
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

    # Auto-vyplnenie "Batéria výkon" = počet × kWh per modul (z labelu typu)
    bat_typ = notion_props.get("Batéria (typ)") or ""
    m_bat = re.search(r"(\d+(?:[.,]\d+)?)\s*kWh", bat_typ)
    per_modul = float(m_bat.group(1).replace(",", ".")) if m_bat else 0
    pocet_raw = notion_props.get("Batéria počet")
    try:
        pocet = int(pocet_raw) if pocet_raw not in (None, "") else 0
    except (TypeError, ValueError):
        pocet = 0
    if pocet > 0 and per_modul > 0:
        update.update(notion_set_number("Batéria výkon", round(pocet * per_modul, 2)))

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
        from datetime import datetime
        datum = datetime.now().strftime("%Y-%m-%d")
        base = f"{ev_id}_{priezvisko}_{datum}"

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

    # folder_name: bez variantu (-A/-B/-C) a bez dátumu — stabilný v čase per zákazník
    ev_id_root = ev_id[:-2] if len(ev_id) >= 2 and ev_id[-2] == "-" and ev_id[-1] in "ABC" else ev_id
    folder_name = f"{ev_id_root}_{priezvisko}"

    return jsonify({
        "success": True,
        "ev_id": ev_id,
        "filename": f"{base}.pdf",
        "folder_name": folder_name,
        "size_bytes": pdf_size,
        "cena_s_dph": ceny["cena_s_dph"],
        "cena_finalna": ceny["cena_finalna"],
        "pdf_base64": pdf_b64,
    })


# ============================================================
# WEBHOOK 3: EMAIL TEMPLATE BUILDER
# Trigger: Notion Button "📧 Odoslať email"
# Vstup: { "page_id": "..." }
# Výstup: { "to": "...", "subject": "...", "body_html": "...",
#          "attachments": [{"name": "...", "dropbox_path": "..."}],
#          "obchodnik": {...}, "variants_sent": ["A","B"] }
# ============================================================
@app.route("/webhook/email-template", methods=["POST"])
@require_secret
def email_template():
    try:
        return _email_template_impl()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"email_template padol: {e}\n{tb}")
        return jsonify({
            "error": str(e),
            "traceback": tb,
        }), 500


def _is_valid_email(email):
    """Jednoduchá validácia emailu — prítomnosť @ + bodka + min dĺžka."""
    if not email or "@" not in email:
        return False
    parts = email.strip().split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    if len(local) < 1 or len(domain) < 4:
        return False
    if "." not in domain:
        return False
    # TLD min 2 znaky
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2:
        return False
    return True


def _email_template_impl():
    body = request.get_json(silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info(f"Email template pre page {page_id}")
    page = notion_get_page(page_id)
    notion_props = notion_props_to_flat(page)

    # Detekuj zakliknuté varianty — tolerantný matching property názvu (akýkoľvek prefix "Variant A/B/C")
    variants_active = []
    log.info(f"notion_props keys: {list(notion_props.keys())[:30]}")
    for k, v in notion_props.items():
        k_lower = k.lower().strip()
        if v == "__YES__":
            if k_lower.startswith("variant a"):
                if "A" not in variants_active:
                    variants_active.append("A")
            elif k_lower.startswith("variant b"):
                if "B" not in variants_active:
                    variants_active.append("B")
            elif k_lower.startswith("variant c"):
                if "C" not in variants_active:
                    variants_active.append("C")
    log.info(f"variants_active: {variants_active}")

    if not variants_active:
        # Diagnostický error: vypíšeme aké hodnoty sme videli pre Variant properties
        variant_props_seen = {k: v for k, v in notion_props.items() if "variant" in k.lower()}
        return jsonify({"error": f"Žiadny variant nie je zakliknutý. Variant properties seen: {variant_props_seen}"}), 400

    # Lead data — z A variantu (kvôli základným údajom; ceny berieme zo všetkých)
    from generate_from_notion import lead_from_notion, OBCHODNICI, DEFAULT_OBCHODNIK
    lead_a = lead_from_notion(notion_props, "A")

    priezvisko = lead_a["meno"].split()[-1] if lead_a.get("meno") else "Zákazník"
    mesto = lead_a.get("mesto", "")
    email_zakaznika = (lead_a.get("email") or notion_props.get("Email") or "").strip()
    if not _is_valid_email(email_zakaznika):
        log.warning(f"Neplatný email pre page {page_id}: '{email_zakaznika}'")
        return jsonify({
            "success": False,
            "email_valid": "false",
            "error": f"Neplatný email zákazníka: '{email_zakaznika}'. Skontroluj 'Email' property v Notion DB.",
            "to": "",
            "subject": "",
            "body_html": "",
            "attachments": [],
            "obchodnik": {},
            "variants_sent": [],
        }), 200  # Status 200 aby Make scenár pokračoval k Notion update
    obchodnik = OBCHODNICI.get(notion_props.get("Obchodník") or notion_props.get("Obchodnik") or "", DEFAULT_OBCHODNIK)

    vykon_kwp = lead_a.get("vykon_kwp", 0)
    bateria_kwh = float(notion_props.get("Batéria výkon") or 0)

    # Ceny zo všetkých zakliknutých variant
    ceny = {
        "A": notion_props.get("Cena A s DPH"),
        "B": notion_props.get("Cena B s DPH"),
        "C": notion_props.get("Cena C s DPH"),
    }

    # Vygeneruj PDF pre každý aktívny variant a vráť ako base64
    from generate_from_notion import safe_filename
    import base64
    priezvisko_clean = safe_filename(priezvisko)
    attachments = []
    cennik = load_cennik()
    for v in variants_active:
        try:
            lead_v = lead_from_notion(notion_props, v)
            if v in ("B", "C"):
                ok, _msg = check_compatibility(lead_v["invertor_kod"], lead_v.get("bateria_kod"))
                if not ok:
                    log.warning(f"Variant {v} incompatible: {_msg}, skipping attachment")
                    continue
            konfig_v = vyrataj_konfig(lead_v, cennik)
            ceny_v = vyrataj_ceny(konfig_v, lead_v)
            navratnost_v = vyrataj_navratnost(konfig_v, ceny_v, lead_v)
            with tempfile.TemporaryDirectory() as tmpdir:
                ev_id_v = lead_v.get("cislo_ponuky", f"EV-XX-001-{v}")
                from datetime import datetime as _dt
                datum_v = _dt.now().strftime("%Y-%m-%d")
                base_v = f"{ev_id_v}_{priezvisko_clean}_{datum_v}"
                grafy_v = vyrob_grafy(navratnost_v, lead_v, tmpdir, base_v)
                pdf_path_v = os.path.join(tmpdir, f"{base_v}.pdf")
                vyrob_html_pdf(lead_v, konfig_v, ceny_v, navratnost_v, grafy_v, pdf_path_v)
                with open(pdf_path_v, "rb") as fp:
                    pdf_b64 = base64.b64encode(fp.read()).decode("ascii")
                # folder_name — stabilný per zákazník, bez variantu/dátumu
                _evid_root = ev_id_v[:-2] if len(ev_id_v) >= 2 and ev_id_v[-2] == "-" and ev_id_v[-1] in "ABC" else ev_id_v
                _folder = f"{_evid_root}_{priezvisko_clean}"
                attachments.append({
                    "filename": f"{base_v}.pdf",
                    "folder_name": _folder,
                    "data": pdf_b64,
                })
        except Exception as e:
            log.error(f"PDF gen pre variant {v} zlyhal: {e}")
            continue

    # ===== EMAIL TEMPLATES =====
    subject = build_subject(priezvisko, mesto, variants_active)
    body_html = build_email_body(priezvisko, mesto, vykon_kwp, bateria_kwh, ceny, variants_active, obchodnik)

    return jsonify({
        "success": True,
        "email_valid": "true",
        "to": email_zakaznika,
        "subject": subject,
        "body_html": body_html,
        "attachments": attachments,
        "obchodnik": obchodnik,
        "variants_sent": variants_active,
    })


def build_subject(priezvisko, mesto, variants):
    """Subject riadok — krátky, identifikovateľný."""
    if len(variants) == 1:
        v_label = {"A": "FVE", "B": "FVE + batéria", "C": "FVE + batéria + wallbox"}[variants[0]]
        return f"Cenová ponuka {v_label} pre {priezvisko}, {mesto}"
    return f"Cenová ponuka FVE — {priezvisko}, {mesto} ({len(variants)} varianty)"


def build_email_body(priezvisko, mesto, kwp, bateria_kwh, ceny, variants, obchodnik):
    """
    HTML email body s marketingovým textom (slovenský trh, 30y FVE expert tone).
    Per-variant blocks + comparison + signature.
    """
    cena_a = ceny.get("A") or 0
    cena_b = ceny.get("B") or 0
    cena_c = ceny.get("C") or 0

    # === INTRO ===
    n_var = len(variants)
    intro = f"""
    <p>Dobrý deň pán/pani {priezvisko},</p>
    <p>v nadväznosti na našu obhliadku Vám zasielam pripravenú cenovú ponuku pre fotovoltickú elektráreň
    pre Vašu domácnosť v <strong>{mesto}</strong>.
    Pripravil som {("jednu variantu" if n_var == 1 else f"{n_var} varianty")} podľa toho, ako chcete využiť energiu zo slnka.</p>
    """

    blocks = []

    # === BLOCK A — iba FVE ===
    if "A" in variants:
        blocks.append(f"""
        <h3 style="color:#1B5E3F;margin-top:24px;">Varianta A — fotovoltika {kwp} kWp</h3>
        <p>Najlacnejšia cesta ako začať. Panely vyrábajú elektrinu cez deň, ktorú spotrebovávate priamo
        — typicky pokryje 60-70 % Vašej dennej spotreby. Ideálne ak doma cez deň žije rodina, sušiete
        bielizeň, varíte alebo používate tepelné čerpadlo na ohrev TÚV.</p>
        <ul>
          <li><strong>Investícia po dotácii Zelená domácnostiam:</strong> {cena_a:,.0f} € s DPH</li>
          <li><strong>Návratnosť:</strong> 6–8 rokov pri dnešnej cene elektriny 0,16 €/kWh</li>
          <li><strong>Záruka:</strong> 30 rokov na panely LONGi, 10 rokov na menič Huawei</li>
          <li><strong>Inštalácia:</strong> 1–2 dni, bez stavebných úprav</li>
        </ul>
        <p style="background:#F0F8F4;padding:12px;border-left:4px solid #1B5E3F;font-size:14px;">
          <strong>Pre slovenský trh:</strong> 60 % FVE inštalácií v rodinných domoch ide bez batérie.
          Distribučné spoločnosti odkupujú prebytky za 0,04–0,06 €/kWh, čo postačí na základnú nezávislosť.
        </p>
        """.replace(",", " "))

    # === BLOCK B — FVE + BESS ===
    if "B" in variants:
        bat_str = f"{bateria_kwh:.0f} kWh" if bateria_kwh else "batéria"
        blocks.append(f"""
        <h3 style="color:#1B5E3F;margin-top:24px;">Varianta B — fotovoltika {kwp} kWp + batéria {bat_str}</h3>
        <p>Energetická nezávislosť — slnko ukladáte do batérie a používate večer/v noci/keď je zamračené.
        Pri zlepšujúcich sa zľavách na komponenty je toto pre Slovákov dnes najatraktívnejšia voľba,
        najmä pre rodiny ktoré sú doma <strong>predovšetkým ráno a večer</strong>.</p>
        <ul>
          <li><strong>Investícia po dotácii:</strong> {cena_b:,.0f} € s DPH</li>
          <li><strong>Pokrytie spotreby:</strong> 85–95 % pri správnom dimenzovaní</li>
          <li><strong>Návratnosť:</strong> 8–11 rokov</li>
          <li><strong>Backup:</strong> pri výpadku siete batéria automaticky prepne dom na ostrov (voliteľne)</li>
          <li><strong>Záruka batérie:</strong> 10 rokov / 6 000 cyklov</li>
        </ul>
        <p style="background:#F0F8F4;padding:12px;border-left:4px solid #1B5E3F;font-size:14px;">
          <strong>Pozor na kalkulácie:</strong> Distribučné poplatky v SR rastú každoročne ~5–8 %.
          Batéria vám teda chráni nielen pred volatilitou ceny silovej elektriny ale aj pred budúcim rastom
          poplatkov za distribúciu.
        </p>
        """.replace(",", " "))

    # === BLOCK C — FVE + BESS + Wallbox ===
    if "C" in variants:
        bat_str = f"{bateria_kwh:.0f} kWh" if bateria_kwh else "batéria"
        blocks.append(f"""
        <h3 style="color:#1B5E3F;margin-top:24px;">Varianta C — kompletné riešenie + wallbox pre elektromobil</h3>
        <p>FVE {kwp} kWp + batéria {bat_str} + smart wallbox. Vaše auto sa nabíja zo slnka — zadarmo —
        a wallbox automaticky reaguje na prebytky FVE. Riešenie pre rodiny s elektromobilom alebo plánom kúpiť
        EV v najbližších rokoch.</p>
        <ul>
          <li><strong>Investícia:</strong> {cena_c:,.0f} € s DPH</li>
          <li><strong>Úspora paliva:</strong> ~ 1 200 € ročne pri 15 000 km/rok namiesto benzínu</li>
          <li><strong>Plná energetická nezávislosť:</strong> spotreba domu + auto z vlastnej elektriny</li>
          <li><strong>Smart logika:</strong> wallbox sa zapne keď FVE má nadbytok, nezasahuje do siete</li>
        </ul>
        <p style="background:#F0F8F4;padding:12px;border-left:4px solid #1B5E3F;font-size:14px;">
          <strong>Slovenský kontext:</strong> Pri raste cien benzínu/nafty a zvyšujúcich sa parkovných
          poplatkoch vo veľkých mestách (Bratislava, Košice) sa elektromobil vracia za 4-6 rokov samostatne.
          S vlastnou FVE ešte rýchlejšie.
        </p>
        """.replace(",", " "))

    # === COMPARISON ak viac variant ===
    comparison = ""
    if len(variants) > 1:
        rows = []
        if "A" in variants:
            rows.append(f"<tr><td>A — iba FVE</td><td style='text-align:right;'>{cena_a:,.0f} €</td><td>~7 rokov</td><td>Šetríš cez deň</td></tr>".replace(",", " "))
        if "B" in variants:
            rows.append(f"<tr><td>B — FVE + batéria</td><td style='text-align:right;'>{cena_b:,.0f} €</td><td>~9 rokov</td><td>Plná denná + nočná nezávislosť</td></tr>".replace(",", " "))
        if "C" in variants:
            rows.append(f"<tr><td>C — komplet + wallbox</td><td style='text-align:right;'>{cena_c:,.0f} €</td><td>~11 rokov</td><td>+ EV nabíjanie zadarmo</td></tr>".replace(",", " "))

        comparison = f"""
        <h3 style="color:#1B5E3F;margin-top:24px;">Krátke porovnanie</h3>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
          <thead>
            <tr style="background:#1B5E3F;color:white;">
              <th style="padding:8px;text-align:left;">Varianta</th>
              <th style="padding:8px;text-align:right;">Cena s DPH</th>
              <th style="padding:8px;text-align:left;">Návratnosť</th>
              <th style="padding:8px;text-align:left;">Komfort</th>
            </tr>
          </thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        <p style="font-size:14px;font-style:italic;color:#555;margin-top:8px;">
        Moja osobná poznámka po 30 rokoch v energetike: ak nemáte konkrétny plán na elektromobil v
        najbližších 2–3 rokoch, varianta B prináša najlepší pomer komfortu k investícii. Hybridný menič
        v balíku umožňuje doplnenie batérie kedykoľvek neskôr — bez prerábania systému.
        </p>
        """

    # === ATTACHMENTY popis ===
    n_pdf = len(variants)
    pdf_note = f"<p>V <strong>prílohe e-mailu</strong> nájdete {('detailný PDF dokument' if n_pdf == 1 else f'{n_pdf} PDF dokumenty')} s technickou špecifikáciou, vizualizáciou rozloženia panelov, návratnostnou kalkuláciou na 25 rokov a podmienkami inštalácie.</p>"

    # === CTA + SIGNATURE ===
    cta = f"""
    <h3 style="color:#1B5E3F;margin-top:24px;">Ďalšie kroky</h3>
    <p>Ponuka platí 30 dní. Ak Vás niektorá varianta zaujala alebo máte otázky, stačí mi odpísať alebo zavolať.
    Dohodneme si <strong>bezplatnú obhliadku</strong> v termíne ktorý Vám vyhovuje — meriame strechu, navrhneme
    optimálne rozloženie panelov a doladíme finálnu ponuku.</p>
    <p style="margin-top:16px;">S úctou a pozdravom,</p>
    <table style="margin-top:8px;font-size:14px;">
      <tr>
        <td style="padding:0;border:none;">
          <strong style="font-size:15px;">{obchodnik["meno"]}</strong><br/>
          <span style="color:#666;">{obchodnik["funkcia"]}</span><br/>
          📞 <a href="tel:{obchodnik["tel"].replace(" ","")}" style="color:#1B5E3F;text-decoration:none;">{obchodnik["tel"]}</a><br/>
          ✉️ <a href="mailto:{obchodnik["email"]}" style="color:#1B5E3F;text-decoration:none;">{obchodnik["email"]}</a>
        </td>
      </tr>
    </table>
    <hr style="margin:20px 0;border:none;border-top:1px solid #ddd;"/>
    <p style="font-size:12px;color:#888;">
      <strong>Energovision s.r.o.</strong> | IČO: 53 036 280 |
      <a href="https://energovision.sk" style="color:#888;">energovision.sk</a>
    </p>
    """

    full_html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;line-height:1.5;color:#333;max-width:700px;">
    {intro}
    {"".join(blocks)}
    {comparison}
    {pdf_note}
    {cta}
    </body></html>
    """
    return full_html


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
            "POST /webhook/parsuj-leady",
            "POST /webhook/auto-konfig",
            "POST /webhook/prepocet",
            "POST /webhook/generate-pdf",
            "POST /webhook/email-template",
        ],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
