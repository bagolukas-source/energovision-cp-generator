"""
strategic_agent.py — AI Strategic Manager Agent
================================================

Funkcie:
- generate_strategic_brief(scope='daily') → vyrobí komplexný brief pre Lukáša + per-role agendy
- collect_context() → agreguje dáta zo Supabase (96 B2B + B2C + tasks + milestones + cashflow)

Output: ai_strategic_briefs row + JSON pre UI.
"""

from __future__ import annotations
import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import anthropic
from supabase import Client

log = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None
MODEL = "claude-sonnet-4-5"


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


STRATEGIC_SYSTEM = """Si AI Strategic Manager pre slovenskú firmu Energovision (fotovoltika + údržba trafostaníc + revízie + elektrotechnické práce).

Tvoja úloha: analyzuj komplet stav firmy a vyrob denný brief pre majiteľa Lukáša Baga a manažérsky tím.

Brief musí byť:
- VECNÝ — žiadne marketingové frázy, žiadne "skvelé výsledky"
- KONKRÉTNY — vždy s odkazom na konkrétny projekt/kolegu/dátum
- AKČNÝ — každá vec má jasnú "čo s tým" odpoveď
- KRÁTKY — Lukáš číta 5 minút, žiadne romány
- SLOVENSKÝ — vrátane technických termínov

Štruktúra výstupu (vždy validný JSON):

{
  "overall_health": "green" | "yellow" | "red",
  "health_score": 0-100,
  "one_liner": "1 veta sumár stavu firmy (max 150 znakov)",
  
  "priorities": [
    {"title": "Krátky title (max 60 znakov)", "why": "Prečo to je TOP priorita", "action": "Konkrétny ďalší krok", "owner": "Dominik | Lukáš | sales | projektant"}
  ],
  
  "risks": [
    {"project_code": "EV-26-XXX | lead_id | null", "issue": "Čo je riziko", "severity": "high | medium | low", "days_overdue": 0, "suggested_action": "Konkrétna akcia"}
  ],
  
  "bottlenecks": [
    {"area": "PBS posudky | žiadosti SSE | atď.", "impact": "Koľko projektov to blokuje", "suggested_action": "Konkrétna akcia"}
  ],
  
  "cashflow": {
    "next_30_days_expected_in": 0,
    "next_30_days_expected_out": 0,
    "overdue_invoices_count": 0,
    "overdue_amount": 0,
    "comment": "1 veta o cashflowe"
  },
  
  "agendas": {
    "Lukáš": ["Top 5 vecí na dnes"],
    "Dominik": ["Top 5 vecí na dnes"],
    "sales": ["Top 5 vecí na dnes"],
    "projektant": ["Top 5 vecí na dnes"]
  },
  
  "task_redistribution": [
    {"task_id": "uuid", "from_user": "name", "to_user": "name", "reason": "Prečo prehodiť"}
  ],
  
  "insights": [
    "Strategic insight 1 (napr. 'Win rate u Dominika 67% za posledný mesiac — najvyšší v tíme')"
  ]
}

Vráť IBA validný JSON, žiadny komentár navôkol."""


