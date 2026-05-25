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
            r.financial.npv_eur += delta
    else:
        for r in results:
            delta = -r.dotacia_eur
            r.dotacia_eur = 0
            r.financial.dotacia_eur = 0
            r.financial.capex_net_eur = r.financial.capex_gross_eur
            r.financial.npv_eur += delta

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

    variants_out = []
    for r in results:
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
