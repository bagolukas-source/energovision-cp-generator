"""
eva_data_lens.py — Eva all-seeing lens
=======================================

Modul agreguje stav firmy z ~90 tabuliek do single struct.
Eva pri každom proactive pass + reply má 360° pohľad.

Sekcie:
- Pipeline: B2B projekty (overview), B2C leady, obhliadky, quotes
- Cashflow: FA splatné/overdue/paid, bank transactions, projekt milestones
- Operations: inverter alarms, performance, SPOT transitions, service tickets
- Materiál: low stock, purchase orders, material reservations
- Úrady: authority requests, permits pending
- Dokumenty: pending uploads, state_machine awaiting decision
- Komunikácia: notifications unread, customer_info_requests pending
- Team: open tasks per role, capacity load
- AI: aktívne artefakty, nedávno použité memories, executed actions
"""

from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from supabase import Client

log = logging.getLogger(__name__)


def _safe_query(supabase: Client, table: str, select: str = "*", limit: int = 50, **filters):
    try:
        q = supabase.table(table).select(select).limit(limit)
        for key, val in filters.items():
            if "__gte" in key:
                q = q.gte(key.replace("__gte", ""), val)
            elif "__lte" in key:
                q = q.lte(key.replace("__lte", ""), val)
            elif "__lt" in key:
                q = q.lt(key.replace("__lt", ""), val)
            elif "__gt" in key:
                q = q.gt(key.replace("__gt", ""), val)
            elif "__in" in key:
                q = q.in_(key.replace("__in", ""), val)
            elif "__neq" in key:
                q = q.neq(key.replace("__neq", ""), val)
            elif "__is_null" in key:
                q = q.is_(key.replace("__is_null", ""), "null") if val else q.not_.is_(key.replace("__is_null", ""), "null")
            else:
                q = q.eq(key, val)
        return q.execute().data or []
    except Exception as e:
        log.warning(f"_safe_query {table} failed: {e}")
        return []


def _count(supabase: Client, table: str, **filters) -> int:
    try:
        q = supabase.table(table).select("*", count="exact", head=True)
        for key, val in filters.items():
            if "__neq" in key:
                q = q.neq(key.replace("__neq", ""), val)
            elif "__in" in key:
                q = q.in_(key.replace("__in", ""), val)
            elif "__lt" in key:
                q = q.lt(key.replace("__lt", ""), val)
            elif "__gte" in key:
                q = q.gte(key.replace("__gte", ""), val)
            else:
                q = q.eq(key, val)
        r = q.execute()
        return r.count or 0
    except Exception:
        return 0


