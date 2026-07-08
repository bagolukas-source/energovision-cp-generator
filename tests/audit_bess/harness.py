"""Audit harness — priame volanie RuleBasedEMS.run_year s vlastným spotom."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cpgen"))

import numpy as np
import pandas as pd

from energovision_analytics.core.models import (
    SiteInput, TariffYearInput, BESSInput, Distribuutor, Sadzba, TypTarify,
    BESSVyrobca, Chemia,
)
from energovision_analytics.battery.pack_model import BatteryPack
from energovision_analytics.ems.rule_based import RuleBasedEMS
from energovision_analytics.ems.dispatch_state import EMSConfig
from energovision_analytics.tariff.retail_calculator import RetailCalculator

SPOT_CSV = os.path.join(os.path.dirname(__file__), "..", "cpgen", "aom_data", "sk_spot_2025_hourly.csv")

def real_spot():
    df = pd.read_csv(SPOT_CSV)
    return df["price_eur_per_mwh"].to_numpy()[:8760]

def make_site(rk_kw=200.0, mrk_kw=200.0, rocna=200_000):
    return SiteInput(
        nazov="AUDIT TEST", distribuutor=Distribuutor.SSE, sadzba=Sadzba.VN,
        rk_kw=rk_kw, mrk_kw=mrk_kw, typ_tarify=TypTarify.SPOT,
        fakturacny_psc="010 01", gps_lat=48.7, gps_lon=19.1,
        rocna_spotreba_kwh=rocna,
    )

def make_tariff():
    # regulovane = 8.5+12.3+5.2+3.27+1.32+4.55 = 35.14; obchodnik = 25
    return TariffYearInput(
        rok=2026, distribuutor=Distribuutor.SSE, sadzba=Sadzba.VN,
        tps_eur_mwh=8.5, distrib_eur_mwh=12.3, straty_eur_mwh=5.2,
        njf_eur_mwh=3.27, spotrebna_dan_eur_mwh=1.32, tss_eur_mwh=4.55,
        mrk_kapacita_eur_mw_mes=4500.0, rk_kapacita_eur_mw_mes=2500.0,
        obchodnik_aditiv_eur_mwh=20.0, obchodnik_prirazka_eur_mwh=5.0,
        fix_silova_eur_mwh=114.0,
    )

def make_bess(kwh, c_rate=0.5):
    return BESSInput(
        vyrobca=BESSVyrobca.HUAWEI, typ=f"AUDIT-{int(kwh)}", chemie=Chemia.LFP,
        nominal_kwh=kwh, usable_kwh=kwh * 0.90, power_kw_ac=kwh * c_rate,
        c_rate_max=max(0.5, c_rate),
    )

ADD_ON = 60.14  # obchodnik+regulovane €/MWh
def retail_kwh(spot):
    return (spot + ADD_ON) / 1000.0

def run_ems(load_kw, pv_kw, spot, bess_kwh, timestep_min=60, rk_kw=200.0, mrk_kw=200.0,
            initial_soc_pct=0.5, use_dynamic_rte=True, **cfg_kwargs):
    """Vráti (intervals, summary, battery, ems). cfg_kwargs -> EMSConfig."""
    n = len(load_kw)
    freq = f"{timestep_min}min"
    ts = pd.date_range("2025-01-01", periods=n, freq=freq)
    site = make_site(rk_kw=rk_kw, mrk_kw=mrk_kw)
    tariff = make_tariff()
    retail = RetailCalculator(tariff, typ_tarify=TypTarify.SPOT)
    cfg = EMSConfig(peak_shave_enabled=False, **cfg_kwargs)
    bat = BatteryPack(make_bess(bess_kwh), initial_soc_pct=initial_soc_pct,
                      use_dynamic_rte=use_dynamic_rte)
    ems = RuleBasedEMS(bat, site, tariff, retail, cfg)
    intervals, summary = ems.run_year(
        np.asarray(load_kw, float), np.asarray(pv_kw, float),
        np.asarray(spot, float), ts, timestep_min)
    return intervals, summary, bat, ems

def pv_only_baseline(load_kw, pv_kw, spot, dt_h=1.0, export_price=0.06, mrk_kw=200.0):
    """Replika FIXNUTEJ _build_pv_only_summary (variants/generator.py, commit 7293560):
    curtail exportu pri spot<0 + MRK clip — PARITA s BESS vetvou (AOM-FIX-31).
    Pozn.: do 2026-07-08 tu parita chýbala → t5 falošne ukazoval pokles hodnoty batérie
    s rastúcou FVE (legacy verzia nižšie ako pv_only_baseline_legacy)."""
    load = np.asarray(load_kw, float); pv = np.asarray(pv_kw, float); sp = np.asarray(spot, float)
    pv_to_load = np.minimum(pv, load) * dt_h
    pv_to_grid = np.maximum(pv - load, 0) * dt_h
    grid_import = np.maximum(load - pv, 0) * dt_h
    curtailed = float(pv_to_grid[sp < 0].sum())
    pv_to_grid = pv_to_grid.copy(); pv_to_grid[sp < 0] = 0.0
    mrk_lim = mrk_kw * dt_h
    over = np.maximum(pv_to_grid - mrk_lim, 0.0)
    curtailed += float(over.sum())
    pv_to_grid = np.minimum(pv_to_grid, mrk_lim)
    tarif = (sp + ADD_ON) / 1000.0
    return {
        "sav_solar_self": float((pv_to_load * tarif).sum()),
        "sav_export": float(pv_to_grid.sum() * export_price),
        "sav_total": float((pv_to_load * tarif).sum() + pv_to_grid.sum() * export_price),
        "import_kwh": float(grid_import.sum()),
        "export_kwh": float(pv_to_grid.sum()),
        "curtailed_kwh": curtailed,
        "cost_eur": float((grid_import * tarif).sum() - pv_to_grid.sum() * export_price),
    }

def pv_only_baseline_legacy(load_kw, pv_kw, spot, dt_h=1.0, export_price=0.06):
    """Stará replika BEZ curtail parity — len na demonštráciu artefaktu v t5."""
    load = np.asarray(load_kw, float); pv = np.asarray(pv_kw, float); sp = np.asarray(spot, float)
    pv_to_load = np.minimum(pv, load) * dt_h
    pv_to_grid = np.maximum(pv - load, 0) * dt_h
    grid_import = np.maximum(load - pv, 0) * dt_h
    tarif = (sp + ADD_ON) / 1000.0
    return {
        "sav_solar_self": float((pv_to_load * tarif).sum()),
        "sav_export": float(pv_to_grid.sum() * export_price),
        "sav_total": float((pv_to_load * tarif).sum() + pv_to_grid.sum() * export_price),
        "import_kwh": float(grid_import.sum()),
        "export_kwh": float(pv_to_grid.sum()),
        "cost_eur": float((grid_import * tarif).sum() - pv_to_grid.sum() * export_price),
    }

def actual_cost(intervals, export_price=0.06):
    """Reálny účet klienta z intervalov: import@retail − export@výkup."""
    c = 0.0
    for iv in intervals:
        c += iv.grid_import_kwh * iv.tarif_buy_eur_kwh - iv.grid_export_kwh * export_price
    return c

def conservation(intervals, summary, bat, initial_soc_kwh):
    """SumaPV + Sumaimport = Sumaload + Sumaexport + dSoC + straty.
    straty := bat_charge_AC − bat_discharge_AC − dSoC_DC (konverzné straty)."""
    d_soc = bat.soc_kwh - initial_soc_kwh
    losses = summary.bat_charge_total_kwh - summary.bat_discharge_total_kwh - d_soc
    _curt = float(getattr(summary, "pv_curtailed_kwh", 0.0) or 0.0)
    lhs = summary.pv_total_kwh + summary.grid_import_kwh
    rhs = summary.load_total_kwh + summary.grid_export_kwh + d_soc + losses + _curt
    resid = lhs - rhs
    denom = max(lhs, 1e-9)
    # nezávislá kontrola tokov (bez battery internals):
    flow_resid_pv = summary.pv_total_kwh - (summary.pv_to_load_kwh + summary.pv_to_bat_kwh + summary.pv_to_grid_kwh
                                             + float(getattr(summary, "pv_curtailed_kwh", 0.0) or 0.0))
    flow_resid_load = summary.load_total_kwh - (summary.pv_to_load_kwh + summary.bat_discharge_total_kwh
                                                + (summary.grid_import_kwh - sum(i.grid_to_bat_kwh for i in intervals)))
    return {"lhs": lhs, "rhs": rhs, "resid": resid, "resid_pct": 100 * resid / denom,
            "d_soc": d_soc, "losses": losses,
            "flow_resid_pv": flow_resid_pv, "flow_resid_load": flow_resid_load}

def batt_value(summary):
    return summary.sav_bess_self_cons_eur + summary.sav_arbitrage_eur
