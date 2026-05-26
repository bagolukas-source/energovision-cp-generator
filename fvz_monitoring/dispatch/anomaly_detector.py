"""Anomaly detector — pravidlové, kohortné a ML-based.

3 vrstvy:
1. Rule-based (jednoduché prahy)            — beží v zero_export.py
2. Cohort-based (z-score voči peer group)   — beží v kpi_engine.compute_cohort_z_scores
3. ML classifier (gradient boosting)        — beží tu

Tento modul rieši vrstvu 3:
- Vstupy: 30-dňové okno KPI + telemetry features per stanica
- Výstup: predikcia "underperformance", "string_fault", "imminent_failure"
  s confidence skóre

Prvá verzia je STUB — model trénovaný neskôr keď máme dostatok labelovaných
incidentov z históriou (cca 6 mesiacov produkcie).

CLI:
    python -m dispatch.anomaly_detector --mode cohort --date 2026-05-25
    python -m dispatch.anomaly_detector --mode ml --site-id <uuid>
"""

from __future__ import annotations

import os
import logging
import argparse
from datetime import datetime, date, timedelta, timezone

from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("anomaly_detector")


def get_supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


# =============================================================================
# Vrstva 2: Cohort outlier alarming
# =============================================================================

def flag_cohort_outliers(day: date, z_threshold: float = 2.0):
    """Po výpočte z-score (kpi_engine) vytvor alarm pre stanice s |z| > threshold."""
    sb = get_supabase()
    res = sb.table("performance_kpis_daily").select(
        "site_id, cohort_z_score, performance_ratio, cohort_pr_median"
    ).eq("day", day.isoformat()).execute().data or []

    for r in res:
        z = r.get("cohort_z_score")
        if z is None or abs(z) < z_threshold:
            continue
        # Skontroluj že nemáme dnes už alarm tej istej kategórie
        existing = sb.table("alarms").select("id").eq(
            "site_id", r["site_id"]
        ).eq("category", "cohort_outlier").gte(
            "detected_at", day.isoformat()
        ).limit(1).execute().data
        if existing:
            continue

        severity = "major" if abs(z) > 3 else "minor"
        sb.table("alarms").insert({
            "site_id": r["site_id"],
            "vendor": "internal",
            "severity": severity,
            "category": "cohort_outlier",
            "title": f"Underperformance vs. peer cohort (z={z:.2f})",
            "description": (
                f"Stanica má PR={r['performance_ratio']:.3f}, "
                f"medián kohorty {r['cohort_pr_median']:.3f}, "
                f"z-score {z:.2f}. Skontroluj tienenie, znečistenie, string-fault."
            ),
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "root_cause_confidence": min(abs(z) / 5.0, 1.0),
            "metadata": {"z_score": z, "pr": r["performance_ratio"], "cohort_median": r["cohort_pr_median"]},
        }).execute()
        log.info(f"Cohort outlier alarm for site {r['site_id']} (z={z:.2f})")


# =============================================================================
# Vrstva 3: ML classifier — STUB
# =============================================================================

def run_ml_classifier(site_id: str):
    """STUB: po nazbieraní 6+ mesiacov dát nahradiť trénovaným modelom.

    Plánovaný flow:
    1. Načítaj posledných 30 dní telemetrie + KPI pre stanicu
    2. Vyextrahuj features: trend PR, var(power), MPPT imbalance, temp delta
    3. Aplikuj trénovaný model → predikcia + confidence
    4. Ulož do anomaly_predictions tabuľky
    5. Ak confidence > 0.7 a prediction != 'normal', vytvor alarm

    V1 (teraz): len pravidlové heuristiky.
    """
    sb = get_supabase()
    # Heuristika: ak 7-dňový PR klesol o > 15 % oproti predchádzajúcemu 7-dňovému
    # bez explainácie zo strany kohorty (kohorta je OK) → string-fault / soiling
    today = date.today()
    week_ago = today - timedelta(days=7)
    two_weeks_ago = today - timedelta(days=14)

    recent = sb.table("performance_kpis_daily").select("performance_ratio").eq(
        "site_id", site_id
    ).gte("day", week_ago.isoformat()).lt("day", today.isoformat()).execute().data or []
    previous = sb.table("performance_kpis_daily").select("performance_ratio").eq(
        "site_id", site_id
    ).gte("day", two_weeks_ago.isoformat()).lt("day", week_ago.isoformat()).execute().data or []

    if len(recent) < 5 or len(previous) < 5:
        return
    avg_recent = sum(r["performance_ratio"] or 0 for r in recent) / len(recent)
    avg_previous = sum(r["performance_ratio"] or 0 for r in previous) / len(previous)
    if avg_previous == 0:
        return
    drop_pct = (avg_previous - avg_recent) / avg_previous * 100
    if drop_pct >= 15:
        sb.table("anomaly_predictions").insert({
            "site_id": site_id,
            "predicted_at": datetime.now(timezone.utc).isoformat(),
            "model_version": "heuristic_v1_pr_drop",
            "prediction_type": "underperformance",
            "confidence": min(drop_pct / 30, 1.0),
            "classification": "trend_decline",
            "recommendation": "Skontroluj string-fault, znečistenie panelov, novo vzniknuté tienenie.",
            "features": {"avg_pr_recent": avg_recent, "avg_pr_previous": avg_previous, "drop_pct": drop_pct},
        }).execute()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["cohort", "ml"], required=True)
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--site-id")
    args = parser.parse_args()

    if args.mode == "cohort":
        target_day = date.fromisoformat(args.date) if args.date else (date.today() - timedelta(days=1))
        flag_cohort_outliers(target_day)
    elif args.mode == "ml":
        sb = get_supabase()
        if args.site_id:
            sites = [{"id": args.site_id}]
        else:
            sites = sb.table("inverter_sites").select("id").eq("monitoring_active", True).execute().data or []
        for s in sites:
            try:
                run_ml_classifier(s["id"])
            except Exception as e:
                log.warning(f"ML classifier fail for {s['id']}: {e}")


if __name__ == "__main__":
    main()
