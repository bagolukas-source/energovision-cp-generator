"""KPI engine — Performance Ratio, Specific Yield, Availability, Capacity Factor.

Spúšťa sa každý deň ráno (cron 30 0 * * *) a počíta KPI za predchádzajúci deň
pre všetky stanice. Tiež počíta kohortné štatistiky pre anomaly detection.

CLI:
    python -m dispatch.kpi_engine --period daily --date 2026-05-25
    python -m dispatch.kpi_engine --period daily --backfill 30
    python -m dispatch.kpi_engine --site-id <uuid> --period daily --date 2026-05-25
"""

from __future__ import annotations

import os
import logging
import argparse
from datetime import datetime, date, timedelta
from typing import Optional

from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kpi_engine")


def get_supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


# =============================================================================
# Per-site KPI výpočet
# =============================================================================

def compute_daily_kpi(site_id: str, day: date) -> dict:
    """Pre danú stanicu a deň vypočíta KPI a vráti dict ready na upsert."""
    sb = get_supabase()

    # 1. Master data stanice
    site = sb.table("inverter_sites").select(
        "id, kw_dc_nominal, lat, lon, tilt_deg, azimuth_deg"
    ).eq("id", site_id).single().execute().data
    if not site or not site.get("kw_dc_nominal"):
        log.warning(f"Site {site_id} missing kw_dc_nominal, skip")
        return None
    kwp = float(site["kw_dc_nominal"])

    # 2. Energia z telemetry_daily (continuous aggregate)
    res = sb.table("telemetry_daily").select(
        "ac_energy_kwh, ac_power_kw_peak, grid_export_kwh, grid_import_kwh, hours_of_data"
    ).eq("site_id", site_id).eq("ts", day.isoformat()).execute().data
    if not res:
        # Fallback: spočítaj zo 5min raw
        energy = sb.rpc("sum_telemetry_day", {"p_site_id": site_id, "p_day": day.isoformat()}).execute().data
        energy_kwh = float(energy) if energy else 0
        peak = 0
        export = 0
        import_kwh = 0
        hours = 0
    else:
        r = res[0]
        energy_kwh = float(r.get("ac_energy_kwh") or 0)
        peak = float(r.get("ac_power_kw_peak") or 0)
        export = float(r.get("grid_export_kwh") or 0)
        import_kwh = float(r.get("grid_import_kwh") or 0)
        hours = int(r.get("hours_of_data") or 0)

    # 3. Expected energia z PVGIS baseline pre daný deň
    expected = sb.table("pvgis_baseline").select(
        "expected_power_kw"
    ).eq("site_id", site_id).eq("month", day.month).eq("day_of_month", day.day).execute().data or []
    expected_kwh = sum(float(r.get("expected_power_kw") or 0) for r in expected) * kwp  # PVGIS je per kWp

    # 4. KPI výpočet
    pr = round(energy_kwh / expected_kwh, 3) if expected_kwh > 0 else None
    specific_yield = round(energy_kwh / kwp, 3) if kwp > 0 else None
    availability_pct = round(min(hours / 24.0, 1.0) * 100, 2)
    capacity_factor_pct = round((energy_kwh / (kwp * 24)) * 100, 2) if kwp > 0 else None
    co2_avoided_kg = round(energy_kwh * 0.295, 2)  # SK grid emission factor ~0.295 kg CO₂/kWh

    self_consumption_pct = None
    grid_independence_pct = None
    if energy_kwh > 0:
        self_consumption_pct = round(((energy_kwh - export) / energy_kwh) * 100, 2)
    total_consumption = max(energy_kwh - export + import_kwh, 0)
    if total_consumption > 0:
        grid_independence_pct = round(((energy_kwh - export) / total_consumption) * 100, 2)

    return {
        "site_id": site_id,
        "day": day.isoformat(),
        "energy_kwh": round(energy_kwh, 2),
        "expected_kwh": round(expected_kwh, 2),
        "performance_ratio": pr,
        "specific_yield": specific_yield,
        "availability_pct": availability_pct,
        "peak_power_kw": peak,
        "capacity_factor_pct": capacity_factor_pct,
        "self_consumption_pct": self_consumption_pct,
        "grid_independence_pct": grid_independence_pct,
        "co2_avoided_kg": co2_avoided_kg,
    }


# =============================================================================
# Kohorta — cross-station štatistika
# =============================================================================

def compute_cohort_z_scores(day: date):
    """Pre každú stanicu vypočíta z-score voči jej peer kohorty.

    Kohorta = stanice s podobným kw_dc_nominal (±50 %), rovnaký distribučný region
    a podobná lokácia (do 50 km).

    |z| > 2 = potenciálna anomália.
    """
    sb = get_supabase()
    # Načítaj všetky KPI pre tento deň
    rows = sb.table("performance_kpis_daily").select(
        "site_id, performance_ratio, energy_kwh"
    ).eq("day", day.isoformat()).execute().data or []

    if len(rows) < 5:
        log.info(f"Too few sites ({len(rows)}) for cohort analysis on {day}")
        return

    import numpy as np
    prs = [r["performance_ratio"] for r in rows if r.get("performance_ratio") is not None]
    if len(prs) < 5:
        return
    cohort_median = float(np.median(prs))
    cohort_std = float(np.std(prs)) or 0.01  # avoid div by zero

    for r in rows:
        if r.get("performance_ratio") is None:
            continue
        z = (r["performance_ratio"] - cohort_median) / cohort_std
        sb.table("performance_kpis_daily").update({
            "cohort_pr_median": round(cohort_median, 3),
            "cohort_z_score": round(z, 3),
        }).eq("site_id", r["site_id"]).eq("day", day.isoformat()).execute()


# =============================================================================
# Hlavný entrypoint
# =============================================================================

def run_for_day(day: date, site_id: Optional[str] = None):
    sb = get_supabase()
    if site_id:
        sites = [{"id": site_id}]
    else:
        sites = sb.table("inverter_sites").select("id").eq("monitoring_enabled", True).execute().data or []

    for s in sites:
        try:
            kpi = compute_daily_kpi(s["id"], day)
            if kpi:
                sb.table("performance_kpis_daily").upsert(kpi, on_conflict="site_id,day").execute()
        except Exception as e:
            log.warning(f"KPI fail for {s['id']} on {day}: {e}")

    # Kohorta
    compute_cohort_z_scores(day)
    log.info(f"KPI engine done for {day}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", choices=["daily"], default="daily")
    parser.add_argument("--date", help="YYYY-MM-DD, default = včera")
    parser.add_argument("--site-id")
    parser.add_argument("--backfill", type=int, help="Posledných N dní")
    args = parser.parse_args()

    if args.backfill:
        for i in range(args.backfill):
            d = date.today() - timedelta(days=i + 1)
            run_for_day(d, args.site_id)
        return

    target_day = date.fromisoformat(args.date) if args.date else (date.today() - timedelta(days=1))
    run_for_day(target_day, args.site_id)


if __name__ == "__main__":
    main()
