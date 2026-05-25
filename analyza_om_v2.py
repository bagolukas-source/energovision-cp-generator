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
    """Konvertuje DB záznam analyza_om na engine RunVariantsRequest dict.
    
    Sizing logika (priority order):
    1. annual MWh known → optimal kWp = annual_MWh × 1000 / 1050 (PV yield SK)
    2. MRK known → siz okolo MRK (50%/80%/100%/150%)
    3. RK known → siz okolo RK
    4. fallback 30 kWp (small residential)
    """
    annual_kwh = float(analyza.get("consumption_annual_mwh", 0) or 0) * 1000
    mrk_kw = float(analyza["om_mrk_kw"]) if analyza.get("om_mrk_kw") else None
    rk_kw = float(analyza["om_rk_kw"]) if analyza.get("om_rk_kw") else None
    
    # MAX_FVE_KWP — distribučný limit: FVE AC výkon ≤ MRK (zmluva s distribútorom).
    # FVE DC kWp môže byť o ~20 % vyššie (DC:AC pomer 1.2, klasický over-sizing menica),
    # ale viac je nezmysel — energia by sa orezala alebo by porušilo MRK.
    max_export = float(analyza["max_export_kw"]) if analyza.get("max_export_kw") else None
    hard_cap_kwp = None
    if mrk_kw:
        # FVE DC ≤ MRK × 1.2 (DC over-sizing pomer)
        hard_cap_kwp = mrk_kw * 1.2
    if max_export and (not hard_cap_kwp or max_export * 1.2 < hard_cap_kwp):
        hard_cap_kwp = max_export * 1.2
    
    # Optimal FVE size — preferuj realny annual spotreba ak existuje
    if annual_kwh > 1000:
        # 100% self-consumption target = annual_MWh × 1000 / 1050 kWh/kWp
        optimal_kwp = annual_kwh / 1050
    elif mrk_kw:
        # B2B without consumption history — sizuj na MRK (DC:AC 1.0)
        optimal_kwp = mrk_kw
    elif rk_kw:
        optimal_kwp = rk_kw
    else:
        optimal_kwp = 30  # small residential default
    
    # CAP na MRK distribučný limit
    if hard_cap_kwp and optimal_kwp > hard_cap_kwp:
        optimal_kwp = hard_cap_kwp
    
    # 4 PV varianty: 40%/65%/85%/100% (max = MRK × 1.2 = hard_cap)
    pv_options = [
        round(optimal_kwp * 0.4, 0),
        round(optimal_kwp * 0.65, 0),
        round(optimal_kwp * 0.85, 0),
        round(optimal_kwp, 0),
    ]
    # Hard cap — žiadny variant nesmie prekročiť MRK × 1.2
    if hard_cap_kwp:
        pv_options = [min(p, hard_cap_kwp) for p in pv_options]
    pv_options = [p for p in pv_options if p >= 5]
    pv_options = sorted(set(pv_options))  # dedup + sort
    
    # BESS variants — scaling podľa PV size
    if optimal_kwp <= 30:
        bess_options = [0, 10, 30]
    elif optimal_kwp <= 100:
        bess_options = [0, 50, 100]
    elif optimal_kwp <= 300:
        bess_options = [0, 100, 250]
    elif optimal_kwp <= 800:
        bess_options = [0, 200, 500]
    else:
        bess_options = [0, 500, 1000]
    
    if annual_kwh <= 0:
        annual_kwh = optimal_kwp * 1000  # ~1000 kWh/kWp rule of thumb
    
    base_kwp = optimal_kwp  # legacy compat
    
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
    from energovision_analytics.api.services.engine_service import run_variants_pipeline, build_run_variants_response
    
    # Načítaj analyza_om
    a_res = sb.table("analyza_om").select("*").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        raise ValueError(f"Analyza {analyza_id} not found")
    
    # Update status
    sb.table("analyza_om").update({"status": "running"}).eq("id", analyza_id).execute()
    
    request_dict = _build_request_from_analyza(analyza)
    log.info(f"[aom-v2] Running pipeline for {analyza_id} with {len(request_dict['variants']['pv_kwp_options'])} PV × {len(request_dict['variants']['bess_kwh_options'])} BESS")
    
    # Wrap engine call — pri chybe nastav status na 'failed' aby analyza neuviazla v running
    try:
        raw_result = run_variants_pipeline(request_dict)
        result = build_run_variants_response(raw_result)
    except Exception as engine_err:
        log.exception("[aom-v2] engine pipeline failed for %s", analyza_id)
        sb.table("analyza_om").update({
            "status": "failed",
            "econ_results": {"error": str(engine_err)[:500]},
            "updated_at": datetime.now().isoformat(),
        }).eq("id", analyza_id).execute()
        raise
    
    # Save variants do DB — variants sú teraz JSON-serializable dicts
    variants = result.get("variants") or []
    if variants:
        # Clear existing variants
        sb.table("analyza_om_variants").delete().eq("analyza_id", analyza_id).execute()
        
        rows = []
        for idx, v in enumerate(variants):
            rows.append({
                "analyza_id": analyza_id,
                "name": v.get("label", f"V{idx+1}"),
                "position": idx + 1,
                "fve_kwp": v.get("pv_kwp", 0),
                "fve_tilt_deg": 35,
                "fve_azimuth_deg": 180,
                "fve_topology": "south",  # default tilt/azimuth topology (constraint: south|east_west|tracker|carport)
                "bess_kwh": v.get("bess_kwh", 0),
                "bess_kw": v.get("bess_kw", 0),
                "bess_arbitrage_enabled": v.get("bess_kwh", 0) > 0,
                "capex_eur": v.get("capex_total_eur", 0),
                "capex_source": "engine_v095_quick",
                "result_samosp_pct": v.get("samospotreba_pct", 0),
                "result_samostat_pct": v.get("samostatnost_pct", 0),
                "result_export_mwh": v.get("export_kwh", 0) / 1000 if v.get("export_kwh") else 0,
                "result_import_mwh": (v.get("grid_import_kwh", 0) or 0) / 1000,
                "result_npv_eur_base": v.get("npv_eur", 0),
                "result_irr_pct_base": v.get("irr_pct", 0),
                "result_payback_y_base": v.get("payback_simple_y", 0),
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
    
    a_res = sb.table("analyza_om").select("*, customers(first_name, last_name, company_name, email, ico)").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        raise ValueError(f"Analyza {analyza_id} not found")
    
    v_res = sb.table("analyza_om_variants").select("*").eq("analyza_id", analyza_id).order("position").execute()
    db_variants = v_res.data or []
    if not db_variants:
        raise ValueError("No variants — spusti run_variants_premium najprv")
    
    # Customer name: preferuj company_name pre B2B, inak first+last_name
    cust = analyza.get("customers") or {}
    if cust.get("company_name"):
        cust_display_name = cust["company_name"]
    else:
        cust_display_name = f"{cust.get('first_name') or ''} {cust.get('last_name') or ''}".strip() or "Klient"
    # Backward compat — niektoré reporting funkcie čítajú customer.get("name")
    if isinstance(cust, dict) and "name" not in cust:
        cust["name"] = cust_display_name
    
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


def _nominatim_geocode_psc(psc: str) -> dict | None:
    """Geocoduje SK PSČ cez OpenStreetMap Nominatim (free, no key).
    Vráti {lat, lon, city, region} alebo None pri chybe.
    User-Agent header POVINNÝ — Nominatim ban policy."""
    import requests
    psc_clean = psc.strip().replace(" ", "")
    if not psc_clean or len(psc_clean) != 5:
        return None
    psc_formatted = f"{psc_clean[:3]} {psc_clean[3:]}"  # "95605" → "956 05"
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "postalcode": psc_formatted,
                "country": "Slovakia",
                "format": "json",
                "limit": 1,
            },
            headers={"User-Agent": "Energovision-CRM/1.0 (lukas.bago@energovision.sk)"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        hit = data[0]
        # Display name format: "956 05, Radošina, okres Topoľčany, Nitriansky kraj, Slovensko"
        display = hit.get("display_name", "")
        parts = [p.strip() for p in display.split(",")]
        city = parts[1] if len(parts) > 1 else None
        return {
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "city": city,
            "display_name": display,
        }
    except Exception as e:
        log.warning(f"[nominatim] geocode failed for psc={psc}: {e}")
        return None


def auto_fill_site_from_psc(psc: str, rocna_spotreba_kwh: float = 30000, rk_kw: float = 25) -> dict:
    """PSČ → distribútor + GPS + odporúčaný profil.
    GPS: primárne Nominatim (OSM, presné), fallback engine psc_to_gps (hrubé)."""
    from energovision_analytics.data.auto_fill import auto_fill_site
    
    # 1) Nominatim — presné GPS podľa PSČ centra
    nominatim = _nominatim_geocode_psc(psc)
    
    try:
        site = auto_fill_site(
            nazov="Auto-fill",
            psc=psc,
            rocna_spotreba_kwh=rocna_spotreba_kwh,
            rk_kw=rk_kw,
        )
        # SiteInput používa gps_lat/gps_lon, nie lat/lon — ale preferujeme Nominatim
        engine_lat = getattr(site, "gps_lat", None) or getattr(site, "lat", None)
        engine_lon = getattr(site, "gps_lon", None) or getattr(site, "lon", None)
        
        # Použiť Nominatim ak je dostupný, inak engine fallback
        if nominatim:
            final_lat = nominatim["lat"]
            final_lon = nominatim["lon"]
            gps_source = "nominatim"
            city = nominatim.get("city")
        else:
            final_lat = engine_lat
            final_lon = engine_lon
            gps_source = "engine_fallback"
            city = None
        
        # MRK + sadzba sú IBA NÁVRHY (heuristika z PSČ + 1.2×RK)
        # Skutočná hodnota MRK musí prísť z faktúry alebo distribučnej zmluvy klienta
        return {
            "ok": True,
            "distribuutor": site.distribuutor.value if hasattr(site.distribuutor, "value") else str(site.distribuutor),
            "lat": final_lat,
            "lon": final_lon,
            "gps_source": gps_source,
            "city": city,
            # Sadzba a MRK sú IBA orientačné — UI ich má použiť len ak používateľ nemá vlastné
            "suggested_sadzba": site.sadzba.value if hasattr(site.sadzba, "value") else str(site.sadzba),
            "suggested_mrk_kw_heuristic": site.mrk_kw,
            "note": "MRK je orientačná hodnota (engine heuristika 1.2×RK). Reálne MRK príde z faktúry / distribučnej zmluvy klienta.",
            "fakturacny_psc": getattr(site, "fakturacny_psc", None),
        }
    except Exception as e:
        log.exception(f"[auto-fill-site] failed for psc={psc}")
        return {"ok": False, "error": str(e)}


def quick_estimate(payload: dict) -> dict:
    """Rýchla kalkulácia bez 15-min dát. Vstupy: kwp, annual_kwh, tarif_buy, psc."""
    kwp = float(payload.get("kwp", 0))
    annual_kwh = float(payload.get("annual_kwh", 0))
    tarif_buy = float(payload.get("tarif_buy", 0.18))  # €/kWh default
    capex_per_kwp = float(payload.get("capex_per_kwp", 800))
    bess_kwh = float(payload.get("bess_kwh", 0))
    capex_per_bess_kwh = float(payload.get("capex_per_bess_kwh", 480))
    discount_rate = float(payload.get("discount_rate", 0.06))
    
    # Heuristics z reálnych ponúk + spot 2025
    yield_per_kwp = 1050  # kWh/kWp/rok pre SK
    self_consumption = 0.65 if bess_kwh > 0 else 0.40  # samospotreba %
    
    pv_production_kwh = kwp * yield_per_kwp
    self_used_kwh = min(pv_production_kwh * self_consumption, annual_kwh * 0.85)
    export_kwh = pv_production_kwh - self_used_kwh
    
    # Úspora = nahradené nákupy + (export × spot priemer ~80 €/MWh)
    saved_buy_eur = self_used_kwh * tarif_buy
    export_revenue_eur = export_kwh * 0.08  # 80 €/MWh
    annual_savings = saved_buy_eur + export_revenue_eur
    
    capex_total = kwp * capex_per_kwp + bess_kwh * capex_per_bess_kwh
    payback_years = capex_total / annual_savings if annual_savings > 0 else 999
    
    # Simplified NPV — 15 rokov, discount rate 6%
    horizon = 15
    cashflows = [-capex_total] + [annual_savings * (0.992 ** y) for y in range(1, horizon + 1)]
    npv = sum(cf / ((1 + discount_rate) ** y) for y, cf in enumerate(cashflows))
    
    return {
        "ok": True,
        "kwp": kwp,
        "bess_kwh": bess_kwh,
        "pv_production_kwh": round(pv_production_kwh),
        "self_used_kwh": round(self_used_kwh),
        "export_kwh": round(export_kwh),
        "self_consumption_pct": round(self_consumption * 100),
        "annual_savings_eur": round(annual_savings),
        "capex_eur": round(capex_total),
        "payback_years": round(payback_years, 1),
        "npv_eur": round(npv),
        "co2_saved_tons_per_year": round(pv_production_kwh * 0.000236, 2),  # 236 g CO2/kWh SK mix
    }
