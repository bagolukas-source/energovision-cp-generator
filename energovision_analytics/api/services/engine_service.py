"""Engine service — orchestrátor medzi API request → engine pipeline → response."""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from energovision_analytics._version import __version__
from energovision_analytics.core.logging import get_logger
from energovision_analytics.core.run_manifest import build_run_manifest
from energovision_analytics.data.auto_fill import (
    auto_fill_site,
    load_profile_from_csv,
    synthetic_load_profile,
)
from energovision_analytics.financial.dotacie import apply_dotacia, load_dotacie_schemes
from energovision_analytics.tariff import TariffEngine
from energovision_analytics.variants import VariantGenerator, pick_top_variants

log = get_logger(__name__)


import os
# engine/api/services/engine_service.py → engine/ je parents[2]
ENGINE_ROOT = Path(__file__).resolve().parents[2]
# Spot CSV: prefer env var (pre Docker / Render), inak parent dir, inak engine/data/spot/
SPOT_CSV = Path(os.environ.get(
    "ENERGO_SPOT_CSV",
    str(ENGINE_ROOT.parent / "sk_spot_2025_hourly.csv"),
))
if not SPOT_CSV.exists():
    # Fallback — Docker / produkčný path
    for alt in [Path("/sk_spot_2025_hourly.csv"),
                ENGINE_ROOT / "data" / "spot" / "sk_spot_2025_hourly.csv"]:
        if alt.exists():
            SPOT_CSV = alt
            break

TARIFF_YAML = Path(os.environ.get(
    "ENERGO_TARIFF_YAML",
    str(ENGINE_ROOT / "data" / "tariffs" / "2026.yaml"),
))


def _profile_template_params(template: str) -> dict:
    """Mapuje template name → synthetic_load_profile parametre."""
    presets = {
        "tenisovy_klub": dict(peak_hours=(17, 22), peak_kw_extra=8.0, base_kw=4.0, winter_factor=1.30),
        "kancelaria":    dict(peak_hours=(8, 17),  peak_kw_extra=6.0, base_kw=3.0, winter_factor=1.15),
        "priemysel_24_7": dict(peak_hours=(6, 18), peak_kw_extra=4.0, base_kw=12.0, winter_factor=1.10),
        "domacnost":     dict(peak_hours=(18, 22), peak_kw_extra=3.0, base_kw=1.5, winter_factor=1.25),
    }
    return presets.get(template, presets["kancelaria"])


def _decode_csv_to_load_kw(csv_b64: str, granularity_min: int, expected_kwh: float) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """Base64 CSV → load_kw array + timestamps."""
    raw = base64.b64decode(csv_b64)
    tmp = Path("/tmp") / f"api_upload_{int(time.time()*1000)}.csv"
    tmp.write_bytes(raw)
    try:
        df, meta = load_profile_from_csv(
            tmp, granularity_min=granularity_min, expected_annual_kwh=expected_kwh
        )
        hourly = df["load_kw"].resample("1h").mean()
        return hourly.to_numpy(), hourly.index
    finally:
        tmp.unlink(missing_ok=True)


def _dotacia_npv_delta(delta_eur: float, fin) -> float:
    """Správny NPV dopad zmeny dotácie o delta: +delta (nižší CAPEX rok 0)
    MÍNUS stratený daňový štít (nižší net CAPEX = menej odpisu).
    delta>0 = viac dotácie."""
    dppo = float(getattr(fin, "dppo_pct", 0.21) or 0.21)
    depr = int(getattr(fin, "depr_years", 6) or 6)
    disc = float(getattr(fin, "discount_rate", 0.06) or 0.06)
    annuity = sum(1.0 / ((1 + disc) ** y) for y in range(1, depr + 1))
    shield_factor = (dppo / depr) * annuity  # NPV strateného štítu na 1 € net CAPEX
    return delta_eur * (1.0 - shield_factor)


