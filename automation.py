"""
automation.py — TOP 5 quick-win automatizácie pre CRM
=====================================================

Funkcie:
- suggest_next_actions(target_type, target_id)
- draft_email(target_type, target_id, purpose)
- order_external_service(project_id, service_type)
- classify_and_prefill(file_url, target_type, target_id, text)
- generate_doc_package(lead_id) → email draft (PDF rieši existujúci endpoint)
"""

from __future__ import annotations
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import anthropic
from supabase import Client

log = logging.getLogger(__name__)

_anthropic_client: Optional[anthropic.Anthropic] = None
MODEL = "claude-sonnet-4-5"


def get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


# =============================================
# Context loaders
# =============================================

def build_context_lead(supabase: Client, lead_id: str) -> Dict[str, Any]:
    lead = supabase.table("leads").select("*, customers(*), activities(action, action_data, created_at)") \
        .eq("id", lead_id).limit(1).execute()
    if not lead.data:
        return {}
    L = lead.data[0]
    L["activities"] = sorted(L.get("activities") or [], key=lambda a: a.get("created_at", ""), reverse=True)[:10]
    try:
        bundles = supabase.table("bundles").select("id,status,total_with_vat,accepted_at,created_at") \
            .eq("lead_id", lead_id).execute()
        L["bundles"] = bundles.data or []
    except Exception:
        L["bundles"] = []
    return L


def build_context_project(supabase: Client, project_id: str) -> Dict[str, Any]:
    p = supabase.table("projects").select("*, customers(*)").eq("id", project_id).limit(1).execute()
    if not p.data:
        return {}
    P = p.data[0]
    try:
        tasks = supabase.table("project_tasks").select("id,name,status,owner_role,due_date") \
            .eq("project_id", project_id).neq("status", "done").order("due_date").limit(20).execute()
        P["open_tasks"] = tasks.data or []
    except Exception:
        P["open_tasks"] = []
    try:
        ms = supabase.table("project_milestones").select("id,milestone_key,fa_no,status,due_date,payment_amount") \
            .eq("project_id", project_id).execute()
        P["milestones"] = ms.data or []
    except Exception:
        P["milestones"] = []
    try:
        ext = supabase.table("external_orders").select("service_type,status,sent_at,due_date,received_at") \
            .eq("project_id", project_id).execute()
        P["external_orders"] = ext.data or []
    except Exception:
        P["external_orders"] = []
    return P


# =============================================
# 1) AI Next Action Suggester
# =============================================

NEXT_ACTION_SYSTEM = """Si AI asistent v slovenskom CRM systéme firmy Energovision (fotovoltika).
Tvoja úloha: na základe stavu leadu/projektu navrhni MAX 3 NAJDÔLEŽITEJŠIE akcie ktoré by mal kolega urobiť TERAZ.

Pravidlá:
- Iba akcie ktoré kolega môže urobiť 1 klikom v CRM (pošli email / vygeneruj dokument / objednať PBS / nastav termín)
- Konkrétne formulácie: NIE "Pošli email klientovi" ALE "Pošli pripomienku na podpis splnomocnenia (4 dni bez reakcie)"
- Ak je všetko v poriadku → vráť prázdny zoznam
- Vždy v slovenčine, 2. osoba ("Pošli...", "Vygeneruj...")

Action keys: send_followup, generate_docs, order_pbs, order_statika, order_geodet, schedule_visit, generate_quote, send_invoice, check_status, request_documents, other

Výstup IBA JSON, žiadny komentár:
{
  "actions": [
    {
      "action_key": "...",
      "button_label": "Krátky text (max 50 znakov)",
      "reason": "1 veta",
      "confidence": 0.0-1.0,
      "payload": {}
    }
  ]
}"""


def _trim(d: Any, max_str=300) -> Any:
    if isinstance(d, str):
        return d[:max_str]
    if isinstance(d, dict):
        return {k: _trim(v, max_str) for k, v in d.items() if k not in ("solaredge_raw", "address_geocode", "raw_data")}
    if isinstance(d, list):
        return [_trim(x, max_str) for x in d[:20]]
    return d


def _parse_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    return json.loads(text)


