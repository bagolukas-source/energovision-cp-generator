# -*- coding: utf-8 -*-
"""Profile intelligence — odvodi charakteristiku odberneho miesta z DAT (nie natvrdo).
Dva rezimy: hourly (plne metriky) / aggregate (load factor + sezonnost z agregatov).
Na inom projekte vrati iny popis."""
from __future__ import annotations
import statistics as st


def _r(x, n=2):
    try: return round(float(x), n)
    except Exception: return None


def classify_profile(hourly=None, monthly_mwh=None, avg_kw=None, peak_kw=None, p95_kw=None):
    """hourly: list of (hour_of_day:int, is_weekend:bool, kw:float) alebo None."""
    day_share = weekend_ratio = peak_hour = load_factor = None
    if hourly:
        kw = [h[2] for h in hourly]
        if kw:
            avg_kw = avg_kw or st.mean(kw); peak_kw = peak_kw or max(kw)
            tot = sum(kw) or 1
            day_share = sum(h[2] for h in hourly if 7 <= h[0] < 18) / tot
            wd = [h[2] for h in hourly if not h[1]]; we = [h[2] for h in hourly if h[1]]
            weekend_ratio = (st.mean(we)/st.mean(wd)) if (wd and we and st.mean(wd) > 0) else None
            byh = {}
            for h in hourly: byh.setdefault(h[0], []).append(h[2])
            peak_hour = max(byh, key=lambda k: st.mean(byh[k]))
    if avg_kw and peak_kw and peak_kw > 0:
        load_factor = avg_kw / peak_kw
    seasonality_cv = None
    if monthly_mwh and len([x for x in monthly_mwh if x]) >= 6:
        m = [x for x in monthly_mwh if x]
        if st.mean(m) > 0: seasonality_cv = st.pstdev(m)/st.mean(m)

    metrics = {"load_factor": _r(load_factor), "day_share_pct": _r(day_share*100,1) if day_share is not None else None,
               "weekend_ratio": _r(weekend_ratio), "seasonality_cv": _r(seasonality_cv,3),
               "peak_hour": peak_hour, "avg_kw": _r(avg_kw,0) if avg_kw else None, "peak_kw": _r(peak_kw,0) if peak_kw else None}

    rezim = None
    if load_factor is not None:
        if load_factor >= 0.6 and (weekend_ratio is None or weekend_ratio >= 0.75):
            rezim = "nepretrzita prevadzka (24/7)".replace("nepretrzita","nepretržitá")
        elif load_factor >= 0.45:
            rezim = "viaczmenná prevádzka" if (day_share is None or day_share < 0.62) else "prevažne denná prevádzka s vysokým využitím"
        elif load_factor >= 0.30:
            rezim = "jednozmenná/denná prevádzka s poklesom mimo pracovného času"
        else:
            rezim = "nepravidelný profil s výraznými výkyvmi odberu"
        if weekend_ratio is not None and weekend_ratio < 0.6:
            rezim += ", znížená prevádzka cez víkendy"
    sezonnost = None
    if seasonality_cv is not None:
        sezonnost = ("minimálna sezónnosť, rovnomerná spotreba počas roka" if seasonality_cv < 0.12
                     else "mierna sezónnosť" if seasonality_cv < 0.25 else "výrazná sezónnosť")
    spicka = None
    if peak_hour is not None:
        spicka = ("ranný špic" if 6 <= peak_hour <= 10 else "poludňajší špic" if 11 <= peak_hour <= 15
                  else "večerný špic" if 16 <= peak_hour <= 21 else "nočný špic")
    fve_fit = None
    if day_share is not None:
        fve_fit = ("vysoká vhodnosť pre FVE — odber sa kryje s dennou výrobou" if day_share >= 0.5
                   else "FVE vhodná najmä s batériou — časť odberu je mimo slnečných hodín")
    elif load_factor is not None:
        fve_fit = ("dobrá vhodnosť pre FVE pri dennom využití" if load_factor >= 0.35
                   else "odporúčaná batéria na presun výroby do odberu")
    return {"metrics": metrics,
            "rezim": rezim or "profil odberu (z dostupných dát)",
            "sezonnost": sezonnost or "sezónnosť nehodnotená",
            "spicka": spicka, "fve_fit": fve_fit,
            "data_mode": "hourly" if hourly else "aggregate"}
