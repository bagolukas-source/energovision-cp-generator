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
NOTION_MATERIAL_PO_DB_ID = os.environ.get("NOTION_MATERIAL_PO_DB_ID", "a8690d6826114d5097c8bbfb36c02d7c")
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
        elif ptype == "files":
            # Files property — vrati JSON pole s file objektmi (name + url)
            files_arr = []
            for f in prop.get("files", []) or []:
                fname = f.get("name", "file")
                if f.get("type") == "external":
                    furl = f.get("external", {}).get("url", "")
                else:
                    furl = f.get("file", {}).get("url", "")
                if furl:
                    files_arr.append({"name": fname, "url": furl})
            out[name] = json.dumps(files_arr, ensure_ascii=False) if files_arr else ""
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
    # Pocet panelov je teraz SELECT v Notione (nie number) - hodnoty 1-30
    props["Počet panelov"] = {"select": {"name": str(sizing["pocet_panelov"])}}
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

    if variant in ("C", "D"):
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

    # Default Status pre nový lead — aby sa zobrazil v 1️⃣ Nové leady view
    props["Status"] = {"select": {"name": "🆕 Došlý lead"}}

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
    """
    All-in-one Auto-konfig.

    1. Sizing zo Spotreby (Panel, Pocet, Menic) — vzdy ma_bateriu=True (Solinteg menic, kompatibilny s baterion)
    2. Konstrukcia default Skridla
    3. Bateria default 1 ks podla znacky menica:
       - Solinteg menic -> Solinteg EBA B5K1 (5.12 alebo 10.24 podla spotreby)
       - Huawei menic   -> Huawei LUNA2000 (5 alebo 7 kWh)
       - GoodWe menic   -> Pylontech Force H3 5.12
    4. Wallbox default podla znacky menica:
       - Solinteg -> Solinteg 11 kW (3F)
       - Huawei   -> Huawei AC Smart 22 kW
       - GoodWe   -> GoodWe 22 kW
    5. Po nastaveni props prepocita ceny pre VSETKY 4 varianty (A/B/C/D)
       a zapise Cena/Naukpna/Zisk + Suma CP s DPH
    6. Vrati zhrnutie s 4 cenami — uzivatel si vyklika ktoru chce ponuknut.
    """
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

    # Auto-sync Obchodnik zo Statusu (ak je per-obchodník variant)
    _sync_obchodnik_zo_statusu(page_id, flat)

    # === SPOTREBA ===
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

    # === SIZING — vzdy s bateriou aby sme dostali Solinteg/hybridny menic ===
    sizing = auto_sizing_from_spotreba(spotreba_val, ma_bateriu=True)
    menic = sizing["menic"]

    # === BATERIA podla znacky menica ===
    bat_aktualna = flat.get("Batéria (typ)")
    if not bat_aktualna:
        if "Solinteg" in menic:
            bat_typ = "Solinteg EBA B5K1 — 10.24 kWh" if spotreba_val > 5000 else "Solinteg EBA B5K1 — 5.12 kWh"
        elif "Huawei" in menic:
            bat_typ = "Huawei LUNA2000 — 7 kWh" if spotreba_val > 5000 else "Huawei LUNA2000 — 5 kWh"
        elif "GoodWe" in menic:
            bat_typ = "Pylontech Force H3 — 5.12 kWh"
        else:
            bat_typ = "Solinteg EBA B5K1 — 10.24 kWh"
    else:
        bat_typ = bat_aktualna

    # === WALLBOX podla znacky menica ===
    wb_aktualna = flat.get("Wallbox (typ)")
    if not wb_aktualna:
        if "Solinteg" in menic:
            wb_typ = "Solinteg 11 kW (3F)"
        elif "Huawei" in menic:
            wb_typ = "Huawei AC Smart 22 kW"
        elif "GoodWe" in menic:
            wb_typ = "GoodWe 22 kW"
        else:
            wb_typ = "Solinteg 11 kW (3F)"
    else:
        wb_typ = wb_aktualna

    # === KONSTRUKCIA default Skridla ===
    konstr_aktualna = flat.get("Konštrukcia (typ)")
    konstr_typ = konstr_aktualna or "Škridla"

    # === KROK 1: Update vsetky komponenty + Spotreba + DEFAULT marze 30% ===
    update_props_komponenty = {
        "Spotreba": {"number": spotreba_val},
        "Spotreba zdroj": {"select": {"name": spotreba_zdroj}},
        "Panel": {"select": {"name": sizing["panel"]}},
        "Počet panelov": {"select": {"name": str(sizing["pocet_panelov"])}},
        "Menič": {"select": {"name": menic}},
        "Konštrukcia (typ)": {"select": {"name": konstr_typ}},
        "Batéria (typ)": {"select": {"name": bat_typ}},
        "Batéria počet": {"select": {"name": "1"}},
        "Wallbox (typ)": {"select": {"name": wb_typ}},
    }

    # Default marza 30% pre vsetky varianty ak este nie su nastavene
    DEFAULT_MARZA = "30"
    for v in ("A", "B", "C", "D"):
        marza_aktualna = flat.get(f"Marža {v} %")
        if not marza_aktualna or marza_aktualna == "":
            update_props_komponenty[f"Marža {v} %"] = {"select": {"name": DEFAULT_MARZA}}

    try:
        notion_update_page(page_id, update_props_komponenty)
    except Exception as e:
        log.exception("Notion update komponent zlyhal")
        return jsonify({"error": f"notion_update komponenty failed: {e}"}), 500

    # === KROK 2: Re-fetch page (s novymi props) a prepocet vsetkych 4 variantov ===
    try:
        page2 = notion_get_page(page_id)
        flat2 = notion_props_to_flat(page2)
        from generate_from_notion import predpocitaj_ceny_pre_record
        ceny = predpocitaj_ceny_pre_record(flat2)
    except Exception as e:
        log.exception("Predpocet zlyhal")
        # Komponenty su nastavene, ale ceny zlyhali - vratime castocny success
        return jsonify({
            "ok": False,
            "warning": "Komponenty nastavene, ale predpocet cien zlyhal",
            "error": str(e),
            "sizing": sizing,
        }), 200

    # === KROK 3: Update Notion s cenami pre A/B/C/D ===
    price_update = {}
    sumarne = {}
    for v in ("A", "B", "C", "D"):
        cv = ceny.get(v, {})
        if cv.get("cena_s_dph"):
            price_update.update(notion_set_number(f"Cena {v} s DPH", round(cv["cena_s_dph"], 2)))
            price_update.update(notion_set_number(f"Nákupná cena {v} €", round(cv["nakupna"], 2)))
            price_update.update(notion_set_number(f"Zisk {v} €", round(cv["zisk"], 2)))
            sumarne[v] = round(cv["cena_s_dph"], 2)

    # Suma CP s DPH = preferuj B (kombi), inak A
    suma = (ceny.get("B", {}).get("cena_s_dph")
            or ceny.get("A", {}).get("cena_s_dph")
            or ceny.get("D", {}).get("cena_s_dph")
            or ceny.get("C", {}).get("cena_s_dph"))
    if suma:
        price_update.update(notion_set_number("Suma CP s DPH", round(suma, 2)))

    # Auto-vyplnenie "Bateria vykon"
    m_bat = re.search(r"(\d+(?:[.,]\d+)?)\s*kWh", bat_typ)
    if m_bat:
        per_modul = float(m_bat.group(1).replace(",", "."))
        price_update.update(notion_set_number("Batéria výkon", round(per_modul * 1, 2)))

    if price_update:
        try:
            notion_update_page(page_id, price_update)
        except Exception as e:
            log.warning("Notion price update zlyhal: %s", e)

    log.info("[auto-konfig] hotovo. Sizing=%s, Ceny=%s", sizing, sumarne)

    return jsonify({
        "ok": True,
        "spotreba": spotreba_val,
        "spotreba_zdroj": spotreba_zdroj,
        "sizing": sizing,
        "komponenty": {
            "panel": sizing["panel"],
            "pocet_panelov": sizing["pocet_panelov"],
            "menic": menic,
            "bateria": bat_typ,
            "wallbox": wb_typ,
            "konstrukcia": konstr_typ,
        },
        "ceny": sumarne,
    })