def suggest_next_actions(supabase: Client, target_type: str, target_id: str, save: bool = True) -> List[Dict[str, Any]]:
    if target_type == "lead":
        ctx = build_context_lead(supabase, target_id)
    elif target_type == "project":
        ctx = build_context_project(supabase, target_id)
    else:
        return []
    if not ctx:
        return []

    ctx_clean = _trim(ctx)
    user_prompt = f"Kontext {target_type}-u:\n```json\n{json.dumps(ctx_clean, default=str, ensure_ascii=False, indent=2)[:6000]}\n```\n\nNavrhni TOP 3 akcie."

    resp = get_anthropic().messages.create(
        model=MODEL, max_tokens=1200,
        system=NEXT_ACTION_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}]
    )

    try:
        parsed = _parse_json(resp.content[0].text)
        actions = parsed.get("actions", [])
    except Exception as e:
        log.warning(f"AI returned non-JSON: {e}")
        actions = []

    if save and actions:
        supabase.table("ai_next_actions").update({"status": "dismissed"}) \
            .eq("target_type", target_type).eq("target_id", target_id).eq("status", "open").execute()
        rows = []
        for i, a in enumerate(actions[:3]):
            rows.append({
                "target_type": target_type,
                "target_id": target_id,
                "workspace": ctx.get("workspace"),
                "rank": i + 1,
                "action_key": a.get("action_key", "other"),
                "button_label": a.get("button_label", "Akcia")[:200],
                "reason": a.get("reason"),
                "payload": a.get("payload") or {},
                "confidence": a.get("confidence"),
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            })
        if rows:
            supabase.table("ai_next_actions").insert(rows).execute()

    return actions


# =============================================
# 2) Draft email reply
# =============================================

DRAFT_EMAIL_SYSTEM = """Si AI asistent pre Energovision (slovenská fotovoltika firma).
Napíš profesionálny email v slovenčine.

Pravidlá:
- Slovenský jazyk, vykanie, zdvorilé ale stručné (3-5 viet)
- Žiadne marketingové frázy, vecne
- Vždy podpis: "S pozdravom,\\n{employee_name}\\nEnergovision s.r.o."
- Ak follow-up: zmieň konkrétnu vec čo čakáš

Výstup IBA JSON: {"subject":"...", "body_text":"..."}"""


def draft_email(supabase: Client, target_type: str, target_id: str,
                purpose: str, employee_name: str = "Dominik Galaba",
                incoming_email: Optional[str] = None) -> Dict[str, Any]:

    if target_type == "lead":
        ctx = build_context_lead(supabase, target_id)
    elif target_type == "project":
        ctx = build_context_project(supabase, target_id)
    elif target_type == "customer":
        c = supabase.table("customers").select("*").eq("id", target_id).limit(1).execute()
        ctx = c.data[0] if c.data else {}
    else:
        ctx = {}

    customer = ctx.get("customers") or ctx
    customer_email = customer.get("email") or customer.get("contact_email")
    customer_name = customer.get("company_name") or f"{customer.get('first_name','')} {customer.get('last_name','')}".strip()

    parts = [
        f"Účel: {purpose}",
        f"Zákazník: {customer_name}",
        f"Email zákazníka: {customer_email or '—'}",
        f"Podpis: {employee_name}",
    ]
    if incoming_email:
        parts.append(f"\nPrišiel mail od klienta:\n```\n{incoming_email[:2000]}\n```\nNapíš odpoveď.")
    else:
        ctx_lite = {k: v for k, v in ctx.items() if k not in ('activities','solaredge_raw')}
        parts.append(f"\nKontext: {json.dumps(ctx_lite, default=str, ensure_ascii=False)[:2500]}")

    resp = get_anthropic().messages.create(
        model=MODEL, max_tokens=800,
        system=DRAFT_EMAIL_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(parts)}]
    )

    try:
        parsed = _parse_json(resp.content[0].text)
        subject = parsed.get("subject", f"Energovision — {purpose}")
        body = parsed.get("body_text", "")
    except Exception:
        subject = f"Energovision — {purpose}"
        body = resp.content[0].text

    row = {
        "target_type": target_type,
        "target_id": target_id,
        "workspace": ctx.get("workspace"),
        "to_emails": [customer_email] if customer_email else [],
        "subject": subject,
        "body_text": body,
        "ai_generated": True,
        "ai_confidence": 0.85,
        "status": "draft",
    }
    ins = supabase.table("email_drafts").insert(row).execute()
    return {"draft_id": (ins.data[0]["id"] if ins.data else None), "subject": subject, "body": body, "to": customer_email}


