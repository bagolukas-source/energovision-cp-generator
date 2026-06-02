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
    
    # SCENÁR-AWARE SIZING ────────────────────────────────────────
    scenario_type = (analyza.get("scenario_type") or "nova_fve").lower()
    existing_kwp = float(analyza.get("existing_fve_kwp") or 0)
    existing_bess = float(analyza.get("existing_bess_kwh") or 0)
    
    if scenario_type == "iba_bess_arbitraz":
        # Iba BESS arbitráž — žiadne FVE
        pv_options = [0]
        # BESS scale based on MRK power
        max_bess_kwh = (mrk_kw or 100) * 4  # 4h kapacita
        bess_options = [
            round(max_bess_kwh * 0.25, 0),
            round(max_bess_kwh * 0.5, 0),
            round(max_bess_kwh * 0.75, 0),
            round(max_bess_kwh, 0),
        ]
        bess_options = [b for b in bess_options if b >= 10]
        bess_options = sorted(set(bess_options))
    elif scenario_type == "pridanie_bess":
        # Existujúca FVE — pridať len BESS, FVE size nemenná
        pv_options = [existing_kwp] if existing_kwp > 0 else [round(optimal_kwp, 0)]
        # BESS pridanie: 4-6h kapacita podľa FVE
        base_fve = existing_kwp or optimal_kwp
        bess_options = [
            round(base_fve * 0.5, 0),
            round(base_fve * 1.0, 0),
            round(base_fve * 1.5, 0),
            round(base_fve * 2.0, 0),
        ]
        bess_options = [b for b in bess_options if b >= 10]
        bess_options = sorted(set(bess_options))
    elif scenario_type == "rozsirenie_fve":
        # Rozšírenie existujúcej FVE — generuj +ΔkWp varianty
        # Engine simuluje TOTAL FVE size (existujúca + nová), ale varianty zobrazí v UI
        # Available headroom = hard_cap - existing
        headroom = (hard_cap_kwp or optimal_kwp * 1.5) - existing_kwp
        if headroom < 20:
            # Žiadny priestor na rozšírenie — fallback 1 variant = existing
            pv_options = [existing_kwp]
        else:
            # 4 prírastky: 25%/50%/75%/100% headroom-u
            deltas = [headroom * 0.25, headroom * 0.5, headroom * 0.75, headroom]
            pv_options = [round(existing_kwp + d, 0) for d in deltas]
            pv_options = sorted(set([p for p in pv_options if p > existing_kwp]))
        # BESS scaling podľa total (zachované existujúce + možnosti add)
        total_max = pv_options[-1] if pv_options else existing_kwp
        if total_max <= 100:
            bess_options = [existing_bess, existing_bess + 50, existing_bess + 100]
        elif total_max <= 300:
            bess_options = [existing_bess, existing_bess + 100, existing_bess + 250]
        elif total_max <= 800:
            bess_options = [existing_bess, existing_bess + 200, existing_bess + 500]
        else:
            bess_options = [existing_bess, existing_bess + 500, existing_bess + 1000]
        bess_options = sorted(set([round(b, 0) for b in bess_options if b >= 0]))
    else:
        # nova_fve / custom — default 4 PV × 3 BESS matrix
        pv_options = [
            round(optimal_kwp * 0.4, 0),
            round(optimal_kwp * 0.65, 0),
            round(optimal_kwp * 0.85, 0),
            round(optimal_kwp, 0),
        ]
        if hard_cap_kwp:
            pv_options = [min(p, hard_cap_kwp) for p in pv_options]
        pv_options = [p for p in pv_options if p >= 5]
        pv_options = sorted(set(pv_options))
        
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