# ============================================================
# WEBHOOK SANDBOX: TEST ROZLOZENIE PANELOV
# Trigger: Notion Button v "🎨 SolarEdge Rebuild Test" DB
# Vstup: { "page_id": "..." }
# Robi: stiahne SolarEdge PDF zo "SolarEdge raw report" property,
# extrahuje obrazky+data, vyrobi branded PDF, vrati base64 + filename.
# Make pak ulozi do Dropbox + nastavi "Branded report" property.
# ============================================================
@app.route("/webhook/test-rozlozenie", methods=["POST"])
@require_secret
def test_rozlozenie():
    body = request.get_json(silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info(f"[test-rozlozenie] page_id={page_id}")

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    # Najdi SolarEdge raw report file URL
    raw_files_json = flat.get("SolarEdge raw report") or ""
    raw_url = None
    if raw_files_json:
        try:
            files = json.loads(raw_files_json)
            if files and isinstance(files, list):
                raw_url = files[0].get("url")
        except (ValueError, TypeError):
            pass

    if not raw_url:
        # Set status na chybu
        try:
            notion_update_page(page_id, {
                "Status": {"select": {"name": "🔴 Chyba"}},
                "Poznámky": {"rich_text": [{"text": {"content": "Chyba: 'SolarEdge raw report' je prazdny — nahraj PDF z Designera."}}]},
            })
        except Exception:
            pass
        return jsonify({"error": "SolarEdge raw report property je prazdna"}), 400

    # Set status na "spracovavam"
    try:
        notion_update_page(page_id, {
            "Status": {"select": {"name": "⚙️ Spracovávam"}},
        })
    except Exception:
        pass

    # Spracovanie
    try:
        from solar_rebuild import process_solaredge_pdf
        import base64 as _b64
        # Heuristika: ak je v Poznamkach "BESS" alebo "bateria", pouzi 90% samospotrebu
        pozn_lower = (flat.get("Poznámky") or "").lower()
        ma_bateriu = ("bess" in pozn_lower or "bateri" in pozn_lower or "wallbox" in pozn_lower)

        pdf_bytes, priezvisko_safe, summary = process_solaredge_pdf(raw_url, ma_bateriu=ma_bateriu)
        pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")

        from datetime import datetime as _dt
        datum = _dt.now().strftime("%Y-%m-%d")
        filename = f"Rozlozenie_{priezvisko_safe}_{datum}.pdf"
        folder_name = f"SolarEdge_rebuild_test/{priezvisko_safe}"

        log.info(f"[test-rozlozenie] hotovo: {filename} ({len(pdf_bytes)//1024} KB)")

        return jsonify({
            "success": True,
            "filename": filename,
            "folder_name": folder_name,
            "data": pdf_b64,
            "summary": summary,
        })

    except Exception as e:
        log.exception("[test-rozlozenie] zlyhalo")
        try:
            notion_update_page(page_id, {
                "Status": {"select": {"name": "🔴 Chyba"}},
                "Poznámky": {"rich_text": [{"text": {"content": f"Chyba: {str(e)[:1800]}"}}]},
            })
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


# ============================================================
# WEBHOOK PROD: SPRACUJ ROZLOZENIE PANELOV (Zakaznici B2C)
# Trigger: Notion Button "🎨 Spracuj rozloženie" v DB Zakaznici B2C
# Vstup: { "page_id": "..." }
# Robi: stiahne SolarEdge PDF z "SolarEdge raw" property,
# extrahuje obrazky+data, vyrobi branded PDF, vrati base64.
# Make pak ulozi do Dropbox + nastavi "Rozlozenie panelov" property.
# Variant-aware: ak ma B/C zaskrtnute, pouzije 90% samospotrebu (s bateriou).
# ============================================================
@app.route("/webhook/spracuj-rozlozenie", methods=["POST"])
@require_secret
def spracuj_rozlozenie():
    body = request.get_json(silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info(f"[spracuj-rozlozenie] page_id={page_id}")

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    raw_files_json = flat.get("SolarEdge raw") or ""
    raw_url = None
    if raw_files_json:
        try:
            files = json.loads(raw_files_json)
            if files and isinstance(files, list):
                raw_url = files[0].get("url")
        except (ValueError, TypeError):
            pass

    if not raw_url:
        return jsonify({
            "success": False,
            "error": "SolarEdge raw property je prazdna — najprv nahraj PDF z Designera."
        }), 200  # 200 aby Make scenar pokracoval na error handler ak chce

    try:
        from solar_rebuild import process_solaredge_pdf
        import base64 as _b64

        # Variant-aware samospotreba — B/C = bateria → 90%
        var_b = flat.get("Variant B — FVE + BESS") == "__YES__"
        var_c = flat.get("Variant C — FVE + BESS + Wallbox") == "__YES__"
        ma_bateriu = var_b or var_c

        pdf_bytes, _pdf_priezvisko, summary = process_solaredge_pdf(raw_url, ma_bateriu=ma_bateriu)
        pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")

        # PRIEZVISKO z Notion title (konzistentne s generate-pdf endpointom)
        # — nie z PDF extrakcie, lebo "BUCEK" v PDF != "Bucek" v Notion
        from generate_from_notion import lead_from_notion as _lfn, safe_filename as _sf
        try:
            _lead = _lfn(flat, "A")
            priezvisko = _lead.get("meno", "").split()[-1] if _lead.get("meno") else "Klient"
        except Exception:
            priezvisko = "Klient"
        priezvisko_safe = _sf(priezvisko) or "Klient"

        # ID z Notion ID ponuky property — rovnaky format ako generate-pdf
        id_p = flat.get("ID ponuky") or ""
        id_match = re.search(r"\d+", str(id_p))
        ev_id = f"EV-26-{int(id_match.group(0)):03d}" if id_match else "EV-XX"
        filename = f"{ev_id}_Rozlozenie_{priezvisko_safe}.pdf"
        folder_name = f"{ev_id}_{priezvisko_safe}"

        log.info(f"[spracuj-rozlozenie] hotovo: {filename} ({len(pdf_bytes)//1024} KB) ma_bateriu={ma_bateriu}")

        return jsonify({
            "success": True,
            "filename": filename,
            "folder_name": folder_name,
            "data": pdf_b64,
            "summary": summary,
        })

    except Exception as e:
        log.exception("[spracuj-rozlozenie] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# WEBHOOK: GENERUJ DOKUMENTY (post-vyhra)
# Trigger: Notion Button "📄 Generuj zmluvy" v Zákazníci B2C
# Vstup: { "page_id": "..." }
# Robi: Načíta Notion data, vyplní 4 templaty (Zmluva, Splnomocnenie,
# GDPR, Dotaznik), vráti base64 + filename + folder. Make uploadne
# do Dropboxu + zapise do Notion property fields.
# ============================================================
@app.route("/webhook/generuj-dokumenty", methods=["POST"])
@require_secret
def generuj_dokumenty():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[generuj-dokumenty] page_id=%s", page_id)

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    # === Zostav lead_data zo zaznamu ===
    from datetime import datetime
    from generate_from_notion import lead_from_notion as _lfn, safe_filename as _sf

    # Pouzi variant A pre zakladne lead udaje
    try:
        _lead = _lfn(flat, "A")
    except Exception:
        _lead = {}

    meno_priezvisko = flat.get("Zákazník", "")
    telefon = flat.get("Telefón", "")
    email = flat.get("Email", "")

    # Adresa: kombinacia ulice, mesto, psc
    ulica = flat.get("Ulica číslo", "")
    mesto = flat.get("Mesto", "")
    psc = flat.get("PSČ", "")
    adresa_parts = [p for p in [ulica, psc, mesto] if p]
    adresa = ", ".join(adresa_parts)

    trvale_bydlisko = flat.get("Trvalé bydlisko", "") or adresa

    cislo_op = flat.get("Číslo OP", "")

    # Datum narodenia format date YYYY-MM-DD -> DD.MM.YYYY
    datum_narodenia_raw = flat.get("date:Dátum narodenia:start", "") or flat.get("Dátum narodenia", "")
    datum_narodenia = ""
    if datum_narodenia_raw:
        try:
            d = datetime.strptime(datum_narodenia_raw[:10], "%Y-%m-%d")
            datum_narodenia = d.strftime("%d.%m.%Y")
        except Exception:
            datum_narodenia = datum_narodenia_raw

    # Cenova ponuka - vyber variant
    # Prioritne čítaj "Variant do zmluvy" select (A/B/C/D)
    # Fallback: prvý zaškrtnutý variant (priorita: B > A > C > D)
    variant_to_use = None
    variant_select = flat.get("Variant do zmluvy") or ""
    if variant_select:
        # Select hodnoty: "A — FVE", "B — FVE + BESS", ...
        m_v = re.match(r"\s*([ABCD])", variant_select)
        if m_v:
            variant_to_use = m_v.group(1)
    if not variant_to_use:
        for v in ("B", "A", "C", "D"):
            prop_name = {
                "A": "Variant A — FVE",
                "B": "Variant B — FVE + BESS",
                "C": "Variant C — FVE + BESS + Wallbox",
                "D": "Variant D — FVE + Wallbox",
            }[v]
            if flat.get(prop_name) == "__YES__":
                variant_to_use = v
                break

    cena_s_dph = 0
    if variant_to_use:
        cena_key = f"Cena {variant_to_use} s DPH"
        cena_s_dph = float(flat.get(cena_key) or 0)
    cena_bez_dph = round(cena_s_dph / 1.23, 2) if cena_s_dph else 0

    # ID ponuky
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id_root = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"
    cislo_cp = f"{ev_id_root}-{variant_to_use or 'A'}"

    # Datum cenovej ponuky
    datum_cp_raw = flat.get("date:Dátum odoslania CP:start", "")
    datum_cp = ""
    if datum_cp_raw:
        try:
            d = datetime.strptime(datum_cp_raw[:10], "%Y-%m-%d")
            datum_cp = d.strftime("%d.%m.%Y")
        except Exception:
            datum_cp = datum_cp_raw

    # Vykon FVE z poctu panelov × 535
    pocet_panelov_raw = flat.get("Počet panelov") or "0"
    try:
        pocet_panelov = int(pocet_panelov_raw)
    except (ValueError, TypeError):
        pocet_panelov = 0
    vykon_kwp = round(pocet_panelov * 535 / 1000, 2)

    datum_dnes = datetime.now().strftime("%d.%m.%Y")

    lead_data = {
        "meno_priezvisko": meno_priezvisko,
        "adresa": adresa,
        "telefon": telefon,
        "email": email,
        "vykon_kwp": vykon_kwp,
        "cislo_cp": cislo_cp,
        "datum_cp": datum_cp,
        "miesto_vykonu": adresa,
        "cena_eur": cena_bez_dph,  # do zmluvy ide cena bez DPH
        "datum_dnes": datum_dnes,
        "datum_narodenia": datum_narodenia,
        "cislo_op": cislo_op,
        "trvale_bydlisko": trvale_bydlisko,
        "ev_id": ev_id_root,
        # Pre dotaznik
        "ulica_cislo": ulica,
        "mesto": mesto,
        "psc": psc,
        "iban": flat.get("IBAN", ""),
        "banka": flat.get("Banka", ""),
        "eic": flat.get("EIC odberného miesta", ""),
        "cislo_obch_partnera": flat.get("Číslo obchodného partnera", ""),
        "spotreba": str(flat.get("Spotreba") or ""),
        "hlavny_istic": flat.get("Hlavný istič", ""),
        "predajca_energii": flat.get("Predajca energií", ""),
        "katastralne_uzemie": flat.get("Katastrálne územie", ""),
        "parcelne_cisla": flat.get("Parcelné čísla", ""),
        "adresa_om": adresa,
    }

    log.info("[generuj-dokumenty] lead_data: meno=%r, ev_id=%r, cena=%r",
             lead_data["meno_priezvisko"], lead_data["ev_id"], lead_data["cena_eur"])

    # === Vyrob 4 dokumenty ===
    try:
        from generuj_dokumenty import vygeneruj_balik_dokumentov
        import base64 as _b64

        with tempfile.TemporaryDirectory() as tmpdir:
            files = vygeneruj_balik_dokumentov(lead_data, tmpdir)

            attachments = []
            for kluc, path in files.items():
                with open(path, "rb") as f:
                    raw = f.read()
                attachments.append({
                    "kluc": kluc,
                    "filename": Path(path).name,
                    "data": _b64.b64encode(raw).decode("ascii"),
                })

            priezvisko_safe = _sf(meno_priezvisko.split()[-1] if meno_priezvisko else "Klient") or "Klient"
            folder_name = f"{ev_id_root}_{priezvisko_safe}"

            log.info("[generuj-dokumenty] vyrobenych %d dokumentov", len(attachments))

            return jsonify({
                "success": True,
                "folder_name": folder_name,
                "attachments": attachments,
                "summary": {
                    "klient": meno_priezvisko,
                    "ev_id": ev_id_root,
                    "cena_bez_dph": cena_bez_dph,
                    "variant": variant_to_use,
                    "vykon_kwp": vykon_kwp,
                },
            })

    except Exception as e:
        log.exception("[generuj-dokumenty] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# WEBHOOK: EMAIL ZMLUVY (poslat klientovi 4 dokumenty)
# Trigger: Notion Button "📧 Poslať zmluvy" v Zákazníci B2C
# Vstup: { "page_id": "..." }
# Robi: Stiahne 4 PDF (Zmluva, Splnomocnenie, GDPR, Dotaznik) z Notion file URLs,
#       vrati base64 + email body. Make pošle cez Outlook.
# ============================================================
@app.route("/webhook/email-zmluvy", methods=["POST"])
@require_secret
def email_zmluvy():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[email-zmluvy] page_id=%s", page_id)

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    # Lead udaje
    meno_priezvisko = flat.get("Zákazník", "")
    priezvisko = meno_priezvisko.split()[-1] if meno_priezvisko else "Klient"
    email_zakaznika = (flat.get("Email") or "").strip()
    if not _is_valid_email(email_zakaznika):
        return jsonify({
            "success": False,
            "email_valid": "false",
            "error": f"Neplatny email: '{email_zakaznika}'",
        }), 200

    # Obchodnik
    from generate_from_notion import OBCHODNICI, DEFAULT_OBCHODNIK, safe_filename as _sf
    obchodnik = OBCHODNICI.get(flat.get("Obchodník") or flat.get("Obchodnik") or "", DEFAULT_OBCHODNIK)

    # ID a folder
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id_root = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"
    priezvisko_safe = _sf(priezvisko) or "Klient"
    folder_name = f"{ev_id_root}_{priezvisko_safe}"

    # Stiahni 4 dokumenty z Notion file URLs
    import base64 as _b64
    attachments = []
    file_props = [
        ("Zmluva PDF", "Zmluva o dielo"),
        ("Splnomocnenie PDF", "Splnomocnenie"),
        ("GDPR súhlas PDF", "GDPR súhlas"),
        ("Dotazník PDF", "Dotazník"),
    ]
    docs_present = []
    for prop_name, label in file_props:
        files_json = flat.get(prop_name) or ""
        if not files_json:
            continue
        try:
            files = json.loads(files_json)
        except (ValueError, TypeError):
            files = []
        if not files:
            continue
        f = files[0]
        url = f.get("url")
        fname = f.get("name") or f"{label}_{priezvisko_safe}"
        if not url:
            continue
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            attachments.append({
                "filename": fname,
                "folder_name": folder_name,
                "data": _b64.b64encode(r.content).decode("ascii"),
            })
            docs_present.append(label)
        except Exception as e:
            log.warning(f"Stiahnutie {prop_name} zlyhalo: {e}")

    if not attachments:
        return jsonify({
            "success": False,
            "error": "Ziadne dokumenty na poslanie. Najprv klikni 📄 Generuj zmluvy.",
        }), 200

    # Email body
    subject = f"Zmluvná dokumentácia — {meno_priezvisko} (Energovision)"
    body_html = f"""
    <p>Dobrý deň pán/pani {priezvisko},</p>
    <p>na základe Vášho súhlasu s našou cenovou ponukou Vám zasielam zmluvnú dokumentáciu k inštalácii fotovoltickej elektrárne. V prílohe nájdete:</p>
    <ul>
      <li><strong>Zmluva o dielo</strong> — zmluva o dodávke a inštalácii FVE</li>
      <li><strong>Splnomocnenie</strong> — pre komunikáciu s distribučnou spoločnosťou, SIEA a stavebným úradom</li>
      <li><strong>Súhlas so spracovaním osobných údajov</strong> (GDPR)</li>
      <li><strong>Dotazník pripojenia FVE</strong> — administratívne podklady pre žiadosť o pripojenie do siete</li>
    </ul>
    <p style="background:#FFF8E1;padding:12px;border-left:4px solid #F59E0B;font-size:14px;">
      <strong>Postup:</strong>
      <br>1. Vytlačte všetky 4 dokumenty.
      <br>2. Vyplňte <strong>Dotazník</strong> (potrebujeme údaje pre žiadosť do distribučky — EIC odberného miesta, IBAN, parcelné čísla, atď.).
      <br>3. Podpíšte všetky 4 dokumenty (3 podpisy + dotazník).
      <br>4. Odošlite ich naskenované späť na <strong>{obchodnik.get('email', 'info@energovision.sk')}</strong>, alebo ich odovzdajte osobne pri obhliadke.
    </p>
    <p>Po prijatí podpísaných dokumentov Vám vystavíme zálohovú faktúru (30 % z ceny) a po jej úhrade pripravíme termín inštalácie (typicky 4–8 týždňov).</p>
    <p>V prípade akýchkoľvek otázok ma neváhajte kontaktovať.</p>
    <p>S pozdravom,<br>
    <strong>{obchodnik.get('meno', 'Dominik Galaba')}</strong><br>
    {obchodnik.get('funkcia', 'Office & Administration Manager')}<br>
    📞 {obchodnik.get('tel', '+421 917 424 564')}<br>
    ✉ {obchodnik.get('email', 'dominik.galaba@energovision.sk')}<br>
    <br>
    <em>Energovision s.r.o. — moderné energetické riešenia, ktoré nadchnú</em></p>
    """

    log.info(f"[email-zmluvy] hotovo: {len(attachments)} attachments, to={email_zakaznika}")

    return jsonify({
        "success": True,
        "email_valid": "true",
        "to": email_zakaznika,
        "subject": subject,
        "body_html": body_html,
        "attachments": attachments,
        "obchodnik": obchodnik,
        "docs_sent": docs_present,
    })


# ============================================================
# WEBHOOK: GENERUJ REALIZACNE DOKUMENTY (Revízna správa + Preberací protokol)
# Trigger: Notion Button "📋 Generuj revíziu+protokol"
# Vstup: { "page_id": "..." }
# Robi: po Realizacii vyrobi revíznu správu (z BYTTERM templatu) +
# preberací protokol s konfig FVE. Vrati 2 docx attachmenty.
# ============================================================
@app.route("/webhook/generuj-realizacne", methods=["POST"])
@require_secret
def generuj_realizacne():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[generuj-realizacne] page_id=%s", page_id)

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    from datetime import datetime
    from generate_from_notion import safe_filename as _sf

    meno_priezvisko = flat.get("Zákazník", "")
    telefon = flat.get("Telefón", "")
    email = flat.get("Email", "")

    ulica = flat.get("Ulica číslo", "")
    mesto = flat.get("Mesto", "")
    psc = flat.get("PSČ", "")
    adresa_parts = [p for p in [ulica, mesto] if p]
    adresa = ", ".join(adresa_parts)
    psc_mesto = f"{psc} {mesto}".strip()

    # Konfig
    pocet_panelov_raw = flat.get("Počet panelov") or "0"
    try:
        pocet_panelov = int(pocet_panelov_raw)
    except (ValueError, TypeError):
        pocet_panelov = 0
    vykon_kwp = round(pocet_panelov * 535 / 1000, 2)

    # Variant do zmluvy — filtruje komponenty
    variant_select = flat.get("Variant do zmluvy") or ""
    m_v = re.match(r"\s*([ABCD])", variant_select)
    variant = m_v.group(1) if m_v else "B"  # fallback B

    # Bateria — iba pri B alebo C
    bateria_typ_raw = flat.get("Batéria (typ)") or ""
    pocet_baterii_raw = flat.get("Batéria počet") or "0"
    try:
        pocet_baterii_int = int(pocet_baterii_raw)
    except (ValueError, TypeError):
        pocet_baterii_int = 0
    if variant in ("B", "C"):
        bateria_typ = bateria_typ_raw
        pocet_baterii = pocet_baterii_int
    else:
        bateria_typ = ""
        pocet_baterii = 0
    # Extrahuj kWh z label
    m_bat = re.search(r"(\d+(?:[.,]\d+)?)\s*kWh", bateria_typ)
    per_modul_kwh = float(m_bat.group(1).replace(",", ".")) if m_bat else 0
    bateria_kwh = round(pocet_baterii * per_modul_kwh, 2)

    menic = flat.get("Menič") or "Solinteg MHT-10K-25"
    konstrukcia = flat.get("Konštrukcia (typ)") or "Škridla"

    # Wallbox — iba pri C alebo D
    wallbox_typ_raw = flat.get("Wallbox (typ)") or ""
    if variant in ("C", "D"):
        wallbox_typ = wallbox_typ_raw
    else:
        wallbox_typ = ""
    ma_wallbox = bool(wallbox_typ)

    # Datum spustenia
    datum_spustenia_raw = flat.get("date:Dátum spustenia:start", "")
    datum_spustenia = ""
    if datum_spustenia_raw:
        try:
            d = datetime.strptime(datum_spustenia_raw[:10], "%Y-%m-%d")
            datum_spustenia = d.strftime("%d.%m.%Y")
        except Exception:
            datum_spustenia = datum_spustenia_raw
    if not datum_spustenia:
        datum_spustenia = datetime.now().strftime("%d.%m.%Y")

    # ID ponuky
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id_root = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"

    lead_data = {
        "meno_priezvisko": meno_priezvisko,
        "adresa": adresa,
        "psc_mesto": psc_mesto,
        "telefon": telefon,
        "email": email,
        "vykon_kwp": vykon_kwp,
        "pocet_panelov": pocet_panelov,
        "menic": menic,
        "bateria_typ": bateria_typ,
        "pocet_baterii": pocet_baterii,
        "bateria_kwh": bateria_kwh,
        "konstrukcia": konstrukcia,
        "wallbox_typ": wallbox_typ,
        "ma_wallbox": ma_wallbox,
        "datum_zahajenia": datum_spustenia,
        "datum_odovzdania": datum_spustenia,
        "cislo_protokolu": ev_id_root,
        "ev_id": ev_id_root,
    }

    log.info("[generuj-realizacne] %s: %.2f kWp + %.2f kWh bat",
             meno_priezvisko, vykon_kwp, bateria_kwh)

    try:
        from generuj_dokumenty import vygeneruj_realizacne_dokumenty
        import base64 as _b64

        with tempfile.TemporaryDirectory() as tmpdir:
            files = vygeneruj_realizacne_dokumenty(lead_data, tmpdir)

            attachments = []
            for kluc, path in files.items():
                with open(path, "rb") as f:
                    raw = f.read()
                attachments.append({
                    "kluc": kluc,
                    "filename": Path(path).name,
                    "data": _b64.b64encode(raw).decode("ascii"),
                })

            priezvisko_safe = _sf(meno_priezvisko.split()[-1] if meno_priezvisko else "Klient") or "Klient"
            folder_name = f"{ev_id_root}_{priezvisko_safe}"

            log.info("[generuj-realizacne] hotovo: %d dokumentov", len(attachments))

            return jsonify({
                "success": True,
                "folder_name": folder_name,
                "attachments": attachments,
                "summary": {
                    "klient": meno_priezvisko,
                    "ev_id": ev_id_root,
                    "vykon_kwp": vykon_kwp,
                    "bateria_kwh": bateria_kwh,
                },
            })

    except Exception as e:
        log.exception("[generuj-realizacne] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# WEBHOOK: GENERUJ PO (Material Purchase Order)
# Trigger: Notion automation pri Status = 💰 Faktúra
# Vstup: { "page_id": "..." }
# Výstup: BOM list, vytvorí rows v Materiál PO DB
# ============================================================
@app.route("/webhook/generuj-po", methods=["POST"])
@require_secret
def generuj_po():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[generuj-po] page_id=%s", page_id)

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)
    meno_priezvisko = flat.get("Zákazník", "")

    # ID ponuky -> EV-26-XXX
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"

    # Konfig pre BOM
    pocet_panelov_raw = flat.get("Počet panelov") or "0"
    try:
        pocet_panelov = int(pocet_panelov_raw)
    except (ValueError, TypeError):
        pocet_panelov = 0

    panel_typ = flat.get("Typ panela") or flat.get("Panel") or "LONGi 535 Wp"

    # Wp z typu panela
    m_wp = re.search(r"(\d+)\s*Wp", panel_typ)
    wp = int(m_wp.group(1)) if m_wp else 535
    vykon_kwp = round(pocet_panelov * wp / 1000, 2)

    menic = flat.get("Menič") or ""
    bateria_typ = flat.get("Batéria (typ)") or ""
    pocet_baterii_raw = flat.get("Batéria počet") or "0"
    try:
        pocet_baterii = int(pocet_baterii_raw)
    except (ValueError, TypeError):
        pocet_baterii = 0
    wallbox_typ = flat.get("Wallbox (typ)") or ""
    konstrukcia = flat.get("Konštrukcia (typ)") or "Škridla"
    distribucka = flat.get("Distribučka") or flat.get("Distribuční") or ""

    # Variant do zmluvy filtruje BOM komponenty
    # A = iba FVE (bez batérie, bez wallboxu)
    # B = FVE + batéria (bez wallboxu)
    # C = FVE + batéria + wallbox
    # D = FVE + wallbox (bez batérie)
    variant_select = flat.get("Variant do zmluvy") or ""
    m_v = re.match(r"\s*([ABCD])", variant_select)
    variant = m_v.group(1) if m_v else "B"  # default B (najčastejší kompromis)

    # Filter komponentov podľa variantu
    if variant == "A":
        bateria_typ_eff = ""
        pocet_baterii_eff = 0
        wallbox_typ_eff = ""
    elif variant == "B":
        bateria_typ_eff = bateria_typ
        pocet_baterii_eff = pocet_baterii
        wallbox_typ_eff = ""
    elif variant == "C":
        bateria_typ_eff = bateria_typ
        pocet_baterii_eff = pocet_baterii
        wallbox_typ_eff = wallbox_typ
    elif variant == "D":
        bateria_typ_eff = ""
        pocet_baterii_eff = 0
        wallbox_typ_eff = wallbox_typ
    else:
        bateria_typ_eff = bateria_typ
        pocet_baterii_eff = pocet_baterii
        wallbox_typ_eff = wallbox_typ

    lead_data = {
        "pocet_panelov": pocet_panelov,
        "vykon_kwp": vykon_kwp,
        "panel_typ": panel_typ,
        "menic": menic,
        "bateria_typ": bateria_typ_eff,
        "pocet_baterii": pocet_baterii_eff,
        "wallbox_typ": wallbox_typ_eff,
        "ma_wallbox": bool(wallbox_typ_eff),
        "konstrukcia": konstrukcia,
        "distribucka": distribucka,
        "variant": variant,
    }

    log.info("[generuj-po] %s: %.2f kWp, %d panelov, batéria=%s×%d, WB=%s",
             meno_priezvisko, vykon_kwp, pocet_panelov, bateria_typ, pocet_baterii, wallbox_typ)

    try:
        from generuj_po import generuj_bom, bom_total

        bom = generuj_bom(lead_data)
        celkom_naklady = bom_total(bom)

        # Vytvor rows v Notion DB Materiál PO
        vytvorene = 0
        zlyhane = []
        for item in bom:
            props = {
                "Položka": {
                    "title": [{"text": {"content": item["polozka"]}}]
                },
                "Projekt (ev_id)": {
                    "rich_text": [{"text": {"content": ev_id}}]
                },
                "Klient": {
                    "rich_text": [{"text": {"content": meno_priezvisko}}]
                },
                "Kategória": {
                    "select": {"name": item["kategoria"]}
                },
                "Množstvo": {
                    "number": item["mnozstvo"]
                },
                "Jednotka": {
                    "select": {"name": item["jednotka"]}
                },
                "Dodávateľ": {
                    "select": {"name": item["dodavatel"]}
                },
                "Cena/ks (€)": {
                    "number": item["cena_ks"]
                },
                "Stav": {
                    "status": {"name": "Not started"}
                },
            }
            if item.get("poznamka"):
                props["Poznámka"] = {
                    "rich_text": [{"text": {"content": item["poznamka"]}}]
                }
            try:
                notion_create_page_in_db(NOTION_MATERIAL_PO_DB_ID, props)
                vytvorene += 1
            except Exception as ex:
                zlyhane.append({"polozka": item["polozka"], "error": str(ex)[:200]})
                log.warning("[generuj-po] Failed: %s — %s", item["polozka"][:40], ex)

        log.info("[generuj-po] %s: %d/%d položiek, celkom %.2f EUR (nákup)",
                 ev_id, vytvorene, len(bom), celkom_naklady)

        return jsonify({
            "success": True,
            "ev_id": ev_id,
            "klient": meno_priezvisko,
            "polozky_total": len(bom),
            "polozky_vytvorene": vytvorene,
            "polozky_zlyhane": zlyhane,
            "celkom_naklady_eur": celkom_naklady,
            "summary": {
                "vykon_kwp": vykon_kwp,
                "pocet_panelov": pocet_panelov,
                "menic": menic,
                "bateria_typ": bateria_typ if bateria_typ else None,
                "pocet_baterii": pocet_baterii,
                "wallbox_typ": wallbox_typ if wallbox_typ else None,
            },
        })

    except Exception as e:
        log.exception("[generuj-po] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# WEBHOOK: EMAIL AGENT — FIRST CONTACT
# Trigger: Make scenár pri Status = 🆕 Došlý lead alebo manuálny button
# Vstup: { "page_id": "..." }
# Výstup: { "to_email": "...", "subject": "...", "body": "...", "extracted_info": {...} }
# ============================================================
@app.route("/webhook/email-agent-first", methods=["POST"])
@require_secret
def email_agent_first():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[email-agent-first] page_id=%s", page_id)

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    # Skip ak Rieši obchodník
    if _human_handles_lead(flat):
        log.info("[email-agent-first] preskočené — rieši obchodník")
        return jsonify({"success": False, "skipped": True, "reason": "rieši_obchodník"}), 200

    # Postavi lead pre email_agent
    meno = flat.get("Zákazník", "")
    email = flat.get("Email", "")
    if not email:
        return jsonify({"error": "lead has no email — cannot start email agent"}), 400

    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id = f"EV-26-{int(m_id.group(0)):03d}" if m_id else f"EV-26-{page_id[:3].upper()}"

    spotreba_raw = flat.get("Spotreba (kWh/rok)")
    try:
        spotreba = int(spotreba_raw) if spotreba_raw else None
    except (ValueError, TypeError):
        spotreba = None

    typ_dopytu = flat.get("Typ dopytu") or ""
    zaujem = [typ_dopytu] if typ_dopytu else ["FVE"]

    lead_data = {
        "ev_id": ev_id,
        "meno": meno,
        "email": email,
        "telefon": flat.get("Telefón", ""),
        "mesto": flat.get("Mesto", ""),
        "spotreba_kwh": spotreba,
        "ma_zaujem_o": zaujem,
        "poznamky": flat.get("Poznámky", ""),
        "zdroj": flat.get("Zdroj", "Web"),
    }

    try:
        from email_agent import vygeneruj_prvy_email
        result = vygeneruj_prvy_email(lead_data)
    except Exception as e:
        log.exception("[email-agent-first] LLM zlyhal")
        return jsonify({"error": f"LLM failed: {e}"}), 500

    # Update Notion — status, transkript, subject
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")

    transcript_entry = (
        f"=== {today} | AGENT (prvý kontakt) ===\n"
        f"Subject: {result.get('subject','')}\n\n"
        f"{result.get('body','')}\n\n"
    )

    update_props = {
        "AI Status": {"select": {"name": "🤖 AI komunikuje"}},
        "AI naposledy poslal": {"date": {"start": today}},
        "AI počet follow-upov": {"number": 0},
        "Email predmet": {"rich_text": [{"text": {"content": result.get("subject", "")[:1900]}}]},
        "Email transkript": {"rich_text": [{"text": {"content": transcript_entry[:1900]}}]},
    }
    quality = result.get("lead_quality")
    if quality:
        update_props["AI lead quality"] = {"select": {"name": quality}}

    try:
        notion_update_page(page_id, update_props)
    except Exception as e:
        log.warning("[email-agent-first] Notion update failed: %s", e)

    return jsonify({
        "success": True,
        "to_email": email,
        "to_name": meno,
        "subject": result.get("subject", ""),
        "body": result.get("body_with_signature", result.get("body", "")),
        "ev_id": ev_id,
        "extracted_info": result.get("extracted_info", {}),
        "tokens": result.get("tokens", 0),
    })


# ============================================================
# WEBHOOK: EMAIL AGENT — REPLY HANDLER
# Trigger: Make scenár pri prichádzajúcom emaile (subject obsahuje [EV-XX-XXX])
# Vstup: { "page_id": "...", "incoming_email": "telo emailu", "incoming_subject": "..." }
# Výstup: { "should_reply": bool, "subject": "...", "body": "...", "handover": bool, "to_email": "..." }
# ============================================================
@app.route("/webhook/email-agent-reply", methods=["POST"])
@require_secret
def email_agent_reply():
    body_data = request.get_json(force=True, silent=True) or {}
    page_id = body_data.get("page_id")
    incoming_email = (body_data.get("incoming_email") or "").strip()
    incoming_subject = (body_data.get("incoming_subject") or "").strip()

    if not page_id or not incoming_email:
        return jsonify({"error": "missing page_id or incoming_email"}), 400

    log.info("[email-agent-reply] page_id=%s, body=%d znakov", page_id, len(incoming_email))

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    # Skip ak Rieši obchodník
    if _human_handles_lead(flat):
        log.info("[email-agent-reply] preskočené — rieši obchodník")
        try:
            notion_update_page(page_id, {"AI Status": {"select": {"name": "⏸ Pozastavené"}}})
        except Exception:
            pass
        return jsonify({"should_reply": False, "skipped": True, "reason": "rieši_obchodník"}), 200

    # Skontroluj AI Status
    ai_status = flat.get("AI Status") or ""
    if "Opt-out" in ai_status or "Pozastav" in ai_status:
        return jsonify({"should_reply": False, "reason": f"AI paused: {ai_status}"}), 200

    meno = flat.get("Zákazník", "")
    email = flat.get("Email", "")
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"

    spotreba_raw = flat.get("Spotreba (kWh/rok)")
    try:
        spotreba = int(spotreba_raw) if spotreba_raw else None
    except (ValueError, TypeError):
        spotreba = None

    follow_up_count_raw = flat.get("AI počet follow-upov") or "0"
    try:
        follow_up_count = int(follow_up_count_raw)
    except (ValueError, TypeError):
        follow_up_count = 0

    lead_data = {
        "ev_id": ev_id,
        "meno": meno,
        "email": email,
        "mesto": flat.get("Mesto", ""),
        "spotreba_kwh": spotreba,
        "typ_strechy": flat.get("Konštrukcia (typ)", ""),
        "ma_zaujem_o": [flat.get("Typ dopytu")] if flat.get("Typ dopytu") else [],
        "poznamky": flat.get("Poznámky", ""),
    }

    # Načítaj transcript z Notion (rich text)
    transcript_raw = flat.get("Email transkript") or ""
    transcript = _parse_email_transcript(transcript_raw)

    try:
        from email_agent import spracuj_odpoved
        result = spracuj_odpoved(lead_data, transcript, incoming_email, follow_up_count=follow_up_count)
    except Exception as e:
        log.exception("[email-agent-reply] LLM zlyhal")
        return jsonify({"error": f"LLM failed: {e}"}), 500

    # Opt-out detection
    if result.get("opted_out"):
        try:
            notion_update_page(page_id, {
                "AI Status": {"select": {"name": "🛑 Opt-out"}},
            })
        except Exception:
            pass
        return jsonify({
            "should_reply": True,
            "to_email": email,
            "to_name": meno,
            "subject": result.get("subject", ""),
            "body": result.get("body", "") + "\n\n— Tím Energovision",
            "opted_out": True,
        })

    # Update Notion — append do transkriptu, extracted info, status
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")

    new_entry = (
        f"=== {today} | KLIENT ===\n{incoming_email[:1500]}\n\n"
        f"=== {today} | AGENT (odpoveď) ===\nSubject: {result.get('subject','')}\n\n{result.get('body','')}\n\n"
    )

    # Append k existujúcemu transcriptu (max 1900 znakov v Notion rich_text)
    combined = (transcript_raw + "\n" + new_entry) if transcript_raw else new_entry
    if len(combined) > 1900:
        combined = combined[-1900:]

    update_props = {
        "Email transkript": {"rich_text": [{"text": {"content": combined}}]},
        "AI naposledy poslal": {"date": {"start": today}},
        "AI počet follow-upov": {"number": 0},  # reset po reply
    }

    ext = result.get("extracted_info") or {}
    if ext.get("spotreba_kwh") and not spotreba:
        try:
            update_props["Spotreba (kWh/rok)"] = {"number": int(ext["spotreba_kwh"])}
        except (ValueError, TypeError):
            pass
    if ext.get("typ_strechy"):
        ts = ext["typ_strechy"]
        if ts in {"Škridla", "Plech kombivrut", "Falcový plech", "Plochá strecha — J 13°", "Plochá strecha — V/Z 10°"}:
            update_props["Konštrukcia (typ)"] = {"select": {"name": ts}}

    quality = result.get("lead_quality")
    if quality:
        update_props["AI lead quality"] = {"select": {"name": quality}}

    handover = bool(result.get("handover_to_dominik"))
    next_action = result.get("next_action", "")

    if handover or next_action == "handover":
        update_props["AI Status"] = {"select": {"name": "📞 Pripravený na hovor"}}
        update_props["Status"] = {"select": {"name": "💼 V riešení"}}
    elif quality == "dead" or next_action == "stop":
        update_props["AI Status"] = {"select": {"name": "❄️ Cold"}}
    else:
        update_props["AI Status"] = {"select": {"name": "🤖 AI komunikuje"}}

    try:
        notion_update_page(page_id, update_props)
    except Exception as e:
        log.warning("[email-agent-reply] Notion update failed: %s", e)

    return jsonify({
        "should_reply": True,
        "to_email": email,
        "to_name": meno,
        "subject": result.get("subject", ""),
        "body": result.get("body_with_signature", result.get("body", "")),
        "handover": handover,
        "lead_quality": quality,
        "tokens": result.get("tokens", 0),
    })


# ============================================================
# WEBHOOK: EMAIL AGENT — FOLLOW-UP CRON
# Trigger: Make cron scenár (denne)
# Vstup: { "page_id": "...", "dni_od_poslednej": 4 }
# Výstup: { "should_send": bool, "subject": "...", "body": "..." }
# ============================================================
@app.route("/webhook/email-agent-followup", methods=["POST"])
@require_secret
def email_agent_followup():
    body_data = request.get_json(force=True, silent=True) or {}
    page_id = body_data.get("page_id")
    dni_od_poslednej = int(body_data.get("dni_od_poslednej") or 3)

    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    # Skip ak Rieši obchodník
    if _human_handles_lead(flat):
        log.info("[email-agent-followup] preskočené — rieši obchodník")
        return jsonify({"should_send": False, "skipped": True, "reason": "rieši_obchodník"}), 200

    meno = flat.get("Zákazník", "")
    email = flat.get("Email", "")
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"

    fu_raw = flat.get("AI počet follow-upov") or "0"
    try:
        fu_count = int(fu_raw)
    except (ValueError, TypeError):
        fu_count = 0

    if fu_count >= 3:
        # už sme dosiahli max → daj cold a stop
        try:
            notion_update_page(page_id, {
                "AI Status": {"select": {"name": "❄️ Cold"}},
                "AI lead quality": {"select": {"name": "cold"}},
            })
        except Exception:
            pass
        return jsonify({"should_send": False, "reason": "max_followups_reached"}), 200

    lead_data = {
        "ev_id": ev_id,
        "meno": meno,
        "email": email,
        "mesto": flat.get("Mesto", ""),
    }
    transcript_raw = flat.get("Email transkript") or ""
    transcript = _parse_email_transcript(transcript_raw)

    try:
        from email_agent import vygeneruj_followup
        result = vygeneruj_followup(lead_data, transcript, fu_count, dni_od_poslednej)
    except Exception as e:
        log.exception("[email-agent-followup] LLM zlyhal")
        return jsonify({"error": f"LLM failed: {e}"}), 500

    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    new_fu_count = fu_count + 1

    new_entry = (
        f"=== {today} | AGENT (follow-up č. {new_fu_count}) ===\n"
        f"Subject: {result.get('subject','')}\n\n{result.get('body','')}\n\n"
    )
    combined = (transcript_raw + "\n" + new_entry) if transcript_raw else new_entry
    if len(combined) > 1900:
        combined = combined[-1900:]

    update_props = {
        "Email transkript": {"rich_text": [{"text": {"content": combined}}]},
        "AI naposledy poslal": {"date": {"start": today}},
        "AI počet follow-upov": {"number": new_fu_count},
    }
    if new_fu_count >= 3 or result.get("next_action") == "stop":
        update_props["AI Status"] = {"select": {"name": "❄️ Cold"}}

    try:
        notion_update_page(page_id, update_props)
    except Exception as e:
        log.warning("[email-agent-followup] Notion update failed: %s", e)

    return jsonify({
        "should_send": True,
        "to_email": email,
        "to_name": meno,
        "subject": result.get("subject", ""),
        "body": result.get("body_with_signature", result.get("body", "")),
        "follow_up_number": new_fu_count,
        "tokens": result.get("tokens", 0),
    })


def _human_handles_lead(flat: dict) -> bool:
    """Vráti True ak je v Notion zaškrtnuté 'Rieši obchodník' — AI sa nemá miešať."""
    val = flat.get("Rieši obchodník")
    # Notion checkbox môže prísť ako True/False (bool) alebo "true"/"false" (string)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "yes", "1", "✓")
    return False


def _parse_email_transcript(raw: str) -> list:
    """Z Notion rich text transcriptu vyparsuj turns pre email_agent."""
    if not raw:
        return []
    turns = []
    # split on === markers
    blocks = re.split(r"={3,}\s*[^=]+\s*={3,}", raw)
    headers = re.findall(r"={3,}\s*([^=]+?)\s*={3,}", raw)
    for header, content in zip(headers, blocks[1:]):
        h_lower = header.lower()
        if "agent" in h_lower:
            role = "agent"
        elif "klient" in h_lower or "zákazník" in h_lower:
            role = "customer"
        else:
            continue
        c = content.strip()
        if c:
            turns.append({"role": role, "content": c[:3000]})
    return turns[-12:]


# ============================================================
# WEBHOOK: CHAT (verejný chatbot widget pre energovision.sk)
# Trigger: JS fetch z embed widgetu na webe
# Vstup: { "history": [{"role":"user|assistant","content":"..."}], "message": "..." }
# Výstup: { "answer": "...", "lead_ready": bool, "lead": {...|null} }
# CORS: povolený pre všetky originy (verejný endpoint)
# ============================================================
@app.route("/webhook/chat", methods=["POST", "OPTIONS"])
def webhook_chat():
    # CORS preflight
    if request.method == "OPTIONS":
        resp = jsonify({"ok": True})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp, 204

    body = request.get_json(force=True, silent=True) or {}
    history = body.get("history") or []
    message = (body.get("message") or "").strip()

    if not message:
        resp = jsonify({"error": "empty message"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 400

    # Hard limits proti zneužitiu
    if len(message) > 4000:
        message = message[:4000]
    if len(history) > 30:
        history = history[-30:]

    try:
        from chatbot import odpovedz_chatbot, extrahuj_lead

        result = odpovedz_chatbot(history, message)

        # Ak chatbot povedal že lead je hotový, extrahujme ho a zapíšme do Notion Default Inbox
        lead_data = None
        lead_saved = False
        if result.get("lead_ready"):
            full_history = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.get("answer", "")},
            ]
            lead_data = extrahuj_lead(full_history)

            if lead_data:
                # Zapíš do Notion Default Inbox alebo Zákazníci B2C
                try:
                    saved = _save_chatbot_lead_to_notion(lead_data, full_history)
                    lead_saved = bool(saved)
                except Exception as ex:
                    log.warning("[chat] Notion save failed: %s", ex)

        resp = jsonify({
            "answer": result.get("answer", ""),
            "lead_ready": result.get("lead_ready", False),
            "lead": lead_data,
            "lead_saved": lead_saved,
            "tokens": result.get("tokens", 0),
        })
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    except Exception as e:
        log.exception("[chat] zlyhalo")
        resp = jsonify({"error": str(e)[:200]})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 500


def _save_chatbot_lead_to_notion(lead: dict, full_history: list):
    """Zapis chatbot lead do Notion DB Zákazníci B2C ako nový záznam so Status = Došlý lead."""
    if not lead:
        return None

    meno = lead.get("meno", "Chatbot lead")
    transcript = "\n".join(
        f"{m.get('role','?').upper()}: {m.get('content','')[:500]}"
        for m in (full_history or [])[-20:]
    )

    props = {
        "Zákazník": {"title": [{"text": {"content": meno}}]},
        "Status": {"select": {"name": "🆕 Došlý lead"}},
    }

    if lead.get("email"):
        props["Email"] = {"email": lead["email"]}
    if lead.get("telefon"):
        props["Telefón"] = {"phone_number": lead["telefon"]}
    if lead.get("mesto"):
        props["Mesto"] = {"rich_text": [{"text": {"content": lead["mesto"]}}]}
    if lead.get("adresa"):
        props["Ulica číslo"] = {"rich_text": [{"text": {"content": lead["adresa"]}}]}
    if lead.get("spotreba_kwh"):
        try:
            props["Spotreba (kWh/rok)"] = {"number": int(lead["spotreba_kwh"])}
        except (ValueError, TypeError):
            pass

    poznamka_parts = []
    if lead.get("poznamka"):
        poznamka_parts.append(f"Chatbot zhrnutie: {lead['poznamka']}")
    if lead.get("ma_zaujem_o"):
        zaujem = ", ".join(lead["ma_zaujem_o"]) if isinstance(lead["ma_zaujem_o"], list) else str(lead["ma_zaujem_o"])
        poznamka_parts.append(f"Záujem o: {zaujem}")
    if lead.get("typ_strechy"):
        poznamka_parts.append(f"Strecha: {lead['typ_strechy']}")
    if lead.get("orientacia"):
        poznamka_parts.append(f"Orientácia: {lead['orientacia']}")
    poznamka_parts.append("--- Transkript z chatu ---")
    poznamka_parts.append(transcript[:1800])

    props["Poznámky"] = {
        "rich_text": [{"text": {"content": "\n".join(poznamka_parts)[:2000]}}]
    }

    try:
        new_page = notion_create_page_in_db(NOTION_DATABASE_ID, props)
        log.info("[chat] Lead saved: %s (page %s)", meno, new_page.get("id"))
        return new_page
    except Exception as e:
        log.warning("[chat] notion_create_page_in_db failed: %s", e)
        return None


# ============================================================
# WEBHOOK 1: PREPOČET CIEN
# Trigger: Notion Button "🔄 Prepočítaj cenu"
# Vstup: { "page_id": "..." }
# ============================================================
def _sync_obchodnik_zo_statusu(page_id, notion_props):
    """Ak Status obsahuje meno obchodníka (V riešení — Dominik/Pavol/Andrej/Lukáš),
    nastav property Obchodnik na celé meno."""
    status_val = notion_props.get("Status") or ""
    if "V rie" not in status_val or "—" not in status_val:
        return False
    # Extrahuj časť za pomlčkou
    parts = status_val.split("—", 1)
    if len(parts) != 2:
        return False
    krstne = parts[1].strip()
    # Map krstného mena na full name (kompatibilné s OBCHODNICI v generate_from_notion.py)
    mapping = {
        "Dominik": "Dominik Galaba",
        "Pavol": "Pavol Kaprál",
        "Andrej": "Andrej Herman",
        "Lukáš": "Lukáš Bago",
    }
    full_name = mapping.get(krstne)
    if not full_name:
        return False
    # Skontroluj či už nie je rovnaký
    current = (notion_props.get("Obchodnik") or notion_props.get("Obchodník") or "").strip()
    if current == full_name:
        return False
    try:
        notion_update_page(page_id, notion_set_select("Obchodnik", full_name))
        log.info(f"[sync-obchodnik] {page_id}: Status='{status_val}' → Obchodnik='{full_name}'")
        return True
    except Exception as e:
        log.warning(f"[sync-obchodnik] update zlyhal: {e}")
        return False


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

    # Auto-sync Obchodnik zo Statusu (ak je per-obchodník variant)
    _sync_obchodnik_zo_statusu(page_id, notion_props)

    # Detekuj zakliknuté varianty - filter pre prepocet
    variants_filter = []
    for k, v in notion_props.items():
        k_lower = k.lower().strip()
        if v == "__YES__":
            if k_lower.startswith("variant a") and "A" not in variants_filter:
                variants_filter.append("A")
            elif k_lower.startswith("variant b") and "B" not in variants_filter:
                variants_filter.append("B")
            elif k_lower.startswith("variant c") and "C" not in variants_filter:
                variants_filter.append("C")
            elif k_lower.startswith("variant d") and "D" not in variants_filter:
                variants_filter.append("D")

    # Ak ziadny variant nezaskrtnuty, ratam vsetky (backward-compat)
    if not variants_filter:
        log.info("Prepocet: ziadny variant zaskrtnuty, ratam vsetky 4")
        ceny = predpocitaj_ceny_pre_record(notion_props)
    else:
        log.info(f"Prepocet: ratam iba zaskrtnute varianty {variants_filter}")
        ceny = predpocitaj_ceny_pre_record(notion_props, variants_filter=variants_filter)

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
    d = ceny.get("D", {})
    if d.get("cena_s_dph"):
        update.update(notion_set_number("Cena D s DPH", round(d["cena_s_dph"], 2)))
        update.update(notion_set_number("Nákupná cena D €", round(d["nakupna"], 2)))
        update.update(notion_set_number("Zisk D €", round(d["zisk"], 2)))

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

    suma = (b.get("cena_s_dph") or a.get("cena_s_dph") or c.get("cena_s_dph") or d.get("cena_s_dph"))
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

    # ===== ROZLOŽENIE PANELOV (extra attachment z Notion files property) =====
    rozlozenie_attached = False
    rozlozenie_json = notion_props.get("Rozloženie panelov") or ""
    if rozlozenie_json:
        try:
            rozlozenie_files = json.loads(rozlozenie_json)
        except (ValueError, TypeError):
            rozlozenie_files = []
        for rf in rozlozenie_files[:3]:  # max 3 files
            try:
                fname = rf.get("name") or "rozlozenie_panelov.pdf"
                furl = rf.get("url")
                if not furl:
                    continue
                # safe filename
                fname_clean = safe_filename(fname.rsplit(".", 1)[0])
                fext = fname.rsplit(".", 1)[-1] if "." in fname else "pdf"
                final_name = f"Rozlozenie_panelov_{priezvisko_clean}.{fext}"
                # Stiahni cez requests + base64
                r_dl = requests.get(furl, timeout=60)
                r_dl.raise_for_status()
                pdf_b64_rozl = base64.b64encode(r_dl.content).decode("ascii")
                _evid_root_r = (lead_a.get("cislo_ponuky") or "EV-XX-001-A")
                _evid_root_r = _evid_root_r[:-2] if len(_evid_root_r) >= 2 and _evid_root_r[-2] == "-" and _evid_root_r[-1] in "ABC" else _evid_root_r
                _folder_r = f"{_evid_root_r}_{priezvisko_clean}"
                attachments.append({
                    "filename": final_name,
                    "folder_name": _folder_r,
                    "data": pdf_b64_rozl,
                })
                rozlozenie_attached = True
                log.info(f"Rozlozenie panelov pridane: {final_name} ({len(r_dl.content)} bajtov)")
            except Exception as e:
                log.warning(f"Stiahnut rozlozenie panelov zlyhalo: {e}")

    # ===== EMAIL TEMPLATES =====
    typ_ponuky = notion_props.get("Typ ponuky") or "Indikatívna"
    subject = build_subject(priezvisko, mesto, variants_active, typ_ponuky=typ_ponuky)
    body_html = build_email_body(priezvisko, mesto, vykon_kwp, bateria_kwh, ceny, variants_active, obchodnik, typ_ponuky=typ_ponuky, ma_rozlozenie=rozlozenie_attached)

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


def build_subject(priezvisko, mesto, variants, typ_ponuky="Indikatívna"):
    """Subject riadok — krátky, identifikovateľný. Pri Indikatívnej ponuke sa pridáva prefix."""
    prefix = "Indikatívna cenová ponuka" if typ_ponuky == "Indikatívna" else "Cenová ponuka"
    if len(variants) == 1:
        v_label = {"A": "FVE", "B": "FVE + batéria", "C": "FVE + batéria + wallbox", "D": "FVE + wallbox"}[variants[0]]
        return f"{prefix} {v_label} pre {priezvisko}, {mesto}"
    return f"{prefix} FVE — {priezvisko}, {mesto} ({len(variants)} varianty)"


def build_email_body(priezvisko, mesto, kwp, bateria_kwh, ceny, variants, obchodnik, typ_ponuky="Indikatívna", ma_rozlozenie=False):
    """
    HTML email body s marketingovým textom (slovenský trh, 30y FVE expert tone).
    Per-variant blocks + comparison + signature.

    typ_ponuky: "Indikatívna" (bez obhliadky, default) alebo "Exaktná" (po obhliadke)
    ma_rozlozenie: True ak v emaily je priložené aj rozlozenie panelov
    """
    cena_a = ceny.get("A") or 0
    cena_b = ceny.get("B") or 0
    cena_c = ceny.get("C") or 0
    cena_d = ceny.get("D") or 0

    # === INTRO — 2 verzie podla typu ponuky ===
    n_var = len(variants)
    n_var_str = "jednu variantu" if n_var == 1 else f"{n_var} varianty"

    if typ_ponuky == "Exaktná":
        # Po obhliadke — preverené data, presné ceny
        intro = f"""
        <p>Dobrý deň pán/pani {priezvisko},</p>
        <p>v nadväznosti na našu obhliadku Vašej nehnuteľnosti v <strong>{mesto}</strong>
        Vám zasielam <strong>presnú cenovú ponuku</strong> pre fotovoltickú elektráreň.
        Údaje sú overené priamo na mieste — strecha, konštrukcia, spotreba aj umiestnenie panelov.
        Pripravil som {n_var_str} podľa toho, ako chcete využiť energiu zo slnka.</p>
        """
    else:
        # Indikatívna — bez obhliadky, len odhad z dopytu
        intro = f"""
        <p>Dobrý deň pán/pani {priezvisko},</p>
        <p>na základe údajov, ktoré ste nám poskytli, Vám zasielam <strong>indikatívnu cenovú ponuku</strong>
        pre fotovoltickú elektráreň pre Vašu domácnosť v <strong>{mesto}</strong>.
        Pripravil som {n_var_str} podľa toho, ako chcete využiť energiu zo slnka.</p>
        <p style="background:#FFF8E1;padding:12px;border-left:4px solid #F59E0B;font-size:14px;margin:16px 0;">
          <strong>Pozn.:</strong> Toto je <em>indikatívna ponuka</em> spracovaná z údajov v dopyte —
          bez fyzickej obhliadky. Presnú cenu Vám pripravíme po obhliadke nehnuteľnosti, kedy si overíme
          stav strechy, ideálne umiestnenie panelov a kabelážnu trasu. Ceny sa môžu mierne upraviť
          (typicky ±5–10 %) podľa skutočného stavu.
        </p>
        """

    if ma_rozlozenie:
        intro += """
        <p style="background:#E0F2F1;padding:12px;border-left:4px solid #1B5E3F;font-size:14px;margin:16px 0;">
          <strong>📐 V prílohe nájdete aj návrh rozloženia panelov</strong> na Vašej streche —
          vizualizáciu z nášho projekčného software, ktorá ukáže optimálne umiestnenie a počet panelov.
        </p>
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

    # === BLOCK D — FVE + Wallbox (bez baterie) ===
    if "D" in variants:
        blocks.append(f"""
        <h3 style="color:#1B5E3F;margin-top:24px;">Varianta D — fotovoltika {kwp} kWp + wallbox (bez batérie)</h3>
        <p>Pre rodiny s elektromobilom ktoré <strong>nepotrebujú batériu</strong> — auto sa nabíja priamo zo slnka cez deň.
        Wallbox automaticky reaguje na prebytky FVE a využíva ich na nabíjanie EV. Optimálne riešenie ak doma cez deň
        bývate menej a hlavnou prioritou je nabíjanie auta zo slnka.</p>
        <ul>
          <li><strong>Investícia po dotácii:</strong> {cena_d:,.0f} € s DPH</li>
          <li><strong>Návratnosť:</strong> 7–9 rokov pri kombinácii FVE + EV nabíjanie</li>
          <li><strong>Výhoda:</strong> nižšia investícia ako varianta C, ale stále plné EV nabíjanie zo slnka</li>
          <li><strong>Hybridný menič:</strong> možnosť doplnenia batérie neskôr bez prerábania systému</li>
        </ul>
        <p style="background:#F0F8F4;padding:12px;border-left:4px solid #1B5E3F;font-size:14px;">
          <strong>Dlhodobý plán:</strong> mnoho rodín začína s variant D a po 2-3 rokoch dopĺňa batériu, keď vidia
          svoj reálny profil spotreby. Týmto spôsobom investujú postupne a vyhnú sa pre/poddimenzovaniu batérie.
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
            "POST /webhook/test-rozlozenie",
            "POST /webhook/spracuj-rozlozenie",
            "POST /webhook/generuj-dokumenty",
            "POST /webhook/email-zmluvy",
            "POST /webhook/generuj-realizacne",
            "POST /webhook/prepocet",
            "POST /webhook/generate-pdf",
            "POST /webhook/email-template",
        ],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