# =============================================
# 3) Order external service
# =============================================

ORDER_EXTERNAL_SYSTEM = """Si AI asistent objednávajúci u externých dodávateľov pre fotovoltiku.
Email v slovenčine, vykanie, vecne, 5-7 viet.

Uveď: adresu projektu, výkon kW, typ inštalácie (strecha/zem), požadovaný termín.
NEUVÁDZAJ: meno klienta, OP, IČO, email klienta.

Podpis: "S pozdravom,\\nEnergovision s.r.o.\\nTel.: +421 917 424 564"

Výstup IBA JSON: {"subject":"...", "body_text":"...", "suggested_due_date":"YYYY-MM-DD"}"""


def order_external_service(supabase: Client, project_id: str, service_type: str,
                            provider_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = build_context_project(supabase, project_id)
    if not ctx:
        return {"error": "Project not found"}
    customer = ctx.get("customers") or {}

    if not provider_id:
        prov_q = supabase.table("external_providers").select("*") \
            .eq("service_type", service_type).eq("active", True).limit(1).execute()
        if not prov_q.data:
            return {"error": f"Žiadny aktívny provider typu '{service_type}'"}
        provider = prov_q.data[0]
        provider_id = provider["id"]
    else:
        provider = supabase.table("external_providers").select("*").eq("id", provider_id).single().execute().data

    service_label = {
        "pbs": "PBS posudok (protipožiarna bezpečnosť stavby)",
        "statika": "Statický posudok strechy",
        "geodet": "Geodetické zameranie",
        "elektrikar": "Elektromontážne práce",
        "revizny_technik": "Revízna správa elektroinštalácie",
    }.get(service_type, service_type)

    avg_days = int(provider.get("avg_response_days") or 7)
    user_prompt = f"""Objednávka: {service_label}
Projekt: {ctx.get('project_code')} — {ctx.get('name','')}
Adresa: {customer.get('city','')} {customer.get('street','')}
Výkon FVE: {ctx.get('scale_kwp','—')} kWp
Typ strechy: {ctx.get('roof_type','—')}
Dodávateľ: {provider.get('company_name')}
Termín očakávaný: do {(datetime.now() + timedelta(days=avg_days)).strftime('%d.%m.%Y')}

Napíš email s objednávkou."""

    resp = get_anthropic().messages.create(
        model=MODEL, max_tokens=600,
        system=ORDER_EXTERNAL_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}]
    )

    try:
        parsed = _parse_json(resp.content[0].text)
        subject = parsed.get("subject", f"Objednávka {service_label} — {ctx.get('project_code')}")
        body = parsed.get("body_text", "")
        due_date = parsed.get("suggested_due_date") or (datetime.now() + timedelta(days=avg_days)).strftime('%Y-%m-%d')
    except Exception:
        subject = f"Objednávka {service_label} — {ctx.get('project_code')}"
        body = resp.content[0].text
        due_date = (datetime.now() + timedelta(days=avg_days)).strftime('%Y-%m-%d')

    order_row = {
        "project_id": project_id,
        "workspace": ctx.get("workspace"),
        "service_type": service_type,
        "provider_id": provider_id,
        "status": "draft",
        "email_subject": subject,
        "email_body": body,
        "due_date": due_date,
    }
    ins = supabase.table("external_orders").insert(order_row).execute()
    order_id = ins.data[0]["id"] if ins.data else None

    draft_row = {
        "target_type": "project",
        "target_id": project_id,
        "workspace": ctx.get("workspace"),
        "to_emails": [provider.get("email")] if provider.get("email") else [],
        "subject": subject,
        "body_text": body,
        "ai_generated": True,
        "status": "draft",
    }
    supabase.table("email_drafts").insert(draft_row).execute()

    return {
        "order_id": order_id, "subject": subject, "body": body,
        "to": provider.get("email"), "provider_name": provider.get("company_name"),
        "due_date": due_date,
    }


