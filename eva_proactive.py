"""
eva_proactive.py — Autonómna Eva (Cowork v2)
=============================================

Eva sa sama pozrie na stav firmy a:
1) Identifikuje udalosti vyžadujúce akciu
2) Skontroluje smart silence (nezareagovala podobne za posledných 24h)
3) Spustí pre-generation (volá automation.py)
4) Vytvorí artifact + chat message
5) Zapíše do proactive_log

Triggery:
- Hourly cron (každú hodinu Mon-Fri, 7-19h SK)
- DB triggers (project_milestones, external_orders)
- Manual trigger
"""

from __future__ import annotations
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import anthropic
from supabase import Client
import eva_data_lens as _lens

log = logging.getLogger(__name__)


def _ai():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def check_silence(supabase: Client, trigger_type: str, related_type: Optional[str],
                  related_id: Optional[str], silence_hours: int = 24) -> bool:
    """True = Eva ÁNO smiet reagovať. False = utlmená."""
    try:
        q = supabase.table("eva_proactive_log") \
            .select("id, silence_until") \
            .eq("trigger_type", trigger_type)
        if related_type:
            q = q.eq("related_type", related_type)
        if related_id:
            q = q.eq("related_id", related_id)
        recent = q.gte("created_at", (datetime.now(timezone.utc) - timedelta(hours=silence_hours)).isoformat()) \
            .limit(1).execute().data or []
        return len(recent) == 0
    except Exception:
        return True  # ak nevieš, dovoľ


def log_action(supabase: Client, trigger_type: str, related_type: Optional[str],
               related_id: Optional[str], action_taken: str,
               artifact_id: Optional[str] = None, message_id: Optional[str] = None,
               reason_skipped: Optional[str] = None, silence_hours: int = 24) -> None:
    """Zapíš že Eva spravila (alebo nespravila) akciu."""
    try:
        supabase.table("eva_proactive_log").insert({
            "trigger_type": trigger_type,
            "related_type": related_type,
            "related_id": related_id,
            "action_taken": action_taken,
            "reason_skipped": reason_skipped,
            "artifact_id": artifact_id,
            "message_id": message_id,
            "silence_until": (datetime.now(timezone.utc) + timedelta(hours=silence_hours)).isoformat(),
        }).execute()
    except Exception as e:
        log.warning(f"log_action failed: {e}")


