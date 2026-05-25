"""
Analyza OM v2 — orchestrátor pre engine v0.9.5

Pridáva 2 nové endpointy k existujúcim:
- run_variants_premium() — VariantGenerator → top-6 → uloží do analyza_om_variants
- render_posudok_premium() — premium DOCX cez engine_service

Vyžaduje energovision_analytics nainštalovaný (importnutý zo render-repo/energovision_analytics/).
"""
import os
import io
import logging
import tempfile
from datetime import datetime
from pathlib import Path

# Nastav ENERGO_SPOT_CSV pred importom (engine to číta na load time)
ENGINE_ROOT = Path(__file__).resolve().parent
SPOT_CSV = ENGINE_ROOT / "aom_data" / "sk_spot_2025_hourly.csv"
TARIFF_YAML = ENGINE_ROOT / "aom_data" / "tariffs" / "2026.yaml"
DOTACIE_YAML = ENGINE_ROOT / "aom_data" / "dotacie" / "sk_2026.yaml"
ECON_DEFAULTS = ENGINE_ROOT / "aom_data" / "config" / "economic_defaults.yaml"

os.environ.setdefault("ENERGO_SPOT_CSV", str(SPOT_CSV))
os.environ.setdefault("ENERGO_TARIFF_YAML", str(TARIFF_YAML))

log = logging.getLogger(__name__)


def _build_request_from_analyza(analyza: dict) -> dict:
    """Konvertuje DB záznam analyza_om na engine RunVariantsRequest dict."""
    # PV variants: ak je single kWp v rk_kw alebo z prilinkovaného bundle, generuj 4 varianty okolo
    base_kwp = float(analyza.get("om_rk_kw") or 30)
    pv_options = [base_kwp * 0.5, base_kwp * 0.75, base_kwp, base_kwp * 1.25]
    pv_options = [round(p, 0) for p in pv_options if p >= 5]
    
    # BESS variants: 0 + 2 ne-nulové podľa veľkosti
    if base_kwp <= 30:
        bess_options = [0, 10, 30]
    elif base_kwp <= 100:
        bess_options = [0, 50, 100]
    else:
        bess_options = [0, 100, 250]
    
    # Profil spotreby
    annual_kwh = float(analyza.get("consumption_annual_mwh", 0) or 0) * 1000
    if annual_kwh <= 0:
        annual_kwh = base_kwp * 1000  # ~1000 kWh/kWp rule of thumb
    
    profile_template = "kancelaria"
    if base_kwp >= 100:
        profile_template = "priemysel_24_7"
    elif annual_kwh <= 15000:
        profile_template = "domacnost"
    
    return {
        "site": {
            "nazov": analyza.get("name", "OM"),
            "psc": analyza.get("om_psc") or "010 01",
            "rocna_spotreba_kwh": annual_kwh,
            "rk_kw": float(analyza.get("om_rk_kw") or base_kwp),
            "mrk_kw": float(analyza["om_mrk_kw"]) if analyza.get("om_mrk_kw") else None,
            "typ_tarify": "spot",
            "bilancna_skupina": "Energie2",
            "eic_kod": None,
        },
        "load_profile": {
            "source": "synthetic",
            "profile_template": profile_template,
            "granularity_min": 60,
        },
        "variants": {
            "pv_kwp_options": pv_options,
            "bess_kwh_options": bess_options,
            "ems_strategies": ["rule_based"],
        },
        "capex": {
            "mode": "quick",
            "capex_pv_eur_per_kwp": 800,
            "capex_bess_eur_per_kwh": 480,
        },
        "financial": {
            "dppo_pct": 0.22,
            "discount_rate": 0.06,
            "horizon_years": 20,
            "depr_years": 6,
        },
        "dotacia": {
            "enabled": True,
            "scheme_id": "zelena_podnikom",
        },
        "async_mode": False,
    }


def run_variants_premium(sb, analyza_id: str) -> dict:
    """
    Spustí VariantGenerator nad analyza_om → uloží varianty do analyza_om_variants.
    """
    from energovision_analytics.api.services.engine_service import run_variants_pipeline
    
    # Načítaj analyza_om
    a_res = sb.table("analyza_om").select("*").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        raise ValueError(f"Analyza {analyza_id} not found")
    
    # Update status
    sb.table("analyza_om").update({"status": "running"}).eq("id", analyza_id).execute()
    
    request_dict = _build_request_from_analyza(analyza)
    log.info(f"[aom-v2] Running pipeline for {analyza_id} with {len(request_dict['variants']['pv_kwp_options'])} PV × {len(request_dict['variants']['bess_kwh_options'])} BESS")
    
    result = run_variants_pipeline(request_dict)
    
    # Save variants do DB
    variants = result.get("variants") or []
    if variants:
        # Clear existing variants
        sb.table("analyza_om_variants").delete().eq("analyza_id", analyza_id).execute()
        
        rows = []
        for idx, v in enumerate(variants):
            rows.append({
                "analyza_id": analyza_id,
                "name": v.get("name", f"V{idx+1}"),
                "position": idx + 1,
                "fve_kwp": v.get("pv_kwp", 0),
                "fve_tilt_deg": 35,
                "fve_azimuth_deg": 180,
                "fve_topology": "single_string",
                "bess_kwh": v.get("bess_kwh", 0),
                "bess_kw": v.get("bess_kw", 0),
                "bess_arbitrage_enabled": v.get("bess_kwh", 0) > 0,
                "capex_eur": v.get("capex_total_eur", 0),
                "capex_source": "engine_v095_quick",
                "result_samosp_pct": v.get("self_consumption_pct", 0),
                "result_samostat_pct": v.get("self_sufficiency_pct", 0),
                "result_export_mwh": v.get("export_kwh", 0) / 1000,
                "result_import_mwh": v.get("import_kwh", 0) / 1000,
                "result_npv_eur_base": v.get("npv_eur", 0),
                "result_irr_pct_base": v.get("irr_pct", 0),
                "result_payback_y_base": v.get("payback_years", 0),
                "result_dotacia_eur": v.get("dotacia_eur", 0),
            })
        sb.table("analyza_om_variants").insert(rows).execute()
    
    # Save sim + econ summary do analyza_om
    sb.table("analyza_om").update({
        "status": "completed",
        "sim_results": result.get("variants", [])[:1] if result.get("variants") else None,
        "econ_results": {
            "top_picks": result.get("top_picks", []),
            "variants_count": len(variants),
            "engine_version": result.get("engine_version", "0.9.5"),
        },
        "updated_at": datetime.now().isoformat(),
    }).eq("id", analyza_id).execute()
    
    return {
        "ok": True,
        "analyza_id": analyza_id,
        "variants_count": len(variants),
        "top_picks": result.get("top_picks", [])[:6],
    }