def run_variants_pipeline(request_dict: dict, progress_cb=None) -> dict:
    """Spustí celý pipeline a vráti structured výsledky.

    Args:
        request_dict: dict zodpovedajúci RunVariantsRequest schéme
        progress_cb: optional callable(pct: float) pre async progress
    """
    t0 = time.time()

    # 1. Auto-fill site
    site_req = request_dict["site"]
    site = auto_fill_site(
        nazov=site_req["nazov"],
        psc=site_req["psc"],
        rocna_spotreba_kwh=site_req["rocna_spotreba_kwh"],
        rk_kw=site_req["rk_kw"],
        mrk_kw=site_req.get("mrk_kw"),
        typ_tarify=site_req.get("typ_tarify", "spot"),
        bilancna_skupina=site_req.get("bilancna_skupina", "Energie2"),
        eic_kod=site_req.get("eic_kod"),
    )
    if progress_cb: progress_cb(10)

    # 2. Load profile
    lp = request_dict["load_profile"]
    if lp["source"] == "csv_base64":
        load_kw, ts = _decode_csv_to_load_kw(
            lp["csv_base64"], lp.get("granularity_min", 60), site.rocna_spotreba_kwh
        )
    else:
        params = _profile_template_params(lp.get("profile_template", "kancelaria"))
        df_syn = synthetic_load_profile(
            annual_kwh=site.rocna_spotreba_kwh, year=2025, granularity_min=60, **params
        )
        load_kw = df_syn["load_kw"].to_numpy()
        ts = df_syn.index
    if progress_cb: progress_cb(25)

    # 3. Spot + tariff
    spot_df = pd.read_csv(SPOT_CSV)
    spot = spot_df["price_eur_per_mwh"].to_numpy()
    tariff_engine = TariffEngine.from_yaml(TARIFF_YAML)

    # UX-AOM-6: aplikuj per-analyza tariff_overrides ak existujú
    tariff_overrides = request_dict.get("tariff_overrides") or {}
    if tariff_overrides:
        for ty in tariff_engine._tariffs.values():
            if tariff_overrides.get("silova_eur_mwh") is not None:
                ty.fix_silova_eur_mwh = float(tariff_overrides["silova_eur_mwh"])
            if tariff_overrides.get("distribucia_eur_mwh") is not None:
                ty.distrib_eur_mwh = float(tariff_overrides["distribucia_eur_mwh"])
            if tariff_overrides.get("tps_eur_mwh") is not None:
                ty.tps_eur_mwh = float(tariff_overrides["tps_eur_mwh"])
            if tariff_overrides.get("oze_eur_mwh") is not None:
                ty.njf_eur_mwh = float(tariff_overrides["oze_eur_mwh"])
            if tariff_overrides.get("ostatne_eur_mwh") is not None:
                # Spotrebná daň + TSS dohromady; engine ich má oddelené, takže rozdelíme 50/50
                half = float(tariff_overrides["ostatne_eur_mwh"]) / 2
                ty.spotrebna_dan_eur_mwh = half
                ty.tss_eur_mwh = half
            if tariff_overrides.get("mrk_kapacita_eur_mw_mes") is not None:
                ty.mrk_kapacita_eur_mw_mes = float(tariff_overrides["mrk_kapacita_eur_mw_mes"])

    n = min(len(load_kw), len(spot), 8760)
    load_kw = load_kw[:n]
    spot = spot[:n]
    ts = ts[:n] if len(ts) > n else ts
    if len(ts) < n:
        ts = pd.date_range("2025-01-01 00:00", periods=n, freq="1h")
    load_df = pd.DataFrame({"load_kw": load_kw}, index=ts)

    # 4. VariantGenerator
    v = request_dict["variants"]
    capex = request_dict.get("capex", {})
    fin = request_dict.get("financial", {})

    gen = VariantGenerator(
        site=site,
        load_df=load_df,
        spot_eur_mwh=spot,
        timestamps=ts,
        tariff_engine=tariff_engine,
        pv_kwp_options=v["pv_kwp_options"],
        bess_kwh_options=v["bess_kwh_options"],
        ems_strategies=v.get("ems_strategies", ["rule_based"]),
        capex_pv_eur_per_kwp=capex.get("capex_pv_eur_per_kwp", 800),
        capex_bess_eur_per_kwh=capex.get("capex_bess_eur_per_kwh", 480),
        dppo_pct=fin.get("dppo_pct", 0.22),
        discount_rate=fin.get("discount_rate", 0.06),
        horizon_years=fin.get("horizon_years", 20),
        depr_years=fin.get("depr_years", 6),
    )
    log.info("Running %d variants", len(v["pv_kwp_options"]) * len(v["bess_kwh_options"]))
    results = gen.run_all(parallel=True)
    if progress_cb: progress_cb(80)

    # 5. Aplikuj dotáciu
    dotacia = request_dict.get("dotacia", {})
    if dotacia.get("enabled", True) and dotacia.get("scheme_id") != "ziadna":
        schemes = load_dotacie_schemes()
        for r in results:
            proj_type = "FVE+BESS" if r.bess_kwh > 0 else "FVE"
            res = apply_dotacia(
                scheme_id=dotacia["scheme_id"],
                capex_eur=r.capex_total_eur,
                samospotreba_pct=r.samospotreba_pct,
                project_type=proj_type, schemes=schemes,
            )
            new_d = res["amount_eur"] if res["eligible"] else 0.0
            delta = new_d - r.dotacia_eur
            r.dotacia_eur = new_d
            r.financial.dotacia_eur = new_d
            r.financial.capex_net_eur = r.financial.capex_gross_eur - new_d
            r.financial.npv_eur += _dotacia_npv_delta(delta, r.financial)
    else:
        for r in results:
            delta = -r.dotacia_eur
            r.dotacia_eur = 0
            r.financial.dotacia_eur = 0
            r.financial.capex_net_eur = r.financial.capex_gross_eur
            r.financial.npv_eur += _dotacia_npv_delta(delta, r.financial)

    if progress_cb: progress_cb(95)

    # 6. Top picker
    top_picks = pick_top_variants(results, n=6)

    # 7. Manifest
    manifest = build_run_manifest(tariff_yaml=TARIFF_YAML, spot_csv=SPOT_CSV)

    elapsed_ms = (time.time() - t0) * 1000
    return {
        "results": results,
        "top_picks": top_picks,
        "manifest": manifest,
        "elapsed_ms": elapsed_ms,
    }