def collect_context(supabase: Client) -> Dict[str, Any]:
    """Agreguje dáta zo Supabase pre AI agenta."""
    today = datetime.now(timezone.utc).date()
    in_30 = (today + timedelta(days=30)).isoformat()
    in_60 = (today + timedelta(days=60)).isoformat()
    week_ago = (today - timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()

    ctx: Dict[str, Any] = {"generated_at": today.isoformat()}

    # === B2B projekty ===
    try:
        projects = supabase.table("b2b_projects_overview").select("*").limit(200).execute().data or []
        ctx["b2b"] = {
            "total": len(projects),
            "by_phase": {},
            "stalled_30days": 0,
            "high_value": [],
            "kwp_total": 0,
            "value_total": 0,
        }
        for p in projects:
            phase = p.get("computed_phase") or "unknown"
            ctx["b2b"]["by_phase"][phase] = ctx["b2b"]["by_phase"].get(phase, 0) + 1
            ctx["b2b"]["kwp_total"] += float(p.get("scale_kwp") or 0)
            ctx["b2b"]["value_total"] += float(p.get("contract_value_no_vat") or 0)
            if p.get("contract_value_no_vat") and float(p["contract_value_no_vat"]) > 100000:
                ctx["b2b"]["high_value"].append({
                    "code": p.get("project_code"),
                    "name": p.get("name"),
                    "phase": phase,
                    "kwp": p.get("scale_kwp"),
                    "value": p.get("contract_value_no_vat"),
                })
            updated = p.get("updated_at")
            if updated and updated < week_ago:
                ctx["b2b"]["stalled_30days"] += 1
    except Exception as e:
        ctx["b2b_error"] = str(e)

    # === B2B faktúry FA1/FA2/FA3 ===
    try:
        inv = supabase.table("b2b_invoices_overview").select("*").execute().data or []
        ctx["b2b_invoices"] = {
            "total": len(inv),
            "paid": sum(1 for i in inv if i.get("computed_status") == "paid"),
            "overdue": sum(1 for i in inv if i.get("computed_status") == "overdue"),
            "overdue_amount": sum(float(i.get("payment_amount") or 0) for i in inv if i.get("computed_status") == "overdue"),
            "due_30days": sum(float(i.get("payment_amount") or 0) for i in inv
                              if i.get("due_date") and i["due_date"] <= in_30 and i.get("computed_status") != "paid"),
            "overdue_projects": [
                {"project_code": i.get("project_code"), "fa_no": i.get("fa_no"), "amount": float(i.get("payment_amount") or 0), "due": i.get("due_date")}
                for i in inv if i.get("computed_status") == "overdue"
            ][:10],
        }
    except Exception as e:
        ctx["b2b_invoices_error"] = str(e)

    # === B2B project_tasks otvorené per role ===
    try:
        tasks = supabase.table("project_tasks").select("status, owner_role, due_date, project_id").neq("status", "done").limit(2000).execute().data or []
        ctx["b2b_tasks"] = {
            "total_open": len(tasks),
            "by_role": {},
            "overdue": 0,
            "due_this_week": 0,
        }
        for t in tasks:
            r = t.get("owner_role") or "unknown"
            ctx["b2b_tasks"]["by_role"][r] = ctx["b2b_tasks"]["by_role"].get(r, 0) + 1
            d = t.get("due_date")
            if d:
                if d < today.isoformat():
                    ctx["b2b_tasks"]["overdue"] += 1
                elif d <= (today + timedelta(days=7)).isoformat():
                    ctx["b2b_tasks"]["due_this_week"] += 1
    except Exception as e:
        ctx["b2b_tasks_error"] = str(e)

    # === B2C Leady ===
    try:
        leads = supabase.table("leads").select("id, status, ev_id, created_at, assigned_to, customers(first_name, last_name, company_name)").eq("workspace","b2c").limit(500).execute().data or []
        ctx["b2c_leads"] = {
            "total": len(leads),
            "by_status": {},
            "stalled_7days": [],
        }
        for l in leads:
            s = l.get("status") or "unknown"
            ctx["b2c_leads"]["by_status"][s] = ctx["b2c_leads"]["by_status"].get(s, 0) + 1
            if l.get("created_at") and l["created_at"] < week_ago and s not in ("hotove", "ukoncene", "lost", "archivovane"):
                c = l.get("customers") or {}
                cname = c.get("company_name") or f"{c.get('first_name','')} {c.get('last_name','')}".strip()
                ctx["b2c_leads"]["stalled_7days"].append({
                    "ev_id": l.get("ev_id"),
                    "status": s,
                    "customer": cname,
                    "days": (today - datetime.fromisoformat(l["created_at"].replace("Z","+00:00")).date()).days
                })
        ctx["b2c_leads"]["stalled_7days"] = ctx["b2c_leads"]["stalled_7days"][:15]
    except Exception as e:
        ctx["b2c_leads_error"] = str(e)

    # === B2C Orders (rozkladané prebiehajúce realizácie) ===
    try:
        orders = supabase.table("orders").select("status, total_with_vat, scheduled_start").eq("workspace","b2c").limit(300).execute().data or []
        ctx["b2c_orders"] = {
            "total": len(orders),
            "by_status": {},
            "value_total": sum(float(o.get("total_with_vat") or 0) for o in orders),
        }
        for o in orders:
            s = o.get("status") or "unknown"
            ctx["b2c_orders"]["by_status"][s] = ctx["b2c_orders"]["by_status"].get(s, 0) + 1
    except Exception as e:
        ctx["b2c_orders_error"] = str(e)

    # === External orders (PBS / statika) ===
    try:
        ext = supabase.table("external_orders").select("service_type, status, due_date, project_id").execute().data or []
        ctx["external_orders"] = {
            "total": len(ext),
            "by_type": {},
            "pending_responses": 0,
            "overdue": 0,
        }
        for e in ext:
            t = e.get("service_type") or "unknown"
            ctx["external_orders"]["by_type"][t] = ctx["external_orders"]["by_type"].get(t, 0) + 1
            if e.get("status") == "sent":
                ctx["external_orders"]["pending_responses"] += 1
                if e.get("due_date") and e["due_date"] < today.isoformat():
                    ctx["external_orders"]["overdue"] += 1
    except Exception as e:
        ctx["external_orders_error"] = str(e)

    # === Recent activities (čo sa stalo dnes/včera) ===
    try:
        acts = supabase.table("activities").select("action, entity_type, created_at").gte("created_at", (today - timedelta(days=2)).isoformat()).limit(200).execute().data or []
        ctx["recent_activity"] = {
            "total": len(acts),
            "by_action": {},
        }
        for a in acts:
            act = a.get("action") or "unknown"
            ctx["recent_activity"]["by_action"][act] = ctx["recent_activity"]["by_action"].get(act, 0) + 1
    except Exception as e:
        ctx["recent_activity_error"] = str(e)

    # === Users / team kapacita ===
    try:
        users = supabase.table("users").select("id, full_name, role, is_active").eq("is_active", True).execute().data or []
        ctx["team"] = [{"id": u["id"], "name": u["full_name"], "role": u.get("role")} for u in users]
    except Exception as e:
        ctx["team_error"] = str(e)

    return ctx


def generate_strategic_brief(supabase: Client, scope: str = "daily", save: bool = True) -> Dict[str, Any]:
    """Hlavná funkcia — vyrobí strategický brief."""
    started = time.time()

    ctx = collect_context(supabase)

    user_prompt = f"""Tu je komplet stav firmy Energovision ku dnešnému dňu.

```json
{json.dumps(ctx, default=str, ensure_ascii=False, indent=2)[:15000]}
```

Vyrob {scope} strategický brief podľa zadaného formátu. Buď konkrétny."""

    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=4000,
        system=STRATEGIC_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}]
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")

    try:
        brief = json.loads(text)
    except Exception as e:
        log.warning(f"AI vrátil non-JSON brief: {e}")
        brief = {
            "overall_health": "yellow",
            "health_score": 50,
            "one_liner": "Brief sa nepodaril spracovať — pozri raw output",
            "priorities": [],
            "risks": [],
            "bottlenecks": [],
            "cashflow": {},
            "agendas": {},
            "task_redistribution": [],
            "insights": [],
            "raw_error": str(e),
            "raw_text": text[:1000],
        }

    duration_ms = int((time.time() - started) * 1000)

    if save:
        row = {
            "scope": scope,
            "overall_health": brief.get("overall_health", "yellow"),
            "health_score": brief.get("health_score"),
            "one_liner": brief.get("one_liner", "")[:500],
            "priorities": brief.get("priorities", []),
            "risks": brief.get("risks", []),
            "bottlenecks": brief.get("bottlenecks", []),
            "cashflow": brief.get("cashflow", {}),
            "agendas": brief.get("agendas", {}),
            "task_redistribution": brief.get("task_redistribution", []),
            "insights": brief.get("insights", []),
            "input_stats": {
                "b2b_total": ctx.get("b2b", {}).get("total"),
                "b2c_leads_total": ctx.get("b2c_leads", {}).get("total"),
                "open_tasks": ctx.get("b2b_tasks", {}).get("total_open"),
                "overdue_invoices": ctx.get("b2b_invoices", {}).get("overdue"),
            },
            "generation_duration_ms": duration_ms,
            "model_used": MODEL,
        }
        try:
            ins = supabase.table("ai_strategic_briefs").insert(row).execute()
            brief["_id"] = ins.data[0]["id"] if ins.data else None
        except Exception as e:
            log.exception("Save brief failed")
            brief["_save_error"] = str(e)

    brief["_duration_ms"] = duration_ms
    return brief
