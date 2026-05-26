"""
analyza_om/engine.py — high-level wrapper okolo CLI skriptov.
Exportuje funkcie pre Flask endpointy v app.py.
"""
from __future__ import annotations

import os
import io
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import numpy as np
import requests

log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = "analyza-om"


def _sb_headers() -> Dict[str, str]:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def storage_download(path: str) -> bytes:
    from urllib.parse import quote
    encoded = quote(path, safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{encoded}"
    r = requests.get(url, headers=_sb_headers(), timeout=60)
    r.raise_for_status()
    return r.content


def storage_upload(path: str, content: bytes, content_type: str = "application/octet-stream") -> bool:
    from urllib.parse import quote
    encoded = quote(path, safe="/")
    headers = {**_sb_headers(), "Content-Type": content_type, "x-upsert": "true"}
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{encoded}"
    r = requests.post(url, headers=headers, data=content, timeout=120)
    if r.status_code in (200, 201):
        return True
    log.error("storage_upload failed: %s %s", r.status_code, r.text[:300])
    return False


def sb_patch(table: str, id: str, payload: Dict[str, Any]) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{id}"
    headers = {**_sb_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"}
    r = requests.patch(url, headers=headers, json=payload, timeout=30)
    return r.status_code in (200, 204)


def sb_insert(table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**_sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"}
    r = requests.post(url, headers=headers, json=rows, timeout=30)
    if r.status_code in (200, 201):
        return r.json()
    log.error("sb_insert failed: %s %s", r.status_code, r.text[:300])
    return []




def _job_create(analyza_id: str, kind: str) -> Optional[str]:
    """Insert new job row, return job_id."""
    rows = sb_insert("analyza_om_jobs", [{
        "analyza_id": analyza_id,
        "kind": kind,
        "status": "running",
        "progress_pct": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }])
    return rows[0]["id"] if rows else None


def _job_update(job_id: str, **patch) -> None:
    if not job_id:
        return
    if patch.get("status") in ("done", "error"):
        patch["finished_at"] = datetime.now(timezone.utc).isoformat()
    sb_patch("analyza_om_jobs", job_id, patch)


def _bump_analyza_status(analyza_id: str, status: str) -> None:
    sb_patch("analyza_om", analyza_id, {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()})

# ---------------- Consumption parsing ----------------
def parse_consumption(analyza_id: str, file_paths: List[str], options: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Download files from Storage, parse, aggregate, return summary + paths to normalized profiles.
    """
    from . import extract_consumption as ec

    opts = options or {}
    series_list = []
    detected_formats = []
    warnings = []

    for storage_path in file_paths:
        try:
            content = storage_download(storage_path)
        except Exception as e:
            warnings.append(f"Download failed for {storage_path}: {e}")
            continue

        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(storage_path).suffix) as tf:
            tf.write(content)
            tmp = Path(tf.name)

        # Auto-detect format
        fmt = None
        try:
            if tmp.suffix.lower() == ".csv":
                series = ec.parse_sse_csv(tmp)
                fmt = "sse_csv_15min"
            elif tmp.suffix.lower() in (".xls", ".xlsx"):
                # Skús v poradí: sse_obis (OBIS kódy + koeficienty) → 96cols → zdis
                series = None
                fmt = None
                for parser_fn, label in [
                    (ec.parse_sse_obis_xls, "sse_obis_xls"),
                    (ec.parse_xls_96cols, "xls_96cols"),
                    (ec.parse_zdis_xls, "zdis_xls"),
                ]:
                    try:
                        series = parser_fn(tmp)
                        fmt = label
                        break
                    except Exception as ex:
                        warnings.append(f"{label} fallback: {ex}")
                if series is None:
                    raise RuntimeError("Žiadny XLS parser nezbehol")
            else:
                warnings.append(f"Unknown format for {storage_path}")
                continue
            series_list.append(series)
            detected_formats.append(fmt)
        except Exception as e:
            warnings.append(f"Parse failed for {storage_path}: {e}")
        finally:
            tmp.unlink(missing_ok=True)

    if not series_list:
        return {"status": "error", "error": "No files parsed", "warnings": warnings}

    # Aggregate (sum by default)
    combined_15min = series_list[0].copy()
    for s in series_list[1:]:
        combined_15min = combined_15min.add(s, fill_value=0)

    # Hourly aggregation
    hourly = ec.aggregate_to_hourly(combined_15min)

    # Summary metrics
    annual_kwh = float(hourly.sum())
    annual_mwh = annual_kwh / 1000.0
    peak_kw_15min = float(combined_15min.max() * 4)  # 15-min energy → kW (×4)
    peak_kw_hourly = float(hourly.max())
    avg_kw = float(hourly.mean())
    coverage_pct = 100.0 * (hourly.notna().sum() / len(hourly)) if len(hourly) > 0 else 0.0

    # Upload normalized files
    hourly_csv = hourly.to_csv().encode("utf-8")
    min15_csv = combined_15min.to_csv().encode("utf-8")
    hourly_path = f"{analyza_id}/consumption_profile.csv"
    min15_path = f"{analyza_id}/consumption_15min.csv"
    storage_upload(hourly_path, hourly_csv, "text/csv")
    storage_upload(min15_path, min15_csv, "text/csv")

    return {
        "status": "ok",
        "detected_formats": detected_formats,
        "summary": {
            "annual_mwh": round(annual_mwh, 2),
            "peak_kw_15min": round(peak_kw_15min, 1),
            "peak_kw_hourly": round(peak_kw_hourly, 1),
            "avg_kw": round(avg_kw, 1),
            "coverage_pct": round(coverage_pct, 2),
            "missing_intervals_count": int(hourly.isna().sum()),
        },
        "outputs": {
            "profile_hourly_path": hourly_path,
            "profile_15min_path": min15_path,
        },
        "warnings": warnings,
    }


# ---------------- Simulation ----------------
def run_simulation(analyza_id: str, profile_path: str, om: Dict[str, Any], variants: List[Dict[str, Any]],
                   pvgis_yield: Optional[float] = None) -> Dict[str, Any]:
    """Hodinová simulácia FVE+BESS pre N variantov."""
    from . import simulacia as sim_mod

    # Download profile
    content = storage_download(profile_path)
    df = pd.read_csv(io.BytesIO(content), index_col=0, parse_dates=True)
    if isinstance(df, pd.DataFrame):
        load_series = df.iloc[:, 0]
    else:
        load_series = df

    # Ensure exactly 8760 hours
    if len(load_series) != 8760:
        # Pad/truncate to 8760
        target_idx = pd.date_range("2025-01-01 00:00", periods=8760, freq="h")
        load_series = load_series.reindex(target_idx, fill_value=load_series.mean())

    load_arr = load_series.values.astype(float)
    timestamps = load_series.index

    lat = float(om.get("lat") or 48.4)
    lon = float(om.get("lon") or 17.6)
    mf = sim_mod.MF_NORTH if lat > 49.0 else sim_mod.MF_SK
    target_yield = pvgis_yield or 1085.0

    results = []
    for v in variants:
        topology = v.get("topology", "south")
        tilt = float(v.get("tilt", 25))
        az = float(v.get("az", 0))

        # PV generation per kWp
        if topology == "east_west":
            pv_per = np.array([sim_mod.pv_per_kWp_ew(ts, lat, lon, tilt, mf) for ts in timestamps])
        else:
            pv_per = np.array([sim_mod.pv_per_kWp(ts, lat, lon, tilt, az, mf) for ts in timestamps])

        # Calibrate to target yield — calibrate() vracia kalibrované pole (nie scalar!)
        pv_per_calibrated = sim_mod.calibrate(pv_per, target_yield)

        fve_kwp = float(v.get("fve_kwp", 0))
        bess_kwh = float(v.get("bess_kwh", 0))
        bess_kw = float(v.get("bess_kw", 0))

        pv_arr = pv_per_calibrated * fve_kwp
        sim_result = sim_mod.simulate(load_arr, pv_arr, bess_kwh=bess_kwh, bess_kw=bess_kw)

        # simulate() vracia: fve_prod, self_use_direct, bess_charge, bess_discharge,
        #                   grid_export, grid_import, self_use, self_use_ratio, coverage, total_load (všetko MWh okrem ratio/coverage)
        results.append({
            "id": v.get("id", v.get("name", "?")),
            "name": v.get("name", "?"),
            "fve_kwp": fve_kwp,
            "bess_kwh": bess_kwh,
            "bess_kw": bess_kw,
            "annual_yield_kwh": round(float(pv_arr.sum()), 0),
            "samosp_pct": round(sim_result.get("self_use_ratio", 0) * 100, 1),
            "samostat_pct": round(sim_result.get("coverage", 0) * 100, 1),
            "export_mwh": round(sim_result.get("grid_export", 0), 2),
            "import_mwh": round(sim_result.get("grid_import", 0), 2),
            "load_mwh": round(sim_result.get("total_load", 0), 2),
            "selfuse_mwh": round(sim_result.get("self_use", 0), 2),
            "peak_export_kw": round(float(np.max(np.maximum(0, pv_arr - load_arr))), 1),
            # Zachovať aj raw kľúče pre economics.calc_savings (schema compat — fix 2026-05-25)
            "self_use": round(sim_result.get("self_use", 0), 4),
            "grid_export": round(sim_result.get("grid_export", 0), 4),
            "grid_import": round(sim_result.get("grid_import", 0), 4),
            "fve_prod": round(sim_result.get("fve_prod", 0), 4),
            "total_load": round(sim_result.get("total_load", 0), 4),
            "self_use_direct": round(sim_result.get("self_use_direct", 0), 4),
            "bess_charge": round(sim_result.get("bess_charge", 0), 4),
            "bess_discharge": round(sim_result.get("bess_discharge", 0), 4),
            "self_use_ratio": round(sim_result.get("self_use_ratio", 0), 4),
            "coverage": round(sim_result.get("coverage", 0), 4),
        })

    return {"status": "ok", "variants": results, "lat": lat, "lon": lon, "target_yield": target_yield}


# ---------------- Economics ----------------
def calc_economics(sim_results: Dict[str, Any], tarif_buy: float, tarif_sell: float,
                   variants_capex: List[Dict[str, Any]], scenarios: Optional[List[str]] = None) -> Dict[str, Any]:
    """NPV/IRR/payback pre každý variant × scenár."""
    from . import economics as ec
    sc_list = scenarios or ["base", "low_sell", "spot_arb"]

    sim_variants = {v["id"]: v for v in sim_results.get("variants", [])}
    capex_map = {c["id"]: c for c in variants_capex}

    out = []
    for vid, sim_v in sim_variants.items():
        cap = capex_map.get(vid, {})
        capex = float(cap.get("capex_eur", 0))
        dotacia = float(cap.get("dotacia_eur", 0))
        if capex == 0:
            continue
        scenarios_results = {}
        for sc in sc_list:
            tb = tarif_buy
            ts = tarif_sell
            if sc == "low_sell":
                ts = tarif_sell * 0.5
            elif sc == "spot_arb":
                tb = tarif_buy * 0.9  # spot arbitrage assumption

            annual_save = ec.calc_savings(sim_v, tb, ts)
            npv_calc = ec.calc_npv(capex, annual_save, dotacia=dotacia)
            # economics vracia "irr"/"payback", defensive fallback aj na nové "irr_pct"/"payback_y"
            irr_val = npv_calc.get("irr_pct", npv_calc.get("irr", 0))
            payback_val = npv_calc.get("payback_y", npv_calc.get("payback", 0))
            scenarios_results[sc] = {
                "annual_save_eur": round(annual_save, 0),
                "npv_eur": round(npv_calc["npv"], 0),
                "irr_pct": round(float(irr_val), 2),
                "payback_y": round(float(payback_val), 2),
            }
        out.append({"id": vid, "name": sim_v["name"], "capex_eur": capex, "dotacia_eur": dotacia, "scenarios": scenarios_results})

    return {"status": "ok", "variants": out}


# ---------------- Full pipeline ----------------
def run_full_pipeline(analyza_id: str) -> Dict[str, Any]:
    """
    Orchestrator: zoberie analyza_om row, beží parse → sim → econ → ulozí späť do DB.
    Volá sa async (background) z webhook endpointu po vytvorení analýzy.
    """
    # 1. Load analyza row
    url = f"{SUPABASE_URL}/rest/v1/analyza_om?id=eq.{analyza_id}&select=*"
    r = requests.get(url, headers=_sb_headers(), timeout=30)
    if r.status_code != 200 or not r.json():
        return {"status": "error", "error": "analyza not found"}
    analyza = r.json()[0]

    # 2. Load variants
    url = f"{SUPABASE_URL}/rest/v1/analyza_om_variants?analyza_id=eq.{analyza_id}&order=position.asc"
    r = requests.get(url, headers=_sb_headers(), timeout=30)
    variants = r.json() if r.status_code == 200 else []
    if not variants:
        return {"status": "error", "error": "no variants"}

    _bump_analyza_status(analyza_id, "running")

    # 3. Parse consumption if not done
    profile_path = analyza.get("consumption_profile_path")
    if not profile_path:
        parse_job = _job_create(analyza_id, "parse_consumption")
        raw_files = analyza.get("consumption_raw_files") or []
        file_paths = [f.get("storage_path") for f in raw_files if f.get("storage_path")]
        if not file_paths:
            _job_update(parse_job, status="error", error_message="no consumption files")
            _bump_analyza_status(analyza_id, "error")
            return {"status": "error", "error": "no consumption files"}
        _job_update(parse_job, progress_pct=30)
        parse_result = parse_consumption(analyza_id, file_paths)
        if parse_result.get("status") != "ok":
            _job_update(parse_job, status="error", error_message=str(parse_result.get("error","")))
            _bump_analyza_status(analyza_id, "error")
            return parse_result
        _job_update(parse_job, progress_pct=100, status="done")
        profile_path = parse_result["outputs"]["profile_hourly_path"]
        sb_patch("analyza_om", analyza_id, {
            "consumption_profile_path": profile_path,
            "consumption_15min_path": parse_result["outputs"]["profile_15min_path"],
            "consumption_annual_mwh": parse_result["summary"]["annual_mwh"],
            "consumption_peak_kw_15min": parse_result["summary"]["peak_kw_15min"],
            "consumption_peak_kw_hourly": parse_result["summary"]["peak_kw_hourly"],
            "consumption_avg_kw": parse_result["summary"]["avg_kw"],
            "consumption_coverage_pct": parse_result["summary"]["coverage_pct"],
            "consumption_detected_format": parse_result.get("detected_formats", [None])[0],
            "consumption_parse_warnings": parse_result.get("warnings", []),
        })

    # 4. Build variants list for sim
    sim_variants = [{
        "id": str(v["id"]),
        "name": v["name"],
        "fve_kwp": v["fve_kwp"],
        "tilt": v.get("fve_tilt_deg", 25),
        "az": v.get("fve_azimuth_deg", 0),
        "topology": v.get("fve_topology", "south"),
        "bess_kwh": v.get("bess_kwh", 0),
        "bess_kw": v.get("bess_kw", 0),
    } for v in variants]

    # 5. Run simulation
    sim_job = _job_create(analyza_id, "sim_run")
    _job_update(sim_job, progress_pct=20)
    om = {"lat": analyza.get("om_lat"), "lon": analyza.get("om_lon")}
    sim_result = run_simulation(analyza_id, profile_path, om, sim_variants, analyza.get("pvgis_yield_kwh_per_kwp"))
    _job_update(sim_job, progress_pct=100, status="done")

    # 6. Calc economics
    econ_job = _job_create(analyza_id, "econ_calc")
    _job_update(econ_job, progress_pct=50)
    tarif_buy = float(analyza.get("tarif_buy") or 0.146)
    tarif_sell = float(analyza.get("tarif_sell") or 0.06)
    dotacia_on = bool(analyza.get("dotacia_enabled", False))
    variants_capex = [{"id": str(v["id"]), "capex_eur": v.get("capex_eur") or 0,
                        "dotacia_eur": (min(50000, 0.45 * (v.get("capex_eur") or 0)) if dotacia_on else 0)} for v in variants]
    econ_result = calc_economics(sim_result, tarif_buy, tarif_sell, variants_capex)
    _job_update(econ_job, progress_pct=100, status="done")

    # 7. Update DB
    sb_patch("analyza_om", analyza_id, {
        "sim_results": sim_result,
        "econ_results": econ_result,
        "status": "done",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    # 8. Update per-variant denormalized results
    sim_by_id = {v["id"]: v for v in sim_result.get("variants", [])}
    econ_by_id = {v["id"]: v for v in econ_result.get("variants", [])}
    for v in variants:
        vid = str(v["id"])
        sim_v = sim_by_id.get(vid)
        econ_v = econ_by_id.get(vid)
        if not sim_v:
            continue
        base = (econ_v or {}).get("scenarios", {}).get("base", {})
        sb_patch("analyza_om_variants", v["id"], {
            "result_samosp_pct": sim_v.get("samosp_pct"),
            "result_samostat_pct": sim_v.get("samostat_pct"),
            "result_export_mwh": sim_v.get("export_mwh"),
            "result_import_mwh": sim_v.get("import_mwh"),
            "result_npv_eur_base": base.get("npv_eur"),
            "result_irr_pct_base": base.get("irr_pct"),
            "result_payback_y_base": base.get("payback_y"),
            "result_dotacia_eur": (econ_v or {}).get("dotacia_eur"),
        })

    return {"status": "ok", "analyza_id": analyza_id, "variants_processed": len(variants)}
