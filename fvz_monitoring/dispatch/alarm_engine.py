"""Alarm engine — deduplikuje, klasifikuje, routuje alarmy podľa pravidiel.

Pravidlá rutovania sa berú zo Supabase tabuľky alarm_routing_rules (existuje).
Korelácia: ak >= N staníc rovnakého vendora hodí ten istý alarm category v
krátkom okne, je to vendor-wide incident → dedup do 1 master alarmu.

CLI:
    python -m dispatch.alarm_engine --dedup
    python -m dispatch.alarm_engine --route-open
"""

from __future__ import annotations

import os
import logging
import argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("alarm_engine")


CORRELATION_WINDOW_MIN = 10        # ak >= N alarmov za N min = korelovaný incident
CORRELATION_THRESHOLD = 5          # >= 5 staníc s rovnakou kategóriou = vendor-wide


def get_supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


# =============================================================================
# Deduplikácia
# =============================================================================

def dedupe_correlated():
    """Detekuje cross-station incidenty a označí ich ako duplikáty 1 master alarmu.

    Pravidlo: >= 5 alarmov rovnakého (vendor, category) v poslednom 10-min okne
    → vyber najnovší ako master, ostatné označ deduplicated_into.
    """
    sb = get_supabase()
    since = (datetime.now(timezone.utc) - timedelta(minutes=CORRELATION_WINDOW_MIN)).isoformat()

    res = sb.table("alarms").select(
        "id, vendor, category, site_id, detected_at"
    ).is_("resolved_at", "null").is_("deduplicated_into", "null").gte("detected_at", since).execute().data or []

    # Group by (vendor, category)
    groups = defaultdict(list)
    for a in res:
        groups[(a["vendor"], a["category"])].append(a)

    for (vendor, category), alarms in groups.items():
        if len(alarms) < CORRELATION_THRESHOLD:
            continue
        # Najnovší = master
        alarms_sorted = sorted(alarms, key=lambda x: x["detected_at"], reverse=True)
        master = alarms_sorted[0]
        children = [a for a in alarms_sorted[1:]]
        log.info(f"Correlated incident: {vendor}/{category} — {len(alarms)} alarms, master={master['id']}")

        for child in children:
            sb.table("alarms").update({"deduplicated_into": master["id"]}).eq("id", child["id"]).execute()

        # Master získa updated title
        sb.table("alarms").update({
            "title": f"[INCIDENT] {category} on {len(alarms)} stations of {vendor}",
            "severity": "major",  # eskalácia
            "metadata": {"correlated_count": len(alarms), "child_ids": [c["id"] for c in children]},
        }).eq("id", master["id"]).execute()


# =============================================================================
# Routing
# =============================================================================

def route_open_alarms():
    """Pre každý open alarm bez assigned_to aplikuje routing pravidlá."""
    sb = get_supabase()
    rules = sb.table("alarm_routing_rules").select("*").order("priority").execute().data or []
    open_alarms = sb.table("alarms").select(
        "id, site_id, severity, category, vendor"
    ).is_("resolved_at", "null").is_("assigned_to", "null").is_("deduplicated_into", "null").execute().data or []

    for alarm in open_alarms:
        for rule in rules:
            if _rule_matches(rule, alarm):
                sb.table("alarms").update({
                    "assigned_to": rule.get("assignee_user_id"),
                    "auto_actions_taken": (alarm.get("auto_actions_taken") or []) + [{
                        "action": "routed",
                        "rule_id": rule.get("id"),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }],
                }).eq("id", alarm["id"]).execute()
                log.info(f"Routed alarm {alarm['id']} via rule {rule.get('id')}")
                break


def _rule_matches(rule: dict, alarm: dict) -> bool:
    """Hrubá implementácia matchu — neskôr expand podľa Supabase schémy."""
    if rule.get("severity_min"):
        sev_order = ["info", "warn", "minor", "major", "critical"]
        if sev_order.index(alarm["severity"]) < sev_order.index(rule["severity_min"]):
            return False
    if rule.get("category") and rule["category"] != alarm["category"]:
        return False
    if rule.get("vendor") and rule["vendor"] != alarm["vendor"]:
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dedup", action="store_true")
    parser.add_argument("--route-open", action="store_true")
    args = parser.parse_args()

    if args.dedup:
        dedupe_correlated()
    if args.route_open:
        route_open_alarms()
    if not (args.dedup or args.route_open):
        # Default = oboje
        dedupe_correlated()
        route_open_alarms()


if __name__ == "__main__":
    main()