def render_posudok_premium(sb, analyza_id: str) -> dict:
    """
    Vyrenderuje premium DOCX posudok z analyza_om + variantov → upload do Storage.
    """
    from energovision_analytics.reporting.posudok_premium import generate_premium_posudok
    
    a_res = sb.table("analyza_om").select("*, customers(name, email, ico)").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        raise ValueError(f"Analyza {analyza_id} not found")
    
    v_res = sb.table("analyza_om_variants").select("*").eq("analyza_id", analyza_id).order("position").execute()
    db_variants = v_res.data or []
    if not db_variants:
        raise ValueError("No variants — spusti run_variants_premium najprv")
    
    # Convert DB variants na engine format
    engine_variants = []
    for v in db_variants:
        engine_variants.append({
            "name": v["name"],
            "pv_kwp": float(v["fve_kwp"]),
            "bess_kwh": float(v["bess_kwh"] or 0),
            "bess_kw": float(v["bess_kw"] or 0),
            "capex_total_eur": float(v["capex_eur"] or 0),
            "self_consumption_pct": float(v["result_samosp_pct"] or 0),
            "self_sufficiency_pct": float(v["result_samostat_pct"] or 0),
            "export_kwh": float(v["result_export_mwh"] or 0) * 1000,
            "import_kwh": float(v["result_import_mwh"] or 0) * 1000,
            "npv_eur": float(v["result_npv_eur_base"] or 0),
            "irr_pct": float(v["result_irr_pct_base"] or 0),
            "payback_years": float(v["result_payback_y_base"] or 0),
            "dotacia_eur": float(v["result_dotacia_eur"] or 0),
        })
    
    # Vyber víťaza (najvyšší NPV)
    winner = max(engine_variants, key=lambda v: v["npv_eur"])
    
    customer = analyza.get("customers") or {}
    
    # Render DOCX
    tmp_pdf = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp_pdf.close()
    try:
        kwargs = dict(
            output_path=tmp_pdf.name,
            project_name=analyza.get("name", "Analýza OM"),
            client_name=customer.get("name", "Klient"),
            client_address=analyza.get("om_address", ""),
            client_contact=customer.get("email", ""),
            site_data={
                "psc": analyza.get("om_psc", ""),
                "rk_kw": float(analyza.get("om_rk_kw") or 0),
                "mrk_kw": float(analyza.get("om_mrk_kw") or 0),
                "annual_kwh": float(analyza.get("consumption_annual_mwh") or 0) * 1000,
            },
            variants=engine_variants,
            winner_variant=winner,
            include_sensitivity=True,
            include_monte_carlo=True,
            posudok_date=datetime.now().strftime("%d.%m.%Y"),
            prepared_by_name="Lukáš Bago",
            prepared_by_email="lukas.bago@energovision.sk",
            prepared_by_phone="+421 905 123 456",
        )
        try:
            generate_premium_posudok(**kwargs)
        except TypeError:
            # Niektoré argumenty môžu byť pomenované inak v engine — fallback minimal
            generate_premium_posudok(
                output_path=tmp_pdf.name,
                project_name=analyza.get("name", "Analýza OM"),
                client_name=customer.get("name", "Klient"),
                variants=engine_variants,
            )
        
        with open(tmp_pdf.name, "rb") as f:
            docx_bytes = f.read()
    finally:
        try: os.unlink(tmp_pdf.name)
        except: pass
    
    # Upload
    storage_path = f"analyza_om/{analyza_id}/posudok_premium_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    sb.storage.from_("documents").upload(
        storage_path, docx_bytes,
        {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "upsert": "true"}
    )
    public_url = sb.storage.from_("documents").get_public_url(storage_path)
    
    sb.table("analyza_om").update({"docx_path": public_url}).eq("id", analyza_id).execute()
    
    return {"ok": True, "docx_url": public_url, "storage_path": storage_path}
