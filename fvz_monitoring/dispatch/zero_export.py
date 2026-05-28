"""Zero-export breach detector.

Pre stanice s `zero_export_enabled=TRUE` a `zero_export_setpoint` skontroluje
v poslednom posuvnom 15-min okne či sa nedostala energia do siete nad toleranciu.

Logika:
- Pre každú stanicu načítaj posledných 30 min telemetrie
- Spočítaj kumulatívny export za posledných 15 min
- Ak > tolerancia (default 0.05 kWh) cez >= 2 sliding windows po sebe = breach
- Vytvor alarm severity='critical', category='zero_export_breach', auto-route

Spúšťa sa každých 15 min ako cron.
"""

from __future__ import annotations

import os
import logging
import argparse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("zero_export")


TOLERANCE_KWH = float(os.environ.get("ZERO_EXPORT_TOLERANCE_KWH", "0.05"))
CONSECUTIVE_WINDOWS = int(os.environ.get("ZERO_EXPORT_CONSECUTIVE_WINDOWS", "2"))


def get_supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


def check_site(site_id: str, site_name: str, limit_kw: float):
    sb = get_supabase()
    now = datetime.now(timezone.utc)
    window_minutes = 15 * CONSECUTIVE_WINDOWS

    # Načítaj posledných N min telemetrie (15min agregát)
    from_ts = (now - timedelta(minutes=window_minutes + 5)).isoformat()
    res = sb.table("telemetry_15min").select(
        "ts, grid_export_kwh"
    ).eq("site_id", site_id).gte("ts", from_ts).order("ts", desc=True).limit(CONSECUTIVE_WINDOWS).execute().data or []

    if len(res) < CONSECUTIVE_WINDOWS:
        return  # nedostatok dát

    # Všetky N consecutive windows musia presahovať tolerancia
    breaches = [r for r in res if (r.get("grid_export_kwh") or 0) > TOLERANCE_KWH]
    if len(breaches) < CONSECUTIVE_WINDOWS:
        return  # OK

    total_export = sum(r["grid_export_kwh"] for r in breaches)
    log.warning(f"[{site_name}] ZERO-EXPORT BREACH: {total_export:.3f} kWh in last {window_minutes} min (limit {limit_kw} kW)")

    # Skontroluj že nemáme už open alarm tej istej kategórie pre túto stanicu
    existing = sb.table("alarms").select("id").eq("site_id", site_id).eq(
        "category", "zero_export_breach"
    ).is_("resolved_at", "null").limit(1).execute().data
    if existing:
        return  # už máme open alarm — neduplikujeme

    # Vytvor alarm
    sb.table("alarms").insert({
        "site_id": site_id,
        "vendor": "internal",
        "severity": "critical",
        "category": "zero_export_breach",
        "title": f"Zero-export breach: {total_export:.2f} kWh into grid in last {window_minutes} min",
        "description": (
            f"Stanica má deklarovaný zero-export limit {limit_kw} kW, "
            f"ale za posledných {window_minutes} min ide do siete "
            f"{total_export:.2f} kWh ({CONSECUTIVE_WINDOWS} consecutive 15-min okien)."
        ),
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {"total_export_kwh": total_export, "window_minutes": window_minutes},
    }).execute()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-id", help="Skontroluj len jednu stanicu")
    args = parser.parse_args()

    sb = get_supabase()
    q = sb.table("inverter_sites").select(
        "id, site_name, zero_export_setpoint"
    ).eq("zero_export_enabled", True).eq("monitoring_enabled", True)
    if args.site_id:
        q = q.eq("id", args.site_id)
    sites = q.execute().data or []

    log.info(f"Checking zero-export breach for {len(sites)} sites")
    for s in sites:
        try:
            check_site(s["id"], s.get("site_name", "?"), float(s.get("zero_export_setpoint") or 0))
        except Exception as e:
            log.warning(f"Zero-export check fail for {s['id']}: {e}")


if __name__ == "__main__":
    main()