def build_run_variants_response(
    pipeline_output: dict, job_id: Optional[str] = None
) -> dict:
    """Konvertuje engine VariantResult objekty na API response dict."""
    results = pipeline_output["results"]
    top_picks = pipeline_output["top_picks"]
    manifest = pipeline_output["manifest"]

    # Vyrob rank_labels mapping (variant_id → list[labels])
    rank_map: dict[str, list[str]] = {}
    for label, v in top_picks:
        rank_map.setdefault(v.variant_id, []).append(label)

    # PVGIS Slovakia mesačné váhy (% z ročnej výroby) — Bratislava 48.15°N
    # Zdroj: PVGIS-SARAH2, sklon 35°, juh
    PVGIS_SK_MONTHLY_WEIGHTS = [
        0.038, 0.057, 0.088, 0.108, 0.119, 0.124,
        0.124, 0.116, 0.091, 0.067, 0.040, 0.028,
    ]
    # SK B2B mesačné distribúcie spotreby (rovnomerné s miernym profilom Z/L)
    SK_LOAD_MONTHLY_WEIGHTS = [
        0.092, 0.088, 0.086, 0.080, 0.076, 0.078,
        0.080, 0.080, 0.082, 0.086, 0.088, 0.084,
    ]

    variants_out = []
    for r in results:
        # === CASHFLOW ARRAY ===
        # rok 0 = -net capex + dotácia, rok 1..N = revenue - opex (+ tax shield 1-6)
        cf_array = []
        for cy in r.financial.yearly_cashflows:
            cf_array.append(cy.net_cashflow)
        # Zaisti že máme aspoň 21 hodnôt (rok 0..20)
        while len(cf_array) < 21:
            cf_array.append(cf_array[-1] if cf_array else 0.0)
        cf_array = cf_array[:25]  # max 25 rokov

        # === VALUE STREAMS BREAKDOWN (annual €) ===
        # Engine rule_based teraz počíta arbitráž round-trip správne (real hodinový tarif_buy
        # na nabíjanie aj vybíjanie, žiadny paušál). bess_self = PV-zdrojové vybitie × retail.
        # Berieme engine hodnoty priamo. Arbitráž môže byť záporná = grid-arbitráž sa pri danom
        # profile/batérii neoplatí (úprimne). Pre report sa bess_self + arbitráž zobrazí ako
        # jeden "batériový prínos" (commingled energia), total je korektný súčet.
        s = r.summary
        value_streams = {
            "solar_self_consumption_eur": float(s.sav_solar_self_cons_eur),
            "solar_export_eur": float(s.sav_solar_export_eur),
            "bess_self_consumption_eur": float(s.sav_bess_self_cons_eur),
            "arbitrage_eur": float(s.sav_arbitrage_eur),
            "peak_shaving_eur": float(s.sav_peak_shaving_eur),
            "mrk_penalty_avoided_eur": float(s.sav_mrk_penalty_avoided_eur),
            "bess_total_eur": float(s.sav_bess_self_cons_eur) + float(s.sav_arbitrage_eur),
            "total_eur": float(s.sav_total_eur),
        }

        # === MONTHLY SUMMARY (12) — odhad z PVGIS SK distribúcie ===
        # Engine spočíta annual; mesačný breakdown derivovaný z PVGIS váh + load profilu.
        # Real per-hour breakdown by vyžadoval engine refactor (track per-month).
        annual_pv = float(s.pv_total_kwh)
        annual_load = float(s.load_total_kwh)
        annual_export = float(s.pv_to_grid_kwh)
        annual_import = float(s.grid_import_kwh)
        annual_savings = float(s.sav_total_eur)
        monthly_summary = []
        for m in range(12):
            pv_w = PVGIS_SK_MONTHLY_WEIGHTS[m]
            load_w = SK_LOAD_MONTHLY_WEIGHTS[m]
            monthly_summary.append({
                "month": m + 1,
                "pv_kwh": annual_pv * pv_w,
                "load_kwh": annual_load * load_w,
                "export_kwh": annual_export * pv_w,
                "import_kwh": annual_import * load_w,
                "solar_to_load_eur": value_streams["solar_self_consumption_eur"] * pv_w,
                "solar_export_eur": value_streams["solar_export_eur"] * pv_w,
                "arbitrage_eur": value_streams["arbitrage_eur"] / 12.0,
                "peak_shaving_eur": value_streams["peak_shaving_eur"] * load_w,
                "total_eur": annual_savings * ((pv_w + load_w) / 2),
            })

        # === ENERGY FLOW (Sankey-style — pre 4-circle diagram) ===
        energy_flow = {
            "pv_total_mwh": annual_pv / 1000.0,
            "pv_to_load_mwh": float(s.pv_to_load_kwh) / 1000.0,
            "pv_to_grid_mwh": float(s.pv_to_grid_kwh) / 1000.0,
            "pv_to_bat_mwh": float(s.pv_to_bat_kwh) / 1000.0,
            "grid_to_load_mwh": float(s.grid_import_kwh) / 1000.0,
            "bat_to_load_mwh": float(s.bat_discharge_total_kwh) / 1000.0,
            "grid_to_bat_mwh": max(0.0, float(s.bat_charge_total_kwh - s.pv_to_bat_kwh)) / 1000.0,  # BESS grid charging (arbitráž)
            "load_total_mwh": annual_load / 1000.0,
            "grid_export_mwh": float(s.grid_export_kwh) / 1000.0,
        }

        # === SOLAR CONSUMPTION BREAKDOWN (pre donut) ===
        if annual_pv > 0:
            solar_consumption_pct = {
                "direct_to_load": (float(s.pv_to_load_kwh) / annual_pv) * 100,
                "charging_battery": (float(s.pv_to_bat_kwh) / annual_pv) * 100,
                "exported": (float(s.pv_to_grid_kwh) / annual_pv) * 100,
                "curtailed": (float(s.pv_clipped_kwh) / annual_pv) * 100,
            }
        else:
            solar_consumption_pct = {"direct_to_load": 0, "charging_battery": 0, "exported": 0, "curtailed": 0}

        # === CARBON ===
        carbon = {
            "co2_avoided_t_per_year": float(s.co2_avoided_t),
            "trees_equivalent": int(s.co2_avoided_t * 1000 / 21),  # 21 kg CO2/tree/year
            "barrels_oil_avoided": int(s.co2_avoided_t * 2.32),     # 1 t CO2 ≈ 2.32 barrels
        }

        variants_out.append({
            "variant_id": r.variant_id,
            "pv_kwp": r.pv_kwp,
            "bess_kwh": r.bess_kwh,
            "bess_kw": r.bess_kw,
            "ems_strategy": r.ems_strategy,
            "capex_pv_eur": r.pv_kwp * r.capex_pv_eur_per_kwp,
            "capex_bess_eur": r.bess_kwh * r.capex_bess_eur_per_kwh,
            "capex_total_eur": r.capex_total_eur,
            "dotacia_eur": r.dotacia_eur,
            "net_capex_eur": r.capex_total_eur - r.dotacia_eur,
            "samospotreba_pct": r.samospotreba_pct,
            "samostatnost_pct": r.samostatnost_pct,
            "pv_total_kwh": r.summary.pv_total_kwh,
            "grid_import_kwh": r.summary.grid_import_kwh,
            "saving_y1_eur": r.saving_y1_eur,
            "npv_eur": r.npv_eur,
            "irr_pct": r.irr_pct,
            "payback_simple_y": r.payback_y,
            "lcoe_eur_mwh": r.financial.lcoe_eur_mwh,
            "lcos_eur_mwh": r.financial.lcos_eur_mwh,
            "label": r.label(),
            "rank_labels": rank_map.get(r.variant_id, []),
            # === NEW: real data pre charts ===
            "cashflow_array": cf_array,
            "value_streams": value_streams,
            "monthly_summary": monthly_summary,
            "energy_flow": energy_flow,
            "solar_consumption_pct": solar_consumption_pct,
            "carbon": carbon,
        })

    return {
        "success": True,
        "job_id": job_id,
        "variants": variants_out,
        "top_picks": [
            {"label": label, "variant_id": v.variant_id, "npv_eur": v.npv_eur}
            for label, v in top_picks
        ],
        "manifest": {
            "engine_version": manifest.engine_version,
            "generated_at": manifest.generated_at,
            "tariff_year": manifest.tariff_year,
            "tariff_hash": manifest.tariff_hash,
            "spot_last_date": manifest.spot_last_date,
            "economic_defaults_hash": manifest.economic_defaults_hash,
        },
        "n_variants_run": len(results),
        "elapsed_ms": pipeline_output["elapsed_ms"],
    }