# =============================================
# 4) Universal Doc Pre-fill
# =============================================

DOC_CLASSIFY_SYSTEM = """Si AI klasifikátor dokumentov pre slovenské CRM firmy Energovision.

Rozpoznaj typ dokumentu:
- "faktura_elektrina" — faktúra za elektrinu
- "obciansky" — OP scan
- "vypis_kataster" — výpis z katastra
- "pd_projekt" — PD FVE
- "zmluva_klient", "splnomocnenie", "gdpr_suhlas"
- "ziadost_pripojenie", "pbs_posudok", "staticky_posudok"
- "geodet_zameranie", "revizna_sprava", "preberaci_protokol"
- "iny"

Extrahuj kľúčové polia. Dátum DD.MM.YYYY → ISO YYYY-MM-DD.

Výstup IBA JSON:
{
  "doc_type": "...",
  "confidence": 0.0-1.0,
  "extracted": { ... },
  "suggested_filename": "..."
}"""


def classify_and_prefill(supabase: Client, target_type: str, target_id: str,
                         file_text_content: str) -> Dict[str, Any]:
    user_prompt = f"Cieľ: {target_type} ({target_id})\n\nDokument:\n```\n{file_text_content[:8000]}\n```\n\nKlasifikuj a extrahuj."

    resp = get_anthropic().messages.create(
        model=MODEL, max_tokens=1500,
        system=DOC_CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}]
    )

    try:
        parsed = _parse_json(resp.content[0].text)
    except Exception as e:
        return {"error": f"AI nevrátil JSON: {e}", "raw": resp.content[0].text[:500]}

    doc_type = parsed.get("doc_type", "iny")
    extracted = parsed.get("extracted") or {}
    confidence = float(parsed.get("confidence") or 0)

    applied: Dict[str, Any] = {}
    if confidence >= 0.7 and extracted:
        if target_type == "lead":
            field_map = {
                "ric_spotreba_kwh": "rocna_spotreba",
                "eic_odberne_miesto": "eic",
                "distribucna": "distribucka",
                "dodavatel": "dodavatel",
                "sadzba_dt": "tarifa",
            }
            updates = {dst: extracted[src] for src, dst in field_map.items() if extracted.get(src)}
            if updates:
                try:
                    supabase.table("leads").update(updates).eq("id", target_id).execute()
                    applied = updates
                except Exception as e:
                    log.warning(f"lead update failed: {e}")

        elif target_type == "project":
            field_map = {"distribucna": "distribucka", "eic_odberne_miesto": "eic"}
            updates = {dst: extracted[src] for src, dst in field_map.items() if extracted.get(src)}
            if updates:
                try:
                    supabase.table("projects").update(updates).eq("id", target_id).execute()
                    applied = updates
                except Exception as e:
                    log.warning(f"project update failed: {e}")

        elif target_type == "customer":
            field_map = {
                "ico": "ico", "dic": "dic", "ic_dph": "ic_dph",
                "op_cislo": "op_cislo", "datum_narodenia": "datum_narodenia",
            }
            updates = {dst: extracted[src] for src, dst in field_map.items() if extracted.get(src)}
            if updates:
                try:
                    supabase.table("customers").update(updates).eq("id", target_id).execute()
                    applied = updates
                except Exception as e:
                    log.warning(f"customer update failed: {e}")

    return {
        "doc_type": doc_type,
        "confidence": confidence,
        "extracted": extracted,
        "applied": applied,
        "suggested_filename": parsed.get("suggested_filename"),
    }


# =============================================
# 5) Doc package + email
# =============================================

def generate_doc_package(supabase: Client, lead_id: str, employee_name: str = "Dominik Galaba") -> Dict[str, Any]:
    ctx = build_context_lead(supabase, lead_id)
    if not ctx:
        return {"error": "Lead not found"}
    purpose = f"odoslanie balíka dokumentov k podpisu (zmluva, splnomocnenie, GDPR, dotazník) — projekt {ctx.get('ev_id','')}"
    return draft_email(supabase, "lead", lead_id, purpose, employee_name)
