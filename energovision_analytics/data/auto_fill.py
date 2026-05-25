"""Auto-fill — predvyplnenie SiteInput z PSČ + CSV + minimálnych vstupov.

Funkcie:
    psc_to_distribuutor(psc) → SSE / ZSD / VSD
    psc_to_gps(psc) → (lat, lon, lokalita)
    auto_fill_site(psc, rocna_spotreba_kwh, rk_kw, mrk_kw, ...) → SiteInput
    load_profile_from_csv(path) → LoadProfileInput (tolerantný parser)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from energovision_analytics.core.exceptions import DataIngestionError
from energovision_analytics.core.models import (
    Distribuutor, LoadProfileInput, Sadzba, SiteInput, TypTarify,
)


# ============================================================================
# SK PSČ → DISTRIBÚTOR mapping (zjednodušený, presný pre väčšinu obcí)
# ============================================================================
def psc_to_distribuutor(psc: str) -> Distribuutor:
    """Vráti distribútora podľa SK PSČ.

    Mapping podľa prvých 2-3 číslic (zjednodušené):
        ZSD: Bratislavský, Trnavský, Trenčiansky, Nitriansky kraj
        SSE: Žilinský, Banskobystrický kraj
        VSD: Košický, Prešovský kraj
    """
    psc_norm = psc.replace(" ", "").strip()
    if len(psc_norm) < 3:
        raise ValueError(f"PSČ '{psc}' je príliš krátke")
    first3 = int(psc_norm[:3])

    # VSD — Košický + Prešovský kraj (04xxx–09xxx)
    if 40 <= first3 <= 99 and not (90 <= first3 <= 99 and first3 not in (91, 92, 93, 94, 95)):
        if first3 >= 40 and first3 <= 87:
            return Distribuutor.VSD

    # SSE — Žilinský + Banskobystrický kraj (01xxx–03xxx, 96xxx–98xxx)
    if 10 <= first3 <= 38:
        return Distribuutor.SSE
    if 960 <= first3 <= 998:
        return Distribuutor.SSE

    # ZSD — Bratislavský + Trnavský + Trenčiansky + Nitriansky (8xxxx, 9xxxx zvyšok)
    return Distribuutor.ZSD


# ============================================================================
# SK PSČ → GPS lokalita (najbližšie referenčné mesto z PVGIS DB)
# ============================================================================
PSC_LOCATION_MAP: list[tuple[range, str, float, float]] = [
    # (psc_range, nazov, lat, lon)
    (range(810, 851), "Bratislava", 48.149, 17.107),
    (range(900, 921), "Bratislava", 48.149, 17.107),
    (range(902, 905), "Pezinok", 48.288, 17.266),
    (range(910, 915), "Trenčín", 48.891, 18.043),
    (range(917, 921), "Trnava", 48.378, 17.587),
    (range(921, 924), "Piešťany", 48.594, 17.829),
    (range(927, 929), "Šaľa", 48.149, 17.880),
    (range(930, 933), "Dunajská Streda", 47.992, 17.611),
    (range(934, 937), "Levice", 48.218, 18.604),
    (range(940, 952), "Nitra", 48.314, 18.087),
    (range(953, 956), "Topoľčany", 48.560, 18.172),
    (range(958, 960), "Partizánske", 48.628, 18.388),
    (range(960, 977), "Banská Bystrica", 48.736, 19.146),
    (range(977, 982), "Brezno", 48.808, 19.638),
    (range(982, 987), "Lučenec", 48.331, 19.668),
    (range(987, 995), "Veľký Krtíš", 48.207, 19.350),
    (range(10, 17), "Žilina", 49.224, 18.740),
    (range(17, 24), "Považská Bystrica", 49.117, 18.451),
    (range(24, 27), "Čadca", 49.434, 18.789),
    (range(27, 31), "Liptovský Mikuláš", 49.083, 19.620),
    (range(31, 35), "Ružomberok", 49.078, 19.305),
    (range(35, 39), "Martin", 49.066, 18.924),
    (range(40, 50), "Košice", 48.722, 21.258),
    (range(50, 54), "Spišská Nová Ves", 48.948, 20.567),
    (range(54, 58), "Poprad", 49.057, 20.298),
    (range(58, 60), "Stará Ľubovňa", 49.296, 20.687),
    (range(60, 69), "Prešov", 49.000, 21.243),
    (range(69, 73), "Vranov nad Topľou", 48.890, 21.685),
    (range(73, 76), "Humenné", 48.937, 21.913),
    (range(76, 80), "Snina", 48.987, 22.158),
    (range(80, 88), "Bardejov", 49.293, 21.275),
]


def psc_to_gps(psc: str) -> tuple[float, float, str]:
    """Vráti (lat, lon, lokalita_nazov) pre dané SK PSČ.

    Fallback: geocentrum SK (48.5°N, 19.0°E).
    """
    psc_norm = psc.replace(" ", "").strip()
    if len(psc_norm) < 3:
        return (48.5, 19.0, "Slovensko (default)")
    first3 = int(psc_norm[:3])

    for psc_range, nazov, lat, lon in PSC_LOCATION_MAP:
        if first3 in psc_range:
            return (lat, lon, nazov)
    return (48.5, 19.0, "Slovensko (default)")


def psc_to_sadzba(psc: str, mrk_kw: float) -> Sadzba:
    """Odhad sadzby podľa MRK (jednoduchá heuristika).

    MRK ≥ 80 kW → typicky VN (priemysel)
    MRK < 80 kW → typicky NN (kancelárie, malé prevádzky, domácnosti)
    """
    return Sadzba.VN if mrk_kw >= 80 else Sadzba.NN


# ============================================================================
# AUTO-FILL SITE INPUT
# ============================================================================
def auto_fill_site(
    nazov: str,
    psc: str,
    rocna_spotreba_kwh: float,
    rk_kw: float,
    mrk_kw: Optional[float] = None,
    typ_tarify: str = "spot",
    bilancna_skupina: str = "Energie2",
    eic_kod: Optional[str] = None,
) -> SiteInput:
    """Vyrobí SiteInput z minimálneho vstupu (PSČ + spotreba + RK).

    Auto-detekuje:
        - Distribútor podľa PSČ
        - Sadzba (NN/VN) podľa MRK
        - GPS súradnice (najbližšie referenčné mesto)
        - MRK = 1.2× RK (default ak nezadané)
    """
    if mrk_kw is None:
        mrk_kw = rk_kw * 1.2

    distribuutor = psc_to_distribuutor(psc)
    sadzba = psc_to_sadzba(psc, mrk_kw)
    lat, lon, _ = psc_to_gps(psc)

    return SiteInput(
        nazov=nazov,
        eic_kod=eic_kod,
        distribuutor=distribuutor,
        sadzba=sadzba,
        rk_kw=rk_kw,
        mrk_kw=mrk_kw,
        typ_tarify=TypTarify(typ_tarify),
        bilancna_skupina=bilancna_skupina,
        fakturacny_psc=psc,
        gps_lat=lat,
        gps_lon=lon,
        rocna_spotreba_kwh=rocna_spotreba_kwh,
    )


# ============================================================================
# LOAD PROFILE FROM CSV — tolerantný parser
# ============================================================================
def load_profile_from_csv(
    path: str | Path,
    timestamp_col: Optional[str] = None,
    value_col: Optional[str] = None,
    granularity_min: int = 15,
    expected_annual_kwh: Optional[float] = None,
) -> tuple[pd.DataFrame, dict]:
    """Tolerantný CSV reader — autodetekuje stĺpce timestamp + value.

    Returns:
        (df, meta) — df má index DatetimeIndex + stĺpec 'load_kw',
                    meta obsahuje statisticky o profile.
    """
    path = Path(path)
    if not path.exists():
        raise DataIngestionError(f"Súbor neexistuje: {path}")

    # Skús viacero oddelovačov + encoding
    df = None
    for sep in [";", ",", "\t"]:
        for encoding in ["utf-8", "cp1250", "iso-8859-2"]:
            try:
                df_try = pd.read_csv(path, sep=sep, encoding=encoding)
                if len(df_try.columns) >= 2 and len(df_try) > 24:
                    df = df_try
                    break
            except Exception:
                continue
        if df is not None:
            break

    if df is None:
        raise DataIngestionError(f"Nedalo sa parsovať CSV {path}")

    # Heuristika — nájdi timestamp stĺpec
    if timestamp_col is None:
        candidates = [c for c in df.columns
                       if any(k in c.lower() for k in
                              ("datetime", "datum", "čas", "cas", "time", "timestamp", "interval", "od"))]
        if not candidates:
            # Skús prvý stĺpec
            candidates = [df.columns[0]]
        timestamp_col = candidates[0]

    # Heuristika — nájdi value stĺpec
    if value_col is None:
        candidates = [c for c in df.columns
                       if any(k in c.lower() for k in
                              ("kwh", "kw", "spotreba", "active", "energy", "odber", "hodnota"))]
        if not candidates:
            candidates = [df.columns[-1]]  # posledný stĺpec
        value_col = candidates[0]

    # Parse timestamps
    df[timestamp_col] = pd.to_datetime(df[timestamp_col], dayfirst=True, errors="coerce")
    df = df.dropna(subset=[timestamp_col, value_col]).sort_values(timestamp_col).reset_index(drop=True)

    # Konverzia kWh → kW
    intervals_per_hour = 60 / granularity_min
    is_kwh = any(k in value_col.lower() for k in ("kwh", "spotreba", "energy", "odber"))
    values_kw = (df[value_col].astype(float) * intervals_per_hour
                  if is_kwh else df[value_col].astype(float))

    out = pd.DataFrame({"load_kw": values_kw.values}, index=df[timestamp_col].values)
    out.index = pd.DatetimeIndex(out.index)
    out.index.name = "timestamp"

    # Meta
    annual_kwh = float(out["load_kw"].sum()) * (granularity_min / 60.0)
    meta = {
        "n_intervals": len(out),
        "granularity_min": granularity_min,
        "annual_kwh": annual_kwh,
        "max_kw": float(out["load_kw"].max()),
        "min_kw": float(out["load_kw"].min()),
        "mean_kw": float(out["load_kw"].mean()),
        "timestamp_col_used": timestamp_col,
        "value_col_used": value_col,
        "first_ts": str(out.index[0]),
        "last_ts": str(out.index[-1]),
    }
    if expected_annual_kwh:
        meta["deviation_vs_expected_pct"] = (annual_kwh / expected_annual_kwh - 1) * 100

    return out, meta


def synthetic_load_profile(
    annual_kwh: float, year: int, granularity_min: int = 60,
    peak_hours: tuple[int, int] = (17, 22),
    peak_kw_extra: float = 8.0, base_kw: float = 4.0,
    winter_factor: float = 1.30,
) -> pd.DataFrame:
    """Synteticky load profile pre prípady keď zákazník nemá CSV.

    Pre prevádzky s večerným peakom (tenis klub, kancelária, fitness, atď.).
    """
    timestamps = pd.date_range(
        start=f"{year}-01-01 00:00",
        end=f"{year}-12-31 23:00",
        freq=f"{granularity_min}min",
    )
    hours = np.array([t.hour for t in timestamps])
    months = np.array([t.month for t in timestamps])

    load_kw = np.full(len(timestamps), base_kw)
    load_kw[(hours >= peak_hours[0]) & (hours <= peak_hours[1])] += peak_kw_extra
    load_kw[(months <= 3) | (months >= 11)] *= winter_factor

    # Scale na expected
    total_kwh = load_kw.sum() * (granularity_min / 60.0)
    load_kw *= annual_kwh / total_kwh

    df = pd.DataFrame({"load_kw": load_kw}, index=timestamps)
    df.index.name = "timestamp"
    return df
