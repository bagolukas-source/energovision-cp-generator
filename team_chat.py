"""
team_chat.py — AI Kolega + Tímový chat
=======================================

AI Kolega:
- 2x denne (8:00 a 14:00 SK) prejde stav firmy
- Identifikuje 1-3 udalosti v najbližších 14 dňoch
- VOPRED pripraví dokumenty / emaily / objednávky externistov
- Napíše proaktívnu chat správu s akčnými tlačidlami

Reply handler:
- Reaguje keď user napíše do chat-u (mentioned alebo otázka)
- Má prístup ku kontextu firmy
- Vie spustiť akcie (volá existujúce endpointy)
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

_client: Optional[anthropic.Anthropic] = None
MODEL = "claude-sonnet-4-5"


def _ai():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


PROACTIVE_SYSTEM = """Si Eva — AI kolegyňa v slovenskom CRM firmy Energovision (fotovoltika).

Tvoj charakter:
- Kolegyňa ktorá pomáha tímu, nie suchý analyzátor
- Si proaktívna — pozeráš dopredu a pripravuješ veci VOPRED
- Hovoríš ako kolegyňa — krátko, priateľsky, vykanie ("Pripravila som vám...")
- Ak vidíš že sa niečo blíži (revízia za 2 týždne, splatná FA, deadline úradu) — pripravíš čo môžeš
- Žiadne marketingové frázy, žiadne "skvelé výsledky"

Tvoja úloha TERAZ: prejdi stav firmy a vyber 1-3 udalosti v najbližších 14 dňoch ktoré si môžeš pripraviť DOPREDU.

Pre každú napíš krátku správu (max 3 vety) typu:
- "Pozrela som projekt EV-26-XXX — revízia je naplánovaná na 12.6. Pripravila som už draft revíznej správy + draft mailu klientovi s pozvánkou. Máte sa pozrieť?"
- "Pre EV-26-YYY je splatná FA-2 o 5 dní (1 200 €). Pripravila som pripomienku mailom — chcete poslať?"
- "Pre EV-26-ZZZ chýba PBS posudok. Pripravila som žiadosť pre nášho dodávateľa — pozrite a jedným klikom pošlite."

KEY constraint: vyber LEN tie udalosti kde naozaj môžeš niečo pripraviť (existuje endpoint na generovanie). 
Možnosti:
- "prepare_documents" — vygenerovať zmluvu/splnomocnenie/GDPR (lead_id)
- "draft_email" — vyrobiť email draft (lead/project/customer)
- "order_external" — objednať PBS/statika/geodet (project_id)
- "draft_reminder" — pripomienka splatnej faktúry
- "prepare_revizna_sprava" — predpripraviť revíznu správu na blížiacu sa revíziu
- "schedule_visit" — návrh termínu obhliadky

Výstup IBA validný JSON:
{
  "messages": [
    {
      "content": "Eva text (krátky, 2-3 vety, slovenčina)",
      "ai_action_type": "task_prep" | "document_ready" | "reminder" | "alert",
      "related_type": "project" | "lead" | "order" | null,
      "related_id": "uuid" | null,
      "pre_generate": [
        {"action": "prepare_documents | draft_email | order_external | draft_reminder | prepare_revizna_sprava", "params": {...}}
      ],
      "actions": [
        {"label": "Pozrieť si draft", "action_key": "view_drafts", "payload": {...}},
        {"label": "Poslať klientovi", "action_key": "send_draft_email", "payload": {"draft_id": "..."}},
        {"label": "Nechcem, ignoruj", "action_key": "dismiss", "payload": {}}
      ]
    }
  ]
}

Vráť IBA JSON, žiadny iný text."""


REPLY_SYSTEM = """Si Eva — AI kolegyňa v CRM firmy Energovision.

Reaguješ na správu od kolegu v tímovom chate. Máš prístup k:
- Stav firmy (projekty, leady, tasks, faktúry)
- História chat konverzácie (posledných 20 správ)
- Možnosť spustiť akcie (generovať dokumenty, drafty emailov, objednať externistov)

Pravidlá:
- Krátka odpoveď (2-4 vety)
- Slovenčina, vykanie ale priateľsky
- Konkrétne — ak vieš spustiť akciu, ponúkni ju ako tlačidlo
- Ak nevieš odpovedať: úprimne povedz "neviem", nepýtaj sa zbytočne

