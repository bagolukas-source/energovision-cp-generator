"""PVGIS baseline — stiahnut hodinový profil typického roka pre každú stanicu.

PVGIS API (Európska komisia, JRC):
  https://re.jrc.ec.europa.eu/api/v5_2/PVcalc
  parametre: lat, lon, peakpower=1, loss=14, angle, aspect, mountingplace='building'/'free'
  outputformat=json

Volá sa raz pri pridaní stanice (alebo manuálne, ak zmenia sa parametre strechy).
Výsledok ukladá do pvgis_baseline tabuľky — referenčný teoretický výnos
pre PR (Performance Ratio) výpočty.

CLI:
    python -m ingest.pvgis_baseline --site-id <uuid>
    python -m ingest.pvgis_baseline --backfill-new       # všetky bez baseline
    python -m ingest.pvgis_baseline --refresh-all
"""

from __future__ import annotations

import os
import sys
import logging
import argparse
from typing import Optional

import requests
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pvgis")


PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"


def fetch_pvgis_hourly(
    lat: float,
    lon: float,
    peak_power_kwp: float = 1.0,
    tilt: float = 30.0,
    azimuth: float = 0.0,  # 0 = juh v PVGIS konvencii, -90 = východ, +90 = západ
    loss_pct: float = 14.0,
    start_year: int = 2020,
    end_year: int = 2020,
) -> list[dict]:
    """Stiahne hodinový profil PVGIS pre 1 rok (TMY-podobné).

    Návrat: list dict-ov {hour_of_year, month, day, hour, irradiance_w_m2,
    expected_power_w, ambient_temp_c}
    """
    params = {
        "lat": lat,
        "lon": lon,
        "startyear": start_year,
        "endyear": end_year,
        "pvcalculation": 1,
        "peakpower": peak_power_kwp,
        "loss": loss_pct,
        "angle": tilt,
        "aspect": azimuth,
        "mountingplace": "building",
        "outputformat": "json",
        "browser": 0,
    }
    r = requests.get(PVGIS_URL, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    hourly = data.get("outputs", {}).get("hourly", [])
    out = []
    for h in hourly:
        # PVGIS time formát: "20200101:0010" → YYYYMMDD:HHMM
        time_str = h.get("time", "")
        if len(time_str) < 13:
            continue
        try:
            year = int(time_str[0:4])
            month = int(time_str[4:6])
            day = int(time_str[6:8])
            hour = int(time_str[9:11])
        except ValueError:
            continue
        # hour_of_year (zjednodušené, nezohľadňuje prestupné roky)
        hoy = (month - 1) * 744 + (day - 1) * 24 + hour
        out.append({
            "hour_of_year": hoy,
            "month": month,
            "day_of_month": day,
            "hour_of_day": hour,
            "irradiance_w_m2": float(h.get("G(i)") or 0),
            "expected_power_kw": float(h.get("P") or 0) / 1000.0,  # PVGIS vracia W, normujeme na kW/kWp
            "ambient_temp_c": float(h.get("T2m") or 0),
        })
    return out


def upsert_baseline(site_id: str, lat: float, lon: float, tilt: float, azimuth: float, peak_kwp: float):
    """Stiahne PVGIS baseline a uloží do Supabase."""
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    # PVGIS azimuth konvencia: 0 = juh, ±180 = sever. Naša DB: 0 = sever, 180 = juh.
    pvgis_azimuth = azimuth - 180  # konverzia: north-based → south-based
    log.info(f"Fetching PVGIS for site {site_id} ({lat}, {lon}) tilt={tilt} az={pvgis_azimuth}")
    hourly = fetch_pvgis_hourly(lat, lon, peak_power_kwp=1.0, tilt=tilt, azimuth=pvgis_azimuth)

    if not hourly:
        log.warning(f"PVGIS returned empty for site {site_id}")
        return

    # Najprv vymaž staré baseline pre tento site
    sb.table("pvgis_baseline").delete().eq("site_id", site_id).execute()

    rows = [{"site_id": site_id, **h} for h in hourly]
    # Batch insert po 500 (Supabase limit)
    for i in range(0, len(rows), 500):
        sb.table("pvgis_baseline").insert(rows[i:i + 500]).execute()
    log.info(f"Inserted {len(rows)} PVGIS hourly rows for site {site_id}")


def backfill_new():
    """Stiahne baseline pre všetky stanice ktoré ešte žiadnu nemajú."""
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    sites = sb.table("inverter_sites").select("id, lat, lon, tilt_deg, azimuth_deg, kw_dc_nominal").execute().data or []
    for site in sites:
        if not (site.get("lat") and site.get("lon")):
            continue
        has = sb.table("pvgis_baseline").select("site_id").eq("site_id", site["id"]).limit(1).execute().data
        if has:
            continue
        try:
            upsert_baseline(
                site_id=site["id"],
                lat=float(site["lat"]),
                lon=float(site["lon"]),
                tilt=float(site.get("tilt_deg") or 30),
                azimuth=float(site.get("azimuth_deg") or 180),  # default juh
                peak_kwp=float(site.get("kw_dc_nominal") or 1),
            )
        except Exception as e:
            log.warning(f"PVGIS fail for site {site['id']}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-id")
    parser.add_argument("--backfill-new", action="store_true")
    parser.add_argument("--refresh-all", action="store_true")
    args = parser.parse_args()

    if args.backfill_new:
        backfill_new()
    elif args.site_id:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
        s = sb.table("inverter_sites").select("*").eq("id", args.site_id).single().execute().data
        upsert_baseline(
            site_id=s["id"],
            lat=float(s["lat"]),
            lon=float(s["lon"]),
            tilt=float(s.get("tilt_deg") or 30),
            azimuth=float(s.get("azimuth_deg") or 180),
            peak_kwp=float(s.get("kw_dc_nominal") or 1),
        )
    else:
        parser.error("zadaj --site-id alebo --backfill-new")


if __name__ == "__main__":
    main()