def collect_full_context(supabase: Client) -> Dict[str, Any]:
    """360° pohľad na stav firmy. Vracia JSON pre Claude system prompt."""
    now = datetime.now(timezone.utc)
    today = now.date()
    in_7 = (today + timedelta(days=7)).isoformat()
    in_14 = (today + timedelta(days=14)).isoformat()
    in_30 = (today + timedelta(days=30)).isoformat()
    week_ago = (today - timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()
    today_iso = today.isoformat()

    ctx: Dict[str, Any] = {"snapshot_at": now.isoformat()}

    # === PIPELINE ===
    # B2B projekty
    ctx["b2b"] = {
        "total": _count(supabase, "projects", workspace="b2b"),
        "by_phase": {},
        "active_projects": _safe_query(supabase, "b2b_projects_overview", 
            "project_code,name,customer_name,computed_phase,scale_kwp,contract_value_no_vat,updated_at",
            limit=30),
    }
    for p in ctx["b2b"]["active_projects"]:
        phase = p.get("computed_phase") or "unknown"
        ctx["b2b"]["by_phase"][phase] = ctx["b2b"]["by_phase"].get(phase, 0) + 1

    # B2C leady
    ctx["b2c_leads"] = {
        "total_active": _count(supabase, "leads", workspace="b2c", **{"status__neq": "lost"}),
        "stalled_7d": _safe_query(supabase, "leads",
            "id,ev_id,status,assigned_to,created_at,customers(first_name,last_name,company_name)",
            workspace="b2c", created_at__lt=week_ago,
            **{"status__neq": "lost"}, limit=15),
    }

    # Obhliadky / inspections
    ctx["inspections"] = {
        "today_tomorrow": _safe_query(supabase, "inspections",
            "id,scheduled_date,status,address_text,customers(first_name,last_name,company_name)",
            scheduled_date__gte=today_iso, scheduled_date__lte=in_7, limit=15),
    }

    # === CASHFLOW ===
    ctx["cashflow"] = {
        "fa_overdue": _safe_query(supabase, "b2b_invoices_overview",
            "project_code,customer_name,fa_no,due_date,payment_amount",
            computed_status="overdue", limit=20),
        "fa_due_30d": _safe_query(supabase, "b2b_invoices_overview",
            "project_code,customer_name,fa_no,due_date,payment_amount,computed_status",
            due_date__gte=today_iso, due_date__lte=in_30,
            **{"computed_status__neq": "paid"}, limit=30),
        "fa_paid_this_month": _count(supabase, "b2b_invoices_overview",
            computed_status="paid", due_date__gte=today.replace(day=1).isoformat()),
    }

    # B2C invoices paid this month
    try:
        b2c_paid = supabase.table("invoices").select("total_with_vat,paid_at") \
            .eq("status", "paid").gte("paid_at", today.replace(day=1).isoformat()) \
            .execute().data or []
        ctx["cashflow"]["b2c_paid_this_month_total"] = sum(float(i.get("total_with_vat") or 0) for i in b2c_paid)
        ctx["cashflow"]["b2c_paid_count"] = len(b2c_paid)
    except Exception:
        pass

    # === OPERATIONS (Inverters / Huawei SPOT) ===
    ctx["operations"] = {
        "active_alarms": _safe_query(supabase, "inverter_alarms",
            "alarm_id,severity,description,fault_time,inverter_id",
            **{"status__in": ["active","unresolved"]}, limit=10),
        "spot_today_actions": _count(supabase, "spot_state_transitions",
            transition_date=today_iso),
    }

    # === MATERIÁL / STOCK ===
    try:
        stock = supabase.table("stock_items") \
            .select("id,quantity,products(name,min_stock,sku)").limit(200).execute().data or []
        low = [s for s in stock if s.get("products") and 
               (s.get("products", {}).get("min_stock") or 0) > 0 and
               (s.get("quantity") or 0) < (s.get("products", {}).get("min_stock") or 0)]
        ctx["material"] = {
            "low_stock_items": [
                {
                    "name": s["products"]["name"],
                    "sku": s["products"].get("sku"),
                    "quantity": s["quantity"],
                    "min": s["products"]["min_stock"],
                }
                for s in low[:10]
            ],
            "low_stock_count": len(low),
        }
    except Exception:
        ctx["material"] = {"low_stock_count": 0}

    # Purchase orders pending
    ctx["material"]["po_pending"] = _safe_query(supabase, "purchase_orders",
        "id,po_number,status,total_no_vat,created_at",
        **{"status__in": ["draft","sent","confirmed"]}, limit=10)

    # === SERVICE TICKETS ===
    ctx["service"] = {
        "open_tickets": _safe_query(supabase, "service_tickets",
            "id,ticket_number,priority,status,subject,customer_id,created_at",
            **{"status__in": ["new","in_progress","waiting"]}, limit=15),
        "open_count": _count(supabase, "service_tickets",
            **{"status__in": ["new","in_progress","waiting"]}),
    }

    # === ÚRADY / PERMITS ===
    ctx["authorities"] = {
        "pending_requests": _safe_query(supabase, "project_authority_requests",
            "id,authority_type,request_type,status,sent_at,project_id",
            **{"status__in": ["sent","waiting","in_review"]}, limit=15),
    }

    # === DOKUMENTY ===
    ctx["documents"] = {
        "pending_upload": _count(supabase, "project_documents", status="awaiting_upload"),
        "in_review": _count(supabase, "project_documents", status="in_review"),
    }

    # === KOMUNIKÁCIA ===
    # Notifications unread
    ctx["notifications"] = {
        "unread_count": _count(supabase, "notifications", read=False),
        "recent_unread": _safe_query(supabase, "notifications",
            "id,title,message,severity,created_at", read=False, limit=10),
    }

    # Customer info requests pending response
    ctx["customer_requests"] = {
        "awaiting_response": _safe_query(supabase, "customer_info_requests",
            "id,customer_id,fields_requested,status,sent_at",
            **{"status__in": ["sent","waiting"]}, limit=10),
    }

    # Email drafts pending
    ctx["email_drafts"] = {
        "pending_count": _count(supabase, "email_drafts", status="draft"),
        "recent": _safe_query(supabase, "email_drafts",
            "id,subject,to_emails,target_type,target_id,created_at",
            status="draft", limit=10),
    }

    # === TASKS QUEUE ===
    ctx["tasks"] = {
        "total_open": _count(supabase, "project_tasks", **{"status__neq": "done"}),
        "overdue": _count(supabase, "project_tasks",
            **{"status__neq": "done"}, due_date__lt=today_iso),
        "due_this_week": _count(supabase, "project_tasks",
            **{"status__neq": "done"}, due_date__gte=today_iso, due_date__lte=in_7),
        "urgent_priority": _count(supabase, "project_tasks",
            priority="urgent", **{"status__neq": "done"}),
    }

    # === EXTERNÉ OBJEDNÁVKY (PBS/statika/...) ===
    ctx["external_orders"] = {
        "draft_count": _count(supabase, "external_orders", status="draft"),
        "sent_pending_response": _count(supabase, "external_orders", status="sent"),
        "overdue_response": _safe_query(supabase, "external_orders",
            "id,service_type,project_id,due_date,sent_at",
            status="sent", due_date__lt=today_iso, limit=10),
    }

    # === BANK ===
    # (cez activities ak je tam camt053 import)
    try:
        bank_recent = supabase.table("activities") \
            .select("action,changes,created_at") \
            .eq("action", "bank_transaction_imported") \
            .gte("created_at", week_ago).limit(20).execute().data or []
        ctx["bank"] = {"recent_transactions": len(bank_recent)}
    except Exception:
        pass

    # === MEETINGS ===
    ctx["meetings"] = {
        "open_action_items": _count(supabase, "meeting_action_items",
            **{"status__neq": "done"}),
        "upcoming": _safe_query(supabase, "meetings",
            "id,title,start_time,attendees",
            start_time__gte=now.isoformat(),
            start_time__lte=(now + timedelta(days=3)).isoformat(),
            limit=10),
    }

    # === RECENT ACTIVITY (last 24h) ===
    try:
        recent = supabase.table("activities") \
            .select("action,entity_type,created_at,users(full_name)") \
            .gte("created_at", (now - timedelta(hours=24)).isoformat()) \
            .order("created_at", desc=True).limit(30).execute().data or []
        by_action = {}
        for a in recent:
            by_action[a["action"]] = by_action.get(a["action"], 0) + 1
        ctx["recent_24h"] = {"total": len(recent), "by_action": by_action}
    except Exception:
        pass

    # === EVA SELF-AWARENESS ===
    ctx["eva_self"] = {
        "active_artifacts": _count(supabase, "eva_artifacts",
            **{"status__in": ["pending","ready"]}),
        "memories_count": _count(supabase, "eva_memory", status="active"),
        "recent_proactive_actions": _safe_query(supabase, "eva_proactive_log",
            "trigger_type,action_taken,created_at",
            created_at__gte=(now - timedelta(hours=24)).isoformat(), limit=20),
    }

    return ctx


def format_for_system_prompt(ctx: Dict[str, Any]) -> str:
    """Format full context ako readable text pre Claude system prompt."""
    parts = ["=== STAV FIRMY ENERGOVISION (real-time) ==="]

    b2b = ctx.get("b2b", {})
    if b2b:
        phases = ", ".join(f"{k}: {v}" for k, v in b2b.get("by_phase", {}).items())
        parts.append(f"\nB2B: {b2b.get('total', 0)} projektov ({phases})")

    b2c = ctx.get("b2c_leads", {})
    if b2c:
        stalled = len(b2c.get("stalled_7d", []))
        parts.append(f"B2C: {b2c.get('total_active', 0)} aktívnych leadov, {stalled} stagnuje >7 dní")

    cash = ctx.get("cashflow", {})
    if cash:
        overdue = cash.get("fa_overdue", [])
        overdue_sum = sum(float(i.get("payment_amount") or 0) for i in overdue)
        due_30 = cash.get("fa_due_30d", [])
        due_30_sum = sum(float(i.get("payment_amount") or 0) for i in due_30)
        parts.append(f"\nCASHFLOW:")
        parts.append(f"- FA po splatnosti: {len(overdue)}× = {overdue_sum:,.0f} €")
        parts.append(f"- FA splatné <30d: {len(due_30)}× = {due_30_sum:,.0f} €")
        if overdue:
            parts.append("  Konkrétne overdue:")
            for o in overdue[:5]:
                parts.append(f"  • {o.get('project_code','?')} {o.get('fa_no','?')} ({o.get('customer_name','?')}) {o.get('payment_amount',0):.0f} €, due {o.get('due_date')}")

    ops = ctx.get("operations", {})
    if ops and ops.get("active_alarms"):
        parts.append(f"\nINVERTORY: {len(ops['active_alarms'])} aktívnych alarmov")
        for a in ops["active_alarms"][:3]:
            parts.append(f"  • [{a.get('severity')}] {a.get('description','?')}")

    mat = ctx.get("material", {})
    if mat and mat.get("low_stock_count", 0) > 0:
        parts.append(f"\nSKLAD: {mat['low_stock_count']} položiek pod minimom")
        for s in mat.get("low_stock_items", [])[:5]:
            parts.append(f"  • {s['name']}: {s['quantity']} ks (min {s['min']})")

    svc = ctx.get("service", {})
    if svc and svc.get("open_count", 0) > 0:
        parts.append(f"\nSERVIS: {svc['open_count']} otvorených ticketov")

    auth = ctx.get("authorities", {})
    if auth and auth.get("pending_requests"):
        parts.append(f"\nÚRADY: {len(auth['pending_requests'])} žiadostí čaká na vybavenie")

    docs = ctx.get("documents", {})
    if docs:
        if docs.get("pending_upload") or docs.get("in_review"):
            parts.append(f"\nDOKUMENTY: {docs.get('pending_upload',0)} čaká upload, {docs.get('in_review',0)} v review")

    notif = ctx.get("notifications", {})
    if notif and notif.get("unread_count", 0) > 0:
        parts.append(f"\nNOTIFIKÁCIE: {notif['unread_count']} neprečítaných")

    tasks = ctx.get("tasks", {})
    if tasks:
        parts.append(f"\nÚLOHY: {tasks.get('total_open',0)} otvorených ({tasks.get('overdue',0)} overdue, {tasks.get('due_this_week',0)} due tento týždeň, {tasks.get('urgent_priority',0)} urgent)")

    ext = ctx.get("external_orders", {})
    if ext:
        parts.append(f"\nEXTERNISTI: {ext.get('draft_count',0)} draftov čaká odoslanie, {ext.get('sent_pending_response',0)} čaká odpoveď")

    meet = ctx.get("meetings", {})
    if meet and meet.get("upcoming"):
        parts.append(f"\nMEETINGY: {len(meet['upcoming'])} nadchádzajúcich do 3 dní")

    insp = ctx.get("inspections", {})
    if insp and insp.get("today_tomorrow"):
        parts.append(f"\nOBHLIADKY: {len(insp['today_tomorrow'])} naplánovaných do 7 dní")

    eva = ctx.get("eva_self", {})
    if eva:
        parts.append(f"\nEVA: {eva.get('active_artifacts',0)} aktívnych artefaktov, {eva.get('memories_count',0)} memories")

    return "\n".join(parts)
