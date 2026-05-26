"""Ingest orchestrátor — spúšťa všetkých vendor adaptérov ako cron job.

CLI:
    python -m ingest.orchestrator --vendor huawei  --action realtime
    python -m ingest.orchestrator --vendor solinteg --action plant-list
    python -m ingest.orchestrator --all-vendors --action realtime
    python -m ingest.orchestrator --all-vendors --action daily-summary --date 2026-05-10
    python -m ingest.orchestrator --all-vendors --action alarms
    python -m ingest.orchestrator --health-check

Použitie z Renderu (cron):
    */5 * * * *   python -m ingest.orchestrator --all-vendors --action realtime
    */5 * * * *   python -m ingest.orchestrator --all-vendors --action alarms
    15 1 * * *    python -m ingest.orchestrator --all-vendors --action daily-summary
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

from .base import VendorAdapter
from .canonical import PlantInfo, TelemetrySnapshot, DailySummary, VendorAlarm
from .vendor_huawei import HuaweiAdapter
from .vendor_solinteg import SolintegAdapter
from .vendor_goodwe import GoodWeAdapter
from .vendor_fronius import FroniusAdapter
from .vendor_sungrow import SungrowAdapter


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("orchestrator")


# =============================================================================
# Vendor registry
# =============================================================================

VENDORS = {
    "huawei":   (HuaweiAdapter,   {
        "username": "HUAWEI_USERNAME", "password": "HUAWEI_PASS",
    }),
    "solinteg": (SolintegAdapter, {
        "username": "SOLINTEG_USERNAME", "password": "SOLINTEG_PASS",
        "api_key": "SOLINTEG_APP_KEY", "api_secret": "SOLINTEG_APP_SECRET",
    }),
    "goodwe":   (GoodWeAdapter,   {
        "username": "GOODWE_USERNAME", "password": "GOODWE_PASS",
    }),
    "fronius":  (FroniusAdapter,  {
        "api_key": "FRONIUS_CLIENT_ID", "api_secret": "FRONIUS_CLIENT_SECRET",
        "access_key_id": "FRONIUS_ACCESS_KEY_ID", "access_key_value": "FRONIUS_ACCESS_KEY_VALUE",
    }),
    "sungrow":  (SungrowAdapter,  {
        "username": "SUNGROW_USERNAME", "password": "SUNGROW_PASS",
        "api_key": "SUNGROW_APP_KEY",
    }),
}


def is_vendor_enabled(vendor: str) -> bool:
    return os.environ.get(f"VENDOR_{vendor.upper()}_ENABLED", "true").lower() == "true"


def make_adapter(vendor: str, credentials_loader=None) -> VendorAdapter:
    """Vytvorí adapter inštanciu z env premenných."""
    if vendor not in VENDORS:
        raise ValueError(f"Unknown vendor: {vendor}")
    AdapterCls, env_map = VENDORS[vendor]
    kwargs = {}
    for param, env_name in env_map.items():
        v = os.environ.get(env_name)
        if v:
            kwargs[param] = v
    if credentials_loader:
        kwargs["credentials_loader"] = credentials_loader
    return AdapterCls(**kwargs)


# =============================================================================
# Supabase persistence layer
# =============================================================================

def get_supabase():
    """Vráti Supabase klienta. Lazy import lebo nie každý CLI mode ho potrebuje."""
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def supabase_credentials_loader(vendor: str) -> dict:
    """Načíta credentials zo Supabase inverter_vendor_credentials tabuľky.

    Reálne stĺpce: vendor, base_url, username, encrypted_password, client_id,
    encrypted_client_secret, current_token, ...
    Mapujeme ich na názvy ktoré base.py očakáva (password, api_secret).
    """
    try:
        sb = get_supabase()
        res = sb.table("inverter_vendor_credentials").select(
            "vendor,base_url,username,encrypted_password,client_id,encrypted_client_secret,is_active"
        ).eq("vendor", vendor).eq("is_active", True).limit(1).execute()
        if res.data:
            r = res.data[0]
            return {
                "username": r.get("username"),
                "password": r.get("encrypted_password"),  # stĺpec sa volá _encrypted_ ale obsahuje plain text (Huawei systemCode)
                "api_key": r.get("client_id"),
                "api_secret": r.get("encrypted_client_secret"),
                "base_url": r.get("base_url"),
            }
    except Exception as e:
        log.warning(f"Failed to load credentials for {vendor} from Supabase: {e}")
    return {}


def upsert_plants(plants: list[PlantInfo]):
    """Upsert plant list do inverter_sites tabuľky cez (vendor, vendor_plant_code) unique key."""
    if not plants:
        return
    sb = get_supabase()
    rows = []
    for p in plants:
        rows.append({
            "vendor": p.vendor,
            "vendor_plant_code": p.vendor_plant_code,
            "site_name": p.site_name,
            "kw_dc_nominal": p.kw_dc_nominal,
            "kw_ac_nominal": p.kw_ac_nominal,
            "battery_kwh_nominal": p.battery_kwh_nominal,
            "lat": p.lat,
            "lon": p.lon,
            "address": p.address,
            "commissioning_date": p.commissioning_date.isoformat() if p.commissioning_date else None,
            "monitoring_active": True,
        })
    sb.table("inverter_sites").upsert(rows, on_conflict="vendor,vendor_plant_code").execute()
    log.info(f"Upserted {len(rows)} plants")


def resolve_site_ids(vendor: str, vendor_plant_codes: list[str]) -> dict[str, str]:
    """Z vendor_plant_code → internal site_id (uuid)."""
    if not vendor_plant_codes:
        return {}
    sb = get_supabase()
    res = (
        sb.table("inverter_sites")
        .select("id, vendor_plant_code")
        .eq("vendor", vendor)
        .in_("vendor_plant_code", vendor_plant_codes)
        .execute()
    )
    return {row["vendor_plant_code"]: row["id"] for row in res.data or []}


def insert_telemetry(vendor: str, snapshots: list[TelemetrySnapshot]):
    if not snapshots:
        return
    sb = get_supabase()
    plant_ids = [s.vendor_plant_code for s in snapshots]
    id_map = resolve_site_ids(vendor, plant_ids)
    rows = []
    skipped = 0
    for s in snapshots:
        site_id = id_map.get(s.vendor_plant_code)
        if not site_id:
            skipped += 1
            continue
        row = s.to_db_row(site_id)
        row["ts"] = s.ts.isoformat()
        rows.append(row)
    if rows:
        # Upsert na (site_id, ts) — pri rovnakom timestamp prepíše
        sb.table("telemetry_5min").upsert(rows, on_conflict="site_id,ts").execute()
        # Update last_seen
        for sid in {r["site_id"] for r in rows}:
            sb.table("inverter_sites").update({"last_seen_at": datetime.now(timezone.utc).isoformat()}).eq("id", sid).execute()
    log.info(f"[{vendor}] inserted {len(rows)} telemetry rows, skipped {skipped} unknown plants")


def insert_alarms(vendor: str, alarms: list[VendorAlarm]):
    if not alarms:
        return
    sb = get_supabase()
    plant_ids = [a.vendor_plant_code for a in alarms]
    id_map = resolve_site_ids(vendor, plant_ids)
    rows = []
    for a in alarms:
        site_id = id_map.get(a.vendor_plant_code)
        rows.append({
            "site_id": site_id,
            "vendor": a.vendor,
            "vendor_alarm_id": a.vendor_alarm_id,
            "severity": a.severity,
            "category": a.category,
            "title": a.title,
            "description": a.description,
            "detected_at": a.detected_at.isoformat() if a.detected_at else datetime.now(timezone.utc).isoformat(),
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "metadata": a.raw,
        })
    sb.table("alarms").upsert(rows, on_conflict="vendor,vendor_alarm_id").execute()
    log.info(f"[{vendor}] upserted {len(rows)} alarms")


def insert_daily_summary(vendor: str, summary: DailySummary):
    if not summary:
        return
    sb = get_supabase()
    id_map = resolve_site_ids(vendor, [summary.vendor_plant_code])
    site_id = id_map.get(summary.vendor_plant_code)
    if not site_id:
        return
    sb.table("performance_kpis_daily").upsert({
        "site_id": site_id,
        "day": summary.day.isoformat(),
        "energy_kwh": summary.energy_kwh,
        "peak_power_kw": summary.peak_power_kw,
        # PR a expected_kwh sa počítajú v kpi_engine.py post-ingest
    }, on_conflict="site_id,day").execute()


# =============================================================================
# Akcie
# =============================================================================

def run_plant_list(adapter: VendorAdapter):
    plants = adapter.fetch_plant_list()
    log.info(f"[{adapter.vendor}] fetched {len(plants)} plants")
    upsert_plants(plants)
    return plants


def run_realtime(adapter: VendorAdapter):
    plants = adapter.fetch_plant_list()
    plant_ids = [p.vendor_plant_code for p in plants]
    snapshots = adapter.fetch_realtime_batch(plant_ids)
    log.info(f"[{adapter.vendor}] {len(snapshots)} realtime snapshots")
    insert_telemetry(adapter.vendor, snapshots)


def run_alarms(adapter: VendorAdapter, since_hours: int = 24):
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    alarms = adapter.fetch_alarms(since=since)
    log.info(f"[{adapter.vendor}] {len(alarms)} alarms")
    insert_alarms(adapter.vendor, alarms)


def run_daily_summary(adapter: VendorAdapter, day: date):
    plants = adapter.fetch_plant_list()
    count = 0
    for p in plants:
        try:
            summary = adapter.fetch_daily_summary(p.vendor_plant_code, day)
            if summary:
                insert_daily_summary(adapter.vendor, summary)
                count += 1
        except Exception as e:
            log.warning(f"[{adapter.vendor}] daily summary fail {p.vendor_plant_code}: {e}")
    log.info(f"[{adapter.vendor}] {count} daily summaries for {day}")


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", choices=list(VENDORS.keys()))
    parser.add_argument("--all-vendors", action="store_true")
    parser.add_argument("--action", choices=["plant-list", "realtime", "daily-summary", "alarms", "health-check"], required=False)
    parser.add_argument("--date", help="YYYY-MM-DD pre daily-summary, default = včera")
    parser.add_argument("--health-check", action="store_true")
    args = parser.parse_args()

    if args.health_check:
        for vendor in VENDORS:
            if not is_vendor_enabled(vendor):
                print(json.dumps({"vendor": vendor, "ok": False, "reason": "disabled"}))
                continue
            adapter = make_adapter(vendor, credentials_loader=supabase_credentials_loader)
            print(json.dumps(adapter.health_check()))
        return

    target_vendors = list(VENDORS.keys()) if args.all_vendors else [args.vendor]
    if not target_vendors[0]:
        parser.error("musí byť --vendor X alebo --all-vendors")

    target_day = date.fromisoformat(args.date) if args.date else (date.today() - timedelta(days=1))

    for vendor in target_vendors:
        if not is_vendor_enabled(vendor):
            log.info(f"[{vendor}] disabled (VENDOR_{vendor.upper()}_ENABLED=false), skipping")
            continue
        log.info(f"[{vendor}] action={args.action}")
        try:
            adapter = make_adapter(vendor, credentials_loader=supabase_credentials_loader)
            if args.action == "plant-list":
                run_plant_list(adapter)
            elif args.action == "realtime":
                run_realtime(adapter)
            elif args.action == "alarms":
                run_alarms(adapter)
            elif args.action == "daily-summary":
                run_daily_summary(adapter, target_day)
        except Exception as e:
            log.exception(f"[{vendor}] failed: {e}")


if __name__ == "__main__":
    main()