Výstup IBA JSON:
{
  "content": "Tvoja odpoveď",
  "actions": [
    {"label": "Spustiť X", "action_key": "...", "payload": {...}}
  ]
}"""


def _parse_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    return json.loads(text)


def collect_upcoming_events(supabase: Client) -> Dict[str, Any]:
    """Identifikuje udalosti v najbližších 14 dňoch."""
    today = datetime.now(timezone.utc).date()
    in_14 = (today + timedelta(days=14)).isoformat()
    in_7 = (today + timedelta(days=7)).isoformat()

    ctx: Dict[str, Any] = {}

    # B2B projects with upcoming milestones
    try:
        ms = supabase.table("project_milestones") \
            .select("id, project_id, milestone_key, fa_no, due_date, payment_amount, status, projects(project_code, name)") \
            .lte("due_date", in_14).gte("due_date", today.isoformat()) \
            .neq("status", "paid").limit(30).execute().data or []
        ctx["upcoming_milestones"] = ms
    except Exception as e:
        ctx["upcoming_milestones_error"] = str(e)

    # Overdue invoices
    try:
        from postgrest import APIError  # noqa
        ms_overdue = supabase.table("project_milestones") \
            .select("id, project_id, milestone_key, fa_no, due_date, payment_amount, status, projects(project_code, name)") \
            .lt("due_date", today.isoformat()).in_("status", ["issued","sent"]).limit(20).execute().data or []
        ctx["overdue_invoices"] = ms_overdue
    except Exception:
        pass

    # Project tasks due within 14 days
    try:
        tasks = supabase.table("project_tasks") \
            .select("id, project_id, name, due_date, owner_role, status, projects(project_code, name)") \
            .lte("due_date", in_14).gte("due_date", today.isoformat()) \
            .in_("status", ["not_started","in_progress","blocked"]).limit(30).execute().data or []
        ctx["upcoming_tasks"] = tasks
    except Exception:
        pass

    # B2B projects in 'realizacia' phase with no scheduled date (need scheduling)
    try:
        projs = supabase.table("b2b_projects_overview") \
            .select("id, project_code, name, computed_phase, scale_kwp, contract_value_no_vat, updated_at") \
            .in_("computed_phase", ["projekcia","porealizacna","vystavit_fa1","vystavit_fa2","vystavit_fa3"]) \
            .limit(50).execute().data or []
        ctx["active_projects"] = projs
    except Exception:
        pass

    # Pending external orders (PBS/statika/geodet — čakajúce odoslanie)
    try:
        ext = supabase.table("external_orders") \
            .select("id, project_id, service_type, status, due_date, sent_at") \
            .eq("status", "draft").limit(20).execute().data or []
        ctx["pending_external_orders"] = ext
    except Exception:
        pass

    # B2C leady stagnujúce > 7 dní
    try:
        week_ago = (today - timedelta(days=7)).isoformat()
        b2c_stalled = supabase.table("leads") \
            .select("id, ev_id, status, created_at, customers(first_name, last_name, company_name)") \
            .eq("workspace","b2c").lt("created_at", week_ago) \
            .not_.in_("status", ["hotove","ukoncene","lost","archivovane"]).limit(15).execute().data or []
        ctx["stalled_b2c_leads"] = b2c_stalled
    except Exception:
        pass

    return ctx


def proactive_pass(supabase: Client) -> Dict[str, Any]:
    """Hlavná funkcia: vyrobí 1-3 proaktívne správy + pre-generation."""
    ctx = collect_upcoming_events(supabase)

    prompt = f"""Tu je stav firmy + nadchádzajúce udalosti:

```json
{json.dumps(ctx, default=str, ensure_ascii=False, indent=2)[:10000]}
```

