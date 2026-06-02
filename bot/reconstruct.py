# -*- coding: utf-8 -*-
"""reconstruct_load — z odberu + dodávky do siete + existujúcej FVE odvodí SKUTOČNÚ spotrebu.
Kľúčové pre retrofit/rozšírenie: nameraný odber zo siete NIE je celá spotreba (existujúca FVE
časť pokrýva). spotreba(t) = odber(t) + (výroba_existujúcej_FVE(t) − dodávka_do_siete(t))."""
import math


def _pvgis_15min_shape(n_per_day=96):
    """Normalizovaný denný PV tvar (E-W/Juh, SK) — Gaussian okolo poludnia. Suma=1 deň."""
    shape=[]
    for i in range(n_per_day):
        h=i*24.0/n_per_day
        d=h-12.0
        v=math.exp(-(d*d)/8.0) if 5<=h<=19 else 0.0
        shape.append(v)
    s=sum(shape) or 1
    return [x/s for x in shape]

# SK PVGIS mesačné váhy (% z ročnej výroby)
PVGIS_MONTHLY=[0.038,0.057,0.088,0.108,0.119,0.124,0.124,0.116,0.091,0.067,0.040,0.028]


def model_existing_pv_kw(timestamps, existing_fve_kwp, yield_kwh_kwp=1050, granularity_min=15):
    """Odhadne výrobu existujúcej FVE (kW) pre každý timestep z kWp + PVGIS tvaru."""
    annual_kwh = existing_fve_kwp * yield_kwh_kwp
    steps_per_day = int(24*60/granularity_min)
    dt_h = granularity_min/60.0
    day_shape = _pvgis_15min_shape(steps_per_day)
    # mesačný scaling: rozdeľ ročnú výrobu podľa mesiacov
    import collections
    days_in_month=[31,28,31,30,31,30,31,31,30,31,30,31]
    out=[]
    for ts in timestamps:
        m=(ts.month-1) if hasattr(ts,"month") else 0
        idx=(ts.hour*60+ts.minute)//granularity_min if hasattr(ts,"hour") else 0
        month_kwh = annual_kwh * PVGIS_MONTHLY[m]
        day_kwh = month_kwh / days_in_month[m]
        kwh_step = day_kwh * day_shape[idx % steps_per_day]
        out.append(kwh_step / dt_h)  # kW
    return out


def reconstruct_load(import_kw, export_kw, existing_fve_kwp, timestamps,
                     yield_kwh_kwp=1050, granularity_min=15):
    """Vráti (true_load_kw, info). true_load = odber + (existujúca FVE výroba − dodávka)."""
    n=min(len(import_kw), len(export_kw), len(timestamps))
    existing_pv = model_existing_pv_kw(timestamps[:n], existing_fve_kwp, yield_kwh_kwp, granularity_min)
    true_load=[]
    for i in range(n):
        imp=float(import_kw[i]); exp=float(export_kw[i]); pv=existing_pv[i]
        load = imp + (pv - exp)
        true_load.append(max(0.0, load))
    dt_h=granularity_min/60.0
    info={
        "true_annual_mwh": round(sum(true_load)*dt_h/1000.0, 1),
        "metered_import_mwh": round(sum(import_kw[:n])*dt_h/1000.0, 1),
        "existing_pv_mwh": round(sum(existing_pv)*dt_h/1000.0, 1),
        "export_mwh": round(sum(export_kw[:n])*dt_h/1000.0, 1),
        "existing_fve_kwp": existing_fve_kwp,
        "note": "Skutočná spotreba zrekonštruovaná spod existujúcej FVE (odber ≠ spotreba).",
    }
    return true_load, info


def classify_situation(analyza: dict) -> dict:
    """Rozpozná typ prípadu z dát/záznamu → metóda analýzy."""
    existing_fve = float(analyza.get("existing_fve_kwp") or 0)
    existing_bess = float(analyza.get("existing_bess_kwh") or 0)
    has_export = bool(analyza.get("_has_export"))  # nastaví ingestion ak dodávka>0
    scenario = (analyza.get("scenario_type") or "").lower()
    want = (analyza.get("_customer_request") or "").lower()

    if existing_fve > 0 or has_export:
        if "bateri" in want or "bess" in want or scenario == "pridanie_bess":
            typ = "retrofit_bess"; desc = "Existujúca FVE — pridať batériu (retrofit)."
        elif "rozšír" in want or "rozsir" in want or scenario == "rozsirenie_fve":
            typ = "expansion_fve"; desc = "Existujúca FVE — rozšírenie výkonu."
        else:
            typ = "existing_fve_general"; desc = "Existujúca FVE — optimalizácia (batéria/rozšírenie)."
        method = "reconstruct_load + simulovať prírastok"
    elif scenario == "iba_bess_arbitraz" or ("arbitr" in want and "fve" not in want):
        typ = "bess_only"; desc = "Len batéria (arbitráž/peak shaving), bez FVE."; method = "BESS-solo dispatch (spot)"
    else:
        typ = "greenfield"; desc = "Nová FVE/BESS od nuly."; method = "štandardný variant sweep"
    return {"type": typ, "description": desc, "method": method,
            "existing_fve_kwp": existing_fve, "existing_bess_kwh": existing_bess, "has_export": has_export}
