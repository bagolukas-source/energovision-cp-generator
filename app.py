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
from sk_gender import oslovenie_pan_pani, oslovenie_plne
import requests
import time as _time

# ============================================================
# FIX: Pre-import heavy moduly aby prvý request po deploye nebol pomalý
# ============================================================
try:
    import weasyprint  # noqa: F401 — len pre warm-up
    import matplotlib  # noqa: F401
    matplotlib.use("Agg")  # threadsafe backend (musí byť pred prvým plt.* volaním)
    import matplotlib.pyplot as _plt  # noqa: F401
    from openpyxl import load_workbook  # noqa: F401
    from docxtpl import DocxTemplate  # noqa: F401
    from docx import Document  # noqa: F401
except Exception as _e:
    # Ak na build čase niečo chýba, neblokujeme štart — len logujeme
    print(f"[warmup] Pre-import zlyhal pre {_e}", flush=True)


# ============================================================
# FIX: Notion API retry helper — handles 429/502/503/504 transient errors
# ============================================================
def _retry_request(fn, *, max_retries=3, base_delay=1.0, retry_codes=(429, 500, 502, 503, 504)):
    """Volá fn() s exponential backoff pri prechodných HTTP chybách.
    fn musi vratit requests.Response objekt."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            r = fn()
            if r.status_code in retry_codes and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                # 429 môže mať Retry-After header
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        try:
                            delay = max(delay, float(ra))
                        except ValueError:
                            pass
                _time.sleep(min(delay, 30))
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries:
                _time.sleep(base_delay * (2 ** attempt))
                continue
            raise
    if last_exc:
        raise last_exc
    return fn()  # final attempt without retry


# ============================================================
# ============================================================
# Activity Log — automaticky audit trail pre kazdy webhook
# DB: 📋 Activity Log (Notion), data_source: 375be89f-db6d-4b19-9766-a237d27a33ea
# ============================================================
ACTIVITY_LOG_DS_ID = "375be89f-db6d-4b19-9766-a237d27a33ea"

def _log_activity(endpoint, status="OK", page_id=None, klient=None, variant=None,
                  duration_ms=None, request_id=None, detail=None, error=None):
    """Zapise aktivitu do Activity Log Notion DB. Nikdy nevyhadzuje exception."""
    try:
        title = f"{endpoint}"
        if klient:
            title += f" — {klient}"
        elif page_id:
            title += f" — {page_id[:8]}"
        title += f" [{status}]"
        
        props = {
            "Akcia": {"title": [{"text": {"content": title[:200]}}]},
            "Endpoint": {"select": {"name": endpoint}},
            "Status": {"select": {"name": status}},
        }
        if page_id:
            props["Page ID"] = {"rich_text": [{"text": {"content": str(page_id)[:200]}}]}
        if klient:
            props["Klient"] = {"rich_text": [{"text": {"content": str(klient)[:200]}}]}
        if variant:
            props["Variant"] = {"select": {"name": str(variant)}}
        if duration_ms is not None:
            props["Duration ms"] = {"number": float(duration_ms)}
        if request_id:
            props["Request ID"] = {"rich_text": [{"text": {"content": str(request_id)[:100]}}]}
        if detail:
            props["Detail"] = {"rich_text": [{"text": {"content": str(detail)[:1900]}}]}
        if error:
            props["Error"] = {"rich_text": [{"text": {"content": str(error)[:1900]}}]}
        
        payload = {
            "parent": {"database_id": ACTIVITY_LOG_DS_ID},
            "properties": props,
        }
        r = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=10)
        # Best-effort — neraisuje aj pri 4xx/5xx
    except Exception as e:
        try:
            log.warning(f"_log_activity zlyhal: {e}")
        except Exception:
            pass


# FIX: Defensive Claude response parser — chráni proti IndexError pri overload
# ============================================================
def _safe_claude_text(resp_json):
    """Bezpečne vyextrahuje text z Claude API response. Vracia '' ak je prázdne/error."""
    if not isinstance(resp_json, dict):
        return ""
    content = resp_json.get("content")
    if not content or not isinstance(content, list) or len(content) == 0:
        return ""
    first = content[0]
    if not isinstance(first, dict):
        return ""
    return first.get("text", "") or ""

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

# ============================================================
# H2: Request size limit — max 10 MB (chranime Render RAM)
# ============================================================
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB

# ============================================================
# H3: Security headers + JSON-only enforcement na ALL responses
# ============================================================
@app.after_request
def _add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response



# ============================================================
# FIX: Global error handler — pre kazdu nezachytenu exception vrati JSON
# Make scenare ocakavaju JSON response, nie HTML 500
# ============================================================
_REQUEST_COUNTER = [0]

@app.before_request
def _assign_request_id():
    import datetime as _dt
    _REQUEST_COUNTER[0] += 1
    rid = f"R{_dt.datetime.utcnow().strftime('%H%M%S')}-{_REQUEST_COUNTER[0] % 10000:04d}"
    request.environ["request_id"] = rid
    log.info(f"[{rid}] {request.method} {request.path}")


# ============================================================
# PUBLIC QUOTE LINK — /p/<ev_id>
# Verejná stránka cenovky pre klienta (BEZ auth) + tlačidlo akceptovať
# Inspired by Thermivio pattern.
# ============================================================
import urllib.parse as _urlparse

PUBLIC_PAGE_HTML = """<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cenová ponuka {{ev_id}} — Energovision</title>
<style>
  * {box-sizing:border-box; margin:0; padding:0}
  body {font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f7fa; color:#1a2e3f; line-height:1.5; padding:20px}
  .wrap {max-width:780px; margin:0 auto; background:white; border-radius:16px; box-shadow:0 4px 24px rgba(0,0,0,0.08); overflow:hidden}
  .hero {background:linear-gradient(135deg, #0f4c75 0%, #3282b8 100%); color:white; padding:48px 32px; text-align:center}
  .hero h1 {font-size:32px; margin-bottom:8px; font-weight:600}
  .hero .ev {opacity:0.9; font-size:14px; letter-spacing:1px}
  .hero .klient {margin-top:24px; font-size:18px}
  .content {padding:32px}
  .row {display:flex; justify-content:space-between; padding:12px 0; border-bottom:1px solid #e5eaf0}
  .row:last-child {border-bottom:none}
  .row .label {color:#5a6b7d}
  .row .val {font-weight:500; text-align:right}
  .price {font-size:42px; font-weight:700; color:#0f4c75; text-align:center; margin:24px 0; letter-spacing:-1px}
  .price small {font-size:14px; color:#5a6b7d; font-weight:400; display:block}
  .accept-form {background:#f0f4f8; padding:24px; border-radius:12px; margin-top:24px}
  .accept-form h3 {margin-bottom:16px; color:#0f4c75}
  .accept-form label {display:block; font-size:14px; color:#5a6b7d; margin-bottom:6px}
  .accept-form input {width:100%; padding:12px; border:1px solid #cdd5e0; border-radius:8px; font-size:16px; margin-bottom:12px}
  .btn {background:#10b981; color:white; border:none; padding:14px 32px; border-radius:8px; font-size:16px; font-weight:600; cursor:pointer; width:100%}
  .btn:hover {background:#059669}
  .btn:disabled {background:#9ca3af; cursor:not-allowed}
  .accepted {background:#ecfdf5; color:#065f46; padding:16px; border-radius:8px; text-align:center; margin-top:16px}
  .footer {text-align:center; padding:24px; color:#5a6b7d; font-size:13px}
  .err {background:#fef2f2; color:#991b1b; padding:12px; border-radius:8px; margin-bottom:12px; font-size:14px}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="ev">{{ev_id}}</div>
    <h1>Cenová ponuka</h1>
    <div class="klient">{{klient}}</div>
  </div>
  <div class="content">
    <div class="row"><span class="label">Variant</span><span class="val">{{variant_label}}</span></div>
    <div class="row"><span class="label">Výkon FVE</span><span class="val">{{vykon_kwp}} kWp ({{pocet_panelov}}× LONGi {{wp_label}})</span></div>
    <div class="row"><span class="label">Konštrukcia</span><span class="val">{{konstrukcia}}</span></div>
    {{bateria_row}}
    {{wallbox_row}}
    <div class="row"><span class="label">Mesto</span><span class="val">{{mesto}}</span></div>
    <div class="row"><span class="label">Platnosť ponuky</span><span class="val">30 dní od {{datum}}</span></div>
    
    <div class="price">
      {{cena_str}} € s DPH
      <small>Cena obsahuje materiál, montáž, sprevádzkovanie a 25-ročnú záruku panelov</small>
    </div>
    
    {{status_block}}
  </div>
  <div class="footer">
    Energovision s.r.o. · IČO 50 408 921<br>
    +421 917 424 564 · obchod@energovision.sk
  </div>
</div>
{{js_block}}
</body>
</html>
"""

PUBLIC_FORM_HTML = """<div class="accept-form">
  <h3>✓ Akceptujem túto ponuku</h3>
  <form id="acceptForm" onsubmit="return submitAccept(event)">
    <label>Vaše meno a priezvisko *</label>
    <input type="text" id="name" required>
    <label>Email pre potvrdenie *</label>
    <input type="email" id="email" required>
    <div id="err" class="err" style="display:none"></div>
    <button type="submit" class="btn" id="btn">Akceptujem ponuku</button>
  </form>
</div>"""

PUBLIC_ACCEPTED_HTML = """<div class="accepted">
  <strong>✓ Ponuka bola akceptovaná</strong><br>
  {{accepted_date}}<br>
  Dominik Galaba (+421 917 424 564) Vás bude kontaktovať do 24 hodín.
</div>"""

PUBLIC_JS = """<script>
async function submitAccept(e) {
  e.preventDefault();
  const btn = document.getElementById('btn');
  const errEl = document.getElementById('err');
  btn.disabled = true;
  btn.textContent = 'Spracovávam...';
  errEl.style.display = 'none';
  try {
    const r = await fetch(window.location.pathname + '/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: document.getElementById('name').value,
        email: document.getElementById('email').value,
      })
    });
    const data = await r.json();
    if (data.success) {
      window.location.reload();
    } else {
      errEl.textContent = data.error || 'Chyba pri spracovaní. Skúste neskôr.';
      errEl.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Akceptujem ponuku';
    }
  } catch (err) {
    errEl.textContent = 'Sieťová chyba: ' + err.message;
    errEl.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Akceptujem ponuku';
  }
  return false;
}
</script>"""


def _find_lead_by_ev_id(ev_id):
    """Najdi Notion page lead-u podla ID ponuky (napr. EV-26-221 alebo EV-221)."""
    # Normalize: extract numerical part
    import re as _re
    m = _re.search(r'\d+', ev_id or "")
    if not m:
        return None
    num = m.group(0)
    # Query DB s filterom ID ponuky CONTAINS num
    url = f"{NOTION_API}/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "filter": {
            "property": "ID ponuky",
            "rich_text": {"contains": num}
        },
        "page_size": 5,
    }
    try:
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])
        # Vyber prvy match
        return results[0] if results else None
    except Exception as e:
        log.warning(f"_find_lead_by_ev_id({ev_id}) zlyhal: {e}")
        return None


@app.route("/p/<ev_id>", methods=["GET"])
def public_quote(ev_id):
    """Verejna stranka cenovky pre klienta. Bez auth."""
    from datetime import datetime as _dt
    
    page = _find_lead_by_ev_id(ev_id)
    if not page:
        return ("""<html><body style="font-family:sans-serif;max-width:600px;margin:60px auto;padding:20px;text-align:center"><h2>Ponuka nenájdená</h2><p>Číslo ponuky <strong>""" + str(ev_id) + """</strong> sme v systéme nenašli. Kontaktujte prosím Dominik Galaba +421 917 424 564.</p></body></html>"""), 404
    
    flat = notion_props_to_flat(page)
    
    # Klient
    klient = flat.get("Zákazník") or "Klient"
    
    # Aktívny variant (prioritne A > B > C > D)
    variant = "A"
    for v in ["A", "B", "C", "D"]:
        for prop_key in [f"Variant {v} - FVE", f"Variant {v} - FVE + BESS", 
                         f"Variant {v} - FVE + BESS + Wallbox", f"Variant {v} - FVE + Wallbox"]:
            if flat.get(prop_key) == "__YES__":
                variant = v
                break
    
    variant_labels = {
        "A": "Variant A — FVE",
        "B": "Variant B — FVE + batéria",
        "C": "Variant C — FVE + batéria + wallbox",
        "D": "Variant D — FVE + wallbox",
    }
    
    # Cena
    cena = flat.get(f"Cena {variant} s DPH") or flat.get("Suma CP s DPH") or 0
    try:
        cena_num = float(cena)
        cena_str = f"{cena_num:,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        cena_str = "—"
    
    # Konfigurácia
    pocet_panelov = flat.get("Počet panelov") or "?"
    panel_typ = flat.get("Panel") or "LONGi 535 Wp"
    wp_match = re.search(r"(\d{3})", str(panel_typ))
    wp_label = wp_match.group(1) + " Wp" if wp_match else "535 Wp"
    
    try:
        kwp = round(int(str(pocet_panelov)) * (int(wp_match.group(1)) if wp_match else 535) / 1000, 2)
        vykon_kwp = f"{kwp:.2f}".replace(".", ",")
    except (ValueError, TypeError, AttributeError):
        vykon_kwp = "—"
    
    konstrukcia = flat.get("Konštrukcia (typ)") or "—"
    mesto = flat.get("Mesto") or "—"
    
    # Voliteľne batéria
    bateria_row = ""
    if variant in ("B", "C"):
        bat_typ = flat.get("Batéria (typ)") or ""
        bat_pocet = flat.get("Batéria počet") or "1"
        if bat_typ:
            bateria_row = f'<div class="row"><span class="label">Batéria</span><span class="val">{bat_pocet}× {bat_typ}</span></div>'
    
    wallbox_row = ""
    if variant in ("C", "D"):
        wb_typ = flat.get("Wallbox (typ)") or ""
        if wb_typ:
            wallbox_row = f'<div class="row"><span class="label">Wallbox</span><span class="val">{wb_typ}</span></div>'
    
    # Status — bol uz akceptovany?
    status = flat.get("Status") or ""
    already_accepted = status in ("🟢 Výhra", "Podpísané", "💰 Faktúra", "🏗 Realizácia", "✅ Hotové")
    
    if already_accepted:
        accepted_date = flat.get("date:Dátum prijatia podpísaných:start") or _dt.now().strftime("%d.%m.%Y")
        status_block = PUBLIC_ACCEPTED_HTML.replace("{{accepted_date}}", accepted_date)
        js_block = ""
    else:
        status_block = PUBLIC_FORM_HTML
        js_block = PUBLIC_JS
    
    html = PUBLIC_PAGE_HTML
    replacements = {
        "{{ev_id}}": ev_id,
        "{{klient}}": klient,
        "{{variant_label}}": variant_labels.get(variant, variant),
        "{{vykon_kwp}}": vykon_kwp,
        "{{pocet_panelov}}": str(pocet_panelov),
        "{{wp_label}}": wp_label,
        "{{konstrukcia}}": konstrukcia,
        "{{bateria_row}}": bateria_row,
        "{{wallbox_row}}": wallbox_row,
        "{{mesto}}": mesto,
        "{{datum}}": _dt.now().strftime("%d.%m.%Y"),
        "{{cena_str}}": cena_str,
        "{{status_block}}": status_block,
        "{{js_block}}": js_block,
    }
    for k, v in replacements.items():
        html = html.replace(k, str(v))
    
    # Audit
    try:
        _log_activity("other", status="STARTED", page_id=page.get("id"), klient=klient,
                      variant=variant, detail=f"Public quote view: /p/{ev_id}")
    except Exception:
        pass
    
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/p/<ev_id>/accept", methods=["POST"])
def public_quote_accept(ev_id):
    """Klient akceptuje ponuku. POST {name, email}."""
    from datetime import datetime as _dt
    
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    
    if not name or len(name) < 3:
        return jsonify({"success": False, "error": "Meno je povinné"}), 400
    if "@" not in email:
        return jsonify({"success": False, "error": "Neplatný email"}), 400
    
    page = _find_lead_by_ev_id(ev_id)
    if not page:
        return jsonify({"success": False, "error": "Ponuka nenájdená"}), 404
    
    page_id = page.get("id")
    flat = notion_props_to_flat(page)
    klient = flat.get("Zákazník") or "Klient"
    
    # IP klienta + user agent
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    user_agent = request.headers.get("User-Agent", "")[:300]
    
    today_iso = _dt.now().strftime("%Y-%m-%d")
    today_human = _dt.now().strftime("%d.%m.%Y %H:%M")
    
    accept_note = f"Klient akceptoval cenovku online dňa {today_human} z IP {client_ip}. Zadané meno: {name}, email: {email}. User-Agent: {user_agent[:100]}"
    
    update = {
        "Status": {"select": {"name": "🟢 Výhra"}},
        "Poznámka": {"rich_text": [{"text": {"content": accept_note[:1900]}}]},
        "date:Dátum prijatia podpísaných:start": today_iso,
    }
    
    try:
        notion_update_page(page_id, update)
        _log_activity("other", status="OK", page_id=page_id, klient=klient,
                      detail=f"PUBLIC ACCEPT by {name} ({email}) from {client_ip}")
        return jsonify({"success": True, "message": "Ponuka akceptovaná"})
    except Exception as e:
        _log_activity("other", status="ERROR", page_id=page_id, klient=klient,
                      error=str(e), detail=f"PUBLIC ACCEPT failed for {name}")
        return jsonify({"success": False, "error": "Chyba pri uložení. Skúste neskôr."}), 500


@app.errorhandler(Exception)
def _handle_uncaught(e):
    import traceback
    tb = traceback.format_exc()
    rid = request.environ.get("request_id", "R-?")
    log.exception(f"[{rid}] UNCAUGHT EXCEPTION v endpointe")
    # Pre HTTP error werkzeug exceptions zachovaj ich status code
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({
            "success": False,
            "error": str(e),
            "code": e.code,
        }), e.code
    return jsonify({
        "success": False,
        "error": str(e),
        "error_type": type(e).__name__,
        "traceback_tail": tb[-800:],
    }), 500

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


# ============================================================
# VALIDÁCIA — overuje že lead má dostatočne kvalitné dáta
# pre generovanie dokumentov
# ============================================================
FAKE_DEFAULTS = {
    "info@energovision.sk",       # firemný email — NIE klient
    "ulica 33",                   # parser default
    "suchohrad",                  # náhodný default
    "topolčany",                  # náhodný default ak nesúhlasí s mestom
}


def _is_fake(value):
    """Detekuje fake default hodnotu (case-insensitive, čistá medzera)."""
    if not value:
        return True
    v = str(value).strip().lower()
    if not v:
        return True
    return v in FAKE_DEFAULTS


def _validate_psc(psc):
    """SR PSČ je 5 cifier."""
    if not psc:
        return False
    digits = re.sub(r'\D', '', str(psc))
    return len(digits) == 5


def validate_lead_for_documents(flat, doc_type="zmluvy"):
    """
    Skontroluje povinné polia pre generovanie dokumentov.
    doc_type: "zmluvy" | "realizacne" | "pd"

    Vráti tuple (is_valid: bool, missing: list[str]).
    """
    missing = []

    # Spoločné pre všetky 3 dokumenty
    meno = flat.get("Zákazník", "")
    if not meno or len(meno.strip()) < 3:
        missing.append("Zákazník (meno a priezvisko)")

    tel = flat.get("Telefón", "")
    if not tel:
        missing.append("Telefón")

    email = flat.get("Email", "")
    if not email or _is_fake(email):
        missing.append("Email (skutočný klienta, nie info@energovision.sk)")

    ulica = flat.get("Ulica číslo", "")
    if not ulica or _is_fake(ulica):
        missing.append("Ulica číslo (skutočná adresa)")

    mesto = flat.get("Mesto", "")
    if not mesto or _is_fake(mesto):
        missing.append("Mesto")

    psc = flat.get("PSČ", "")
    if not _validate_psc(psc):
        missing.append(f"PSČ (musí byť 5 cifier, je: {psc!r})")

    if doc_type == "zmluvy":
        # Zmluvy potrebujú variant + cenu
        variant = flat.get("Variant do zmluvy", "")
        if not variant:
            missing.append("Variant do zmluvy (A/B/C/D)")
        pocet_p = flat.get("Počet panelov", "")
        if not pocet_p or str(pocet_p).strip() == "0":
            missing.append("Počet panelov")
        # Splnomocnenie potrebuje OP + dátum narodenia
        if not flat.get("Číslo OP"):
            missing.append("Číslo OP")
        if not flat.get("date:Dátum narodenia:start"):
            missing.append("Dátum narodenia")
        # Dotazník potrebuje IBAN, banka, EIC
        if not flat.get("IBAN"):
            missing.append("IBAN")
        if not flat.get("Banka"):
            missing.append("Banka")
        if not flat.get("EIC odberného miesta"):
            missing.append("EIC odberného miesta")

    elif doc_type == "realizacne":
        # Revízia + protokol potrebujú sériové čísla
        if not flat.get("Sériové č. meniča"):
            missing.append("Sériové č. meniča")
        if not flat.get("date:Dátum odovzdania:start"):
            missing.append("Dátum odovzdania")

    elif doc_type == "pd":
        # PD potrebuje distribučnú spoločnosť a parcely
        if not flat.get("Distribučná spoločnosť"):
            missing.append("Distribučná spoločnosť (SSD/VSD/ZSDIS)")
        if not flat.get("Parcelné čísla"):
            missing.append("Parcelné čísla")
        if not flat.get("Variant do zmluvy"):
            missing.append("Variant do zmluvy (A/B/C/D)")

    return (len(missing) == 0, missing)


def notion_get_page(page_id):
    """Stiahne Notion page properties — s retry na 429/5xx."""
    r = _retry_request(lambda: requests.get(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, timeout=20))
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
    r = _retry_request(lambda: requests.patch(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, json=payload, timeout=20))
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
    r = _retry_request(lambda: requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=20))
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

    user_prompt = f"Tu je raw lead text — moze obsahovat 1 alebo viac leadov:\n\n{raw_text[:15000]}"

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 8192,
        "temperature": 0.1,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    # FIX: retry pri 429/5xx Claude API (overload)
    r = _retry_request(lambda: requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90))
    r.raise_for_status()
    resp = r.json()
    # FIX: defensive parse — pri prázdnom/malformed response neraisne IndexError
    text = _safe_claude_text(resp).strip()
    if not text:
        raise RuntimeError("Claude API vratila prazdny response (mozno overloaded)")

    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("Claude vratil ne-JSON (truncated?): %s; pokus o opravu...", str(e))
        # Pokus o opravu truncated JSON — extrahuj kompletné leads pred prerušením
        repaired = _repair_truncated_leads_json(text)
        if repaired is not None:
            log.info("JSON oprava uspesna, %d leadov zachranenych", len(repaired))
            return repaired
        log.error("Claude vratil ne-JSON: %s", text[:500])
        raise RuntimeError(f"Claude vratil neplatny JSON: {e}")

    return data.get("leads", [])


def _repair_truncated_leads_json(text):
    """Opravi truncated JSON tak, ze najde kompletne lead objekty pred prerusenim.
    Vrati list dictov alebo None ak sa nic neda zachranit."""
    import json as _json
    # Najdi otvorenie "leads": [
    m = re.search(r'"leads"\s*:\s*\[', text)
    if not m:
        return None
    start = m.end()
    # Skenuj forward a hladaj kompletne objekty
    leads = []
    depth = 0
    obj_start = None
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                obj_str = text[obj_start:i+1]
                try:
                    leads.append(_json.loads(obj_str))
                except Exception:
                    pass
                obj_start = None
    return leads if leads else None


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
    props["Variant A - FVE"] = {"checkbox": variant == "A"}
    props["Variant B - FVE + BESS"] = {"checkbox": variant == "B"}
    props["Variant C - FVE + BESS + Wallbox"] = {"checkbox": variant == "C"}

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
_BOOT_TIME = None

@app.route("/health")
def health():
    """Detailny health endpoint — uptime, env-check, version."""
    global _BOOT_TIME
    import datetime as _dt
    if _BOOT_TIME is None:
        _BOOT_TIME = _dt.datetime.utcnow()
    uptime_s = (_dt.datetime.utcnow() - _BOOT_TIME).total_seconds()
    env_ok = {
        "NOTION_TOKEN": bool(NOTION_TOKEN),
        "ANTHROPIC_API_KEY": bool(ANTHROPIC_API_KEY),
        "WEBHOOK_SECRET": bool(WEBHOOK_SECRET),
        "NOTION_DATABASE_ID": bool(os.environ.get("NOTION_DATABASE_ID", "")),
        "GITHUB_PAT": bool(os.environ.get("GITHUB_PAT", "")),
    }
    return jsonify({
        "status": "ok",
        "service": "energovision-cp-generator",
        "uptime_seconds": round(uptime_s, 1),
        "uptime_human": f"{int(uptime_s // 3600)}h {int((uptime_s % 3600) // 60)}m",
        "version": "2026-05-18-v3",
        "env": env_ok,
        "env_all_set": all(env_ok.values()),
        "model": ANTHROPIC_MODEL,
    })


# ============================================================
# ADMIN: PUSH TO GITHUB
# Bridge endpoint — pouziva GITHUB_PAT env var na commit do GitHub via Contents API.
# Auth: X-Admin-Secret header (musi matchnut WEBHOOK_SECRET)
# Body: {"files": [{"filename": "app.py", "content_b64": "...", "message": "fix bug"}]}
# ============================================================
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "bagolukas-source")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "energovision-cp-generator")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")


def _gh_get_sha(filename):
    if not GITHUB_PAT:
        return None
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{filename}?ref={GITHUB_BRANCH}"
    headers = {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            return r.json().get("sha")
    except Exception as e:
        log.warning(f"_gh_get_sha({filename}) zlyhal: {e}")
    return None


def _gh_put_file(filename, content_b64, message, sha=None):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    payload = {"message": message, "content": content_b64, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}


@app.route("/admin/push", methods=["POST"])
def admin_push():
    """Commit suborov do GitHub. Auth cez X-Admin-Secret = WEBHOOK_SECRET."""
    received = request.headers.get("X-Admin-Secret", "")
    if not WEBHOOK_SECRET or received != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    if not GITHUB_PAT:
        return jsonify({"error": "GITHUB_PAT nie je nastaveny v Render env vars"}), 500

    body = request.get_json(silent=True) or {}
    files = body.get("files", [])
    default_msg = body.get("message", "admin push from Claude bridge")
    if not files:
        return jsonify({"error": "missing 'files' array"}), 400

    results = []
    for f in files:
        filename = f.get("filename", "")
        content_b64 = f.get("content_b64", "")
        msg = f.get("message", default_msg)
        if not filename or not content_b64:
            results.append({"file": filename, "ok": False, "error": "missing filename or content_b64"})
            continue
        try:
            sha = _gh_get_sha(filename)
            status, resp = _gh_put_file(filename, content_b64, msg, sha)
            ok = status in (200, 201)
            results.append({
                "file": filename,
                "ok": ok,
                "status": status,
                "action": "updated" if sha else "created",
                "commit_sha": resp.get("commit", {}).get("sha", "") if isinstance(resp, dict) else "",
                "error": resp.get("message") if not ok and isinstance(resp, dict) else None,
            })
        except Exception as e:
            results.append({"file": filename, "ok": False, "error": str(e)})

    ok_count = sum(1 for r in results if r.get("ok"))
    return jsonify({
        "success": ok_count == len(files),
        "pushed": ok_count,
        "total": len(files),
        "results": results,
    })


@app.route("/admin/push-info", methods=["GET"])
def admin_push_info():
    """Diagnostika — over ci je vsetko nastavene pre /admin/push (verejne, len bool flagy)."""
    return jsonify({
        "github_pat_set": bool(GITHUB_PAT),
        "github_owner": GITHUB_OWNER,
        "github_repo": GITHUB_REPO,
        "github_branch": GITHUB_BRANCH,
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "endpoint": "/admin/push",
        "auth_header": "X-Admin-Secret = WEBHOOK_SECRET",
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
            # NEPREPISUJ Surový lead — zachovaj pôvodný text aby user mohol skúsiť znova
            notion_update_page(page_id, {
                "Status": {"select": {"name": "🔴 Chyba"}},
                "Poznámka": {"rich_text": [{"text": {"content": f"Claude error: {e}"[:1900]}}]},
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

    # FIX: Ak sa ŽIADNY lead nepodaril, NEZAPISUJ Spracované — nastav Chyba s diagnostikou
    if len(created) == 0:
        try:
            err_summary = (failed[0].get("error", "?")[:300] if failed else "Claude nenasiel ziadne validne leady")
            notion_update_page(page_id, {
                "Status": {"select": {"name": "🔴 Chyba"}},
                "Počet vyparsovaných": {"number": 0},
                "Poznámka": {"rich_text": [{"text": {"content": f"Parsing FAILED: {len(leads)} leadov z Claude, ale všetky padli pri vytváraní v Notion DB. Prvý error: {err_summary}"[:1900]}}]},
            })
        except Exception as e:
            log.warning("Error status update zlyhal: %s", e)
        return jsonify({"ok": False, "error": "all_creates_failed", "claude_returned": len(leads), "failed": failed}), 500

    # Aspoň 1 lead sa vytvoril — Spracované + clear raw
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
                raw_url = files[0].get("url") if files else None
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
                raw_url = files[0].get("url") if files else None
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
        var_b = flat.get("Variant B - FVE + BESS") == "__YES__"
        var_c = flat.get("Variant C - FVE + BESS + Wallbox") == "__YES__"
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

@app.route("/webhook/spracuj-rozlozenie-supabase", methods=["POST"])
@require_secret
def spracuj_rozlozenie_supabase():
    """Adapter pre Supabase CRM — prijíma raw_url priamo, bez Notion."""
    body = request.get_json(silent=True) or {}
    raw_url = body.get("raw_url")
    ma_bateriu = bool(body.get("ma_bateriu", False))
    ev_id = body.get("ev_id", "EV-XX")
    priezvisko = body.get("priezvisko", "Klient")

    if not raw_url:
        return jsonify({"success": False, "error": "missing raw_url"}), 400

    log.info(f"[spracuj-rozlozenie-supabase] raw_url={raw_url[:60]}... ma_bateriu={ma_bateriu}")

    try:
        from solar_rebuild import process_solaredge_pdf
        from generate_from_notion import safe_filename as _sf
        import base64 as _b64

        pdf_bytes, _, summary = process_solaredge_pdf(raw_url, ma_bateriu=ma_bateriu)
        priezvisko_safe = _sf(priezvisko) or "Klient"
        filename = f"{ev_id}_Rozlozenie_{priezvisko_safe}.pdf"
        pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")

        log.info(f"[spracuj-rozlozenie-supabase] hotovo: {filename} ({len(pdf_bytes)//1024} KB)")

        return jsonify({
            "success": True,
            "filename": filename,
            "data": pdf_b64,
            "summary": summary,
        })
    except Exception as e:
        log.exception("[spracuj-rozlozenie-supabase] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


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

    # === VALIDÁCIA — odmietnuť ak chýbajú povinné polia ===
    is_valid, missing = validate_lead_for_documents(flat, doc_type="zmluvy")
    if not is_valid:
        msg = "Lead nemá vyplnené povinné polia: " + ", ".join(missing)
        log.warning("[generuj-dokumenty] %s", msg)
        # Zapíš error do Notion Poznámky aby Lukáš videl
        try:
            notion_update_page(page_id, {
                "Poznámky": {"rich_text": [{"text": {"content": "❌ Generovanie zmluv zlyhalo: " + msg[:200]}}]}
            })
        except Exception:
            pass
        return jsonify({"success": False, "error": msg, "missing": missing}), 400

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
                "A": "Variant A - FVE",
                "B": "Variant B - FVE + BESS",
                "C": "Variant C - FVE + BESS + Wallbox",
                "D": "Variant D - FVE + Wallbox",
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
        f = files[0] if files else None
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
    <p>Dobrý deň {oslovenie_pan_pani("", priezvisko)} {priezvisko},</p>
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

    # === VALIDÁCIA ===
    is_valid, missing = validate_lead_for_documents(flat, doc_type="realizacne")
    if not is_valid:
        msg = "Lead nemá vyplnené povinné polia pre revíznu/protokol: " + ", ".join(missing)
        log.warning("[generuj-realizacne] %s", msg)
        try:
            notion_update_page(page_id, {
                "Poznámky": {"rich_text": [{"text": {"content": "❌ Generovanie realizacných zlyhalo: " + msg[:200]}}]}
            })
        except Exception:
            pass
        return jsonify({"success": False, "error": msg, "missing": missing}), 400

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
    panel_typ = flat.get("Panel") or "LONGi 535 Wp"

    # Sériové čísla pre revíznu správu/protokol
    sn_menic = flat.get("Sériové č. meniča") or ""
    sn_panelov = flat.get("Sériové č. panelov") or ""

    # Doplnkové polia
    hlavny_istic = flat.get("Hlavný istič") or "3x25A"
    predajca_energii = flat.get("Predajca energií") or ""

    # Wallbox — iba pri C alebo D
    wallbox_typ_raw = flat.get("Wallbox (typ)") or ""
    if variant in ("C", "D"):
        wallbox_typ = wallbox_typ_raw
    else:
        wallbox_typ = ""
    ma_wallbox = bool(wallbox_typ)

    # Datumy — Dátum revízie, Dátum odovzdania, Dátum spustenia
    def _parse_d(raw):
        if not raw:
            return ""
        try:
            d = datetime.strptime(raw[:10], "%Y-%m-%d")
            return d.strftime("%d.%m.%Y")
        except Exception:
            return raw

    datum_revizie = _parse_d(flat.get("date:Dátum revízie:start", ""))
    datum_odovzdania = _parse_d(flat.get("date:Dátum odovzdania:start", ""))
    datum_spustenia = _parse_d(flat.get("date:Dátum spustenia:start", ""))

    datum_dnes_str = datetime.now().strftime("%d.%m.%Y")
    if not datum_revizie:
        datum_revizie = datum_odovzdania or datum_spustenia or datum_dnes_str
    if not datum_odovzdania:
        datum_odovzdania = datum_revizie
    if not datum_spustenia:
        datum_spustenia = datum_odovzdania

    # ID ponuky
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id_root = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"

    # Číslo PoUVV — z Notion alebo z ev_id
    cislo_pouvv = flat.get("Číslo PoUVV") or f"P-26-{ev_id_root.replace('EV-26-', '')}"
    revizny_technik = flat.get("Revízny technik") or "Miloš Ďurička"

    lead_data = {
        "meno_priezvisko": meno_priezvisko,
        "adresa": adresa,
        "psc_mesto": psc_mesto,
        "telefon": telefon,
        "email": email,
        "vykon_kwp": vykon_kwp,
        "pocet_panelov": pocet_panelov,
        "panel_typ": panel_typ,
        "menic": menic,
        "sn_menic": sn_menic,
        "sn_panelov": sn_panelov,
        "bateria_typ": bateria_typ,
        "pocet_baterii": pocet_baterii,
        "bateria_kwh": bateria_kwh,
        "konstrukcia": konstrukcia,
        "wallbox_typ": wallbox_typ,
        "ma_wallbox": ma_wallbox,
        "hlavny_istic": hlavny_istic,
        "predajca_energii": predajca_energii,
        "datum_zahajenia": datum_spustenia,
        "datum_odovzdania": datum_odovzdania,
        "datum_revizie": datum_revizie,
        "datum_dnes": datum_dnes_str,
        "cislo_protokolu": ev_id_root,
        "cislo_pouvv": cislo_pouvv,
        "revizny_technik": revizny_technik,
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
# WEBHOOK: GENERUJ PD (Projektová dokumentácia — malé zdroje do 10 kW)
# Trigger: Notion button "🏗 Generuj projekt"
# Vstup: { "page_id": "..." }
# Výstup: 5 DOCX dokumentov (krycí list, zoznam, technická správa, PoUVV, súhrnná)
# ============================================================
@app.route("/webhook/generuj-pd", methods=["POST"])
@require_secret
def generuj_pd():
    body = request.get_json(force=True, silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    log.info("[generuj-pd] page_id=%s", page_id)

    try:
        page = notion_get_page(page_id)
    except Exception as e:
        return jsonify({"error": f"notion_get failed: {e}"}), 500

    flat = notion_props_to_flat(page)

    # === VALIDÁCIA ===
    is_valid, missing = validate_lead_for_documents(flat, doc_type="pd")
    if not is_valid:
        msg = "Lead nemá vyplnené povinné polia pre PD: " + ", ".join(missing)
        log.warning("[generuj-pd] %s", msg)
        try:
            notion_update_page(page_id, {
                "Poznámky": {"rich_text": [{"text": {"content": "❌ Generovanie PD zlyhalo: " + msg[:200]}}]}
            })
        except Exception:
            pass
        return jsonify({"success": False, "error": msg, "missing": missing}), 400

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
    trvale_bydlisko = flat.get("Trvalé bydlisko", "") or adresa

    # Variant + konfig
    variant_select = flat.get("Variant do zmluvy") or ""
    m_v = re.match(r"\s*([ABCD])", variant_select)
    variant = m_v.group(1) if m_v else "B"

    pocet_panelov_raw = flat.get("Počet panelov") or "0"
    try:
        pocet_panelov = int(pocet_panelov_raw)
    except (ValueError, TypeError):
        pocet_panelov = 0
    vykon_kwp = round(pocet_panelov * 535 / 1000, 2)

    panel_typ = flat.get("Panel") or "LONGi 535 Wp"
    menic = flat.get("Menič") or "Solinteg MHT-10K-25"
    konstrukcia = flat.get("Konštrukcia (typ)") or "Šikmá strecha (škridla)"
    hlavny_istic = flat.get("Hlavný istič") or "3x25A"

    # Bateria iba B/C
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
    m_bat = re.search(r"(\d+(?:[.,]\d+)?)\s*kWh", bateria_typ)
    per_modul_kwh = float(m_bat.group(1).replace(",", ".")) if m_bat else 0
    bateria_kwh = round(pocet_baterii * per_modul_kwh, 2)

    # Wallbox iba C/D
    wallbox_typ_raw = flat.get("Wallbox (typ)") or ""
    if variant in ("C", "D"):
        wallbox_typ = wallbox_typ_raw
    else:
        wallbox_typ = ""
    ma_wallbox = bool(wallbox_typ)

    # ID ponuky → ev_id (P-26-XXX)
    id_p = flat.get("ID ponuky") or ""
    m_id = re.search(r"\d+", str(id_p))
    ev_id_root = f"EV-26-{int(m_id.group(0)):03d}" if m_id else "EV-XX"

    # PD-špecifické polia
    dis = flat.get("Distribučná spoločnosť") or ""
    cislo_pouvv = flat.get("Číslo PoUVV") or f"PoUVV-{ev_id_root}"
    parcely = flat.get("Parcelné čísla") or ""
    eic = flat.get("EIC odberného miesta") or ""
    eic_dodavka = flat.get("EIC dodávka") or ""
    katastr = flat.get("Katastrálne územie") or ""
    predajca = flat.get("Predajca energií") or ""

    lead_data = {
        "meno_priezvisko": meno_priezvisko,
        "telefon": telefon, "email": email,
        "adresa": adresa, "trvale_bydlisko": trvale_bydlisko,
        "ulica_cislo": ulica, "mesto": mesto, "psc": psc,
        "vykon_kwp": vykon_kwp, "pocet_panelov": pocet_panelov,
        "panel_typ": panel_typ, "menic": menic,
        "bateria_typ": bateria_typ, "pocet_baterii": pocet_baterii, "bateria_kwh": bateria_kwh,
        "konstrukcia": konstrukcia,
        "ma_wallbox": ma_wallbox, "wallbox_typ": wallbox_typ,
        "hlavny_istic": hlavny_istic,
        "dis": dis,
        "cislo_pouvv": cislo_pouvv,
        "parcelne_cisla": parcely,
        "eic": eic, "eic_dodavka": eic_dodavka,
        "katastralne_uzemie": katastr,
        "predajca_energii": predajca,
        "datum_dnes": datetime.now().strftime("%d.%m.%Y"),
        "ev_id": ev_id_root,
        "variant": variant,
    }

    log.info("[generuj-pd] %s: %.2f kWp variant=%s, dis=%s",
             meno_priezvisko, vykon_kwp, variant, dis)

    # Skús stiahnuť SolarEdge raw PDF pre technický výkres (voliteľné)
    solaredge_pdf_bytes = None
    raw_files_json = flat.get("SolarEdge raw") or ""
    if raw_files_json:
        try:
            import json as _json
            files_arr = _json.loads(raw_files_json) if isinstance(raw_files_json, str) else raw_files_json
            if files_arr and isinstance(files_arr, list):
                se_url = None
                for f in files_arr:
                    if isinstance(f, dict):
                        se_url = f.get("file", {}).get("url") or f.get("external", {}).get("url") or f.get("url")
                        if se_url:
                            break
                if se_url:
                    r = requests.get(se_url, timeout=60)
                    r.raise_for_status()
                    solaredge_pdf_bytes = r.content
                    log.info("[generuj-pd] SolarEdge PDF stiahnutý (%d B)", len(solaredge_pdf_bytes))
        except Exception as e:
            log.warning("[generuj-pd] SolarEdge raw nedostupný: %s", e)

    try:
        from generuj_pd import vygeneruj_projektovu_dokumentaciu
        import base64 as _b64

        with tempfile.TemporaryDirectory() as tmpdir:
            files = vygeneruj_projektovu_dokumentaciu(lead_data, tmpdir, solaredge_pdf_bytes=solaredge_pdf_bytes)

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
            folder_name = f"{ev_id_root}_{priezvisko_safe}/Projekcia"

            log.info("[generuj-pd] hotovo: %d dokumentov", len(attachments))

            return jsonify({
                "success": True,
                "folder_name": folder_name,
                "attachments": attachments,
                "summary": {
                    "klient": meno_priezvisko,
                    "ev_id": ev_id_root,
                    "vykon_kwp": vykon_kwp,
                    "variant": variant,
                    "dis": dis,
                },
            })

    except Exception as e:
        log.exception("[generuj-pd] zlyhalo")
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
    import traceback, datetime as _dt
    body = request.get_json(silent=True) or {}
    page_id = body.get("page_id")
    if not page_id:
        return jsonify({"error": "missing page_id"}), 400

    _ts = _dt.datetime.now().strftime("%H:%M:%S")
    log.info(f"[{_ts}] Prepočet START page={page_id}")
    try:
        _result = _prepocet_inner(page_id)
        # Diagnostický zápis do Poznámka — kolega vidí kedy a čo
        try:
            _ceny = _result.get("ceny", {})
            _parts = []
            for v in ("A", "B", "C", "D"):
                _c = _ceny.get(v, {})
                if isinstance(_c, dict) and _c.get("cena_s_dph"):
                    _parts.append(f"{v}={_c['cena_s_dph']:.0f}€")
                elif isinstance(_c, dict) and _c.get("error"):
                    _parts.append(f"{v}=ERR")
            _summary = ", ".join(_parts) if _parts else "nič"
            _diag = f"[{_ts}] Prepočet ✅ {_summary} ({_result.get('fields_updated',0)} polí)"
            _existing = notion_get_page(page_id).get("properties", {}).get("Poznámka", {}).get("rich_text", [])
            _existing_text = "".join(t.get("plain_text","") for t in _existing)
            # Odstráň predošlý diagnostický riadok (ak začína "[HH:MM:SS] Prepočet")
            import re as _re
            _existing_text = _re.sub(r"^\[\d{2}:\d{2}:\d{2}\] Prepočet[^\n]*\n?", "", _existing_text)
            _new_pozn = _diag + ("\n" + _existing_text if _existing_text else "")
            notion_update_page(page_id, notion_set_text("Poznámka", _new_pozn[:1900]))
        except Exception as _e:
            log.warning(f"[{_ts}] Diagnostický zápis zlyhal: {_e}")
        return jsonify({"success": True, **_result})
    except Exception as e:
        _tb = traceback.format_exc()
        log.error(f"[{_ts}] Prepočet FAIL page={page_id} → {e}\n{_tb}")
        try:
            _diag = f"[{_ts}] Prepočet ❌ {type(e).__name__}: {str(e)[:120]}"
            notion_update_page(page_id, notion_set_text("Poznámka", _diag))
        except Exception:
            pass
        return jsonify({"success": False, "error": str(e), "traceback": _tb[-500:]}), 500


def _prepocet_inner(page_id):
    """Pôvodná logika prepocet endpointu, vyňatá pre try/except wrapping."""
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

        # Auto-fill default baterie/wallboxu ak je Variant B/C/D zaskrtnuty ale prislusna polia su prazdne
        auto_fill_props = {}
        needs_battery = any(v in variants_filter for v in ("B", "C"))
        if needs_battery and not notion_props.get("Batéria (typ)"):
            menic = notion_props.get("Menič") or ""
            try:
                spotreba_val = float(str(notion_props.get("Spotreba") or 4000).replace(",", "."))
            except (TypeError, ValueError):
                spotreba_val = 4000
            if "Solinteg" in menic:
                bat_typ = "Solinteg EBA B5K1 — 10.24 kWh" if spotreba_val > 5000 else "Solinteg EBA B5K1 — 5.12 kWh"
            elif "Huawei" in menic:
                bat_typ = "Huawei LUNA2000 — 7 kWh" if spotreba_val > 5000 else "Huawei LUNA2000 — 5 kWh"
            elif "GoodWe" in menic:
                bat_typ = "Pylontech Force H3 — 5.12 kWh"
            else:
                bat_typ = "Solinteg EBA B5K1 — 5.12 kWh"
            notion_props["Batéria (typ)"] = bat_typ
            notion_props["Batéria počet"] = "1"
            auto_fill_props["Batéria (typ)"] = {"select": {"name": bat_typ}}
            auto_fill_props["Batéria počet"] = {"select": {"name": "1"}}
            log.info(f"Prepocet auto-fill: Bateria (typ) -> {bat_typ}")

        needs_wallbox = any(v in variants_filter for v in ("C", "D"))
        if needs_wallbox and not notion_props.get("Wallbox (typ)"):
            menic = notion_props.get("Menič") or ""
            if "Huawei" in menic:
                wb_typ = "Huawei AC Smart 22 kW"
            elif "GoodWe" in menic:
                wb_typ = "GoodWe 22 kW"
            else:
                wb_typ = "Solinteg 11 kW (3F)"
            notion_props["Wallbox (typ)"] = wb_typ
            auto_fill_props["Wallbox (typ)"] = {"select": {"name": wb_typ}}
            log.info(f"Prepocet auto-fill: Wallbox (typ) -> {wb_typ}")

        if auto_fill_props:
            try:
                notion_update_page(page_id, auto_fill_props)
            except Exception as e:
                log.warning(f"Prepocet auto-fill Notion update zlyhal: {e}")

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

    return {"ceny": ceny, "fields_updated": len(update)}


# ============================================================
# WEBHOOK 2: GENERATE PDF
# Trigger: Notion Button "🖨 Vytlač ponuku"
# Vstup: { "page_id": "...", "variant": "A" | "B" | "C" }
# ============================================================
@app.route("/webhook/generate-pdf", methods=["POST"])
@require_secret
def generate_pdf():
    """Wrap pre _generate_pdf_impl s try/except a validnym JSON pri chybe (Make scenar nepadne)."""
    try:
        return _generate_pdf_impl()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"generate_pdf padol: {e}\n{tb}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback_tail": tb[-500:],
            "pdf_base64": "",  # prazdny string, NIE undefined — Make sa nezadusi
            "filename": "",
            "folder_name": "",
        }), 500


def _generate_pdf_impl():
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

    # === CRM OVERRIDE ===
    # Ak Supabase CRM posiela vlastnú cenu (Cena X s DPH + Marža X), použijeme ju priamo.
    # Inak by Render použil Hagard ceny bez 35% Energovision zľavy + svoju 21% maržu → mismatch.
    crm_cena_s_dph = None
    try:
        for key in (f"Cena {variant} s DPH", f"Cena {variant}", "Cena s DPH"):
            val = flat_props.get(key)
            if val is None or val == "" or val == 0:
                continue
            crm_cena_s_dph = float(val)
            if crm_cena_s_dph > 0:
                break
    except (TypeError, ValueError):
        crm_cena_s_dph = None

    if crm_cena_s_dph and crm_cena_s_dph > 0:
        crm_marza = flat_props.get(f"Marža {variant}")
        try:
            crm_marza = float(crm_marza) if crm_marza is not None else None
        except (TypeError, ValueError):
            crm_marza = None
        dph = 0.23
        crm_cena_bez_dph = crm_cena_s_dph / (1 + dph)
        # Dotácia + ZD logika ostáva
        dotacia = ceny.get("dotacia", 0) if lead.get("dotacia", True) else 0
        cena_po_dot = crm_cena_s_dph - dotacia
        ceny = {
            "nakupna_material": ceny.get("nakupna_material", 0),
            "nakupna_praca": ceny.get("nakupna_praca", 0),
            "nakupna_spolu": ceny.get("nakupna_spolu", 0),
            "rezerva_eur": 0,
            "marza_eur": crm_cena_bez_dph - ceny.get("nakupna_spolu", 0),
            "cena_bez_dph": crm_cena_bez_dph,
            "cena_s_dph": crm_cena_s_dph,
            "dotacia": dotacia,
            "cena_po_dotacii": cena_po_dot,
            "zlava_eur": 0,
            "cena_finalna": cena_po_dot,
            "marza_pct": crm_marza if crm_marza is not None else ceny.get("marza_pct"),
            "zisk": crm_cena_bez_dph - ceny.get("nakupna_spolu", 0),
            "_source": "CRM_OVERRIDE",
        }
        log.info(f"[generate-pdf-supabase] CRM override použitý — cena s DPH = {crm_cena_s_dph}, marža = {crm_marza}%")

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
        <p>Dobrý deň {oslovenie_pan_pani("", priezvisko)} {priezvisko},</p>
        <p>v nadväznosti na našu obhliadku Vašej nehnuteľnosti v <strong>{mesto}</strong>
        Vám zasielam <strong>presnú cenovú ponuku</strong> pre fotovoltickú elektráreň.
        Údaje sú overené priamo na mieste — strecha, konštrukcia, spotreba aj umiestnenie panelov.
        Pripravil som {n_var_str} podľa toho, ako chcete využiť energiu zo slnka.</p>
        """
    else:
        # Indikatívna — bez obhliadky, len odhad z dopytu
        intro = f"""
        <p>Dobrý deň {oslovenie_pan_pani("", priezvisko)} {priezvisko},</p>
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
# WEBHOOK: GENERATE PDF FROM SUPABASE PAYLOAD
# Trigger: Energovision FVE CRM (Next.js)
# Vstup: { "flat_props": {...}, "variant": "A"|"B"|"C"|"D", "quote_id": "uuid" }
# Výstup: rovnaký ako /webhook/generate-pdf — pdf_base64 + filename
# Účel: CRM nemá Notion page_id, posiela rovnaký formát flat dict
# ============================================================
@app.route("/webhook/generate-pdf-supabase", methods=["POST"])
@require_secret
def generate_pdf_supabase():
    """Adapter pre Supabase CRM — prijíma flat_props payload namiesto page_id."""
    try:
        return _generate_pdf_supabase_impl()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"generate_pdf_supabase padol: {e}\n{tb}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback_tail": tb[-500:],
            "pdf_base64": "",
            "filename": "",
            "folder_name": "",
        }), 500


def _generate_pdf_supabase_impl():
    body = request.get_json(silent=True) or {}
    flat_props = body.get("flat_props") or {}
    variant = body.get("variant", "A")

    if not flat_props:
        return jsonify({"error": "missing flat_props"}), 400

    log.info(f"Generate PDF (Supabase) variant {variant}, klient {flat_props.get('Zákazník', '?')}")

    # Reuse — lead_from_notion berie flat dict + variant (NIE Notion page)
    from generate_from_notion import lead_from_notion
    lead = lead_from_notion(flat_props, variant)

    if variant in ("B", "C"):
        ok, msg = check_compatibility(lead["invertor_kod"], lead.get("bateria_kod"))
        if not ok:
            return jsonify({"error": f"incompatible: {msg}"}), 400

    cennik = load_cennik()
    konfig = vyrataj_konfig(lead, cennik)
    ceny = vyrataj_ceny(konfig, lead)

    # === CRM OVERRIDE ===
    # Ak Supabase CRM posiela vlastnú cenu (Cena X s DPH + Marža X), použijeme ju priamo.
    # Inak by Render použil Hagard ceny bez 35% Energovision zľavy + svoju 21% maržu → mismatch.
    crm_cena_s_dph = None
    try:
        for key in (f"Cena {variant} s DPH", f"Cena {variant}", "Cena s DPH"):
            val = flat_props.get(key)
            if val is None or val == "" or val == 0:
                continue
            crm_cena_s_dph = float(val)
            if crm_cena_s_dph > 0:
                break
    except (TypeError, ValueError):
        crm_cena_s_dph = None

    if crm_cena_s_dph and crm_cena_s_dph > 0:
        crm_marza = flat_props.get(f"Marža {variant}")
        try:
            crm_marza = float(crm_marza) if crm_marza is not None else None
        except (TypeError, ValueError):
            crm_marza = None
        dph = 0.23
        crm_cena_bez_dph = crm_cena_s_dph / (1 + dph)
        # Dotácia + ZD logika ostáva
        dotacia = ceny.get("dotacia", 0) if lead.get("dotacia", True) else 0
        cena_po_dot = crm_cena_s_dph - dotacia
        ceny = {
            "nakupna_material": ceny.get("nakupna_material", 0),
            "nakupna_praca": ceny.get("nakupna_praca", 0),
            "nakupna_spolu": ceny.get("nakupna_spolu", 0),
            "rezerva_eur": 0,
            "marza_eur": crm_cena_bez_dph - ceny.get("nakupna_spolu", 0),
            "cena_bez_dph": crm_cena_bez_dph,
            "cena_s_dph": crm_cena_s_dph,
            "dotacia": dotacia,
            "cena_po_dotacii": cena_po_dot,
            "zlava_eur": 0,
            "cena_finalna": cena_po_dot,
            "marza_pct": crm_marza if crm_marza is not None else ceny.get("marza_pct"),
            "zisk": crm_cena_bez_dph - ceny.get("nakupna_spolu", 0),
            "_source": "CRM_OVERRIDE",
        }
        log.info(f"[generate-pdf-supabase] CRM override použitý — cena s DPH = {crm_cena_s_dph}, marža = {crm_marza}%")

    navratnost = vyrataj_navratnost(konfig, ceny, lead)

    with tempfile.TemporaryDirectory() as tmpdir:
        priezvisko = safe_filename(lead["meno"].split()[-1])
        ev_id = lead.get("cislo_ponuky", f"EV-XX-001-{variant}")
        from datetime import datetime
        datum = datetime.now().strftime("%Y-%m-%d")
        base = f"{ev_id}_{priezvisko}_{datum}"

        grafy = vyrob_grafy(navratnost, lead, tmpdir, base)
        pdf_path = os.path.join(tmpdir, f"{base}.pdf")
        vyrob_html_pdf(lead, konfig, ceny, navratnost, grafy, pdf_path)
        pdf_size = os.path.getsize(pdf_path)

        import base64
        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode("ascii")

    ev_id_root = ev_id[:-2] if len(ev_id) >= 2 and ev_id[-2] == "-" and ev_id[-1] in "ABCD" else ev_id
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
        "source": "supabase",
        "quote_id": body.get("quote_id"),
    })



# ============================================================
# WEBHOOK: EMAIL TEMPLATE FROM SUPABASE PAYLOAD
# Trigger: Energovision FVE CRM
# Vstup: { "flat_props": {...}, "variants": ["A","B","C","D"], "typ_ponuky": "Indikatívna"|"Exaktná" }
# Výstup: { to, subject, body_html, obchodnik, variants_sent }
# ============================================================
@app.route("/webhook/email-template-supabase", methods=["POST"])
@require_secret
def email_template_supabase():
    """Adapter pre CRM — generuje email subject + body_html z flat_props."""
    try:
        return _email_template_supabase_impl()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"email_template_supabase padol: {e}\n{tb}")
        return jsonify({"error": str(e), "traceback_tail": tb[-500:]}), 500


def _email_template_supabase_impl():
    body = request.get_json(silent=True) or {}
    flat_props = body.get("flat_props") or {}
    variants = body.get("variants") or []
    typ_ponuky = body.get("typ_ponuky", "Indikatívna")
    ma_rozlozenie = bool(body.get("ma_rozlozenie", False))

    if not flat_props or not variants:
        return jsonify({"error": "missing flat_props or variants"}), 400

    from generate_from_notion import lead_from_notion, OBCHODNICI, DEFAULT_OBCHODNIK, safe_filename

    lead_a = lead_from_notion(flat_props, variants[0] if variants else "A")
    priezvisko = lead_a["meno"].split()[-1] if lead_a.get("meno") else "Zákazník"
    mesto = lead_a.get("mesto", "")
    email_zakaznika = (lead_a.get("email") or flat_props.get("Email") or "").strip()

    if not _is_valid_email(email_zakaznika):
        return jsonify({
            "success": False,
            "email_valid": "false",
            "error": f"Neplatný email zákazníka: '{email_zakaznika}'",
            "to": "", "subject": "", "body_html": "",
        }), 200

    obchodnik = OBCHODNICI.get(
        flat_props.get("Obchodník") or flat_props.get("Obchodnik") or "",
        DEFAULT_OBCHODNIK
    )

    vykon_kwp = lead_a.get("vykon_kwp", 0)
    bateria_kwh = float(flat_props.get("Batéria výkon") or 0)

    ceny = {
        "A": flat_props.get("Cena A s DPH") or flat_props.get("Cena A"),
        "B": flat_props.get("Cena B s DPH") or flat_props.get("Cena B"),
        "C": flat_props.get("Cena C s DPH") or flat_props.get("Cena C"),
        "D": flat_props.get("Cena D s DPH") or flat_props.get("Cena D"),
    }

    subject = build_subject(priezvisko, mesto, variants, typ_ponuky=typ_ponuky)
    body_html = build_email_body(
        priezvisko, mesto, vykon_kwp, bateria_kwh, ceny, variants,
        obchodnik, typ_ponuky=typ_ponuky, ma_rozlozenie=ma_rozlozenie
    )

    return jsonify({
        "success": True,
        "email_valid": "true",
        "to": email_zakaznika,
        "subject": subject,
        "body_html": body_html,
        "obchodnik": obchodnik,
        "variants_sent": variants,
    })


# ============================================================
# WEBHOOK: Parsuj leady — Supabase verzia (raw_text -> JSON leads)
# Trigger: Energovision FVE CRM (/leady modal "Parsuj")
# Vstup: { "raw_text": "..." }
# Výstup: { ok, count, leads: [...] }  — leads sa vytvárajú v Next.js
# ============================================================
@app.route("/webhook/parsuj-leady-supabase", methods=["POST"])
@require_secret
def parsuj_leady_supabase():
    """CRM verzia parsera — vezme raw text, vráti pole extrahovaných leadov.
    Bez Notion side-effects. Next.js si potom leady vytvorí v Supabase sám."""
    body = request.get_json(force=True, silent=True) or {}
    raw_text = (body.get("raw_text") or "").strip()
    if not raw_text:
        return jsonify({"ok": False, "error": "raw_text je prazdny"}), 400

    if len(raw_text) > 30000:
        return jsonify({"ok": False, "error": "raw_text prilis dlhy (max 30k znakov)"}), 400

    log.info("[parsuj-leady-supabase] raw_text dlzka=%d znakov", len(raw_text))

    try:
        leads = claude_extract_leads(raw_text)
    except Exception as e:
        log.exception("Claude extraction zlyhala")
        return jsonify({"ok": False, "error": f"claude failed: {e}"}), 500

    if not leads:
        return jsonify({"ok": False, "error": "Claude nenasiel ziadne leady"}), 400

    log.info("[parsuj-leady-supabase] extrahovanych leadov: %d", len(leads))
    return jsonify({"ok": True, "count": len(leads), "leads": leads})


# ============================================================
# WEBHOOK: GENERUJ DOKUMENTY — Supabase verzia
# Vstup: { order_id, kind: 'zmluva'|'splnomocnenie'|'gdpr' }
# Fetchne order + customer + lead z Supabase REST API, naplní DOCX,
# uploadne do Supabase Storage, vráti pdf_url (vlastne docx_url)
# ============================================================
@app.route("/webhook/generuj-dokumenty-supabase", methods=["POST"])
@require_secret
def generuj_dokumenty_supabase():
    body = request.get_json(force=True, silent=True) or {}
    order_id = body.get("order_id")
    kind = body.get("kind", "zmluva")
    if not order_id:
        return jsonify({"error": "missing order_id"}), 400
    if kind not in ("zmluva", "splnomocnenie", "gdpr"):
        return jsonify({"error": "kind musi byt zmluva|splnomocnenie|gdpr"}), 400

    SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not SUPABASE_KEY:
        return jsonify({"error": "SUPABASE_SERVICE_ROLE_KEY not set"}), 500

    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

    # Fetch order + customer + lead
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/orders",
        headers=headers,
        params={"select": "*,customers(*),leads(ev_id,distribucka,assigned_to,users:users!leads_assigned_to_fkey(full_name,email,phone,funkcia))", "id": f"eq.{order_id}"},
        timeout=30
    )
    if not r.ok:
        return jsonify({"error": f"supabase fetch: {r.status_code}", "body": r.text}), 500
    rows = r.json()
    if not rows:
        return jsonify({"error": "order_not_found"}), 404
    order = rows[0]
    cust = order.get("customers") or {}
    lead = order.get("leads") or {}
    obch = lead.get("users") if isinstance(lead.get("users"), dict) else {}

    # Fetch bundle pre vykon_kwp + payment_terms + panel info
    bundle = {}
    if lead.get("ev_id"):
        # Bundle je naviazaný na lead — najdi cez quote_bundles.lead_id = leads.id (potrebujem lead.id)
        # Skús cez order.bundle_id ak existuje, alebo cez lead.id (order má lead_id)
        lead_id_for_bundle = order.get("lead_id")
        if lead_id_for_bundle:
            br = requests.get(
                f"{SUPABASE_URL}/rest/v1/quote_bundles",
                headers=headers,
                params={"select": "*", "lead_id": f"eq.{lead_id_for_bundle}", "order": "created_at.desc", "limit": "1"},
                timeout=15
            )
            if br.ok and br.json():
                bundle = br.json()[0]

    pocet_panelov = int(bundle.get("pocet_panelov") or 0)
    panel_sku = bundle.get("panel_sku") or "PAN-001"
    panel_wp = 535 if panel_sku == "PAN-002" else 470
    vykon_kwp = round(pocet_panelov * panel_wp / 1000, 2) if pocet_panelov else 0

    # Záruka podľa panela (LONGi Hi-MO X10 = 25r produktová + 30r lineárna)
    if panel_sku == "PAN-001":  # LONGi Hi-MO X10 470 Wp
        zaruka_panely_produkt = 25
        zaruka_panely_linear = 30
    else:  # PAN-002 LONGi Hi-MO 6 535 Wp
        zaruka_panely_produkt = 15
        zaruka_panely_linear = 25

    # Platobné podmienky
    pt = bundle.get("payment_terms") or "60_30_10"
    platby_map = {
        "60_40": "60% - zálohová faktúra vopred\n40% - po dokončení diela",
        "50_50": "50% - zálohová faktúra vopred\n50% - po dokončení diela",
        "30_70": "30% - zálohová faktúra vopred\n70% - po dokončení diela",
        "60_30_10": "60% - zálohová faktúra vopred\n30% - po nainštalovaní elektrárne\n10% - po protokolárnom odovzdaní",
    }
    platby_text = platby_map.get(pt, platby_map["60_30_10"])

    meno = (cust.get("company_name") or f"{cust.get('first_name','')} {cust.get('last_name','')}".strip())
    ulica = cust.get("street") or ""
    psc = cust.get("postal_code") or ""
    mesto = cust.get("city") or ""
    adresa = ", ".join(filter(None, [ulica, psc, mesto])) or "(adresa nedoplnená)"
    ev_id = lead.get("ev_id") or order.get("order_number", "ORD")
    variant = order.get("accepted_variant") or "A"
    cislo_cp = f"{ev_id}-{variant}"
    cena = float(order.get("total_with_vat", 0))

    from datetime import datetime
    today = datetime.now().strftime("%d.%m.%Y")

    # Oslovenie pán/pani — derivované z mena a priezviska
    oslovenie = oslovenie_pan_pani(cust.get("first_name", ""), cust.get("last_name", ""))

    # Format dátum narodenia DD.MM.YYYY
    datum_narodenia_raw = cust.get("datum_narodenia", "")
    datum_narodenia = ""
    if datum_narodenia_raw:
        try:
            from datetime import datetime as _dt2
            d = _dt2.strptime(str(datum_narodenia_raw)[:10], "%Y-%m-%d")
            datum_narodenia = d.strftime("%d.%m.%Y")
        except Exception:
            datum_narodenia = str(datum_narodenia_raw)

    lead_data = {
        "meno_priezvisko": meno,
        "first_name": cust.get("first_name", ""),
        "last_name": cust.get("last_name", ""),
        "oslovenie": oslovenie,
        "cislo_op": cust.get("cislo_op", ""),
        "datum_narodenia": datum_narodenia,
        "adresa": adresa,
        "ulica": ulica,
        "psc": psc,
        "mesto": mesto,
        "telefon": cust.get("phone", ""),
        "email": cust.get("email", ""),
        "vykon_kwp": vykon_kwp,
        "pocet_panelov": pocet_panelov,
        "cislo_cp": cislo_cp,
        "datum_cp": today,
        "datum_dnes": today,
        "miesto_vykonu": adresa,
        "cena_eur": cena,
        "platby": platby_text,
        "payment_terms": pt,
        "zaruka_panely_produkt": zaruka_panely_produkt,
        "zaruka_panely_linear": zaruka_panely_linear,
        "ev_id": ev_id,
        "obchodnik_meno": (obch or {}).get("full_name", "Energovision tím"),
        "obchodnik_email": (obch or {}).get("email", "info@energovision.sk"),
        "obchodnik_tel": (obch or {}).get("phone", "+421 917 424 564"),
        "obchodnik_funkcia": (obch or {}).get("funkcia", "Obchodný zástupca"),
        "distribucka": lead.get("distribucka", "ZSD"),
    }

    # Vytvor temp file + naplň
    import tempfile, os as _os
    from pathlib import Path
    tmpdir = Path(tempfile.mkdtemp())

    try:
        from generuj_dokumenty import naplnif_zmluvu, naplnif_splnomocnenie, naplnif_gdpr
    except Exception as e:
        return jsonify({"error": f"import generuj_dokumenty zlyhal: {e}"}), 500

    out_path = tmpdir / f"{ev_id}_{kind}.docx"
    try:
        if kind == "zmluva":
            naplnif_zmluvu(lead_data, str(out_path))
        elif kind == "splnomocnenie":
            naplnif_splnomocnenie(lead_data, str(out_path))
        elif kind == "gdpr":
            naplnif_gdpr(lead_data, str(out_path))
    except Exception as e:
        log.exception("naplnif zlyhal")
        return jsonify({"error": f"naplnif: {e}"}), 500

    # Upload do Supabase Storage
    with open(out_path, "rb") as f:
        file_bytes = f.read()

    storage_path = f"orders/{order_id}/{kind}_{ev_id}.docx"
    up = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/documents/{storage_path}",
        headers={**headers, "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "x-upsert": "true"},
        data=file_bytes,
        timeout=30
    )
    if not up.ok:
        log.warning("storage upload zlyhal: %s %s", up.status_code, up.text)
        return jsonify({"error": "storage_upload_failed", "body": up.text}), 500

    public_url = f"{SUPABASE_URL}/storage/v1/object/public/documents/{storage_path}"

    # Update order admin field
    field = f"{kind}_url"
    if kind == "zmluva":
        field = "zmluva_url"
    elif kind == "gdpr":
        field = "gdpr_url"
    elif kind == "splnomocnenie":
        field = "splnomocnenie_url"

    requests.patch(
        f"{SUPABASE_URL}/rest/v1/orders",
        headers={**headers, "Content-Type": "application/json"},
        params={"id": f"eq.{order_id}"},
        json={field: public_url},
        timeout=10
    )

    try:
        out_path.unlink()
        tmpdir.rmdir()
    except Exception:
        pass

    return jsonify({"ok": True, "url": public_url, "kind": kind, "filename": f"{ev_id}_{kind}.docx"})




# ============================================================
# WEBHOOK: DOCX → PDF konverzia (pre admin emaily klientovi)
# Vstup: { "docx_url": "https://..." }
# Výstup: { "pdf_base64": "..." }
# ============================================================
@app.route("/webhook/docx-to-pdf", methods=["POST"])
@require_secret
def docx_to_pdf_endpoint():
    body = request.get_json(force=True, silent=True) or {}
    docx_url = body.get("docx_url")
    if not docx_url:
        return jsonify({"error": "missing docx_url"}), 400

    import base64
    from io import BytesIO
    try:
        import mammoth
        from weasyprint import HTML

        # 1) Stiahni docx
        r = requests.get(docx_url, timeout=30)
        if not r.ok:
            return jsonify({"error": f"download failed: {r.status_code}"}), 500

        # 2) DOCX → HTML cez mammoth (bez LibreOffice, beží na 512 MB)
        result = mammoth.convert_to_html(BytesIO(r.content))
        html_body = result.value

        # Wrap do A4 dokumentu s minimal CSS
        html_full = f"""<!DOCTYPE html><html lang="sk"><head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: 'Helvetica', sans-serif; font-size: 10pt; color: #1a1a1a; line-height: 1.45; }}
  h1 {{ font-size: 16pt; margin: 12pt 0 6pt; }}
  h2 {{ font-size: 13pt; margin: 10pt 0 5pt; }}
  h3 {{ font-size: 11pt; margin: 8pt 0 4pt; }}
  p {{ margin: 4pt 0; }}
  table {{ border-collapse: collapse; margin: 6pt 0; }}
  td, th {{ border: 0.5pt solid #ccc; padding: 4pt 6pt; }}
  strong {{ font-weight: 700; }}
</style></head><body>{html_body}</body></html>"""

        # 3) HTML → PDF cez WeasyPrint (už máme)
        pdf_bytes = HTML(string=html_full).write_pdf()
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        return jsonify({"ok": True, "pdf_base64": pdf_b64, "size_bytes": len(pdf_bytes), "method": "mammoth+weasyprint"})
    except Exception as e:
        log.exception("docx-to-pdf failed")
        return jsonify({"error": str(e)}), 500




# ============================================================
# WEBHOOK: AI PARSER FAKTÚRY ZA ELEKTRINU
# Vstup: { "pdf_base64": "..." } alebo { "pdf_url": "https://..." }
# Výstup: { ok, parsed: { meno, adresa_*, eic, kwh_rocne, distribucka, ... } }
# Použiteľné pre B2C zákazníka — Claude Vision API parsuje PDF stranu 1-2
# ============================================================
@app.route("/webhook/parsuj-fakturu-ele", methods=["POST"])
@require_secret
def parsuj_fakturu_ele():
    body = request.get_json(force=True, silent=True) or {}
    pdf_url = body.get("pdf_url")
    pdf_b64_input = body.get("pdf_base64")

    if not pdf_url and not pdf_b64_input:
        return jsonify({"error": "missing pdf_url alebo pdf_base64"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    import base64
    from io import BytesIO

    try:
        # Stiahni PDF
        if pdf_url:
            r = requests.get(pdf_url, timeout=30)
            if not r.ok:
                return jsonify({"error": f"download failed: {r.status_code}"}), 500
            pdf_bytes = r.content
        else:
            pdf_bytes = base64.b64decode(pdf_b64_input)

        # PDF → text (PyMuPDF — už v requirements)
        try:
            import fitz  # pymupdf
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            full_text = ""
            pages_count = min(3, doc.page_count)
            # Stačí prvých 3 strán (obsahuje všetko podstatné)
            for i in range(pages_count):
                full_text += doc[i].get_text() + "\n\n"
            doc.close()
        except Exception as e:
            return jsonify({"error": f"pdf extract failed: {e}"}), 500

        # Pošli text do Claude — raw requests (žiadne anthropic SDK)
        prompt = """Z faktúry za elektrinu (text nižšie) extrahuj údaje a vráť ich ako JSON.

PRAVIDLÁ:
- Ak údaj nie je vo faktúre, vráť null (nie "" prázdny string).
- "miesto_spotreby" = adresa kde sa elektrina spotrebúva (typicky adresa rodinného domu/inštalácie FVE)
- "korespondencna_adresa" = adresa kde sa posiela faktúra (typicky trvalý pobyt zákazníka, ak je iná ako miesto spotreby)
- Ak sú obe adresy rovnaké, korespondencna_adresa nech je null
- Rozdeľ adresu na ulica/mesto/psc (PSČ = 5 číslic, mesto BEZ PSČ)
- "kwh_rocne" = súčet NT + VT + 1T spotreby za celé zúčtovacie obdobie (typicky 1 rok). Ak je obdobie kratšie, prepočítaj na 12 mesiacov.
- "distribucna_sadzba" = D1/D2/D3 Aktiv/D4/D5/... (z faktúry typicky "D3 aktiv" alebo "DD2")
- "distribucna_spolocnost" = ZSD (ZSE = ZSD Západoslovenská), SSD (SSE = SSD Stredoslovenská), VSD (VSE = VSD Východoslovenská)
- "dodavatel_elektriny" = ZSE / SSE / VSE / iný (názov dodávateľa, nie distribučky)

VRÁŤ LEN JSON, žiadny iný text:
{
  "meno": "Meno Priezvisko alebo Firma s.r.o.",
  "miesto_spotreby": { "ulica": "...", "mesto": "...", "psc": "..." },
  "korespondencna_adresa": { "ulica": "...", "mesto": "...", "psc": "..." },
  "eic": "24ZZS...",
  "kwh_rocne": 2057,
  "distribucna_sadzba": "D3 aktiv",
  "distribucna_spolocnost": "ZSD",
  "dodavatel_elektriny": "ZSE",
  "zakaznicke_cislo": "...",
  "cislo_miesta_spotreby": "...",
  "zuctovacie_obdobie_od": "2025-04-01",
  "zuctovacie_obdobie_do": "2026-03-31"
}

TEXT FAKTÚRY:
"""
        prompt += full_text[:15000]  # cap aby nepretiekol token limit

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }
        r = _retry_request(lambda: requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90))
        r.raise_for_status()
        resp = r.json()
        raw_text = _safe_claude_text(resp)

        # Parse JSON (Claude občas vráti markdown ```json...``` — odstrániť)
        import json, re
        raw_text = raw_text.strip()
        m = re.search(r'\{[\s\S]*\}', raw_text)
        if not m:
            return jsonify({"error": "Claude nevrátil JSON", "raw": raw_text[:500]}), 500
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            return jsonify({"error": f"JSON parse: {e}", "raw": m.group(0)[:500]}), 500

        return jsonify({"ok": True, "parsed": parsed, "pages_extracted": pages_count})
    except Exception as e:
        log.exception("parsuj-fakturu-ele failed")
        return jsonify({"error": str(e)}), 500


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
            "POST /webhook/parsuj-leady-supabase",
            "POST /webhook/auto-konfig",
            "POST /webhook/test-rozlozenie",
            "POST /webhook/spracuj-rozlozenie",
            "POST /webhook/generuj-dokumenty",
            "POST /webhook/email-zmluvy",
            "POST /webhook/generuj-realizacne",
            "POST /webhook/prepocet",
            "POST /webhook/generate-pdf",
            "POST /webhook/email-template",
            "POST /webhook/generate-pdf-supabase",
            "POST /webhook/email-template-supabase",
        ],
    })



# ============================================================
# FIELD PROTOKOLY — obhliadkový + preberací + servisný
# ============================================================
from datetime import datetime, timezone
import hashlib

PROTOKOL_HTML = """
<!DOCTYPE html>
<html lang="sk"><head><meta charset="utf-8"/><style>
@page { size: A4; margin: 18mm 15mm; }
body { font-family: 'Helvetica', sans-serif; font-size: 10pt; color: #1e293b; line-height: 1.4; }
.brand { background: #1F4E78; color: white; padding: 14px 18px; margin: -18mm -15mm 14px -15mm; }
.brand h1 { margin: 0; font-size: 18pt; font-weight: bold; }
.brand .sub { font-size: 9pt; opacity: 0.85; margin-top: 3px; }
.meta { display: flex; justify-content: space-between; background: #f1f5f9; padding: 8px 12px; border-radius: 6px; margin-bottom: 14px; font-size: 9pt; }
h2 { background: #fef3c7; border-left: 4px solid #f59e0b; padding: 4px 10px; font-size: 12pt; margin: 18px 0 8px 0; }
h3 { color: #1F4E78; margin: 14px 0 6px 0; font-size: 11pt; }
.row { display: flex; gap: 14px; margin: 6px 0; }
.row .label { font-weight: bold; min-width: 140px; color: #475569; }
.checklist li { list-style: none; margin: 3px 0; padding: 4px 8px; border-radius: 4px; }
.checklist li.done { background: #d1fae5; color: #065f46; }
.checklist li.todo { background: #fef2f2; color: #991b1b; }
.checklist li .icon { font-weight: bold; margin-right: 8px; }
.photos { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; margin-top: 10px; }
.photos img { width: 100%; height: 80px; object-fit: cover; border: 1px solid #e2e8f0; border-radius: 4px; }
.notes { background: #fef9e7; border: 1px solid #fde68a; padding: 10px 12px; border-radius: 6px; font-style: italic; }
.signature { margin-top: 20px; padding-top: 14px; border-top: 2px solid #1F4E78; display: flex; justify-content: space-between; }
.signature .sig { width: 45%; }
.signature img { max-width: 180px; max-height: 80px; }
.footer { position: fixed; bottom: 8mm; left: 15mm; right: 15mm; text-align: center; font-size: 8pt; color: #94a3b8; }
</style></head><body>
<div class="brand">
  <h1>{{ title }}</h1>
  <div class="sub">{{ subtitle }}</div>
</div>

<div class="meta">
  <div><strong>ID:</strong> {{ doc_id }}</div>
  <div><strong>Dátum:</strong> {{ date_str }}</div>
  <div><strong>Technik:</strong> {{ technician }}</div>
</div>

<h3>Zákazník</h3>
<div class="row"><span class="label">Meno / firma:</span> {{ customer_name }}</div>
{% if customer_phone %}<div class="row"><span class="label">Telefón:</span> {{ customer_phone }}</div>{% endif %}
{% if customer_email %}<div class="row"><span class="label">E-mail:</span> {{ customer_email }}</div>{% endif %}
<div class="row"><span class="label">Miesto inštalácie:</span> {{ installation_address }}</div>
{% if gps %}<div class="row"><span class="label">GPS:</span> {{ gps }}</div>{% endif %}

<h2>{{ section_title }}</h2>
{% if checklist %}
<ul class="checklist">
{% for item in checklist %}
  <li class="{{ 'done' if item.done else 'todo' }}"><span class="icon">{{ '✓' if item.done else '✗' }}</span>{{ item.label }}</li>
{% endfor %}
</ul>
{% endif %}

{% if notes %}
<h3>Poznámka technika</h3>
<div class="notes">{{ notes }}</div>
{% endif %}

{% if photo_urls %}
<h3>Fotodokumentácia ({{ photo_urls|length }} fotiek)</h3>
<div class="photos">
{% for url in photo_urls[:9] %}
  <img src="{{ url }}" />
{% endfor %}
</div>
{% if photo_urls|length > 9 %}<p style="font-size:8pt;color:#64748b;margin-top:6px;">+ ďalších {{ photo_urls|length - 9 }} fotiek v digitálnom archíve.</p>{% endif %}
{% endif %}

<div class="signature">
  <div class="sig">
    <strong>Klient — podpis:</strong><br/>
    {% if customer_signature_url %}<img src="{{ customer_signature_url }}" />{% else %}<em style="color:#94a3b8">(nepodpísané)</em>{% endif %}
    <div style="border-top:1px solid #94a3b8;margin-top:6px;padding-top:3px;font-size:9pt;">{{ customer_name }}</div>
  </div>
  <div class="sig">
    <strong>Technik — podpis:</strong><br/>
    {% if technician_signature_url %}<img src="{{ technician_signature_url }}" />{% else %}<em style="color:#94a3b8">(elektronický záznam {{ doc_id }})</em>{% endif %}
    <div style="border-top:1px solid #94a3b8;margin-top:6px;padding-top:3px;font-size:9pt;">{{ technician }}</div>
  </div>
</div>

<div class="footer">
  Energovision s.r.o. · IČO 53 036 280 · www.energovision.sk · {{ doc_id }} · vygenerované {{ generated_at }}
</div>
</body></html>
"""


@app.route("/webhook/generuj-protokol", methods=["POST"])
def generuj_protokol():
    """Vygeneruje PDF protokol (obhliadka / preberací / servis) zo Supabase dát.
    Body: { kind: 'inspection'|'handover'|'service', id: uuid }
    Vráti: { ok, pdf_base64, sha256, filename }
    """
    try:
        data = request.get_json(force=True)
        kind = data.get("kind", "inspection")
        entity_id = data.get("id")
        if not entity_id:
            return jsonify({"error": "missing_id"}), 400

        from jinja2 import Template
        from weasyprint import HTML

        supa_url = os.environ.get("SUPABASE_URL", "")
        supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not supa_url or not supa_key:
            return jsonify({"error": "supabase_env_missing"}), 500
        headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}"}

        ctx = {}
        if kind == "inspection":
            r = requests.get(
                f"{supa_url}/rest/v1/inspections",
                params={"id": f"eq.{entity_id}", "select": "*,customers(first_name,last_name,company_name,phone,email,city,street,postal_code,installation_same_as_billing,installation_street,installation_city,installation_postal_code),technician:users!inspections_technician_id_fkey(full_name)"},
                headers=headers, timeout=20
            )
            ins = (r.json() or [{}])[0]
            if not ins.get("id"):
                return jsonify({"error": "inspection_not_found"}), 404
            cust = ins.get("customers") or {}

            # Foto z documents
            r2 = requests.get(
                f"{supa_url}/rest/v1/documents",
                params={"inspection_id": f"eq.{entity_id}", "kind": "eq.photo", "select": "storage_url"},
                headers=headers, timeout=10
            )
            photo_urls = [d.get("storage_url") for d in (r2.json() or []) if d.get("storage_url")]

            checklist_data = ins.get("checklist_data") or {}
            CL_LABELS = {
                "strecha_typ": "Typ strechy zaznamenaný",
                "strecha_orientacia": "Orientácia a sklon strechy zmerané",
                "strecha_stav": "Stav krytiny OK",
                "tienenie": "Tienenie skontrolované",
                "miesto_panely": "Plocha pre panely vyznačená",
                "miesto_menic": "Miesto pre menič vybraté",
                "miesto_bateria": "Miesto pre batériu",
                "rozvadzac": "Hlavný rozvádzač",
                "elektromer": "Elektromer skontrolovaný",
                "uzemnenie": "Uzemnenie objektu",
                "pristupova_cesta": "Prístupová cesta",
                "lesenie": "Lešenie potrebné?",
                "suhlas_susedy": "Súhlas susedov",
                "klient_pripravoval_dotacie": "Klient pripravuje dotácie",
            }
            checklist = [{"label": CL_LABELS.get(k, k), "done": bool(v)} for k, v in checklist_data.items()]
            checklist += [{"label": CL_LABELS[k], "done": False} for k in CL_LABELS if k not in checklist_data]

            # Adresa inštalácie (3-address logic)
            if cust.get("installation_same_as_billing") is False and cust.get("installation_street"):
                inst_addr = f"{cust.get('installation_street','')}, {cust.get('installation_postal_code','')} {cust.get('installation_city','')}".strip(", ")
            else:
                inst_addr = f"{cust.get('street','')}, {cust.get('postal_code','')} {cust.get('city','')}".strip(", ")

            doc_id = f"OBH-{datetime.now().strftime('%Y-%m')}-{entity_id[:8]}"
            ctx = {
                "title": "Protokol z obhliadky",
                "subtitle": "Záznam o technickej obhliadke pre fotovoltickú elektráreň",
                "section_title": "Kontrolný zoznam obhliadky",
                "doc_id": doc_id,
                "date_str": datetime.fromisoformat((ins.get("scheduled_at") or ins.get("created_at")).replace("Z","+00:00")).strftime("%d.%m.%Y %H:%M") if ins.get("scheduled_at") else datetime.now().strftime("%d.%m.%Y"),
                "technician": (ins.get("technician") or {}).get("full_name") or "—",
                "customer_name": cust.get("company_name") or f"{cust.get('first_name','')} {cust.get('last_name','')}".strip() or "—",
                "customer_phone": cust.get("phone"),
                "customer_email": cust.get("email"),
                "installation_address": inst_addr or "—",
                "gps": f"{ins['gps_lat']:.5f}, {ins['gps_lng']:.5f}" if ins.get("gps_lat") else None,
                "checklist": checklist,
                "notes": ins.get("notes"),
                "photo_urls": photo_urls,
                "customer_signature_url": ins.get("customer_signature_url"),
                "technician_signature_url": None,
                "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            }

        elif kind == "handover":
            r = requests.get(
                f"{supa_url}/rest/v1/handover_protocols",
                params={"id": f"eq.{entity_id}", "select": "*,orders(order_number,ev_id,customers(first_name,last_name,company_name,phone,email,city,street,postal_code))"},
                headers=headers, timeout=20
            )
            h = (r.json() or [{}])[0]
            if not h.get("id"):
                return jsonify({"error": "handover_not_found"}), 404
            order = h.get("orders") or {}
            cust = order.get("customers") or {}

            r2 = requests.get(
                f"{supa_url}/rest/v1/documents",
                params={"order_id": f"eq.{order.get('id','none')}", "kind": "eq.photo", "select": "storage_url"},
                headers=headers, timeout=10
            )
            photo_urls = [d.get("storage_url") for d in (r2.json() or []) if d.get("storage_url")]

            checks = h.get("checklist_data") or {}
            FVE_LABELS = {
                "installation_per_project": "Inštalácia podľa projektu",
                "konstrukcia_ukotvenie": "Nosná konštrukcia — kotvenie OK",
                "panely_aretacia": "Aretácia panelov — moment OK",
                "dc_polarita": "Polarita DC stringov skontrolovaná",
                "dc_voc_isc": "Voc a Isc v limite",
                "ac_pripojenie": "AC pripojenie — fázovanie + svorky",
                "uzemnenie": "Uzemnenie + ekvipotenciálne prepojenie",
                "izolacny_odpor": "Izolačný odpor DC ≥ 1 MΩ",
                "rcd_test": "RCD test 30 mA OK",
                "spd_typ2": "SPD typ 2 (DC + AC)",
                "spustenie_menica": "Spustenie meniča, firmware",
                "monitoring_app": "Monitoring aktívny",
                "stitky_a_oznacenie": "Bezpečnostné štítky",
                "ziadost_o_pripojenie": "Žiadosť o pripojenie",
                "revizna_sprava": "Revízna správa",
                "zaskolenie_klienta": "Zaškolenie zákazníka",
                "navod_odovzdany": "Návod odovzdaný",
                "zarucny_list": "Záručné listy",
                "pracovisko_uprat": "Pracovisko upratané",
            }
            checklist = [{"label": FVE_LABELS.get(k, k), "done": bool(v)} for k, v in checks.items()]
            checklist += [{"label": FVE_LABELS[k], "done": False} for k in FVE_LABELS if k not in checks]

            doc_id = h.get("protocol_number") or f"PROT-{entity_id[:8]}"
            ctx = {
                "title": "Preberací protokol",
                "subtitle": f"Záznam o ukončení inštalácie · {order.get('ev_id') or order.get('order_number','')}",
                "section_title": "Kontrolný zoznam pri odovzdaní",
                "doc_id": doc_id,
                "date_str": datetime.fromisoformat(h.get("customer_signed_at","").replace("Z","+00:00")).strftime("%d.%m.%Y %H:%M") if h.get("customer_signed_at") else datetime.now().strftime("%d.%m.%Y"),
                "technician": h.get("technician_name") or "—",
                "customer_name": cust.get("company_name") or f"{cust.get('first_name','')} {cust.get('last_name','')}".strip() or "—",
                "customer_phone": cust.get("phone"),
                "customer_email": cust.get("email"),
                "installation_address": f"{cust.get('street','')}, {cust.get('postal_code','')} {cust.get('city','')}".strip(", "),
                "gps": h.get("signed_gps"),
                "checklist": checklist,
                "notes": h.get("notes"),
                "photo_urls": photo_urls,
                "customer_signature_url": h.get("customer_signature_url"),
                "technician_signature_url": h.get("technician_signature_url"),
                "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            }

        elif kind == "service":
            r = requests.get(
                f"{supa_url}/rest/v1/service_tickets",
                params={"id": f"eq.{entity_id}", "select": "*,customers(first_name,last_name,company_name,phone,email,city,street,postal_code),assignee:users!service_tickets_assignee_id_fkey(full_name)"},
                headers=headers, timeout=20
            )
            t = (r.json() or [{}])[0]
            if not t.get("id"):
                return jsonify({"error": "ticket_not_found"}), 404
            cust = t.get("customers") or {}

            doc_id = t.get("ticket_number") or f"SVC-{entity_id[:8]}"
            ctx = {
                "title": "Servisný protokol",
                "subtitle": f"Záznam o servisnom zásahu · {doc_id}",
                "section_title": "Riešenie problému",
                "doc_id": doc_id,
                "date_str": datetime.now().strftime("%d.%m.%Y %H:%M"),
                "technician": (t.get("assignee") or {}).get("full_name") or "—",
                "customer_name": cust.get("company_name") or f"{cust.get('first_name','')} {cust.get('last_name','')}".strip() or "—",
                "customer_phone": cust.get("phone"),
                "customer_email": cust.get("email"),
                "installation_address": f"{cust.get('street','')}, {cust.get('postal_code','')} {cust.get('city','')}".strip(", "),
                "gps": None,
                "checklist": [
                    {"label": f"Popis problému: {t.get('title','—')}", "done": True},
                    {"label": f"Priorita: {t.get('priority','normal')}", "done": True},
                    {"label": "Servisný zásah vykonaný", "done": t.get("status") == "resolved"},
                ],
                "notes": t.get("resolution") or t.get("description"),
                "photo_urls": [],
                "customer_signature_url": t.get("customer_signature_url"),
                "technician_signature_url": t.get("technician_signature_url"),
                "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            }
        else:
            return jsonify({"error": f"unknown_kind: {kind}"}), 400

        html = Template(PROTOKOL_HTML).render(**ctx)
        pdf_bytes = HTML(string=html).write_pdf()
        import base64
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        sha = hashlib.sha256(pdf_bytes).hexdigest()
        filename = f"{ctx['doc_id'].replace('/','-')}.pdf"
        return jsonify({"ok": True, "pdf_base64": b64, "sha256": sha, "filename": filename, "doc_id": ctx["doc_id"]})
    except Exception as e:
        log.exception("generuj-protokol failed")
        return jsonify({"error": str(e)}), 500



# ============================================================
# B2B DOCUMENT GENERATORS (G2 — replace Make scenár 8299009 docx-templater modules)
# ============================================================

@app.route("/webhook/generate-b2b-zod", methods=["POST"])
@require_secret
def generate_b2b_zod():
    """
    Generuje Zmluvu o dielo B2B z templates_b2b/Nova_klasicka_ZOD.docx.
    Vstup: { "project_id": "uuid" }
    Výstup: { "success": True, "filename": "...", "data": "base64", "summary": {...} }
    """
    body = request.get_json(force=True, silent=True) or {}
    project_id = body.get("project_id")
    if not project_id:
        return jsonify({"error": "missing project_id"}), 400

    SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not SUPABASE_KEY:
        return jsonify({"error": "SUPABASE_SERVICE_ROLE_KEY not set"}), 500
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

    # Fetch project + customer
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/projects",
        headers=headers,
        params={"select": "*,customers(*)", "id": f"eq.{project_id}"},
        timeout=30,
    )
    if not r.ok:
        return jsonify({"error": f"supabase fetch projects: {r.status_code}", "body": r.text}), 500
    rows = r.json()
    if not rows:
        return jsonify({"error": "project_not_found"}), 404
    project = rows[0]
    customer = project.get("customers") or {}

    # Fetch milestones (kvôli dátumom FA1/FA2/FA3 — ZoD na ne odkazuje)
    mr = requests.get(
        f"{SUPABASE_URL}/rest/v1/project_milestones",
        headers=headers,
        params={"select": "*", "project_id": f"eq.{project_id}", "order": "fa_no.asc"},
        timeout=15,
    )
    milestones = mr.json() if mr.ok else []

    try:
        from generuj_b2b import generuj_zod
        import base64 as _b64

        with tempfile.TemporaryDirectory() as tmpdir:
            zod_path = generuj_zod(
                project=project,
                customer=customer,
                milestones=milestones,
                out_dir=tmpdir,
            )
            with open(zod_path, "rb") as f:
                raw = f.read()
            filename = Path(zod_path).name
            log.info("[generate-b2b-zod] success project=%s file=%s (%d B)", project_id, filename, len(raw))
            return jsonify({
                "success": True,
                "filename": filename,
                "data": _b64.b64encode(raw).decode("ascii"),
                "summary": {
                    "project_id": project_id,
                    "project_name": project.get("name"),
                    "customer_name": customer.get("company_name"),
                    "contract_value_no_vat": project.get("contract_value_no_vat"),
                },
            })
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": f"template missing: {e}"}), 500
    except Exception as e:
        log.exception("[generate-b2b-zod] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/webhook/generate-b2b-faktura", methods=["POST"])
@require_secret
def generate_b2b_faktura():
    """
    Generuje Word faktúru pre konkrétny milestone (FA1/FA2/FA3). Lukáš ju potom
    manuálne nahodí do Flowii ako originál.

    Vstup: { "project_id": "uuid", "fa_no": 1, "faktura_cislo": "2026/0042", "variabilny_symbol": "20260042" }
    Výstup: { "success": True, "filename": "...", "data": "base64", "summary": {...} }
    """
    body = request.get_json(force=True, silent=True) or {}
    project_id = body.get("project_id")
    fa_no = body.get("fa_no")
    faktura_cislo = body.get("faktura_cislo")
    variabilny_symbol = body.get("variabilny_symbol") or (faktura_cislo or "").replace("/", "").replace("-", "")

    if not project_id:
        return jsonify({"error": "missing project_id"}), 400
    if not fa_no:
        return jsonify({"error": "missing fa_no (1/2/3)"}), 400
    if not faktura_cislo:
        return jsonify({"error": "missing faktura_cislo (napr. 2026/0042)"}), 400

    SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not SUPABASE_KEY:
        return jsonify({"error": "SUPABASE_SERVICE_ROLE_KEY not set"}), 500
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

    # Fetch project + customer
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/projects",
        headers=headers,
        params={"select": "*,customers(*)", "id": f"eq.{project_id}"},
        timeout=30,
    )
    if not r.ok:
        return jsonify({"error": f"supabase fetch: {r.status_code}", "body": r.text}), 500
    rows = r.json()
    if not rows:
        return jsonify({"error": "project_not_found"}), 404
    project = rows[0]
    customer = project.get("customers") or {}

    # Fetch konkrétny milestone
    mr = requests.get(
        f"{SUPABASE_URL}/rest/v1/project_milestones",
        headers=headers,
        params={"select": "*", "project_id": f"eq.{project_id}", "fa_no": f"eq.{fa_no}"},
        timeout=15,
    )
    if not mr.ok:
        return jsonify({"error": f"milestone fetch: {mr.status_code}", "body": mr.text}), 500
    milestones = mr.json()
    if not milestones:
        return jsonify({"error": f"milestone fa_no={fa_no} not found"}), 404
    milestone = milestones[0]

    try:
        from generuj_b2b import generuj_faktura
        import base64 as _b64

        with tempfile.TemporaryDirectory() as tmpdir:
            fa_path = generuj_faktura(
                project=project,
                customer=customer,
                milestone=milestone,
                faktura_cislo=faktura_cislo,
                variabilny_symbol=variabilny_symbol,
                out_dir=tmpdir,
            )
            with open(fa_path, "rb") as f:
                raw = f.read()
            filename = Path(fa_path).name
            log.info("[generate-b2b-faktura] success project=%s fa%s file=%s (%d B)",
                     project_id, fa_no, filename, len(raw))
            return jsonify({
                "success": True,
                "filename": filename,
                "data": _b64.b64encode(raw).decode("ascii"),
                "summary": {
                    "project_id": project_id,
                    "fa_no": fa_no,
                    "faktura_cislo": faktura_cislo,
                    "payment_amount": milestone.get("payment_amount"),
                    "payment_pct": milestone.get("payment_pct"),
                    "due_date": milestone.get("due_date"),
                },
            })
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": f"template missing: {e}"}), 500
    except Exception as e:
        log.exception("[generate-b2b-faktura] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# B2B NOTION → SUPABASE MIGRATION (N6 — autonomous from Chrome)
# Trigger: Claude (Cowork mode) cez Chrome browser fetch
# ============================================================
import migrate_notion_b2b as _mnb


@app.route("/webhook/migrate-notion-build-mapping", methods=["POST", "GET"])
@require_secret
def migrate_notion_build_mapping():
    """Vyrobí mapping Supabase project -> Notion page. Vráti JSON pre orchestráciu."""
    try:
        result = _mnb.build_mapping()
        return jsonify({"success": True, **result})
    except Exception as e:
        log.exception("[migrate-notion-build-mapping] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/webhook/migrate-notion-project", methods=["POST"])
@require_secret
def migrate_notion_project():
    """Migruje 1 projekt — 9 file properties z Notion -> Supabase Storage + project_documents."""
    body = request.get_json(silent=True) or {}
    notion_page_id = body.get("notion_page_id")
    supabase_project_id = body.get("supabase_project_id")
    ds = body.get("ds")
    dry_run = bool(body.get("dry_run", False))
    if not notion_page_id or not supabase_project_id:
        return jsonify({"success": False, "error": "missing notion_page_id or supabase_project_id"}), 400
    try:
        result = _mnb.migrate_one(notion_page_id, supabase_project_id, ds, dry_run)
        return jsonify({"success": result.get("ok", False), **result})
    except Exception as e:
        log.exception("[migrate-notion-project] zlyhalo")
        return jsonify({"success": False, "error": str(e)}), 500



# ============================================================
# AI DOCUMENT CLASSIFIER — analyzuje uploadnutý PDF, určí kind/folder/state
# Trigger: CRM Server Action po file upload (drag-drop v hero/per-task)
# ============================================================
B2B_DOC_KINDS = {
    "zod": ("02_Administrativa/03_Zmluva_o_dielo/02_Zmluva_s_IFT", "signed"),
    "splnomocnenie": ("02_Administrativa/02_Dokumenty_01", "signed"),
    "dotaznik": ("02_Administrativa/02_Dokumenty_01", "draft"),
    "lv": ("01_Podklady/03_Podklady_od_zakaznika", "draft"),
    "zop_signed": ("02_Administrativa/05_DIS-DS", "signed"),
    "zopad_signed": ("02_Administrativa/05_DIS-DS", "signed"),
    "stanovisko_zop": ("02_Administrativa/05_DIS-DS", "approved"),
    "stanovisko_rp": ("02_Administrativa/05_DIS-DS", "approved"),
    "opaos": ("04_Realizacia/05_Revizie", "approved"),
    "faktura": ("02_Administrativa/04_Fakturacia", "issued"),
    "preberaci_protokol": ("04_Realizacia/03_Protokoly", "signed"),
    "dsv": ("03_Projekcia/02_DSV", "draft"),
    "mpp": ("03_Projekcia/03_MPP", "draft"),
    "pbs": ("03_Projekcia/04_PBS", "draft"),
    "statika": ("03_Projekcia/05_Statika", "draft"),
    "iny": ("01_Podklady", "draft"),
}

DS_FOLDER_MAP = {"SSD": "05_DIS-SSD", "VSD": "05_DIS-VSD", "ZSDIS": "05_DIS-ZSDIS"}


@app.route("/webhook/classify-document", methods=["POST"])
@require_secret
def classify_document():
    """Klasifikuje uploadnutý dokument cez Claude API.

    Body: { project_id, file_url (storage path), filename, project_ds, project_code }
    Vráti: { kind, folder, state, confidence, suggested_task_no, reasoning }
    """
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id")
    file_url = body.get("file_url")
    filename = body.get("filename", "")
    project_ds = body.get("project_ds", "")
    project_code = body.get("project_code", "")
    if not project_id or not file_url:
        return jsonify({"error": "missing project_id or file_url"}), 400

    # 1) Stiahni súbor zo Supabase Storage
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not SUPABASE_KEY:
        return jsonify({"error": "SUPABASE_SERVICE_ROLE_KEY not set"}), 500

    from urllib.parse import quote
    storage_url = f"{SUPABASE_URL}/storage/v1/object/b2b-documents/{quote(file_url, safe='/')}"
    try:
        r = requests.get(storage_url, headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}, timeout=60)
        if r.status_code != 200:
            return jsonify({"error": f"Storage fetch {r.status_code}", "detail": r.text[:200]}), 500
        file_bytes = r.content
    except Exception as e:
        return jsonify({"error": f"Storage fetch exception: {e}"}), 500

    # 2) Extrahuj text z PDF (max 3 strany)
    text_excerpt = ""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("pdf",):
        try:
            import fitz  # pymupdf
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            for i in range(min(3, doc.page_count)):
                text_excerpt += doc[i].get_text() + "\n"
            doc.close()
            text_excerpt = text_excerpt[:6000]
        except Exception as e:
            text_excerpt = f"[PDF extract failed: {e}]"
    elif ext in ("docx", "doc"):
        try:
            from io import BytesIO
            import docx
            d = docx.Document(BytesIO(file_bytes))
            text_excerpt = "\n".join([p.text for p in d.paragraphs])[:6000]
        except Exception as e:
            text_excerpt = f"[DOCX extract failed: {e}]"
    elif ext in ("jpg", "jpeg", "png"):
        text_excerpt = "[Obrazový súbor — klasifikuj z filename]"
    else:
        text_excerpt = "[Neznámy formát]"

    # 3) Claude prompt
    prompt = f"""Klasifikuj tento dokument pre Energovision B2B FVE projekt.

KONTEXT PROJEKTU:
- Project code: {project_code}
- Distribučná spoločnosť: {project_ds}
- Filename: {filename}

OBSAH (prvé strany):
{text_excerpt}

KATEGÓRIE (vráť len jeden):
- zod = Zmluva o dielo (Energovision_ZoD.pdf, podpisaná zmluva s klientom)
- splnomocnenie = Splnomocnenie/Plnomocenstvo na úkony s DS
- dotaznik = Dotazník k pripojeniu lokálneho zdroja FVE
- lv = List vlastníctva (z katastra)
- zop_signed = Podpísaná Zmluva o pripojení (od DS)
- zopad_signed = Zmluva o prístupe a distribúcii (od DS)
- stanovisko_zop = Stanovisko/Vyjadrenie ku žiadosti o pripojenie (od DS)
- stanovisko_rp = Stanovisko/Vyjadrenie k technickej dokumentácii / k PD (od DS)
- opaos = OPaOS revízna správa (FVZ revízia)
- faktura = Faktúra (Energovision vystavená klientovi)
- preberaci_protokol = Preberací protokol diela
- dsv = DSV / dielenská PD
- mpp = MPP (montážno-prevádzkový predpis)
- pbs = PBS (požiarno-bezpečnostné stanovisko)
- statika = Statický posudok
- iny = nič z vyššie uvedeného

VRÁŤ LEN JSON (žiadny iný text):
{{
  "kind": "stanovisko_rp",
  "confidence": 0.95,
  "reasoning": "Krátko prečo (1 veta)",
  "document_number": "číslo dokumentu ak je v texte (napr. 202511-C06-0049-1 alebo 2026/0042), inak null",
  "is_signed": true,
  "is_approved": true
}}
"""

    headers = {
        "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        rr = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=60)
        rr.raise_for_status()
        resp = rr.json()
        raw_text = ""
        for block in resp.get("content", []):
            if block.get("type") == "text":
                raw_text += block.get("text", "")
    except Exception as e:
        return jsonify({"error": f"Claude API: {e}"}), 500

    # 4) Parse JSON response
    import json, re
    m = re.search(r"\{[\s\S]*\}", raw_text)
    if not m:
        return jsonify({"error": "Claude did not return JSON", "raw": raw_text[:300]}), 500
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:
        return jsonify({"error": f"JSON parse: {e}", "raw": raw_text[:300]}), 500

    kind = parsed.get("kind", "iny")
    if kind not in B2B_DOC_KINDS:
        kind = "iny"
    folder, default_state = B2B_DOC_KINDS[kind]

    # DIS folder resolution
    if folder == "02_Administrativa/05_DIS-DS":
        ds_folder = DS_FOLDER_MAP.get(project_ds, "05_DIS-DS")
        folder = f"02_Administrativa/{ds_folder}"

    state = default_state
    if parsed.get("is_approved"):
        state = "approved"
    elif parsed.get("is_signed"):
        state = "signed"

    return jsonify({
        "ok": True,
        "kind": kind,
        "folder": folder,
        "state": state,
        "confidence": float(parsed.get("confidence", 0.0)),
        "reasoning": parsed.get("reasoning", ""),
        "document_number": parsed.get("document_number"),
        "filename": filename,
    })



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

@app.route("/webhook/transcribe-audio", methods=["POST"])
@require_secret
def transcribe_audio():
    """Stub — TODO napojiť na Whisper API alebo Anthropic audio.
    Zatiaľ vráti error a user musí použiť 'paste mode' v MeetingRecorder.
    """
    body = request.get_json(silent=True) or {}
    audio_url = body.get("audio_url")
    if not audio_url:
        return jsonify({"ok": False, "error": "audio_url missing"}), 400
    return jsonify({
        "ok": False,
        "error": "transcribe not implemented yet — use paste mode v CRM s vloženým prepisom z Granola/Otter/Whisper",
        "audio_url": audio_url,
    }), 501

@app.route("/webhook/import-notion-content", methods=["POST"])
@require_secret
def import_notion_content():
    """Import full Notion page content (blocks + comments) → projects.notion_mirror_md.
    
    Body:
      { "all": true, "limit": 5 }     # bulk for first 5 projects
      { "all": true }                  # bulk for ALL projects
      { "notion_page_id": "X", "supabase_project_id": "Y" }   # single
    """
    body = request.get_json(silent=True) or {}
    try:
        if body.get("all"):
            limit = int(body.get("limit", 0)) or None
            result = _mnb.import_all_content(limit=limit)
            return jsonify(result)
        else:
            np = body.get("notion_page_id")
            sp = body.get("supabase_project_id")
            if not np or not sp:
                return jsonify({"ok": False, "error": "missing notion_page_id or supabase_project_id"}), 400
            result = _mnb.import_one_content(np, sp)
            return jsonify(result)
    except Exception as e:
        log.exception("[import-notion-content] zlyhalo")
        return jsonify({"ok": False, "error": str(e)}), 500



# ============================================================
# HUAWEI / SPOT 3-state — Pilier 4
# ============================================================
try:
    import huawei_spot as _hs
except Exception as _e:
    log.warning("huawei_spot module not loaded: %s", _e)
    _hs = None


def _hs_auth_ok(req) -> bool:
    secret = req.headers.get("X-Webhook-Secret") or req.args.get("secret")
    expected = os.environ.get("WEBHOOK_SECRET")
    return bool(expected) and secret == expected


@app.route("/webhook/okte-ingest", methods=["POST", "GET"])
def webhook_okte_ingest():
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _hs is None:
        return jsonify({"ok": False, "error": "huawei_spot module not available"}), 500
    body = request.get_json(silent=True) or {}
    backfill = int(body.get("backfill_days", 0) or request.args.get("backfill_days", 0))
    target_day_str = body.get("day") or request.args.get("day")
    target_day = None
    if target_day_str:
        from datetime import date as _date
        try:
            target_day = _date.fromisoformat(target_day_str)
        except Exception:
            return jsonify({"ok": False, "error": "invalid day format YYYY-MM-DD"}), 400
    try:
        result = _hs.okte_ingest(target_day=target_day, backfill_days=backfill)
        return jsonify(result)
    except Exception as e:
        log.exception("[okte-ingest] failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook/spot-reactor", methods=["POST", "GET"])
def webhook_spot_reactor():
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _hs is None:
        return jsonify({"ok": False, "error": "huawei_spot module not available"}), 500
    body = request.get_json(silent=True) or {}
    dry_override = body.get("dry_run_override")
    if dry_override is None and "dry_run" in request.args:
        dry_override = request.args.get("dry_run") in ("1", "true", "yes")
    try:
        result = _hs.spot_reactor(dry_run_override=dry_override)
        return jsonify(result)
    except Exception as e:
        log.exception("[spot-reactor] failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook/spot-pause", methods=["POST"])
def webhook_spot_pause():
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _hs is None:
        return jsonify({"ok": False, "error": "huawei_spot module not available"}), 500
    body = request.get_json(silent=True) or {}
    reason = body.get("reason", "API call")
    return jsonify(_hs.global_pause(reason=reason))


@app.route("/webhook/huawei-test", methods=["GET"])
def webhook_huawei_test():
    """Smoke test: login + check token."""
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _hs is None:
        return jsonify({"ok": False, "error": "huawei_spot module not available"}), 500
    token = _hs.huawei_login(force=True)
    return jsonify({"ok": bool(token), "token_present": bool(token), "token_preview": (token[:16] + "...") if token else None})



@app.route("/webhook/spot-manual-command", methods=["POST"])
def webhook_spot_manual_command():
    """
    Manuálny override z dashboardu:
    POST { "site_id": "...", "target_state": "NORMAL|ZERO_EXPORT_ONLY|FULL_SHUTDOWN", "issued_by": "...", "reason": "..." }
    """
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _hs is None:
        return jsonify({"ok": False, "error": "huawei_spot module not available"}), 500
    body = request.get_json(silent=True) or {}
    site_id = body.get("site_id")
    target_state = body.get("target_state")
    issued_by = body.get("issued_by", "manual")
    reason = body.get("reason", "Manuálny override z dashboardu")
    if not site_id or not target_state:
        return jsonify({"ok": False, "error": "missing site_id or target_state"}), 400
    try:
        return jsonify(_hs.manual_force_state(site_id, target_state, issued_by, reason))
    except Exception as e:
        log.exception("[spot-manual-command] failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook/huawei-sync-stations", methods=["POST", "GET"])
def webhook_huawei_sync_stations():
    """Pull station list from Huawei → upsert do inverter_sites. Pre prípad keď kolega nainštaluje novú FVE."""
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _hs is None:
        return jsonify({"ok": False, "error": "huawei_spot module not available"}), 500
    try:
        return jsonify(_hs.sync_huawei_stations())
    except Exception as e:
        log.exception("[huawei-sync-stations] failed")
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# ANALYZA OM module — Pilier FVE+BESS posudok
# ============================================================
try:
    from analyza_om import engine as _aom
except Exception as _e:
    log.warning("analyza_om not loaded: %s", _e)
    _aom = None


@app.route("/webhook/aom-run-pipeline", methods=["POST"])
def webhook_aom_run_pipeline():
    """Spustí full pipeline (parse → sim → econ) pre danú analyza_id."""
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _aom is None:
        return jsonify({"ok": False, "error": "analyza_om module not loaded"}), 500
    body = request.get_json(silent=True) or {}
    analyza_id = body.get("analyza_id")
    if not analyza_id:
        return jsonify({"ok": False, "error": "missing analyza_id"}), 400
    try:
        result = _aom.run_full_pipeline(analyza_id)
        return jsonify(result)
    except Exception as e:
        log.exception("[aom-run-pipeline] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/aom-parse-only", methods=["POST"])
def webhook_aom_parse_only():
    """Iba parse consumption files — pre Krok 2.5 wizard preview."""
    if not _hs_auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401
    if _aom is None:
        return jsonify({"ok": False, "error": "analyza_om module not loaded"}), 500
    body = request.get_json(silent=True) or {}
    analyza_id = body.get("analyza_id")
    file_paths = body.get("file_paths") or []
    if not analyza_id or not file_paths:
        return jsonify({"status": "error", "error": "missing analyza_id or file_paths"}), 400
    try:
        return jsonify(_aom.parse_consumption(analyza_id, file_paths, body.get("options")))
    except Exception as e:
        log.exception("[aom-parse-only] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


# ===================================================================
# AUTOMATION TOP-5 — quick win endpointy
# ===================================================================
import automation as _automation


# Lazy supabase client pre nové AI moduly (automation/team_chat/strategic)
_sb_client = None
def _sb():
    global _sb_client
    if _sb_client is None:
        from supabase import create_client
        import os as _os
        _sb_client = create_client(
            _os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co"),
            _os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
    return _sb_client



@app.route("/webhook/ai-next-actions", methods=["POST"])
def webhook_ai_next_actions():
    """Vygeneruje 3 next-action návrhy pre lead/project. Uloží do ai_next_actions."""
    body = request.get_json(force=True) or {}
    target_type = body.get("target_type")
    target_id = body.get("target_id")
    if not target_type or not target_id:
        return jsonify({"status": "error", "error": "target_type a target_id sú povinné"}), 400
    try:
        actions = _automation.suggest_next_actions(_sb(), target_type, target_id, save=True)
        return jsonify({"status": "ok", "actions": actions})
    except Exception as e:
        log.exception("[ai-next-actions] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/draft-email", methods=["POST"])
def webhook_draft_email():
    """Vygeneruje AI draft emailu pre lead/project/customer. Uloží do email_drafts."""
    body = request.get_json(force=True) or {}
    try:
        result = _automation.draft_email(
            _sb(),
            body.get("target_type", "lead"),
            body["target_id"],
            body.get("purpose", "kontakt s klientom"),
            body.get("employee_name", "Dominik Galaba"),
            body.get("incoming_email"),
        )
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("[draft-email] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/order-external", methods=["POST"])
def webhook_order_external():
    """Vytvorí draft objednávky externistu (PBS/statika/geodet/...)."""
    body = request.get_json(force=True) or {}
    project_id = body.get("project_id")
    service_type = body.get("service_type")
    if not project_id or not service_type:
        return jsonify({"status": "error", "error": "project_id a service_type sú povinné"}), 400
    try:
        result = _automation.order_external_service(
            _sb(), project_id, service_type, body.get("provider_id")
        )
        if "error" in result:
            return jsonify({"status": "error", **result}), 400
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("[order-external] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/doc-prefill", methods=["POST"])
def webhook_doc_prefill():
    """AI klasifikuje + extrahuje dáta z dokumentu, predvyplní polia v DB."""
    body = request.get_json(force=True) or {}
    target_type = body.get("target_type")
    target_id = body.get("target_id")
    text = body.get("text_content")
    if not target_type or not target_id or not text:
        return jsonify({"status": "error", "error": "target_type, target_id, text_content sú povinné"}), 400
    try:
        result = _automation.classify_and_prefill(_sb(), target_type, target_id, text)
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("[doc-prefill] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/doc-package", methods=["POST"])
def webhook_doc_package():
    """1-klik: AI draftne email pre balík dokumentov. (PDF generuje existujúci endpoint.)"""
    body = request.get_json(force=True) or {}
    lead_id = body.get("lead_id")
    if not lead_id:
        return jsonify({"status": "error", "error": "lead_id povinné"}), 400
    try:
        result = _automation.generate_doc_package(_sb(), lead_id, body.get("employee_name", "Dominik Galaba"))
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("[doc-package] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


# ===================================================================
# AI Strategic Manager — manažérsky brief na dashboard
# ===================================================================
import strategic_agent as _strategic


@app.route("/webhook/ai-strategic-brief", methods=["POST", "GET"])
def webhook_strategic_brief():
    """Vygeneruje strategický brief a uloží do ai_strategic_briefs."""
    body = request.get_json(silent=True) or {}
    scope = body.get("scope") or request.args.get("scope") or "daily"
    try:
        brief = _strategic.generate_strategic_brief(_sb(), scope=scope, save=True)
        return jsonify({"status": "ok", "brief": brief})
    except Exception as e:
        log.exception("[strategic-brief] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


# ===================================================================
# AI Kolega Eva + Team Chat
# ===================================================================
import team_chat as _team_chat


@app.route("/webhook/team-chat-proactive", methods=["POST", "GET"])
def webhook_team_chat_proactive():
    """Cron: AI Kolega prejde stav firmy a napíše 1-3 proaktívne správy + predpripraví drafty."""
    try:
        result = _team_chat.proactive_pass(_sb())
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("[team-chat-proactive] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/team-chat-reply", methods=["POST"])
def webhook_team_chat_reply():
    """User píše do chat-u → AI reaguje (s context-om)."""
    body = request.get_json(force=True) or {}
    msg = (body.get("message") or "").strip()
    if not msg:
        return jsonify({"status": "error", "error": "Prázdna správa"}), 400
    try:
        result = _team_chat.handle_reply(
            _sb(), msg, body.get("user_id"), body.get("user_name"),
            skip_insert_user=bool(body.get("skip_insert_user", False))
        )
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("[team-chat-reply] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


# ===================================================================
# EVA COWORK v2 — Memory + Proactive autonomous
# ===================================================================
import eva_memory as _eva_memory
import eva_proactive as _eva_proactive


@app.route("/webhook/eva-memory-extract", methods=["POST"])
def webhook_eva_memory_extract():
    """Po user→AI exchange, vyextrahuj memories. Volá sa po každom Eva reply."""
    body = request.get_json(force=True) or {}
    try:
        saved = _eva_memory.extract_memories(
            _sb(),
            user_msg=body.get("user_msg", ""),
            ai_reply=body.get("ai_reply", ""),
            user_id=body.get("user_id"),
            user_name=body.get("user_name"),
            source_msg_id=body.get("source_msg_id"),
        )
        return jsonify({"status": "ok", "saved_count": len(saved), "memories": saved})
    except Exception as e:
        log.exception("[eva-memory-extract] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/eva-memory-search", methods=["POST"])
def webhook_eva_memory_search():
    """Vector search relevant memories pre query."""
    body = request.get_json(force=True) or {}
    try:
        results = _eva_memory.search_relevant(
            _sb(),
            query=body.get("query", ""),
            k=int(body.get("k", 20)),
            filter_role=body.get("filter_role"),
            filter_topic=body.get("filter_topic"),
            threshold=float(body.get("threshold", 0.4)),
        )
        return jsonify({"status": "ok", "memories": results})
    except Exception as e:
        log.exception("[eva-memory-search] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/webhook/eva-proactive-hourly", methods=["POST", "GET"])
def webhook_eva_proactive_hourly():
    """Hourly autonomous pass — Eva sama sa pozrie čo treba."""
    try:
        result = _eva_proactive.hourly_autonomous_pass(_sb())
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("[eva-proactive-hourly] failed")
        return jsonify({"status": "error", "error": str(e)}), 500


# ============================================================
# RAYNET DISCOVERY — pull cenoviek/firiem/produktov pre offline analýzu
# ============================================================
import raynet_discovery as _raynet

@app.route("/webhook/raynet-whoami", methods=["GET", "POST"])
def webhook_raynet_whoami():
    """Quick auth check — overí že credentials fungujú. No-secret (read-only).
    Body môže obsahovať: {raynet_user, raynet_key, raynet_instance}."""
    body = request.get_json(silent=True) or {}
    if body.get("raynet_user") and body.get("raynet_key"):
        _raynet.set_creds(body["raynet_user"], body["raynet_key"], body.get("raynet_instance", "energovision"))
    try:
        return jsonify({"ok": True, "whoami": _raynet.whoami()})
    except Exception as e:
        log.exception("[raynet-whoami] failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook/raynet-discover", methods=["POST"])
def webhook_raynet_discover():
    """Stiahne všetky quotations/business_cases/products/companies do Supabase staging.
    Telo: {only?, raynet_user?, raynet_key?, raynet_instance?}. No-secret (write only do staging)."""
    body = request.get_json(silent=True) or {}
    if body.get("raynet_user") and body.get("raynet_key"):
        _raynet.set_creds(body["raynet_user"], body["raynet_key"], body.get("raynet_instance", "energovision"))
    only = body.get("only")
    try:
        sb = _sb()
        if only == "products":
            out = {"products": _raynet.discover_products(sb)}
        elif only == "companies":
            out = {"companies": _raynet.discover_companies(sb)}
        elif only == "business_cases":
            out = {"business_cases": _raynet.discover_business_cases(sb)}
        elif only == "quotations":
            out = {"quotations": _raynet.discover_quotations(sb)}
        else:
            out = _raynet.discover_all(sb)
        return jsonify({"ok": True, **out})
    except Exception as e:
        log.exception("[raynet-discover] failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/webhook/raynet-fetch-items", methods=["POST"])
def webhook_raynet_fetch_items():
    """Batch fetch items pre ponuky bez items. Volaj viackrát kým ostávajú."""
    body = request.get_json(silent=True) or {}
    if body.get("raynet_user") and body.get("raynet_key"):
        _raynet.set_creds(body["raynet_user"], body["raynet_key"], body.get("raynet_instance", "energovision"))
    try:
        max_offers = int(body.get("max_offers", 100))
        out = _raynet.fetch_offer_detail_items(_sb(), max_offers=max_offers)
        return jsonify({"ok": True, **out})
    except Exception as e:
        log.exception("[raynet-fetch-items] failed")
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


# ============================================================
# B2B KALKULAČKA — generovanie BOM + uloženie cenovky
# ============================================================
import b2b_calculator as _b2b_calc

@app.route("/webhook/b2b-calc-preview", methods=["POST"])
def webhook_b2b_calc_preview():
    """Vygeneruje BOM bez uloženia (live preview v UI)."""
    body = request.get_json(silent=True) or {}
    try:
        result = _b2b_calc.calculate_bom(_sb(), body)
        return jsonify({"ok": True, **result})
    except Exception as e:
        log.exception("[b2b-calc-preview] failed")
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


@app.route("/webhook/b2b-calc-save", methods=["POST"])
def webhook_b2b_calc_save():
    """Vygeneruje BOM a uloží do b2b_quotes + b2b_quote_items."""
    body = request.get_json(silent=True) or {}
    try:
        config = body.get("config", {})
        result = _b2b_calc.calculate_bom(_sb(), config)
        quote = _b2b_calc.save_quote(
            _sb(), config, result["items"], result["totals"],
            customer_id=body.get("customer_id"),
            lead_id=body.get("lead_id"),
            project_id=body.get("project_id"),
            user_id=body.get("user_id"),
        )
        return jsonify({"ok": True, "quote": quote, **result})
    except Exception as e:
        log.exception("[b2b-calc-save] failed")
        return jsonify({"ok": False, "error": str(e)[:500]}), 500