def _generate_ai_narrative(analyza: dict, variants: list, top_picks: list) -> dict:
    """Vygeneruje energetik-úvahu pre posudok pomocou Claude API.
    Vracia 4-paragraph narrative + scenárový kontext."""
    try:
        import anthropic
        import os
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        scenario_type = analyza.get("scenario_type") or "nova_fve"
        scenario_desc = analyza.get("scenario_description") or ""
        existing_kwp = float(analyza.get("existing_fve_kwp") or 0)
        existing_samosp = float(analyza.get("existing_fve_samosp_pct") or 0)
        mrk = analyza.get("om_mrk_kw")
        rk = analyza.get("om_rk_kw")
        annual = float(analyza.get("consumption_annual_mwh") or 0)

        # Top 3 varianty pre referenciu
        top_3 = sorted([v for v in variants if v.get("npv_eur") is not None],
                       key=lambda x: x.get("npv_eur") or 0, reverse=True)[:3]
        top_summary = [
            {
                "label": v.get("label", "?"),
                "pv_kwp": v.get("pv_kwp"),
                "bess_kwh": v.get("bess_kwh"),
                "capex_eur": v.get("capex_total_eur"),
                "npv_eur": v.get("npv_eur"),
                "irr_pct": v.get("irr_pct"),
                "payback_y": v.get("payback_simple_y"),
                "samosp_pct": v.get("samospotreba_pct"),
            }
            for v in top_3
        ]

        scenario_label = {
            "nova_fve": "Nová FVE (greenfield)",
            "rozsirenie_fve": f"Rozšírenie existujúcej FVE ({existing_kwp:.0f} kWp v prevádzke, samosp. {existing_samosp:.0f}%)",
            "pridanie_bess": f"Pridanie BESS k existujúcej FVE {existing_kwp:.0f} kWp",
            "iba_bess_arbitraz": "Iba BESS arbitráž bez FVE",
            "custom": "Custom scenár",
        }.get(scenario_type, scenario_type)

        prompt = f"""Si senior energetik s 15-ročnou praxou v dimenzovaní fotovoltických elektrární a BESS pre slovenský priemysel.

Klient {analyza.get('name', 'OM')} prišiel s týmto scenárom:
SCENÁR: {scenario_label}
{f'POZNÁMKA OBCHODNÍKA: {scenario_desc}' if scenario_desc else ''}

PARAMETRE ODBERNÉHO MIESTA:
- Ročná spotreba: {annual:.0f} MWh
- MRK (max rezerv. kapacita pre odber): {mrk} kW
- RK (rez. kapacita pre dodávku): {rk or 'nie zadané'} kW
- Sadzba: {analyza.get('om_sadzba', 'VN')}
- Tarif klienta: nákup {analyza.get('tarif_buy', 0.146):.3f} €/kWh, výkup {analyza.get('tarif_sell', 0.06):.3f} €/kWh

TOP 3 VARIANTY PODĽA NPV:
{top_summary}

Napíš 4 odseky úvahy v slovenčine pre TECHNICKO-EKONOMICKÝ POSUDOK (nie strojový dump):
1. PREČO TÁTO FVE/BESS DIMENZIA — argument prečo top variant pasuje k profilu odberu
2. PREČO BESS ÁNO/NIE — argument pre alebo proti batérií pri danej cene exportu a profile
3. STRATEGICKÝ POHĽAD 5-r HORIZONT — budúce zmeny tarif, dotácie, regulácia
4. ČO BY MOHLO ZLEPŠIŤ EKONOMIKU — konkrétne odporúčania (zmena tarif, optimizéry, etapizácia)

Píš ako energetik radí klientovi pri káve, nie ako AI. Konkrétne čísla. Energovision štýl — vecne, bez marketingu."""

        msg = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        narrative_text = msg.content[0].text if msg.content else ""

        return {
            "text": narrative_text,
            "scenario_type": scenario_type,
            "scenario_description": scenario_desc,
            "top_3_used": top_summary,
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        log.warning("[ai-narrative] generation failed: %s", e)
        return {"error": str(e)[:200], "scenario_type": analyza.get("scenario_type")}


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
    
    # AI NARRATIVE — Claude úvaha pre posudok (energetik perspective)
    ai_narrative = _generate_ai_narrative(analyza, result.get("variants", []), result.get("top_picks", []))

    # Save sim + econ summary do analyza_om
    # full_response sa použije pri renderingu Premium DOCX posudku (musí mať bohatú schému variantov)
    sb.table("analyza_om").update({
        "status": "completed",
        "sim_results": result.get("variants", [])[:1] if result.get("variants") else None,
        "econ_results": {
            "top_picks": result.get("top_picks", []),
            "variants_count": len(variants),
            "engine_version": result.get("engine_version", "0.9.5"),
            "full_response": result,
            "ai_narrative": ai_narrative,
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

    # Posudok number — buď explicit z analyza alebo auto-generate
    posudok_number = analyza.get("posudok_number")
    if not posudok_number:
        # Auto: P-YY-XXX format - počíta sa z poradia
        yr = datetime.now().strftime("%y")
        try:
            count_res = sb.table("analyza_om").select("id", count="exact").execute()
            seq = (count_res.count or 0)
            posudok_number = f"P-{yr}-{seq:03d}"
        except Exception:
            posudok_number = f"P-{yr}-XXX"

    # AI narrative + scenario z econ_results
    ai_narrative_obj = econ.get("ai_narrative") or {}
    ai_narrative_text = ai_narrative_obj.get("text", "") if isinstance(ai_narrative_obj, dict) else ""

    # Render DOCX — funkcia VRACIA bytes (žiadny output_path!)
    docx_bytes = generate_premium_posudok(
        client_name=client_name,
        project_id=project_id,
        posudok_number=posudok_number,
        client_address=analyza.get("om_address") or "",
        client_contact=client_contact,
        project_name=analyza.get("name") or "Hybridné riešenie FVE + BESS",
        site_meta=site_meta,
        run_response=run_response,
        engine_version=engine_version,
        manifest_footer=f"Engine v{engine_version} | {posudok_number} | Analýza OM {project_id}",
        posudok_date=datetime.now().strftime("%d.%m.%Y"),
        prepared_by_name="Lukáš Bago",
        prepared_by_email="lukas.bago@energovision.sk",
        prepared_by_phone="0918 187 762",
        # Scenario-aware
        scenario_type=analyza.get("scenario_type") or "nova_fve",
        scenario_description=analyza.get("scenario_description"),
        existing_fve_kwp=float(analyza.get("existing_fve_kwp") or 0),
        existing_bess_kwh=float(analyza.get("existing_bess_kwh") or 0),
        existing_fve_samosp_pct=float(analyza["existing_fve_samosp_pct"]) if analyza.get("existing_fve_samosp_pct") else None,
        ai_narrative=ai_narrative_text or None,
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


def _generate_ai_expert_commentary(context: dict, analyza: dict) -> dict:
    """
    Vygeneruje AI Expert posúdenie pre Orkestra posudok.

    Volá Claude Sonnet 4.5 (anthropic SDK, model claude-sonnet-4-5-20250929)
    s štruktúrovaným JSON o projekte. Output: 4 sekcie:
      - commentary: 3-4 paragrafov expert posúdenia (HTML s <p> tagmi)
      - recommendations: 5-7 konkrétnych odporúčaní
      - anomalies: 0-4 anomálie/quick wins
      - open_questions: 5-7 tailored otázok pre klienta

    Pri zlyhaní (timeout, parse error, network) vráti prázdne defaults
    a posudok sa vygeneruje BEZ AI sekcie (fail-safe).
    """
    import json
    try:
        from anthropic import Anthropic
        client = Anthropic()  # API key z ANTHROPIC_API_KEY env

        # Pripraviť čisté input data (skrátiť dlhé polia, zaokrúhliť čísla)
        input_data = {
            "klient": context.get("client_name"),
            "lokalita": context.get("site_address"),
            "om": {
                "mrk_kw": context.get("mrk_kw"),
                "rocna_spotreba_mwh": round(context.get("annual_kwh", 0) / 1000, 1),
                "tarif": context.get("tarif_typ"),
            },
            "vybrany_variant": {
                "nazov": context.get("label"),
                "fve_kwp": context.get("pv_kwp"),
                "bess_kwh": context.get("bess_kwh"),
                "bess_kw": context.get("bess_kw"),
                "capex_eur": round(context.get("capex_total_eur", 0)),
                "dotacia_eur": round(context.get("dotacia_eur", 0)),
                "net_capex_eur": round(context.get("net_capex_eur", 0)),
                "saving_y1_eur": round(context.get("saving_y1_eur", 0)),
                "payback_y": round(context.get("payback_years", 0), 1),
                "npv_20r_eur": round(context.get("npv_eur", 0)),
                "irr_pct": round(context.get("irr_pct", 0), 1),
                "samospotreba_pct": round(context.get("samospotreba_pct", 0), 1),
                "samostatnost_pct": round(context.get("samostatnost_pct", 0), 1),
            },
            "energia_mwh": {
                "fve_vyroba": round(context.get("pv_total_mwh", 0), 1),
                "samospotreba_fve_do_odberu": round(context.get("pv_to_load_mwh", 0), 1),
                "export_do_siete": round(context.get("pv_to_grid_mwh", 0), 1),
                "import_zo_siete": round(context.get("grid_to_load_mwh", 0), 1),
                "celkova_spotreba": round(context.get("load_total_mwh", 0), 1),
            },
            "scenare_ekonomiky": [
                {
                    "nazov": s.get("name"),
                    "rocne_uspory_eur": round(s.get("annual_save_eur", 0)),
                    "payback_y": round(s.get("payback_years", 0), 1),
                    "npv_eur": round(s.get("npv_eur", 0)),
                    "irr_pct": round(s.get("irr_pct", 0), 1),
                } for s in context.get("scenarios", [])
            ],
            "alternativne_varianty_top": [
                {
                    "nazov": v.get("label"),
                    "fve_kwp": v.get("pv_kwp"),
                    "bess_kwh": v.get("bess_kwh"),
                    "capex_eur": round(v.get("capex_total_eur", 0)),
                    "npv_eur": round(v.get("npv_eur", 0)),
                    "irr_pct": round(v.get("irr_pct", 0), 1),
                    "payback_y": round(v.get("payback_years", 0), 1),
                } for v in context.get("other_variants", [])[:4]
            ],
            "dotacia": {
                "schema": context.get("dotacia_scheme_name"),
                "max_eur": context.get("dotacia_max_eur"),
                "ziska_dotaciu": context.get("dotacia_eur", 0) > 0,
            },
            "co2": {
                "usetrene_t_rocne": round(context.get("co2_avoided_tonnes", 0), 1),
                "redukcia_pct": round(context.get("co2_reduction_pct", 0), 1),
            },
        }

        system_prompt = (
            "Si senior energetický konzultant Energovision (slovenský EPC dodávateľ FVE/BESS/trafostaníc/elektroprác).\n"
            "Tvoja úloha — vyrobiť expert posúdenie technicko-ekonomického posudku pre B2B klienta.\n\n"
            "Tón: vecný, technický, ako KEMA / Frost & Sullivan. Slovenčina.\n"
            "ŽIADNE marketingové frázy. Bez superlatívov ('vynikajúce', 'najlepšie', 'skvelé').\n"
            "Vždy fakt + dôsledok. NEHALUCINUJ — drž sa iba k poslaným dátam.\n\n"
            "Output STRIKTNÝ JSON s 4 kľúčmi:\n"
            "{\n"
            "  \"commentary\": \"3-4 paragrafov expertného posúdenia (HTML s <p> tagmi)\",\n"
            "  \"recommendations\": [{\"title\": \"krátky názov\", \"detail\": \"1-2 vety\"}, ... 5-7 položiek],\n"
            "  \"anomalies\": [{\"title\": \"krátky názov\", \"detail\": \"1-2 vety\"}, ... 0-4 (môže byť prázdne)],\n"
            "  \"open_questions\": [{\"title\": \"krátka otázka\", \"detail\": \"prečo to overiť\"}, ... 5-7]\n"
            "}\n\n"
            "PRAVIDLÁ:\n"
            "- commentary: zhodnoť výber variantu vs alternatívy, samospotrebu vs export, ekonomiku, dotácia áno/nie. ŽIADNY predaj.\n"
            "- recommendations: konkrétne ČO MÁ KLIENT UROBIŤ (technické a finančné kroky pre TENTO projekt).\n"
            "- anomalies: čo je v dátach NEZVYČAJNÉ (príliš podimenzované, ekonomické anomálie, lepšie alternatívy v top-piku).\n"
            "- open_questions: NIE generický checklist (tarif, MRK, statika) — TAILORED pre TENTO projekt s ohľadom na konkrétne čísla.\n\n"
            "Iba JSON, nič iné. Žiadne markdown fences."
        )

        msg = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=3500,
            temperature=0.3,
            system=system_prompt,
            messages=[
                {"role": "user", "content": f"Vstupné dáta projektu:\n{json.dumps(input_data, ensure_ascii=False, indent=2)}\n\nVyrob expert posúdenie podľa štandardu Energovision."}
            ]
        )

        text = msg.content[0].text.strip()
        # Strip optional markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # drop opening fence
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        if text.startswith("json"):
            text = text[4:].strip()

        result = json.loads(text)
        return {
            "ai_commentary": result.get("commentary", ""),
            "ai_recommendations": result.get("recommendations", []),
            "ai_anomalies": result.get("anomalies", []),
            "ai_open_questions": result.get("open_questions", []),
        }
    except Exception as e:
        logging.error(f"AI Expert commentary generation failed: {e}")
        return {
            "ai_commentary": "",
            "ai_recommendations": [],
            "ai_anomalies": [],
            "ai_open_questions": [],
        }


def render_posudok_orkestra(sb, analyza_id: str) -> dict:
    """
    NOVÝ posudok — Orkestra HTML šablóna → PDF (WeasyPrint) + DOCX (LibreOffice).

    Vlna 1 + Vlna 3 (2026-06-02):
    - 4 nové sekcie (cenové scenáre, vplyv dotácie, záver, otvorené otázky)
    - KOMPLETNÝ data mapping z dostupných variant polí + PVGIS koeficientov
    - Energy flow, cf_array (21 rokov), monthly_summary (12 mes.), CO2 — reálne čísla
    - Hourly profile 24h pred/po (B2B typický)
    - Klient name z customers.company_name alebo first/last
    """
    import subprocess
    import tempfile
    import os
    from posudok_orkestra import generate_orkestra_pdf, render_orkestra_html

    a_res = sb.table("analyza_om").select(
        "*, customers(first_name, last_name, company_name, email, ico)"
    ).eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        raise ValueError(f"Analyza {analyza_id} not found")

    econ = analyza.get("econ_results") or {}
    run_response = econ.get("full_response") or {}
    variants = run_response.get("variants", [])

    # Fallback z DB tabuľky analyza_om_variants
    if not variants:
        v_res = sb.table("analyza_om_variants").select("*").eq("analyza_id", analyza_id).order("position").execute()
        variants = []
        for v in (v_res.data or []):
            variants.append({
                "id": v.get("id"),
                "label": v.get("name", "Variant"),
                "pv_kwp": float(v.get("fve_kwp") or 0),
                "bess_kwh": float(v.get("bess_kwh") or 0),
                "bess_kw": float(v.get("bess_kw") or 0),
                "capex_total_eur": float(v.get("capex_eur") or 0),
                "dotacia_eur": float(v.get("result_dotacia_eur") or 0),
                "npv_eur": float(v.get("result_npv_eur_base") or 0),
                "irr_pct": float(v.get("result_irr_pct_base") or 0),
                "payback_simple_y": float(v.get("result_payback_y_base") or 0),
                "samospotreba_pct": float(v.get("result_samosp_pct") or 0),
                "samostatnost_pct": float(v.get("result_samostat_pct") or 0),
                "import_mwh": float(v.get("result_import_mwh") or 0),
                "export_mwh": float(v.get("result_export_mwh") or 0),
                "saving_y1_eur": float(v.get("result_saving_y1_eur") or 0),
            })

    if not variants:
        raise ValueError("No variants — run simulation first")

    # Selected variant
    selected_id = analyza.get("selected_variant_id")
    selected = None
    if selected_id:
        for v in variants:
            if v.get("variant_id") == selected_id or v.get("id") == selected_id:
                selected = v
                break
    if not selected:
        selected = max(variants, key=lambda v: v.get("npv_eur", 0))

    # Customer name
    cust = analyza.get("customers") or {}
    client_name = (
        cust.get("company_name")
        or (f"{cust.get('first_name') or ''} {cust.get('last_name') or ''}").strip()
        or analyza.get("om_name")
        or "Klient"
    )

    # === Tech config ===
    pv_kwp = float(selected.get("pv_kwp", 0))
    bess_kwh = float(selected.get("bess_kwh", 0))
    bess_kw = float(selected.get("bess_kw", 0))
    annual_kwh = float(analyza.get("consumption_annual_mwh") or 0) * 1000
    mrk_kw = float(analyza.get("om_mrk_kw") or 0)

    # === Financial ===
    capex_total = float(selected.get("capex_total_eur") or selected.get("capex_eur") or 0)
    dotacia = float(selected.get("dotacia_eur") or 0)
    net_capex = capex_total - dotacia
    saving_y1 = float(selected.get("saving_y1_eur") or 0)
    # Ak chýba saving_y1, odvod z payback (saving = net_capex / payback)
    if saving_y1 <= 0:
        payback_raw = float(selected.get("payback_simple_y") or selected.get("payback_y") or 0)
        if payback_raw > 0:
            saving_y1 = net_capex / payback_raw
    npv = float(selected.get("npv_eur") or 0)
    irr = float(selected.get("irr_pct") or 0)
    payback = float(selected.get("payback_simple_y") or selected.get("payback_y") or 0)
    samosp_pct = float(selected.get("samospotreba_pct") or 0)
    samostat_pct = float(selected.get("samostatnost_pct") or 0)

    # === Energy flow agregáty (engine field name fallbacks) ===
    pv_total_mwh = float(
        selected.get("pv_total_mwh")
        or selected.get("fve_prod")
        or (pv_kwp * 0.98)  # PVGIS yield ~980 kWh/kWp SK
    )
    import_mwh = float(selected.get("import_mwh") or selected.get("grid_import") or 0)
    export_mwh = float(selected.get("export_mwh") or selected.get("grid_export") or 0)
    load_total_mwh = annual_kwh / 1000

    # PV decomposition
    pv_to_load_mwh = (samosp_pct / 100.0) * pv_total_mwh if samosp_pct > 0 else max(0, pv_total_mwh - export_mwh)
    pv_to_grid_mwh = export_mwh
    residual_pv = max(0, pv_total_mwh - pv_to_load_mwh - pv_to_grid_mwh)
    pv_to_bat_mwh = residual_pv if bess_kwh > 0 else 0
    curtailed_mwh = 0 if bess_kwh > 0 else residual_pv

    # Battery discharge (roundtrip 92%)
    bat_to_load_mwh = pv_to_bat_mwh * 0.92 if bess_kwh > 0 else 0
    grid_to_load_mwh = max(0, load_total_mwh - pv_to_load_mwh - bat_to_load_mwh)

    # Pct rozdelenie FVE produkcie
    if pv_total_mwh > 0:
        direct_to_load_pct = (pv_to_load_mwh / pv_total_mwh) * 100
        charging_battery_pct = (pv_to_bat_mwh / pv_total_mwh) * 100
        exported_pct = (pv_to_grid_mwh / pv_total_mwh) * 100
        curtailed_pct = (curtailed_mwh / pv_total_mwh) * 100
    else:
        direct_to_load_pct = charging_battery_pct = exported_pct = curtailed_pct = 0.0

    # === Cashflow array (21 hodnôt: y0..y20) ===
    PV_DEGRADATION = 0.005
    OPEX_RATE = 0.015
    annual_opex = capex_total * OPEX_RATE
    cf_array = [-net_capex]
    for y in range(1, 21):
        degraded_saving = saving_y1 * ((1 - PV_DEGRADATION) ** (y - 1))
        cf_array.append(degraded_saving - annual_opex)
    accumulated_cf_final = sum(cf_array)

    # === Monthly summary (12 mesiacov) — PVGIS koeficienty pre SK ===
    PV_MONTHLY = [0.038, 0.057, 0.084, 0.107, 0.115, 0.116, 0.119, 0.107, 0.090, 0.067, 0.045, 0.055]
    TARIF_BUY_EUR_MWH = 120.0
    TARIF_SELL_EUR_MWH = 65.0
    monthly_solar_to_load = [pv_to_load_mwh * 1000 * c * (TARIF_BUY_EUR_MWH / 1000) for c in PV_MONTHLY]
    monthly_solar_export = [pv_to_grid_mwh * 1000 * c * (TARIF_SELL_EUR_MWH / 1000) for c in PV_MONTHLY]
    arb_total_eur = bat_to_load_mwh * 110 if bess_kwh > 0 else 0
    monthly_arbitrage = [arb_total_eur / 12.0] * 12

    # === Hourly load profile 24h (pred/po) ===
    HOURLY_LOAD = [0.025, 0.024, 0.024, 0.024, 0.025, 0.028, 0.033, 0.045,
                   0.058, 0.065, 0.068, 0.068, 0.065, 0.060, 0.058, 0.058,
                   0.052, 0.048, 0.043, 0.040, 0.038, 0.034, 0.030, 0.027]
    HOURLY_PV = [0, 0, 0, 0, 0, 0.01, 0.03, 0.06, 0.09, 0.12, 0.13, 0.14,
                 0.14, 0.13, 0.11, 0.09, 0.06, 0.03, 0.01, 0, 0, 0, 0, 0]
    daily_load_kwh = load_total_mwh * 1000 / 365
    daily_pv_kwh = pv_total_mwh * 1000 / 365
    hourly_load_kw_before = [daily_load_kwh * w for w in HOURLY_LOAD]
    hourly_load_kw_after = [max(0.0, daily_load_kwh * w - daily_pv_kwh * HOURLY_PV[i]) for i, w in enumerate(HOURLY_LOAD)]

    # === CO2 (SK grid 0.25 t/MWh, 2024) ===
    clean_mwh = pv_to_load_mwh + bat_to_load_mwh + pv_to_grid_mwh
    co2_avoided_tonnes = clean_mwh * 0.25
    co2_reduction_pct = (clean_mwh / load_total_mwh * 100) if load_total_mwh > 0 else 0
    trees_equivalent = int(co2_avoided_tonnes * 1000 / 21)
    barrels_oil = int(co2_avoided_tonnes * 2.32)

    # === 3 cenové scenáre ===
    saving_low = saving_y1 * 0.75
    npv_low = sum(saving_low * ((1 - PV_DEGRADATION) ** (y - 1)) - annual_opex for y in range(1, 21)) - net_capex
    payback_low = (net_capex / saving_low) if saving_low > 0 else 99
    irr_low = max(0, irr - 5)
    arb_uplift = 0.10 if bess_kwh > 0 else 0
    saving_arb = saving_y1 * (1 + arb_uplift)
    npv_arb = sum(saving_arb * ((1 - PV_DEGRADATION) ** (y - 1)) - annual_opex for y in range(1, 21)) - net_capex
    payback_arb = (net_capex / saving_arb) if saving_arb > 0 else 99
    irr_arb = irr + (arb_uplift * 100 * 0.5)

    scenarios_ctx = [
        {
            "name": "Báza (ÚRSO 2026)",
            "is_base": True,
            "tarif_buy_eur_kwh": TARIF_BUY_EUR_MWH / 1000,
            "tarif_sell_eur_kwh": TARIF_SELL_EUR_MWH / 1000,
            "annual_save_eur": saving_y1,
            "payback_years": payback,
            "npv_eur": npv,
            "irr_pct": irr,
            "note": "Štandardná ÚRSO 2026 cena + priemer spotových cien OKTE 2025.",
        },
        {
            "name": "Nízky výkup (defenzívny)",
            "is_base": False,
            "tarif_buy_eur_kwh": TARIF_BUY_EUR_MWH / 1000,
            "tarif_sell_eur_kwh": (TARIF_SELL_EUR_MWH * 0.5) / 1000,
            "annual_save_eur": saving_low,
            "payback_years": payback_low,
            "npv_eur": npv_low,
            "irr_pct": irr_low,
            "note": "Konzervatívny scenár — výkup elektriny iba 50 % bázy. Citlivosť na PPA podmienky.",
        },
        {
            "name": "Spot s arbitrážou BS",
            "is_base": False,
            "tarif_buy_eur_kwh": TARIF_BUY_EUR_MWH / 1000,
            "tarif_sell_eur_kwh": TARIF_SELL_EUR_MWH / 1000,
            "annual_save_eur": saving_arb,
            "payback_years": payback_arb,
            "npv_eur": npv_arb,
            "irr_pct": irr_arb,
            "note": ("BESS arbitráž — nákup pri nízkych spotových cenách, predaj pri špičke. +10 % savings." if bess_kwh > 0 else "Bez BESS — variant nedosahuje arbitráž potenciál. Identické s bázou."),
        },
    ]

    # === Vplyv dotácie (porovnanie s/bez) ===
    if dotacia > 0:
        cf_no_dot = [-capex_total]
        for y in range(1, 21):
            cf_no_dot.append(saving_y1 * ((1 - PV_DEGRADATION) ** (y - 1)) - annual_opex)
        npv_without_dotacia = sum(cf_no_dot)
        payback_without_dotacia = (capex_total / saving_y1) if saving_y1 > 0 else 99
        irr_without_dotacia = max(0, irr - 4)
    else:
        npv_without_dotacia = npv
        payback_without_dotacia = payback
        irr_without_dotacia = irr

    # === Other variants pre porovnanie ===
    other_variants = []
    for v in variants:
        if v is selected:
            continue
        other_variants.append({
            "label": v.get("label", "Variant"),
            "pv_kwp": v.get("pv_kwp", 0),
            "bess_kwh": v.get("bess_kwh", 0),
            "capex_total_eur": v.get("capex_total_eur", v.get("capex_eur", 0)),
            "dotacia_eur": v.get("dotacia_eur", 0),
            "npv_eur": v.get("npv_eur", 0),
            "irr_pct": v.get("irr_pct", 0),
            # template (posudok.html) cita v.payback_simple_y - pole musi mat tento nazov
            "payback_simple_y": v.get("payback_simple_y", v.get("payback_y", 0)),
            "payback_years": v.get("payback_simple_y", v.get("payback_y", 0)),
        })

    # === Build full context ===
    context = {
        "project_name": analyza.get("name") or analyza.get("om_name") or "Hybridné riešenie FVE + BESS",
        "project_id": analyza.get("posudok_number") or f"AOM-{str(analyza_id)[:8]}",
        "client_name": client_name,
        "site_address": analyza.get("om_address") or "—",
        "posudok_date": datetime.now().strftime("%d.%m.%Y"),
        "prepared_by_name": "Lukáš Bago",
        "prepared_by_email": "lukas.bago@energovision.sk",
        "prepared_by_phone": "0918 187 762",
        "engine_version": "0.9.6",
        "label": selected.get("label", "Variant"),
        "pv_kwp": pv_kwp,
        "bess_kwh": bess_kwh,
        "bess_kw": bess_kw,
        "inverter_kw": selected.get("inverter_kw", pv_kwp * 0.9),
        "fve_topology": analyza.get("fve_topology") or "Juh, 35°",
        "mrk_kw": mrk_kw,
        "annual_kwh": annual_kwh,
        "tarif_typ": analyza.get("om_tarif_typ") or "spot",
        "ems_strategy": "Samospotreba + arbitráž BS" if bess_kwh > 0 else "Samospotreba (bez BESS)",
        "capex_total_eur": capex_total,
        "capex_pv_eur": selected.get("capex_pv_eur", capex_total * 0.7 if bess_kwh > 0 else capex_total),
        "capex_bess_eur": selected.get("capex_bess_eur", capex_total * 0.3 if bess_kwh > 0 else 0),
        "capex_other_eur": 0,
        "dotacia_eur": dotacia,
        "net_capex_eur": net_capex,
        "saving_y1_eur": saving_y1,
        "payback_years": payback,
        "irr_pct": irr,
        "npv_eur": npv,
        "samospotreba_pct": samosp_pct,
        "samostatnost_pct": samostat_pct,
        "pv_total_mwh": pv_total_mwh,
        "pv_to_load_mwh": pv_to_load_mwh,
        "pv_to_grid_mwh": pv_to_grid_mwh,
        "pv_to_bat_mwh": pv_to_bat_mwh,
        "grid_to_load_mwh": grid_to_load_mwh,
        "bat_to_load_mwh": bat_to_load_mwh,
        "grid_to_bat_mwh": 0,
        "load_total_mwh": load_total_mwh,
        "grid_import_mwh": import_mwh,
        "grid_export_mwh": export_mwh,
        "direct_to_load_pct": direct_to_load_pct,
        "charging_battery_pct": charging_battery_pct,
        "exported_pct": exported_pct,
        "curtailed_pct": curtailed_pct,
        "co2_avoided_tonnes": co2_avoided_tonnes,
        "co2_reduction_pct": co2_reduction_pct,
        "trees_equivalent": trees_equivalent,
        "barrels_oil": barrels_oil,
        "cf_array": cf_array,
        "accumulated_cf_final": accumulated_cf_final,
        "monthly_solar_to_load": monthly_solar_to_load,
        "monthly_solar_export": monthly_solar_export,
        "monthly_arbitrage": monthly_arbitrage,
        "hourly_load_kw_before": hourly_load_kw_before,
        "hourly_load_kw_after": hourly_load_kw_after,
        "dotacia_scheme_name": "Zelená podnikom",
        "dotacia_max_eur": 50000,
        "dotacia_intensity_pct": 45,
        "payback_without_dotacia": payback_without_dotacia,
        "npv_without_dotacia": npv_without_dotacia,
        "irr_without_dotacia": irr_without_dotacia,
        "scenarios": scenarios_ctx,
        "open_questions": [],
        "other_variants": other_variants,
        "n_variants_run": len(variants),
        "spot_avg_eur_mwh": 103,
    }

    # === Generate AI Expert commentary (Claude Sonnet 4.5) ===
    # Pri zlyhaní vráti prázdne defaults → posudok sa vygeneruje bez AI sekcie
    ai_data = _generate_ai_expert_commentary(context, analyza)
    context.update(ai_data)

    # === Generate PDF ===
    pdf_bytes = generate_orkestra_pdf(context)

    # === PDF upload ===
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = f"analyza_om/{analyza_id}/posudok_orkestra_{ts}.pdf"
    sb.storage.from_("documents").upload(
        pdf_path, pdf_bytes,
        {"content-type": "application/pdf", "upsert": "true"}
    )
    pdf_url = sb.storage.from_("documents").get_public_url(pdf_path)

    # === DOCX conversion via LibreOffice ===
    docx_url = None
    docx_size_kb = 0
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = os.path.join(tmpdir, "posudok.html")
            html_content = render_orkestra_html(context)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            subprocess.run([
                "libreoffice", "--headless", "--convert-to", "docx",
                "--outdir", tmpdir, html_path
            ], capture_output=True, timeout=60, text=True)
            docx_local = os.path.join(tmpdir, "posudok.docx")
            if os.path.exists(docx_local):
                with open(docx_local, "rb") as f:
                    docx_bytes = f.read()
                docx_size_kb = len(docx_bytes) // 1024
                docx_path = f"analyza_om/{analyza_id}/posudok_orkestra_{ts}.docx"
                sb.storage.from_("documents").upload(
                    docx_path, docx_bytes,
                    {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "upsert": "true"}
                )
                docx_url = sb.storage.from_("documents").get_public_url(docx_path)
    except Exception as e:
        logging.error(f"DOCX conversion failed: {e}")

    # === Update analyza_om ===
    sb.table("analyza_om").update({
        "posudok_orkestra_pdf_url": pdf_url,
        "posudok_orkestra_docx_url": docx_url,
        "posudok_orkestra_generated_at": datetime.now().isoformat(),
    }).eq("id", analyza_id).execute()

    return {
        "ok": True,
        "pdf_url": pdf_url,
        "docx_url": docx_url,
        "size_kb": len(pdf_bytes) // 1024,
        "docx_size_kb": docx_size_kb,
        "engine": "orkestra-v1.1",
        "client": client_name,
    }

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
    """Hodinová simulácia 8760h pre rýchly odhad bez upload 15-min dát.

    Engine:
    1) Load profile 8760h — generovaný z profile_template (24/7 / 8-16 / kancelária / domácnosť)
    2) PV profile 8760h — PVGIS monthly × daily gaussian okolo poludnia
    3) Per-hour dispatch: pv→load → pv→bat (RTE 0.94 charge) → bat→load (deficit) → pv→grid
    4) Agregát: ročná úspora, samospotreba %, NPV 15r, payback, per-month chart

    Pridáva sa per-month aggregate (mesačná spotreba/export/úspora) pre sanity check.
    """
    import numpy as np
    
    kwp = float(payload.get("kwp", 0))
    annual_kwh = float(payload.get("annual_kwh", 0))
    tarif_buy = float(payload.get("tarif_buy", 0.18))
    capex_per_kwp = float(payload.get("capex_per_kwp", 800))
    bess_kwh = float(payload.get("bess_kwh", 0))
    bess_kw = float(payload.get("bess_kw", bess_kwh * 0.5)) if bess_kwh > 0 else 0  # default C/2
    capex_per_bess_kwh = float(payload.get("capex_per_bess_kwh", 480))
    discount_rate = float(payload.get("discount_rate", 0.06))
    profile_template = str(payload.get("profile_template", "kancelaria"))
    export_price = float(payload.get("export_price_eur_kwh", 0.06))

    # === 1) LOAD PROFILE 8760h ===
    HOURS = 8760
    hours = np.arange(HOURS)
    day_of_year = hours // 24
    hour_of_day = hours % 24
    day_of_week = (day_of_year + 1) % 7  # 0=Sun, 1=Mon, ..., 6=Sat (rok začína Po pre simplicity)

    # Base shape — per hour of day, per profile
    PROFILE_SHAPES = {
        # 0  1  2  3  4  5  6  7   8   9  10  11  12  13  14  15  16  17  18  19  20  21  22  23
        "priemysel_24_7":   [0.85,0.80,0.75,0.75,0.75,0.80,0.85,0.95,1.00,1.00,1.00,1.00,1.00,1.00,1.00,1.00,1.00,0.95,0.90,0.88,0.85,0.85,0.85,0.85],
        "priemysel_8_16":   [0.25,0.20,0.20,0.20,0.20,0.25,0.40,0.85,1.00,1.00,1.00,1.00,1.00,1.00,1.00,1.00,1.00,0.70,0.40,0.30,0.28,0.25,0.25,0.25],
        "kancelaria":       [0.20,0.18,0.18,0.18,0.18,0.20,0.30,0.60,0.85,1.00,1.00,1.00,0.95,0.95,1.00,1.00,0.95,0.80,0.55,0.40,0.30,0.25,0.22,0.20],
        "domacnost":        [0.30,0.25,0.22,0.20,0.20,0.25,0.45,0.65,0.55,0.40,0.35,0.40,0.50,0.45,0.40,0.45,0.55,0.75,0.95,1.00,0.95,0.80,0.55,0.40],
    }
    shape = np.array(PROFILE_SHAPES.get(profile_template, PROFILE_SHAPES["kancelaria"]))
    
    # Replicate per hour cez celý rok
    load_kw = shape[hour_of_day]
    
    # Víkendový faktor (Sa=Sun = day_of_week 0 alebo 6)
    weekend = ((day_of_week == 0) | (day_of_week == 6))
    weekend_factor = {
        "priemysel_24_7":  0.85,  # mierne nižšia záťaž víkend
        "priemysel_8_16":  0.30,  # pracovné dni only
        "kancelaria":      0.20,
        "domacnost":       1.15,  # cez víkend doma — vyššia spotreba
    }.get(profile_template, 0.40)
    load_kw = np.where(weekend, load_kw * weekend_factor, load_kw)
    
    # Sezónna variácia (zima viac kúrenie + svetlo, leto AC)
    month_factor = np.array([1.15, 1.10, 1.00, 0.92, 0.88, 0.90, 0.95, 0.95, 0.92, 1.00, 1.10, 1.18])
    month_for_hour = ((day_of_year * 12 // 365) % 12)  # 0..11
    load_kw *= month_factor[month_for_hour]
    
    # Škálovať aby celkový annual_kwh sedel
    raw_total = load_kw.sum()  # kWh za rok (1h timestep)
    if raw_total > 0:
        load_kw *= (annual_kwh / raw_total)

    # === 2) PV PROFILE 8760h ===
    # Mesačné SK yieldy kWh/kWp (Bratislava)
    monthly_kwh_per_kwp = [38, 58, 95, 125, 140, 145, 152, 138, 105, 70, 42, 32]
    annual_yield = sum(monthly_kwh_per_kwp)  # ~1140 kWh/kWp
    
    # Daily curve — Gaussian okolo poludnia, šírka σ=2.5 hodín
    daily_curve = np.exp(-0.5 * ((np.arange(24) - 12.5) / 2.5) ** 2)
    daily_curve[(np.arange(24) < 5) | (np.arange(24) > 20)] = 0  # noc
    daily_curve /= daily_curve.sum()  # normalizácia na 1
    
    # Per-hour PV: monthly_kwh_per_kwp[m] × kwp × daily_curve[h]
    pv_kw = np.zeros(HOURS)
    for m in range(12):
        days_in_month = [31,28,31,30,31,30,31,31,30,31,30,31][m]
        # Hodiny v tomto mesiaci
        start_h = sum([31,28,31,30,31,30,31,31,30,31,30,31][:m]) * 24
        end_h = start_h + days_in_month * 24
        n_days = days_in_month
        # Energia per mesiac per kWp
        month_total_kwh = monthly_kwh_per_kwp[m] * kwp
        # Per deň
        per_day_kwh = month_total_kwh / n_days
        # Per hour cez celý mesiac
        for d in range(n_days):
            day_start = start_h + d * 24
            pv_kw[day_start:day_start+24] = daily_curve * per_day_kwh
    
    # === 3) PER-HOUR DISPATCH ===
    soc_kwh = bess_kwh * 0.5  # start 50% SoC
    rte_charge = 0.97
    rte_discharge = 0.97
    soc_min = bess_kwh * 0.10
    soc_max = bess_kwh * 0.95

    pv_to_load = np.zeros(HOURS)
    pv_to_bat = np.zeros(HOURS)
    pv_to_grid = np.zeros(HOURS)
    bat_to_load = np.zeros(HOURS)
    grid_to_load = np.zeros(HOURS)

    for h in range(HOURS):
        pv = pv_kw[h]
        load = load_kw[h]
        # 1. PV → load
        to_load = min(pv, load)
        pv_to_load[h] = to_load
        rem_pv = pv - to_load
        rem_load = load - to_load
        
        # 2. PV → BAT
        if bess_kwh > 0 and rem_pv > 0 and soc_kwh < soc_max:
            charge_room = (soc_max - soc_kwh)
            max_charge_kw = min(rem_pv, bess_kw)
            charge_kwh_in = min(max_charge_kw, charge_room / rte_charge)
            pv_to_bat[h] = charge_kwh_in
            soc_kwh += charge_kwh_in * rte_charge
            rem_pv -= charge_kwh_in
        
        # 3. BAT → load
        if bess_kwh > 0 and rem_load > 0 and soc_kwh > soc_min:
            available = (soc_kwh - soc_min) * rte_discharge
            max_disch_kw = min(rem_load, bess_kw)
            disch = min(max_disch_kw, available)
            bat_to_load[h] = disch
            soc_kwh -= disch / rte_discharge
            rem_load -= disch
        
        # 4. PV → grid (zvyšok)
        pv_to_grid[h] = rem_pv
        # 5. Grid → load (zvyšok)
        grid_to_load[h] = rem_load

    # === 4) AGREGÁTY ===
    pv_total = pv_kw.sum()
    self_used = pv_to_load.sum() + pv_to_bat.sum()  # PV ktoré zostalo doma
    bat_discharge = bat_to_load.sum()
    export_total = pv_to_grid.sum()
    grid_import = grid_to_load.sum()
    
    samospotreba_pct = (self_used / pv_total * 100) if pv_total > 0 else 0
    
    # Úspora: priame nahradenie + export
    saved_buy_eur = (pv_to_load.sum() + bat_to_load.sum()) * tarif_buy
    export_eur = export_total * export_price
    annual_savings = saved_buy_eur + export_eur
    
    capex_total = kwp * capex_per_kwp + bess_kwh * capex_per_bess_kwh
    payback_years = capex_total / annual_savings if annual_savings > 0 else 999
    
    # NPV 15 rokov, eskalácia 2 % retail
    horizon = 15
    cashflows = [-capex_total]
    for y in range(1, horizon + 1):
        cf = annual_savings * (1.02 ** (y-1)) * (0.992 ** (y-1))  # +2 % retail eskalácia, -0.8 % degradace
        cashflows.append(cf)
    npv = sum(cf / ((1 + discount_rate) ** i) for i, cf in enumerate(cashflows))

    # Per-month chart data
    months = []
    for m in range(12):
        days_in_month = [31,28,31,30,31,30,31,31,30,31,30,31][m]
        start_h = sum([31,28,31,30,31,30,31,31,30,31,30,31][:m]) * 24
        end_h = start_h + days_in_month * 24
        months.append({
            "month": m + 1,
            "pv_kwh": round(pv_kw[start_h:end_h].sum()),
            "load_kwh": round(load_kw[start_h:end_h].sum()),
            "self_used_kwh": round((pv_to_load[start_h:end_h].sum() + bat_to_load[start_h:end_h].sum())),
            "export_kwh": round(pv_to_grid[start_h:end_h].sum()),
            "import_kwh": round(grid_to_load[start_h:end_h].sum()),
        })

    return {
        "ok": True,
        "engine": "quick_v2_hourly",
        "kwp": kwp,
        "bess_kwh": bess_kwh,
        "bess_kw": bess_kw,
        "profile_template": profile_template,
        "pv_production_kwh": round(pv_total),
        "self_used_kwh": round(self_used + bat_discharge),
        "bat_discharge_kwh": round(bat_discharge),
        "export_kwh": round(export_total),
        "import_kwh": round(grid_import),
        "self_consumption_pct": round(samospotreba_pct, 1),
        "annual_savings_eur": round(annual_savings),
        "saved_buy_eur": round(saved_buy_eur),
        "export_revenue_eur": round(export_eur),
        "capex_eur": round(capex_total),
        "payback_years": round(payback_years, 1),
        "npv_eur": round(npv),
        "co2_saved_tons_per_year": round((pv_to_load.sum() + bat_discharge + export_total) * 0.000236, 2),
        "monthly": months,
    }


def _quick_estimate_legacy(payload: dict) -> dict:
    """Rýchla kalkulácia bez 15-min dát. Vstupy: kwp, annual_kwh, tarif_buy, psc."""
    kwp = float(payload.get("kwp", 0))
    annual_kwh = float(payload.get("annual_kwh", 0))
    tarif_buy = float(payload.get("tarif_buy", 0.18))  # €/kWh default
    capex_per_kwp = float(payload.get("capex_per_kwp", 800))
    bess_kwh = float(payload.get("bess_kwh", 0))
    capex_per_bess_kwh = float(payload.get("capex_per_bess_kwh", 480))
    discount_rate = float(payload.get("discount_rate", 0.06))
    profile_template = str(payload.get("profile_template", "kancelaria"))
    
    # Heuristics z reálnych ponúk + spot 2025
    yield_per_kwp = 1050  # kWh/kWp/rok pre SK
    
    # Samospotreba per typ prevádzky (kalibrované z reálnych SK B2B ponúk)
    # bez BESS / s BESS (BESS posúva self-cons o ~15-25 %)
    profile_self_cons = {
        "priemysel_24_7":   (0.78, 0.92),  # priemysel 24/7 — najvyššia samospotreba
        "priemysel_8_16":   (0.58, 0.78),  # priemysel pracovné dni 8-16
        "kancelaria":       (0.50, 0.72),  # kancelária 9-17 + víkendy doma
        "domacnost":        (0.30, 0.65),  # rodinný dom, cez deň prázdny
    }
    base_sc, bess_sc = profile_self_cons.get(profile_template, (0.45, 0.68))
    self_consumption = bess_sc if bess_kwh > 0 else base_sc
    
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