def create_artifact(supabase: Client, artifact_type: str, title: str,
                    description: Optional[str] = None, preview_text: Optional[str] = None,
                    related_type: Optional[str] = None, related_id: Optional[str] = None,
                    related_draft_id: Optional[str] = None, file_url: Optional[str] = None,
                    workspace: Optional[str] = None, tags: Optional[List[str]] = None,
                    metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Vytvor eva_artifact."""
    try:
        row = {
            "artifact_type": artifact_type,
            "title": title[:200],
            "description": description,
            "preview_text": (preview_text or "")[:500],
            "related_type": related_type,
            "related_id": related_id,
            "related_draft_id": related_draft_id,
            "file_url": file_url,
            "workspace": workspace,
            "tags": tags or [],
            "metadata": metadata or {},
        }
        ins = supabase.table("eva_artifacts").insert(row).execute()
        return ins.data[0]["id"] if ins.data else None
    except Exception as e:
        log.warning(f"create_artifact failed: {e}")
        return None


def post_and_track(supabase: Client, content: str, action_type: str,
                   artifact_type: str, artifact_title: str,
                   related_type: Optional[str] = None, related_id: Optional[str] = None,
                   preview_text: Optional[str] = None,
                   actions: Optional[List[Dict]] = None,
                   metadata: Optional[Dict] = None) -> Optional[str]:
    """Pošli chat message + zaregistruj artifact do eva_artifacts (zásobník Eva pripravila)."""
    mid = post_chat_message(
        supabase, content, action_type=action_type,
        related_type=related_type, related_id=related_id,
        actions=actions,
    )
    if mid:
        try:
            artifact_row = {
                "artifact_type": artifact_type,
                "title": artifact_title,
                "description": content[:500],
                "preview_text": (preview_text or content)[:500],
                "related_type": related_type,
                "related_id": related_id,
                "message_id": mid,
                "status": "pending",
                "metadata": metadata or {},
            }
            supabase.table("eva_artifacts").insert(artifact_row).execute()
        except Exception as e:
            log.warning(f"post_and_track artifact insert failed: {e}")
    return mid


def post_chat_message(supabase: Client, content: str, action_type: str = "task_prep",
                      related_type: Optional[str] = None, related_id: Optional[str] = None,
                      artifacts: Optional[List[Dict]] = None,
                      actions: Optional[List[Dict]] = None) -> Optional[str]:
    """Pošli proaktívnu správu Evy do team_chat_messages."""
    try:
        row = {
            "role": "ai",
            "user_name": "Eva (AI kolegyňa)",
            "content": content,
            "ai_action_type": action_type,
            "ai_related_type": related_type,
            "ai_related_id": related_id,
            "ai_drafts": artifacts or [],
            "ai_actions": actions or [],
            "ai_model": "claude-sonnet-4-5",
        }
        ins = supabase.table("team_chat_messages").insert(row).execute()
        return ins.data[0]["id"] if ins.data else None
    except Exception as e:
        log.warning(f"post_chat_message failed: {e}")
        return None


# =========================================
# Hourly autonomous pass
# =========================================

def hourly_autonomous_pass(supabase: Client) -> Dict[str, Any]:
    """360° autonomous pass — Eva má prístup ku všetkým DB tabuľkám.
    
    Eva sa pozrie na stav firmy a SAMA rozhodne čo treba urobiť TERAZ.
    
    Pozrie:
    1) Milestones s due_date v <7 dňoch a status='issued' → pripomienka klientovi
    2) External_orders status='draft' >2h → upomienka že treba odoslať
    3) project_tasks overdue → pripomenutie ownerovi
    4) Stalled B2C leads >7 dní → reactivation email
    5) Project_milestones overdue → urgentný email
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    actions_taken = []
    
    # Načítaj 360° kontext pre awareness (zalogujeme dôležité metriky)
    try:
        full_ctx = _lens.collect_full_context(supabase)
        log.info(f"[Eva pass] B2B={full_ctx.get('b2b',{}).get('total')} overdue_FA={len(full_ctx.get('cashflow',{}).get('fa_overdue',[]))} alarms={len(full_ctx.get('operations',{}).get('active_alarms',[]))} low_stock={full_ctx.get('material',{}).get('low_stock_count',0)} unread_notif={full_ctx.get('notifications',{}).get('unread_count',0)}")
    except Exception:
        pass


    # 1) FA splatné <7 dní + UŽ OVERDUE — use b2b_invoices_overview view
    try:
        in_7 = (today + timedelta(days=7)).isoformat()
        ms = supabase.table("b2b_invoices_overview") \
            .select("project_id, project_code, customer_name, fa_no, due_date, payment_amount, computed_status") \
            .lte("due_date", in_7) \
            .in_("computed_status", ["overdue","sent","issued","ready_to_issue"]) \
            .limit(15).execute().data or []
        for m in ms:
            related_id = m["project_id"]
            if not check_silence(supabase, "invoice_due", "project", related_id, silence_hours=24):
                continue
            days_left = (datetime.fromisoformat(m["due_date"]).date() - today).days
            code = m.get("project_code", "?")
            amount = float(m.get("payment_amount") or 0)
            content = f"Pre projekt {code} je splatná FA {m.get('fa_no','?')} o {days_left} dní ({amount:,.0f} €). Pripravila som draft pripomienky klientovi — pošlite jedným klikom."

            mid = post_and_track(
                supabase, content,
                action_type="reminder",
                artifact_type="invoice_reminder",
                artifact_title=f"Pripomienka FA pre {code} ({amount:,.0f} €)",
                related_type="project", related_id=related_id,
                actions=[{"label": "Otvor projekt", "action_key": "open_project", "payload": {"id": related_id}}],
                metadata={"days_until_due": days_left, "amount_eur": amount, "fa_no": m.get("fa_no")},
            )
            log_action(supabase, "invoice_due", "project", related_id, "sent_message", message_id=mid, silence_hours=48)
            actions_taken.append({"type": "invoice_due", "project": code, "days": days_left})
    except Exception as e:
        log.warning(f"hourly check FA failed: {e}")

    # 2) External orders čakajú odoslanie >2h
    try:
        cutoff = (now - timedelta(hours=2)).isoformat()
        ext = supabase.table("external_orders") \
            .select("id, service_type, project_id, email_subject, created_at") \
            .eq("status", "draft").lt("created_at", cutoff).limit(5).execute().data or []
        for e in ext:
            related_id = e["id"]
            if not check_silence(supabase, "external_response", "external_order", related_id, silence_hours=12):
                continue
            content = f"Draft objednávky '{e.get('email_subject','?')}' ({e['service_type']}) čaká už viac ako 2 hodiny na odoslanie. Pošleme?"
            mid = post_and_track(
                supabase, content, action_type="reminder",
                artifact_type="external_draft",
                artifact_title=f"Draft objednávky: {e.get('email_subject','?')}",
                related_type="external_order", related_id=related_id,
                actions=[{"label": "Pozri draft", "action_key": "open_project", "payload": {"id": e["project_id"]}}],
                metadata={"service_type": e.get("service_type"), "project_id": e.get("project_id")},
            )
            log_action(supabase, "external_response", "external_order", related_id, "sent_message", message_id=mid)
            actions_taken.append({"type": "external_pending", "id": related_id})
    except Exception as ex:
        log.warning(f"hourly check external failed: {ex}")

    # 3) Overdue tasks (1 najurgentnejší per projekt)
    try:
        tasks = supabase.table("project_tasks") \
            .select("id, project_id, name, due_date, owner_role, projects(project_code)") \
            .lt("due_date", today.isoformat()).neq("status", "done") \
            .order("due_date").limit(5).execute().data or []
        for t in tasks:
            related_id = t["project_id"]
            if not check_silence(supabase, "task_overdue", "project", related_id, silence_hours=48):
                continue
            project = t.get("projects") or {}
            code = project.get("project_code", "?")
            days = (today - datetime.fromisoformat(t["due_date"]).date()).days
            content = f"[{code}] Úloha '{t.get('name','?')}' je {days} dní po termíne. Owner: {t.get('owner_role','?')}."
            mid = post_and_track(
                supabase, content, action_type="alert",
                artifact_type="task_overdue_alert",
                artifact_title=f"Overdue úloha: {t.get('name','?')} ({code})",
                related_type="project", related_id=related_id,
                actions=[{"label": "Otvor projekt", "action_key": "open_project", "payload": {"id": related_id}}],
                metadata={"days_overdue": days, "owner_role": t.get("owner_role"), "task_id": t.get("id")},
            )
            log_action(supabase, "task_overdue", "project", related_id, "sent_message", message_id=mid, silence_hours=48)
            actions_taken.append({"type": "task_overdue", "project": code, "days": days})
    except Exception as ex:
        log.warning(f"hourly check tasks failed: {ex}")

    return {"actions_taken": actions_taken, "count": len(actions_taken)}