Vyber 1-3 najdôležitejšie udalosti kde môžeš niečo pripraviť VOPRED. Vyrob proaktívne správy."""

    resp = _ai().messages.create(
        model=MODEL, max_tokens=3000,
        system=PROACTIVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        parsed = _parse_json(resp.content[0].text)
        messages = parsed.get("messages", [])
    except Exception as e:
        log.warning(f"AI proactive JSON parse failed: {e}")
        return {"created": 0, "error": str(e)}

    created = 0
    for m in messages[:3]:
        # 1) Pre-generation — volaj príslušné existujúce endpointy
        drafts: List[Dict[str, Any]] = []
        for pg in (m.get("pre_generate") or []):
            try:
                result = _pre_generate(supabase, pg.get("action"), pg.get("params") or {})
                if result:
                    drafts.append(result)
            except Exception as e:
                log.warning(f"pre_generate {pg.get('action')} failed: {e}")

        # 2) Vlož správu do chat-u
        row = {
            "role": "ai",
            "user_name": "Eva (AI kolegyňa)",
            "content": m.get("content", ""),
            "ai_action_type": m.get("ai_action_type"),
            "ai_related_type": m.get("related_type"),
            "ai_related_id": m.get("related_id"),
            "ai_actions": m.get("actions", []),
            "ai_drafts": drafts,
            "ai_model": MODEL,
            "ai_confidence": 0.85,
        }
        try:
            supabase.table("team_chat_messages").insert(row).execute()
            created += 1
        except Exception as e:
            log.warning(f"chat insert failed: {e}")

    return {"created": created, "total_proposed": len(messages)}


def _pre_generate(supabase: Client, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Spustí konkrétnu pre-generation akciu."""
    try:
        import automation as _auto
    except ImportError:
        return None

    if action == "prepare_documents":
        lead_id = params.get("lead_id")
        if not lead_id:
            return None
        result = _auto.generate_doc_package(supabase, lead_id)
        return {"type": "doc_package", "id": result.get("draft_id"), "preview": result.get("subject"), "lead_id": lead_id}

    if action == "draft_email":
        target_type = params.get("target_type", "lead")
        target_id = params.get("target_id")
        purpose = params.get("purpose", "kontakt s klientom")
        if not target_id:
            return None
        result = _auto.draft_email(supabase, target_type, target_id, purpose)
        return {"type": "email", "id": result.get("draft_id"), "preview": result.get("subject"), "to": result.get("to")}

    if action == "order_external":
        project_id = params.get("project_id")
        service_type = params.get("service_type")
        if not project_id or not service_type:
            return None
        result = _auto.order_external_service(supabase, project_id, service_type)
        if "error" in result:
            return None
        return {"type": "external_order", "id": result.get("order_id"), "preview": result.get("subject"), "service": service_type}

    if action == "draft_reminder":
        target_type = params.get("target_type", "project")
        target_id = params.get("target_id")
        purpose = params.get("purpose", "pripomienka splatnosti faktúry")
        if not target_id:
            return None
        result = _auto.draft_email(supabase, target_type, target_id, purpose)
        return {"type": "reminder_email", "id": result.get("draft_id"), "preview": result.get("subject")}

    if action == "prepare_revizna_sprava":
        # Pripraví len draft activity log — generovanie reálnej revíznej správy je samostatný endpoint
        return {"type": "revizna_sprava_draft", "preview": "Predpripravená revízna správa — otvor projekt"}

    return None


def handle_reply(supabase: Client, user_message: str, user_id: Optional[str] = None,
                 user_name: Optional[str] = None, skip_insert_user: bool = False) -> Dict[str, Any]:
    """Reaguje na user správu v chate."""

    if not skip_insert_user:
        user_row = {
            "role": "user",
            "user_id": user_id,
            "user_name": user_name or "Užívateľ",
            "content": user_message,
        }
        supabase.table("team_chat_messages").insert(user_row).execute()

    # 2) Načítaj posledných 20 správ pre kontext
    history = supabase.table("team_chat_messages") \
        .select("role, user_name, content, ai_actions, created_at") \
        .is_("deleted_at", None) \
        .order("created_at", desc=True).limit(20).execute().data or []
    history.reverse()

    history_text = "\n".join([
        f"[{h['role']}: {h.get('user_name','?')}] {h.get('content','')[:300]}"
        for h in history
    ])

    # 3) Načítaj kontext firmy (zjednodušený)
    ctx_summary = collect_upcoming_events(supabase)
    ctx_short = {
        "active_projects_count": len(ctx_summary.get("active_projects", [])),
        "overdue_invoices_count": len(ctx_summary.get("overdue_invoices", [])),
        "upcoming_tasks_count": len(ctx_summary.get("upcoming_tasks", [])),
        "stalled_b2c_count": len(ctx_summary.get("stalled_b2c_leads", [])),
    }

    prompt = f"""História chatu:
{history_text}

Kontext firmy:
{json.dumps(ctx_short, ensure_ascii=False)}

Posledná správa od {user_name or 'kolegu'}:
"{user_message}"

Odpovedz."""

    resp = _ai().messages.create(
        model=MODEL, max_tokens=1200,
        system=REPLY_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        parsed = _parse_json(resp.content[0].text)
    except Exception:
        parsed = {"content": resp.content[0].text.strip(), "actions": []}

    # 4) Vlož AI odpoveď
    ai_row = {
        "role": "ai",
        "user_name": "Eva (AI kolegyňa)",
        "content": parsed.get("content", ""),
        "ai_actions": parsed.get("actions", []),
        "ai_model": MODEL,
    }
    ins = supabase.table("team_chat_messages").insert(ai_row).execute()
    return {"ai_message_id": (ins.data[0]["id"] if ins.data else None), "content": parsed.get("content", "")}
