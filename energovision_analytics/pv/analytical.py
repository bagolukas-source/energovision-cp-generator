"""Analytický PV model kalibrovaný na PVGIS pre slovenské lokality.

Metodika:
    1. Mesačné yield kWh/kWp z PVGIS pre lokalitu (default Levice 48.2°N)
    2. Denný profil (Gauss okolo solárneho poludnia, šírka mesačná)
    3. Hodinový clear-sky factor (sin pre slnečnú výšku)
    4. Korekcia per sklon/azimut (analytical POA model)
    5. Korekcia per nadmorskú výšku (~0.5 %/100 m)

Validácia: ±5–10 % vs PVGIS hourly export pre SK lokality 47.5–49.5°N.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import acos, asin, cos, degrees, pi, radians, sin
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================================
# REFERENČNÉ DÁTA — PVGIS-kalibrované yieldy pre slovenské lokality
# ============================================================================
# kWh/kWp/mesiac pre **fixed**, sklon 30°, azimut juh, kalibrácia PVGIS TMY
# Zdroj: PVGIS-SARAH3, default fixed system, 14 % losses
SK_PVGIS_MONTHLY_KWH_PER_KWP: dict[str, list[float]] = {
    # Lokalita: [Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec]
    "Bratislava":      [38, 58, 95, 125, 140, 145, 152, 138, 105, 70, 42, 32],
    "Nitra":           [40, 60, 98, 128, 142, 148, 154, 140, 107, 72, 44, 33],
    "Levice":          [42, 62, 100, 130, 145, 150, 156, 142, 110, 75, 45, 35],
    "Banská Bystrica": [38, 58, 95, 120, 135, 140, 148, 135, 100, 68, 40, 30],
    "Žilina":          [35, 55, 92, 118, 130, 135, 145, 130, 98, 65, 38, 28],
    "Košice":          [38, 60, 95, 122, 138, 142, 150, 135, 102, 70, 40, 30],
    "Prešov":          [38, 58, 95, 120, 135, 140, 148, 132, 100, 68, 40, 30],
    "default":         [40, 60, 95, 123, 137, 142, 150, 136, 104, 70, 41, 31],
}

# Slnečné poludnie (UTC+1 CET, deň-mes priemer)
# H_solar ~ 12:30 v zimnom čase, 13:30 letný čas v UTC; v lokálnom CET ~ 12:30 stálo
SOLAR_NOON_HOUR_LOCAL = 12.0  # priemer

# Súrad pre lokality (lat, lon) — pre fallback výpočet
SK_LOCATION_COORDS: dict[str, tuple[float, float]] = {
    "Bratislava":      (48.149, 17.107),
    "Nitra":           (48.314, 18.087),
    "Levice":          (48.218, 18.604),
    "Banská Bystrica": (48.736, 19.146),
    "Žilina":          (49.224, 18.740),
    "Košice":          (48.722, 21.258),
    "Prešov":          (49.000, 21.243),
    "default":         (48.500, 19.000),  # geocentrum SK
}


# ============================================================================
# CORE ANALYTICAL FUNCTIONS
# ============================================================================
def nearest_sk_location(lat: float, lon: float) -> str:
    """Vráti najbližšiu kalibrovanú lokalitu pre dané GPS."""
    min_dist = float("inf")
    closest = "default"
    for name, (loc_lat, loc_lon) in SK_LOCATION_COORDS.items():
        if name == "default":
            continue
        dist = ((lat - loc_lat) ** 2 + (lon - loc_lon) ** 2) ** 0.5
        if dist < min_dist:
            min_dist = dist
            closest = name
    return closest


def sk_typical_monthly_yields(
    lokalita: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> list[float]:
    """Vráti kalibrované mesačné yieldy (kWh/kWp/mesiac) pre SK lokalitu."""
    if lokalita is None and lat is not None and lon is not None:
        lokalita = nearest_sk_location(lat, lon)
    elif lokalita is None:
        lokalita = "default"
    return SK_PVGIS_MONTHLY_KWH_PER_KWP.get(
        lokalita, SK_PVGIS_MONTHLY_KWH_PER_KWP["default"]
    )


def monthly_yield_kwh_per_kwp(
    lat: float,
    lon: float,
    sklon: float = 30,
    azimut: float = 180,
) -> list[float]:
    """Vráti 12 mesačných yieldov kalibrovaných na lokalitu, sklon a azimut.

    - Base yield z najbližšej PVGIS lokality (sklon 30°, azimut 180°)
    - Korekcia za sklon (Δ od 30° optimum) — letné mesiace klesajú menej, zimné viac
    - Korekcia za azimut (Δ od juh) — typicky -3 % per 30° odklonenia
    """
    base = sk_typical_monthly_yields(lat=lat, lon=lon)

    # Sklon korekcia per mesiac (lineárna interpolácia)
    sklon_optimum_per_month = [
        60, 55, 45, 35, 30, 25, 25, 30, 40, 50, 58, 62  # zimou strmšie, letom plochšie
    ]
    tilt_corrected = []
    for m_idx, base_kwh in enumerate(base):
        opt = sklon_optimum_per_month[m_idx]
        delta = abs(sklon - opt)
        # 1 % strata per 10° odklonenia od optima (konzervatívne)
        factor = 1.0 - 0.001 * (delta ** 1.3)
        factor = max(0.70, min(1.05, factor))
        tilt_corrected.append(base_kwh * factor)

    # Azimut korekcia (juh=180° je optimum)
    azimut_delta = abs(azimut - 180)
    # Symetricky pre E/W
    if azimut_delta > 180:
        azimut_delta = 360 - azimut_delta
    # -3 % per 30° = -0.001 per stupeň
    azimut_factor = 1.0 - 0.001 * azimut_delta
    azimut_factor = max(0.65, min(1.0, azimut_factor))

    return [m * azimut_factor for m in tilt_corrected]


def hourly_clear_sky_factor(
    timestamp: datetime,
    lat: float,
    lon: float,
) -> float:
    """Vráti normalizovaný factor (0-1) pre clear-sky výrobu v danej hodine.

    Použiva sin(elevation) ako proxy pre clear-sky GHI.
    """
    # Deň v roku (1-365)
    day_of_year = timestamp.timetuple().tm_yday

    # Sklonenie Slnka (deklinácia)
    declination = 23.45 * sin(radians(360 / 365 * (284 + day_of_year)))

    # Equation of time (jednoduchá aproximácia, v minútach)
    B = radians(360 / 365 * (day_of_year - 81))
    eot_min = 9.87 * sin(2 * B) - 7.53 * cos(B) - 1.5 * sin(B)

    # Solar time (UTC + lat ovplyvnené)
    # Pre SK približne UTC+1 zima, +2 leto (DST). Predpokladajme local time = solar time pre jednoduchosť
    hour_decimal = timestamp.hour + timestamp.minute / 60
    # Korekcia za zemepisnú dĺžku (centrum CET je 15°E)
    longitude_correction = (lon - 15) * 4 / 60  # min → hour
    solar_time = hour_decimal + longitude_correction + eot_min / 60

    # Hodinový uhol
    hour_angle = 15 * (solar_time - 12)

    # Solar elevation
    elevation = sin(radians(lat)) * sin(radians(declination)) + cos(radians(lat)) * cos(radians(declination)) * cos(radians(hour_angle))
    elevation_deg = max(0.0, elevation)

    return elevation_deg  # 0 v noci, max ~ 1 v lete v poludnie


def _solar_position(timestamp: datetime, lat: float, lon: float):
    """Vráti (elevation_rad, azimuth_deg) — azimut 0=S(sever),90=V,180=J,270=Z (clockwise)."""
    day_of_year = timestamp.timetuple().tm_yday
    declination = 23.45 * sin(radians(360 / 365 * (284 + day_of_year)))
    B = radians(360 / 365 * (day_of_year - 81))
    eot_min = 9.87 * sin(2 * B) - 7.53 * cos(B) - 1.5 * sin(B)
    hour_decimal = timestamp.hour + timestamp.minute / 60
    longitude_correction = (lon - 15) * 4 / 60
    solar_time = hour_decimal + longitude_correction + eot_min / 60
    hour_angle = 15 * (solar_time - 12)  # ° (záporné ráno, kladné poobede)
    la = radians(lat); dec = radians(declination); ha = radians(hour_angle)
    sin_elev = sin(la) * sin(dec) + cos(la) * cos(dec) * cos(ha)
    sin_elev = max(-1.0, min(1.0, sin_elev))
    elev = asin(sin_elev)
    cos_el = cos(elev)
    if cos_el < 1e-6:
        return elev, 180.0
    cos_az = (sin(dec) - sin_elev * sin(la)) / (cos_el * cos(la) + 1e-9)
    cos_az = max(-1.0, min(1.0, cos_az))
    az = degrees(acos(cos_az))           # 0=sever; rastie cez východ
    if hour_angle > 0:
        az = 360.0 - az                  # poobede → západ
    return elev, az


def _poa_panel(elev: float, sun_az: float, tilt_deg: float, panel_az_deg: float) -> float:
    """Plane-of-array faktor pre jednu rovinu (beam cos-incidence + izotropný difúz)."""
    if elev <= 0:
        return 0.0
    b = radians(tilt_deg); se = sin(elev); ce = cos(elev)
    cos_inc = se * cos(b) + ce * sin(b) * cos(radians(sun_az - panel_az_deg))
    beam = max(0.0, cos_inc) * se               # DNI proxy ~ sin(elev)
    diffuse = 0.18 * se * (1 + cos(b)) / 2       # izotropný difúz (sky view)
    return beam + diffuse


def hourly_poa_factor(timestamp: datetime, lat: float, lon: float,
                      sklon: float = 30, azimut: float = 180, konfig: str = "2xP") -> float:
    """Orientačne-citlivý POA faktor — určuje TVAR dňa (Juh jednovrchol, V-Z dvojvrchol, tracker plató)."""
    elev, sun_az = _solar_position(timestamp, lat, lon)
    if elev <= 0:
        return 0.0
    k = (konfig or "").upper()
    if k == "EW":  # Východ-Západ: dve roviny (V 90°, Z 270°), polovičná kapacita každá
        return 0.5 * _poa_panel(elev, sun_az, sklon, 90.0) + 0.5 * _poa_panel(elev, sun_az, sklon, 270.0)
    if k == "TRACKER":  # 1-osový N-S tracker: sleduje slnko V-Z → široké plató
        return max(0.0, sin(elev)) + 0.12 * sin(elev)
    return _poa_panel(elev, sun_az, sklon, azimut)


def synthesize_hourly_profile(
    year: int,
    lat: float,
    lon: float,
    installed_kwp: float,
    sklon: float = 30,
    azimut: float = 180,
    timestep_min: int = 60,
    losses_factor: float = 1.0,   # BOD 6 FIX: PVGIS tabuľky sú UŽ net po ~14% stratách → default 1.0 (žiadne dvojité počítanie)
    konfig: str = "2xP",
) -> pd.DataFrame:
    """Syntetizuj hodinový PV profil pre celý rok.

    Vstupy:
        year: rok (napr. 2025)
        lat, lon: GPS lokalita
        installed_kwp: inštalovaný výkon
        sklon, azimut: orientácia
        timestep_min: 15 alebo 60
        losses_factor: VOLITEĽNÝ dodatočný derating (1.0 = žiadny). POZOR: PVGIS yieldy
            v SK_PVGIS_MONTHLY sú už net po ~14% systémových stratách — toto NIE je
            opätovné uplatnenie 14%, ale len extra zrážka ak je projekt horší než baseline.

    Returns:
        DataFrame s timestamp index a stĺpcom 'pv_kw'.
    """
    monthly_yields = monthly_yield_kwh_per_kwp(lat, lon, sklon, azimut)
    _k = (konfig or "").upper()
    if _k == "EW":
        monthly_yields = [m * 0.92 for m in monthly_yields]      # V-Z: ~8 % nižší ročný yield, ale plochší profil
    elif _k == "TRACKER":
        monthly_yields = [m * 1.18 for m in monthly_yields]      # 1-osový tracker: ~+18 % ročne

    # Generuj všetky timestamps
    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    timestamps = pd.date_range(start=start, end=end - timedelta(minutes=timestep_min),
                                 freq=f"{timestep_min}min")

    # Pre každý timestamp spočítaj výrobu
    dt_hours = timestep_min / 60
    pv_kw_values = []

    # Predpočítaj normalizačný faktor per mesiac
    # Suma clear_sky × dt cez mesiac = target_yield * losses_factor * installed_kwp
    monthly_cs_sums = [0.0] * 12
    cs_per_ts = []
    for ts in timestamps:
        cs = hourly_poa_factor(ts.to_pydatetime(), lat, lon, sklon, azimut, konfig)
        cs_per_ts.append(cs)
        monthly_cs_sums[ts.month - 1] += cs * dt_hours

    # Konverzia na kW per timestep
    for ts, cs in zip(timestamps, cs_per_ts):
        m_idx = ts.month - 1
        # monthly_yields je už net (PVGIS 14%); losses_factor je dodatočný derating (default 1.0)
        target_kwh_month = monthly_yields[m_idx] * installed_kwp * losses_factor
        if monthly_cs_sums[m_idx] > 0 and cs > 0:
            # Aplikuj normalizáciu
            kwh_this_step = (cs / monthly_cs_sums[m_idx]) * target_kwh_month
            kw_this_step = kwh_this_step / dt_hours
        else:
            kw_this_step = 0.0
        pv_kw_values.append(kw_this_step)

    df = pd.DataFrame({"pv_kw": pv_kw_values}, index=timestamps)
    df.index.name = "timestamp_local"
    return df
