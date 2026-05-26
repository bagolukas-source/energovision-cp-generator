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
    
    # Engine semantika: rk_kw = max IMPORT zo siete, mrk_kw = max EXPORT do siete.
    # SK terminológia: om_mrk_kw / om_rk_kw oboje hovoria o IMPORTNEJ kapacite (rezervovanej).
    # max_export_kw je samostatný field z pripojovacej zmluvy distribútora.
    # Fallback: ak chýba export, default = rovnaké ako import (so safety max).
    sk_import_kw = (
        float(analyza["om_mrk_kw"]) if analyza.get("om_mrk_kw")
        else float(analyza["om_rk_kw"]) if analyza.get("om_rk_kw")
        else base_kwp  # last-resort fallback
    )
    sk_export_kw = float(analyza["max_export_kw"]) if analyza.get("max_export_kw") else sk_import_kw
    engine_rk_kw = sk_import_kw
    engine_mrk_kw = max(sk_export_kw, sk_import_kw)  # engine validates mrk >= rk

    return {
        "site": {
            "nazov": analyza.get("name", "OM"),
            "psc": analyza.get("om_psc") or "010 01",
            "rocna_spotreba_kwh": annual_kwh,
            "rk_kw": engine_rk_kw,
            "mrk_kw": engine_mrk_kw,
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
            "enabled": bool(analyza.get("dotacia_enabled", False)),
            "scheme_id": analyza.get("dotacia_scheme") or "zelena_podnikom",
        },
        "ems_config": {
            "arb_min_spread_eur_mwh": float(analyza.get("arb_min_spread_eur_mwh") or 30),
            "max_efc_per_year": float(analyza.get("max_efc_per_year") or 1000),
            "negative_spot_curtail": bool(analyza.get("negative_spot_curtail", True)),
            "mrk_export_penalty_eur_kwh": float(analyza.get("mrk_export_penalty_eur_kwh") or 0.03),
        },
        "tariff_overrides": {
            "silova_eur_mwh": analyza.get("tarif_silova_eur_mwh"),
            "distribucia_eur_mwh": analyza.get("tarif_distribucia_eur_mwh"),
            "tps_eur_mwh": analyza.get("tarif_tps_eur_mwh"),
            "oze_eur_mwh": analyza.get("tarif_oze_eur_mwh"),
            "ostatne_eur_mwh": analyza.get("tarif_ostatne_eur_mwh"),
            "fix_mes_eur": analyza.get("tarif_fix_mes_eur"),
            "mrk_kapacita_eur_mw_mes": analyza.get("tarif_mrk_kapacita_eur_mw_mes"),
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
    # full_response sa použije pri renderingu Premium DOCX posudku (musí mať bohatú schému variantov)
    sb.table("analyza_om").update({
        "status": "completed",
        "sim_results": result.get("variants", [])[:1] if result.get("variants") else None,
        "econ_results": {
            "top_picks": result.get("top_picks", []),
            "variants_count": len(variants),
            "engine_version": result.get("engine_version", "0.9.5"),
            "full_response": result,  # full build_run_variants_response output (variants+top_picks+manifest)
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
    
    Architektúra:
    - run_variants_premium ukladá full_response z build_run_variants_response do econ_results.full_response
    - tento endpoint ho vyberie a posiela priamo do generate_premium_posudok(run_response=...)
    - generate_premium_posudok vracia bytes (NEMÁ output_path parameter)
    """
    from energovision_analytics.reporting.posudok_premium import generate_premium_posudok
    
    a_res = sb.table("analyza_om").select("*, customers(first_name, last_name, company_name, email, ico)").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        raise ValueError(f"Analyza {analyza_id} not found")
    
    # Plný engine response — zapisuje sa pri run_variants_premium
    econ = analyza.get("econ_results") or {}
    run_response = econ.get("full_response")
    
    # Fallback: ak full_response chýba (legacy analýza pred patch1), rebuilduj minimal z analyza_om_variants
    if not run_response or not run_response.get("variants"):
        v_res = sb.table("analyza_om_variants").select("*").eq("analyza_id", analyza_id).order("position").execute()
        db_variants = v_res.data or []
        if not db_variants:
            raise ValueError("No variants — spusti run_variants_premium najprv")
        
        rebuilt_variants = []
        for v in db_variants:
            pv_kwp = float(v.get("fve_kwp") or 0)
            bess_kwh = float(v.get("bess_kwh") or 0)
            bess_kw = float(v.get("bess_kw") or 0)
            capex_total = float(v.get("capex_eur") or 0)
            dotacia = float(v.get("result_dotacia_eur") or 0)
            # Odhadneme capex split (PV ~70% ak BESS prítomné, inak 100%)
            if bess_kwh > 0:
                capex_pv = capex_total * 0.7
                capex_bess = capex_total * 0.3
            else:
                capex_pv = capex_total
                capex_bess = 0.0
            rebuilt_variants.append({
                "variant_id": v.get("name", f"V{v.get('position', 0)}"),
                "label": v.get("name", "Variant"),
                "pv_kwp": pv_kwp,
                "bess_kwh": bess_kwh,
                "bess_kw": bess_kw,
                "ems_strategy": "rule_based",
                "capex_pv_eur": capex_pv,
                "capex_bess_eur": capex_bess,
                "capex_total_eur": capex_total,
                "dotacia_eur": dotacia,
                "net_capex_eur": capex_total - dotacia,
                "samospotreba_pct": float(v.get("result_samosp_pct") or 0),
                "samostatnost_pct": float(v.get("result_samostat_pct") or 0),
                "pv_total_kwh": pv_kwp * 1050,  # PVGIS yield approx
                "grid_import_kwh": float(v.get("result_import_mwh") or 0) * 1000,
                "saving_y1_eur": 0,  # nepoznáme, fallback
                "npv_eur": float(v.get("result_npv_eur_base") or 0),
                "irr_pct": float(v.get("result_irr_pct_base") or 0),
                "payback_simple_y": float(v.get("result_payback_y_base") or 0),
                "lcoe_eur_mwh": 0,
                "lcos_eur_mwh": 0,
                "rank_labels": [],
            })
        run_response = {
            "variants": rebuilt_variants,
            "top_picks": econ.get("top_picks", []),
            "n_variants_run": len(rebuilt_variants),
            "manifest": {},
        }
    
    # Customer
    cust = analyza.get("customers") or {}
    if cust.get("company_name"):
        client_name = cust["company_name"]
    else:
        client_name = f"{cust.get('first_name') or ''} {cust.get('last_name') or ''}".strip() or "Klient"
    client_contact = cust.get("email") or ""
    
    # Site meta — kľúče ktoré posudok_premium očakáva
    site_meta = {
        "lokalita": analyza.get("om_address") or "",
        "psc": analyza.get("om_psc") or "",
        "distribuutor": analyza.get("om_distributor") or "",
        "sadzba": analyza.get("om_sadzba") or "NN",
        "typ_tarify": analyza.get("om_tarif_typ") or "spot",
        "rk_kw": float(analyza.get("om_rk_kw") or 0),
        "mrk_kw": float(analyza.get("om_mrk_kw") or 0),
        "rocna_spotreba_kwh": float(analyza.get("consumption_annual_mwh") or 0) * 1000,
    }
    
    # Project ID — engine_version pre manifest footer
    engine_version = econ.get("engine_version") or "0.9.5"
    project_id = analyza.get("name") or f"AOM-{str(analyza_id)[:8]}"
    
    # Render DOCX — funkcia VRACIA bytes (žiadny output_path!)
    docx_bytes = generate_premium_posudok(
        client_name=client_name,
        project_id=project_id,
        client_address=analyza.get("om_address") or "",
        client_contact=client_contact,
        project_name=analyza.get("name") or "Hybridné riešenie FVE + BESS",
        site_meta=site_meta,
        run_response=run_response,
        engine_version=engine_version,
        manifest_footer=f"Engine v{engine_version} | Analýza OM {project_id}",
        posudok_date=datetime.now().strftime("%d.%m.%Y"),
        prepared_by_name="Lukáš Bago",
        prepared_by_email="lukas.bago@energovision.sk",
        prepared_by_phone="0918 187 762",
    )
    
    # Upload do Storage
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


# ============================================================
# enrich_econ_full_response — obohatí econ_results.full_response
# po OLD pipeline (analyza_om/engine.py run_full_pipeline) bez
# prepisu analyza_om_variants. Volá NEW engine cez svoj auto-sizing
# z _build_request_from_analyza a posunie len full_response field.
# ============================================================
def enrich_econ_full_response(sb, analyza_id: str) -> dict:
    """
    Po starom run_full_pipeline pridá full_response (carbon, energy_flow,
    value_streams, monthly_summary, cashflow_array) do econ_results.
    NESAHA analyza_om_variants — len obohaí JSONB.

    Vracia: {"ok": True, "enriched": True, "winner": {...}} alebo {"ok": False, ...}
    """
    from energovision_analytics.api.services.engine_service import (
        run_variants_pipeline, build_run_variants_response
    )
    
    a_res = sb.table("analyza_om").select("id,name,om_psc,om_rk_kw,om_mrk_kw,max_export_kw,consumption_annual_mwh,consumption_peak_kw_hourly,econ_results").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        return {"ok": False, "error": "analyza not found"}
    
    # Vyrob request_dict (rovnaký path ako run_variants_premium)
    try:
        request_dict = _build_request_from_analyza(analyza)
    except Exception as e:
        log.exception("[enrich-full-response] build_request failed for %s", analyza_id)
        return {"ok": False, "error": f"build_request: {e}"}
    
    # Spusti NEW engine pipeline (chunked auto-matrix)
    try:
        raw_result = run_variants_pipeline(request_dict)
        result = build_run_variants_response(raw_result)
    except Exception as engine_err:
        log.exception("[enrich-full-response] engine pipeline failed for %s", analyza_id)
        return {"ok": False, "error": f"engine: {engine_err}"}
    
    # Vyber winner (top NPV variant) — to budú dáta pre posudok
    variants = result.get("variants") or []
    if not variants:
        return {"ok": False, "error": "no variants from engine"}
    
    # Najvyšší NPV s aspoň FVE > 0
    winner = max(
        (v for v in variants if (v.get("pv_kwp") or 0) > 0),
        key=lambda v: v.get("npv_eur") or 0,
        default=variants[0]
    )
    
    # Vytvor top_picks štruktúru aká je očakávaná v UI:
    # top_picks[0].results.{carbon, energy_flow, value_streams, monthly_summary, ...}
    top_picks_synth = [{
        "rank": 1,
        "topology": winner.get("label", "auto"),
        "pv_kwp": winner.get("pv_kwp"),
        "bess_kwh": winner.get("bess_kwh"),
        "bess_kw": winner.get("bess_kw"),
        "results": {
            "savings_eur_y1": winner.get("annual_save_eur") or winner.get("savings_eur_y1") or 0,
            "npv_eur": winner.get("npv_eur"),
            "irr_pct": winner.get("irr_pct"),
            "payback_y": winner.get("payback_simple_y") or winner.get("payback_y"),
            "carbon": winner.get("carbon") or {},
            "energy_flow": winner.get("energy_flow") or {},
            "value_streams": winner.get("value_streams") or {},
            "monthly_summary": winner.get("monthly_summary") or [],
            "cashflow_array": winner.get("cashflow_array") or [],
            "samospotreba_pct": winner.get("samospotreba_pct"),
            "samostatnost_pct": winner.get("samostatnost_pct"),
            "capex_total_eur": winner.get("capex_total_eur"),
        }
    }]
    
    # Mergni do existing econ_results (preserve old "variants" key z OLD enginu)
    existing_econ = analyza.get("econ_results") or {}
    new_econ = {
        **existing_econ,
        "top_picks": top_picks_synth,
        "full_response": result,
        "engine_enriched_at": datetime.now().isoformat(),
        "engine_version": result.get("engine_version", "0.9.5"),
    }
    
    sb.table("analyza_om").update({
        "econ_results": new_econ,
        "updated_at": datetime.now().isoformat(),
    }).eq("id", analyza_id).execute()
    
    return {
        "ok": True,
        "enriched": True,
        "winner": {
            "pv_kwp": winner.get("pv_kwp"),
            "bess_kwh": winner.get("bess_kwh"),
            "savings_eur_y1": top_picks_synth[0]["results"]["savings_eur_y1"],
            "co2_t": (winner.get("carbon") or {}).get("co2_avoided_t_per_year"),
            "bat_discharge_mwh": ((winner.get("energy_flow") or {}).get("bat_to_load_mwh") or 0),
        }
    }