def export_variant_intervals(request_dict: dict, pv_kwp: float, bess_kwh: float,
                             ems_strategy: str = "rule_based") -> dict:
    """Spustí JEDEN variant s keep_intervals=True a vráti REÁLNE hodinové časové rady
    (load before, net load after, pv, battery activity, SoC, spot) pre Orkestra-style
    interval grafy. Pre PV-only (bess=0 → engine nebeží dispatch) dopočíta toky
    deterministicky z PV simulácie + load (fyzikálna bilancia, žiadny odhad).

    Pozn.: premium engine beží hodinovo (8760), takže rozlíšenie je hodinové.
    """
    import numpy as _np
    from energovision_analytics.pv.system import PVSystemSim

    # --- setup (zhodné s run_variants_pipeline kroky 1-3) ---
    site_req = request_dict["site"]
    site = auto_fill_site(
        nazov=site_req["nazov"], psc=site_req["psc"],
        rocna_spotreba_kwh=site_req["rocna_spotreba_kwh"], rk_kw=site_req["rk_kw"],
        mrk_kw=site_req.get("mrk_kw"), typ_tarify=site_req.get("typ_tarify", "spot"),
        bilancna_skupina=site_req.get("bilancna_skupina", "Energie2"), eic_kod=site_req.get("eic_kod"),
    )
    lp = request_dict["load_profile"]
    if lp["source"] == "csv_base64":
        load_kw, ts = _decode_csv_to_load_kw(lp["csv_base64"], lp.get("granularity_min", 60), site.rocna_spotreba_kwh)
    else:
        params = _profile_template_params(lp.get("profile_template", "kancelaria"))
        df_syn = synthetic_load_profile(annual_kwh=site.rocna_spotreba_kwh, year=2025, granularity_min=60, **params)
        load_kw = df_syn["load_kw"].to_numpy(); ts = df_syn.index
    spot_df = pd.read_csv(SPOT_CSV); spot = spot_df["price_eur_per_mwh"].to_numpy()
    tariff_engine = TariffEngine.from_yaml(TARIFF_YAML)
    tov = request_dict.get("tariff_overrides") or {}
    if tov:
        for ty in tariff_engine._tariffs.values():
            if tov.get("silova_eur_mwh") is not None: ty.fix_silova_eur_mwh = float(tov["silova_eur_mwh"])
            if tov.get("distribucia_eur_mwh") is not None: ty.distrib_eur_mwh = float(tov["distribucia_eur_mwh"])
            if tov.get("tps_eur_mwh") is not None: ty.tps_eur_mwh = float(tov["tps_eur_mwh"])
            if tov.get("oze_eur_mwh") is not None: ty.njf_eur_mwh = float(tov["oze_eur_mwh"])
            if tov.get("ostatne_eur_mwh") is not None:
                _h = float(tov["ostatne_eur_mwh"]) / 2; ty.spotrebna_dan_eur_mwh = _h; ty.tss_eur_mwh = _h
            if tov.get("mrk_kapacita_eur_mw_mes") is not None: ty.mrk_kapacita_eur_mw_mes = float(tov["mrk_kapacita_eur_mw_mes"])

    n = min(len(load_kw), len(spot), 8760)
    load_kw = load_kw[:n]; spot = spot[:n]
    ts = ts[:n] if len(ts) > n else ts
    if len(ts) < n:
        ts = pd.date_range("2025-01-01 00:00", periods=n, freq="1h")
    load_df = pd.DataFrame({"load_kw": load_kw}, index=ts)
    capex = request_dict.get("capex", {}); fin = request_dict.get("financial", {})

    gen = VariantGenerator(
        site=site, load_df=load_df, spot_eur_mwh=spot, timestamps=ts, tariff_engine=tariff_engine,
        pv_kwp_options=[pv_kwp], bess_kwh_options=[bess_kwh], ems_strategies=[ems_strategy],
        capex_pv_eur_per_kwp=capex.get("capex_pv_eur_per_kwp", 800),
        capex_bess_eur_per_kwh=capex.get("capex_bess_eur_per_kwh", 480),
        dppo_pct=fin.get("dppo_pct", 0.22), discount_rate=fin.get("discount_rate", 0.06),
        horizon_years=fin.get("horizon_years", 20), depr_years=fin.get("depr_years", 6),
    )
    r = gen.run_single(pv_kwp, bess_kwh, ems_strategy, keep_intervals=True)

    load_before, after, pv_out, batt, soc_kwh, spot_out = [], [], [], [], [], []
    if r.intervals:
        for iv in r.intervals:
            dt = iv.dt_hours or 1.0
            imp = iv.grid_import_kwh / dt; exp = iv.grid_export_kwh / dt
            charge = (iv.pv_to_bat_kwh + iv.grid_to_bat_kwh) / dt
            disch = iv.bat_to_load_kwh / dt
            load_before.append(round(iv.load_kw, 2)); after.append(round(imp - exp, 2))
            pv_out.append(round(iv.pv_kw, 2)); batt.append(round(disch - charge, 2))
            soc_kwh.append(round(iv.bat_soc_kwh_end, 2)); spot_out.append(round(iv.spot_eur_mwh, 1))
    else:
        pv_in = gen._make_pv(pv_kwp)
        if pv_in:
            pv_kw = PVSystemSim(pv_in, site).simulate_year(int(pd.Timestamp(ts[0]).year), 60)["pv_kw"].to_numpy()[:n]
        else:
            pv_kw = _np.zeros(n)
        if len(pv_kw) < n:
            pv_kw = _np.concatenate([pv_kw, _np.zeros(n - len(pv_kw))])
        mrk_lim = float(site.mrk_kw or 0)
        neg_curtail = bool((request_dict.get("ems_config") or {}).get("negative_spot_curtail", True))
        for i in range(n):
            L = float(load_kw[i]); P = float(pv_kw[i]); sp = float(spot[i])
            pv_to_load = min(P, L); exp = max(0.0, P - pv_to_load)
            if neg_curtail and sp < 0: exp = 0.0
            if mrk_lim: exp = min(exp, mrk_lim)
            grid = L - pv_to_load
            load_before.append(round(L, 2)); after.append(round(grid - exp, 2))
            pv_out.append(round(P, 2)); batt.append(0.0); soc_kwh.append(0.0); spot_out.append(round(sp, 1))

    return {
        "granularity_min": 60,
        "start_iso": pd.Timestamp(ts[0]).isoformat(),
        "n": len(load_before),
        "variant": {"pv_kwp": float(pv_kwp), "bess_kwh": float(bess_kwh),
                    "bess_usable_kwh": round(float(bess_kwh) * 0.90, 1)},
        "load_before_kw": load_before,
        "net_load_after_kw": after,
        "pv_kw": pv_out,
        "battery_kw": batt,
        "soc_kwh": soc_kwh,
        "spot_eur_mwh": spot_out,
    }
