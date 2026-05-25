"""
AOM AI Strategist — 7-vrstvový AI poradca

Vrstvy:
  A — Klient profile classification
  B — Smart variant generation (5 archetypov)
  C — Constraint checks
  D — Conversational refinement (chat)
  E — Learning z historických projektov
  F — Anomálie + Quick wins
  G — Dotácie matching

Output: JSON s 5 navrhnutými variantmi + reasoning + warnings + anomálie + dotácie + similar projects.

Senior konzultant tón — vecný, technický, ako KEMA / Frost & Sullivan, slovenčina.
"""
import os
import json
import time
import logging
from typing import Optional

import anthropic
from anthropic import Anthropic

log = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-5-20250929"  # Sonnet 4.5
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


# ============================================================
# VRSTVA A — Klient profile classifier
# ============================================================

def classify_client_profile(analyza: dict, consumption_profile: list[float] = None) -> dict:
    """Klasifikuje klienta z 15-min profilu + tarif + sadzba.
    Vráti: {classification, patterns_detected[]}
    """
    annual_kwh = float(analyza.get("consumption_annual_mwh", 0) or 0) * 1000
    peak_kw = float(analyza.get("consumption_peak_kw_hourly", 0) or 0)
    mrk_kw = float(analyza.get("om_mrk_kw") or 0)
    sadzba = analyza.get("om_sadzba", "NN")
    
    # Heuristics
    patterns = []
    classification = ""
    
    if annual_kwh > 0 and peak_kw > 0:
        # Profile ratio (peak/avg)
        avg_kw = annual_kwh / 8760
        peak_ratio = peak_kw / avg_kw if avg_kw > 0 else 0
        
        if peak_ratio < 1.5 and annual_kwh > 100000:
            classification = "Priemyselný subjekt — 24/7 prevádzka, plochý profil"
            patterns.append({"label": "Plochý 24/7 profil → BESS arbitráž má vysoký potenciál", "type": "opportunity"})
        elif peak_ratio > 3 and 7000 < annual_kwh < 60000:
            classification = "Kancelária / komerčný subjekt — denná špička 8-17, víkend menej"
            patterns.append({"label": "Denná peak v slnečných hodinách → vysoký potenciál samospotreby (PV bez BESS)", "type": "opportunity"})
        elif annual_kwh < 15000:
            classification = "Domácnosť / malý odber — večerný peak"
            patterns.append({"label": "Večerná spotreba → BESS vyrovná solar-night gap", "type": "opportunity"})
        elif annual_kwh > 60000 and peak_ratio < 2.5:
            classification = "Stredný komerčný subjekt — variabilná prevádzka"
        else:
            classification = "Komerčný subjekt — štandardný profil"
    else:
        classification = f"Klient {sadzba}, MRK {mrk_kw} kW (chýba podrobný 15-min profil — odporúčam upload pre presnejšiu analýzu)"
        patterns.append({"label": "Chýba 15-min profil — analýza bude syntetická", "type": "warning"})
    
    # MRK utilization
    if mrk_kw > 0 and peak_kw > 0:
        util = peak_kw / mrk_kw
        if util < 0.5:
            patterns.append({"label": f"MRK využité na {util*100:.0f}% → máš rezervu pre PV bez upgrade prípojky", "type": "opportunity"})
        elif util > 0.9:
            patterns.append({"label": f"MRK využité na {util*100:.0f}% — pri pridaní PV možno treba MRK zvýšiť", "type": "warning"})
    
    return {
        "classification": classification,
        "patterns_detected": patterns,
        "peak_kw": peak_kw,
        "annual_kwh": annual_kwh,
        "avg_kw": annual_kwh / 8760 if annual_kwh > 0 else 0,
    }


# ============================================================
# VRSTVA B — Smart variant generation (5 archetypov)
# ============================================================

def generate_smart_variants(sb, analyza: dict, profile: dict, capex_overrides: dict = None) -> list[dict]:
    """Vygeneruje 5 archetypov a spustí engine na výpočet metrík.
    
    Archetypes:
      1. Konzervatívny (low risk, žiadny BESS)
      2. Optimálny NPV (engine compute)
      3. Energy independence (vysoký BESS pre samostatnosť)
      4. Spot arbitráž (BESS pre nočné nabíjanie)
      5. Stretch (over-spec — pre porovnanie)
    """
    annual_kwh = profile["annual_kwh"]
    peak_kw = profile["peak_kw"]
    mrk_kw = float(analyza.get("om_mrk_kw") or 0)
    max_export = float(analyza.get("max_export_kw") or 0)

    # Fallback: ak chýba 15-min profil + faktúra → odhad z MRK
    # B2B load factor: realisticky 20-35 % (priemyselný 35-50 %, kancelária 15-25 %)
    annual_estimated = False
    if annual_kwh <= 0 and mrk_kw > 0:
        annual_kwh = mrk_kw * 8760 * 0.25  # 25 % load factor (konzervatívny B2B priemer)
        peak_kw = mrk_kw * 0.70
        annual_estimated = True
        log.info(f"[smart_variants] annual_kwh odhadnuté z MRK {mrk_kw} kW (25%LF) → {annual_kwh:.0f} kWh/rok")

    # Max FVE limit — primárne podľa max_export (DC/AC ratio 1.20), sekundárne MRK
    if max_export > 0:
        max_fve_kwp = max_export * 1.20    # napr. 550 kW max_export → 660 kWp
    elif mrk_kw > 0:
        max_fve_kwp = mrk_kw * 1.10        # nemá max_export → konzervatívne MRK × 1.10
    else:
        max_fve_kwp = 9999

    # Base kWp = 80% pokrytie ročnej spotreby (PVGIS yield 1050 kWh/kWp), ALE cap max_fve_kwp
    if annual_kwh > 0:
        base_kwp = round(min(annual_kwh * 0.8 / 1050, max_fve_kwp), 0)
    else:
        base_kwp = round(min(20, max_fve_kwp), 0)
    base_kwp = max(5, base_kwp)
    
    # Archetype multipliery — Optimal blízko base_kwp, Stretch len mierne nad max_fve_kwp
    archetypes = [
        {
            "label": "Konzervatívny",
            "archetype": "conservative",
            "fve_kwp": round(min(base_kwp * 0.35, max_fve_kwp * 0.4), 0),
            "bess_kwh": 0,
            "rationale_hint": "Iba samospotreba, žiadne BESS, žiadne riziko prebytkov — bezpečný štart",
        },
        {
            "label": "Optimálny NPV",
            "archetype": "optimal_npv",
            "fve_kwp": round(min(base_kwp * 0.70, max_fve_kwp * 0.80), 0),
            "bess_kwh": round(min(annual_kwh / 365 * 0.30, 100), 0) if annual_kwh > 0 else 0,
            "rationale_hint": "Optimálny pomer cena/výkon — pokryje ~70% spotreby s minimálnym BESS",
        },
        {
            "label": "Energy independence",
            "archetype": "independence",
            "fve_kwp": round(min(base_kwp, max_fve_kwp), 0),
            "bess_kwh": round(min(annual_kwh / 365 * 0.50, 200), 0) if annual_kwh > 0 else 0,
            "rationale_hint": "Maximálna samostatnosť — väčší BESS, ale ostáva pod max_export",
        },
        {
            "label": "Spot arbitráž",
            "archetype": "spot_arbitrage",
            "fve_kwp": round(min(base_kwp * 0.50, max_fve_kwp * 0.6), 0),
            "bess_kwh": round(min(annual_kwh / 365 * 0.80, 300), 0) if annual_kwh > 0 else 0,
            "rationale_hint": "Menšie PV, väčšie BESS — primárny cieľ spot arbitráž (nákup nočný/predaj poludnie)",
        },
        {
            "label": "Stretch (over-spec)",
            "archetype": "stretch",
            "fve_kwp": round(max_fve_kwp, 0),
            "bess_kwh": round(min(annual_kwh / 365 * 0.60, 400), 0) if annual_kwh > 0 else 0,
            "rationale_hint": "Max FVE pod max_export limit — pre porovnanie hornej hranice (prebytky idú na predaj)",
        },
    ]
    
    # Volá engine pre každý archetype (rýchla simulácia)
    try:
        from energovision_analytics.api.services.engine_service import run_variants_pipeline, build_run_variants_response
        
        # Komponuj request pre všetky archetypy naraz
        pv_options = sorted(set(a["fve_kwp"] for a in archetypes))
        bess_options = sorted(set(a["bess_kwh"] for a in archetypes))
        
        request_dict = {
            "site": {
                "nazov": analyza.get("name", "OM"),
                "psc": analyza.get("om_psc") or "010 01",
                "rocna_spotreba_kwh": annual_kwh,
                "rk_kw": float(analyza.get("om_rk_kw") or mrk_kw / 1.2 or 30),
                "mrk_kw": mrk_kw if mrk_kw > 0 else None,
                "typ_tarify": "spot",
                "bilancna_skupina": "Energie2",
            },
            "load_profile": {
                "source": "synthetic",
                "profile_template": _detect_profile_template(profile),
                "granularity_min": 60,
            },
            "variants": {
                "pv_kwp_options": pv_options,
                "bess_kwh_options": bess_options,
                "ems_strategies": ["rule_based"],
            },
            "capex": {"mode": "quick", "capex_pv_eur_per_kwp": 800, "capex_bess_eur_per_kwh": 480},
            "financial": {"dppo_pct": 0.22, "discount_rate": 0.06, "horizon_years": 20, "depr_years": 6},
            "dotacia": {"enabled": True, "scheme_id": "zelena_podnikom"},
            "async_mode": False,
        }
        
        raw_result = run_variants_pipeline(request_dict)
        # Konvertuj VariantResult dataclasses na JSON-serializable dicts
        result = build_run_variants_response(raw_result)
        engine_variants = result.get("variants") or []
        
        # Match engine results to archetypes (closest match by PV+BESS)
        # build_run_variants_response vracia: npv_eur, irr_pct, payback_simple_y, samospotreba_pct, samostatnost_pct, capex_total_eur, dotacia_eur
        for arch in archetypes:
            match = _find_best_match(engine_variants, arch["fve_kwp"], arch["bess_kwh"])
            if match:
                arch["npv_eur"] = float(match.get("npv_eur", 0) or 0)
                arch["irr_pct"] = float(match.get("irr_pct", 0) or 0)
                arch["payback_years"] = float(match.get("payback_simple_y", 0) or 0)
                arch["self_consumption_pct"] = float(match.get("samospotreba_pct", 0) or 0)
                arch["self_sufficiency_pct"] = float(match.get("samostatnost_pct", 0) or 0)
                arch["capex_total_eur"] = float(match.get("capex_total_eur", 0) or 0)
                arch["dotacia_eur"] = float(match.get("dotacia_eur", 0) or 0)
            else:
                _apply_economic_fallback(arch, annual_kwh, annual_estimated, capex_overrides)
            # Ak engine match má 0 NPV/payback (zlyhal výpočet) — fallback
            if (arch.get("npv_eur") or 0) <= 0 or (arch.get("payback_years") or 0) <= 0:
                _apply_economic_fallback(arch, annual_kwh, annual_estimated, capex_overrides)
    except Exception as e:
        log.warning(f"Engine call failed, using estimates: {e}")
        for arch in archetypes:
            _apply_economic_fallback(arch, annual_kwh, annual_estimated, capex_overrides)
    
    # Mark all archetypes if estimated
    if annual_estimated:
        for arch in archetypes:
            arch["estimated_from_mrk"] = True
    
    return archetypes


def _apply_economic_fallback(arch: dict, annual_kwh: float, estimated: bool, capex_overrides: dict = None):
    """Jednoduchý ekonomický odhad — pre prípady keď engine nemá 15-min profil
    alebo engine zlyhá. Rešpektuje capex_overrides z UI.
    """
    overrides = capex_overrides or {}
    capex_per_kwp = float(overrides.get("capex_per_kwp") or 760.0)
    capex_per_kwh = float(overrides.get("capex_per_kwh_bess") or 430.0)
    cena_nakup = float(overrides.get("cena_nakup_eur_kwh") or 0.15)
    cena_predaj = float(overrides.get("cena_predaj_eur_kwh") or 0.02)

    kwp = float(arch.get("fve_kwp") or 0)
    bess = float(arch.get("bess_kwh") or 0)

    capex_pv = kwp * capex_per_kwp
    capex_bess = bess * capex_per_kwh
    capex_total = capex_pv + capex_bess

    archetype_key = arch.get("archetype", "")
    samospotreba_base = {
        "conservative":   0.85,
        "optimal_npv":    0.55,
        "independence":   0.55,
        "spot_arbitrage": 0.50,
        "stretch":        0.35,
        "custom":         0.55,
    }
    samospotreba = samospotreba_base.get(archetype_key, 0.50)
    # User override samospotreby (z custom variantu)
    if arch.get("samospotreba_override_pct") is not None:
        samospotreba = float(arch["samospotreba_override_pct"]) / 100.0
    elif bess > 0 and kwp > 0:
        bess_per_kwp = bess / kwp
        bess_bonus = min(0.30, bess_per_kwp * 0.3)
        samospotreba = min(0.90, samospotreba + bess_bonus)
    if annual_kwh > 0 and kwp > 0:
        max_sams = min(1.0, annual_kwh / (kwp * 1050))
        samospotreba = min(samospotreba, max_sams)

    annual_production = kwp * 1050
    self_consumed = annual_production * samospotreba
    exported = max(0.0, annual_production - self_consumed)
    saving_y1 = self_consumed * cena_nakup + exported * cena_predaj

    dotacia = min(capex_total * 0.30, 200000) if kwp <= 500 else 0
    net_capex = max(1.0, capex_total - dotacia)

    payback_simple = net_capex / saving_y1 if saving_y1 > 0 else 99.0
    annuity_factor = 11.47  # (1-(1+0.06)^-20)/0.06 @ 6 %
    npv = saving_y1 * 0.79 * annuity_factor - net_capex  # 79 % po DPPO 21 %

    arch["npv_eur"] = round(npv, 0)
    arch["payback_years"] = round(min(payback_simple, 25.0), 1)
    arch["self_consumption_pct"] = round(samospotreba * 100, 0)
    arch["self_sufficiency_pct"] = round((self_consumed / annual_kwh * 100) if annual_kwh > 0 else 0, 0)
    arch["capex_total_eur"] = round(capex_total, 0)
    arch["dotacia_eur"] = round(dotacia, 0)
    arch["irr_pct"] = round((saving_y1 * 0.79 / net_capex * 100) if net_capex > 0 else 0, 1)
    arch["saving_y1_eur"] = round(saving_y1, 0)
    arch["fallback_estimate"] = True
    arch["assumptions"] = {
        "capex_per_kwp": capex_per_kwp,
        "capex_per_kwh_bess": capex_per_kwh,
        "cena_nakup_eur_kwh": cena_nakup,
        "cena_predaj_eur_kwh": cena_predaj,
        "samospotreba_pct": round(samospotreba * 100, 0),
        "dotacia_eur": dotacia,
    }




def compute_custom_variant(analyza: dict, custom_input: dict, capex_overrides: dict = None) -> dict:
    """Vyrobí jeden custom variant podľa user-zadaných parametrov.
    Volá _apply_economic_fallback (rýchly odhad). Engine sa nevolá lebo by zbytočne
    bežal pre 1 variant a nezohľadnil by user samospotrebu override.
    """
    profile_data = classify_client_profile(analyza)
    annual_kwh = profile_data["annual_kwh"]
    annual_estimated = annual_kwh <= 0

    overrides = dict(capex_overrides or {})
    # custom_input môže obsahovať custom_capex_per_kwp ktorý prebije override default
    if custom_input.get("capex_per_kwp"):
        overrides["capex_per_kwp"] = float(custom_input["capex_per_kwp"])
    if custom_input.get("capex_per_kwh_bess"):
        overrides["capex_per_kwh_bess"] = float(custom_input["capex_per_kwh_bess"])

    arch = {
        "label": custom_input.get("name") or "Vlastný variant",
        "archetype": "custom",
        "fve_kwp": float(custom_input.get("fve_kwp") or 0),
        "bess_kwh": float(custom_input.get("bess_kwh") or 0),
        "bess_kw": float(custom_input.get("bess_kw") or 0),
        "rationale_hint": custom_input.get("note") or "User-defined konfigurácia",
    }
    if custom_input.get("samospotreba_pct") is not None:
        arch["samospotreba_override_pct"] = float(custom_input["samospotreba_pct"])

    _apply_economic_fallback(arch, annual_kwh, annual_estimated, overrides)
    if annual_estimated:
        arch["estimated_from_mrk"] = True
    return arch


def _detect_profile_template(profile: dict) -> str:
    annual = profile["annual_kwh"]
    peak = profile["peak_kw"]
    avg = profile["avg_kw"]
    ratio = peak / avg if avg > 0 else 0
    
    if annual > 100000 and ratio < 2:
        return "priemysel_24_7"
    if annual < 15000:
        return "domacnost"
    if 7000 < annual < 60000 and ratio > 2.5:
        return "kancelaria"
    return "kancelaria"  # default


def _find_best_match(engine_variants: list[dict], target_kwp: float, target_bess: float) -> Optional[dict]:
    best = None
    best_dist = float("inf")
    for v in engine_variants:
        d = abs(v.get("pv_kwp", 0) - target_kwp) + abs(v.get("bess_kwh", 0) - target_bess) * 0.3
        if d < best_dist:
            best_dist = d
            best = v
    return best


# ============================================================
# VRSTVA C — Constraint checks
# ============================================================

def check_constraints(analyza: dict, variants: list[dict]) -> list[dict]:
    """Overí kompatibilitu variantov s OM constraint."""
    warnings = []
    max_export = float(analyza.get("max_export_kw") or 0)
    mrk_kw = float(analyza.get("om_mrk_kw") or 0)
    sadzba = analyza.get("om_sadzba", "NN")
    
    for v in variants:
        kwp = v["fve_kwp"]
        bess_kwh = v.get("bess_kwh", 0)
        ac_kw = kwp / 1.10  # DC/AC ratio
        
        # Max export check
        if max_export > 0 and ac_kw > max_export * 1.1:
            warnings.append({
                "variant": v["label"],
                "kind": "export_limit",
                "severity": "high",
                "message": f"AC výkon {ac_kw:.0f} kW prekročí max_export {max_export:.0f} kW → potrebné obmedzenie cez COM100E (zero-export mode pri >max_export)",
            })
        
        # MRK check (BESS výkon vs MRK)
        bess_kw_est = bess_kwh * 0.5  # typicky 0.5 C-rate
        if mrk_kw > 0 and bess_kw_est > mrk_kw * 0.7:
            warnings.append({
                "variant": v["label"],
                "kind": "mrk_battery",
                "severity": "medium",
                "message": f"BESS výkon ~{bess_kw_est:.0f} kW je vysoký vs MRK {mrk_kw:.0f} kW — pri vybíjaní z BESS pozor na ladenie EMS",
            })
        
        # Trafostanica check (pri VN/VVN)
        if sadzba in ("VN", "VVN") and kwp > 250:
            warnings.append({
                "variant": v["label"],
                "kind": "trafo_capacity",
                "severity": "low",
                "message": f"Pri {kwp:.0f} kWp na {sadzba} odporúčam audit trafostanice — VTL ochrana + sieťový kódex",
            })
    
    return warnings


# ============================================================
# VRSTVA F — Anomálie + Quick wins
# ============================================================

def detect_anomalies_and_opportunities(analyza: dict, profile: dict) -> dict:
    annual = profile["annual_kwh"]
    peak = profile["peak_kw"]
    sadzba = analyza.get("om_sadzba", "NN")
    
    anomalies = []
    opportunities = []
    
    if profile.get("peak_kw", 0) > 0 and profile.get("avg_kw", 0) > 0:
        peak_ratio = profile["peak_kw"] / profile["avg_kw"]
        if peak_ratio > 5:
            anomalies.append({
                "label": f"Extrémny peak ratio {peak_ratio:.1f}× — pozri profile, môže ísť o rozbeh strojov alebo dimenzovanú chybu",
                "impact": "high",
            })
        elif peak_ratio < 1.3:
            anomalies.append({
                "label": "Veľmi plochý profil — typicky datacenter / chladiarne / 24/7 priemysel → BESS arbitráž má extrémny potenciál",
                "impact": "opportunity",
            })
    
    # Spot opportunities
    opportunities.append({
        "label": "OKTE 2025 spot mal záporné hodiny ~18-31 dní/rok (jarné poludnia) → BESS arbitráž navyše ~6-12 €/kWh ročne",
        "action": "Pridať BESS aj keď samospotreba je nízka — spot arbitráž zaplatí",
    })
    
    if sadzba == "NN":
        opportunities.append({
            "label": "Pri NN máš nárok na 'samospotreba' režim (bez výmenníka) → menej byrokracie",
            "action": "Zachovať pripojenie na NN, neuvažovať VN upgrade",
        })
    
    return {"anomalies": anomalies, "opportunities": opportunities}


# ============================================================
# VRSTVA G — Dotácie matching
# ============================================================

def match_dotacie(variants: list[dict]) -> dict:
    """Overí ktoré varianty spĺňajú Zelená podnikom 2026."""
    # Zelená podnikom 2026 parametre (z aom_data/dotacie/sk_2026.yaml)
    SCHEME = "Zelená podnikom 2026"
    MAX_AMOUNT = 200000
    INTENSITY = 0.30
    MIN_SAMOSP = 0.80
    DEADLINE = "2026-06-30"
    
    eligible = []
    for v in variants:
        samosp = v.get("self_consumption_pct", 0) / 100 if v.get("self_consumption_pct", 0) > 1 else v.get("self_consumption_pct", 0)
        capex = v.get("capex_total_eur", 0)
        dotacia_amount = min(capex * INTENSITY, MAX_AMOUNT)
        
        ok = samosp >= MIN_SAMOSP and capex > 0
        eligible.append({
            "label": v["label"],
            "eligible": ok,
            "dotacia_eur": round(dotacia_amount, 0) if ok else 0,
            "reason": "Samospotreba ≥80%" if ok else f"Samospotreba {samosp*100:.0f}% < požadovaných 80%",
        })
    
    return {
        "scheme": SCHEME,
        "max_amount": MAX_AMOUNT,
        "intensity_pct": INTENSITY * 100,
        "min_samospotreba_pct": MIN_SAMOSP * 100,
        "deadline": DEADLINE,
        "eligible_variants": eligible,
    }


# ============================================================
# VRSTVA E — Learning z historických projektov
# ============================================================

def find_similar_projects(sb, profile: dict, top_n: int = 3) -> list[dict]:
    res = sb.table("aom_historical_projects").select("*").execute()
    historical = res.data or []
    
    target_type = _detect_profile_template(profile)
    target_kwh = profile["annual_kwh"]
    
    scored = []
    for h in historical:
        sim = 0
        if h.get("client_type") == target_type:
            sim += 50
        h_kwh = float(h.get("client_kwh_per_year") or 0)
        if h_kwh > 0 and target_kwh > 0:
            ratio = min(h_kwh, target_kwh) / max(h_kwh, target_kwh)
            sim += int(ratio * 50)
        
        predicted = float(h.get("predicted_payback_y") or 0)
        actual = float(h.get("actual_payback_y") or 0)
        delta_pct = ((actual - predicted) / predicted * 100) if predicted > 0 else 0
        
        scored.append({
            "name": h["client_name"],
            "similarity_pct": sim,
            "fve_kwp": h.get("fve_kwp"),
            "bess_kwh": h.get("bess_kwh"),
            "predicted_payback_y": predicted,
            "actual_payback_y": actual,
            "delta_pct": round(delta_pct, 1),
            "notes": h.get("notes"),
        })
    
    scored.sort(key=lambda x: x["similarity_pct"], reverse=True)
    return scored[:top_n]


# ============================================================
# VRSTVA D — Conversational refinement (Claude chat)
# ============================================================

def chat_refinement(sb, analyza_id: str, user_message: str, user_name: str = None) -> dict:
    """User pošle správu, AI odpovedá. AI vie modifikovať varianty."""
    # Načítaj kontext
    a_res = sb.table("analyza_om").select("*").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    s_res = sb.table("analyza_om_ai_suggestions").select("*").eq("analyza_id", analyza_id).order("created_at", desc=True).limit(1).execute()
    last_sug = s_res.data[0] if s_res.data else None
    chat_res = sb.table("analyza_om_ai_chat").select("role, content, created_at").eq("analyza_id", analyza_id).order("created_at").limit(20).execute()
    history = chat_res.data or []
    
    system_prompt = _build_system_prompt(analyza, last_sug)
    
    messages = []
    for h in history:
        if h["role"] in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})
    
    client = _get_client()
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages,
        )
        ai_text = resp.content[0].text if resp.content else ""
    except Exception as e:
        log.exception("[aom-ai-chat] Claude failed")
        ai_text = f"⚠ AI chat momentálne nedostupný: {str(e)[:200]}"
    
    # Save AI response
    sb.table("analyza_om_ai_chat").insert({
        "analyza_id": analyza_id,
        "role": "assistant",
        "content": ai_text,
        "user_name": "AI Strategist",
        "action_type": "pure_chat",
    }).execute()
    
    return {"ok": True, "reply": ai_text}


def _build_system_prompt(analyza: dict, last_sug: Optional[dict]) -> str:
    """Senior konzultant tón — vecné, technické, slovenčina."""
    sug_summary = ""
    if last_sug:
        variants = last_sug.get("variants") or []
        v_summary = "\n".join(
            f"  - {v.get('label')}: {v.get('fve_kwp')} kWp + {v.get('bess_kwh', 0)} kWh BESS → NPV {v.get('npv_eur', 0):.0f} €, payback {v.get('payback_years', 0):.1f} r"
            for v in variants[:5]
        )
        sug_summary = f"\n\nAKTUÁLNE NAVRHNUTÉ VARIANTY:\n{v_summary}\n\nKLASIFIKÁCIA KLIENTA: {last_sug.get('client_classification', '—')}"
    
    return f"""Si **Senior Energy Strategist** pre Energovision (slovenská FVE/BESS firma).

TÓN:
- Vecný, technický, priamy (ako KEMA / Frost & Sullivan konzultant)
- Slovenčina, krátke vety, žiadny smalltalk, žiadny marketing
- Pomocou konkrétnych čísiel (€, kWh, %, roky)
- Odpovedaj v 3-6 vetách (max), pokiaľ to nie je výpočet

OBLASTI EXPERTNÝCH ZNALOSTÍ:
- OKTE spot DAM 2025 dáta (8760 h, priemer ~85 €/MWh, 31 záporných hodín)
- Distribučné tarify SK 2026 (SSE/ZSD/VSD)
- Naumann-Schimpe LFP degradácia (1.5-2 %/rok kalibrovaná)
- DPPO 22 % daňový odpis 6r (novela 2025)
- Dotácia Zelená podnikom 2026 (max 200k €, 30 % intenzita, min 80 % samospotreba, deadline 30.06.2026)
- Reálne projekty Energovision (RATUFA, AGROSTAV, RE-PLAST)

KLIENT TEJTO ANALÝZY:
- Názov: {analyza.get('name', '—')}
- PSČ: {analyza.get('om_psc', '—')}
- Sadzba: {analyza.get('om_sadzba', '—')}, MRK {analyza.get('om_mrk_kw', 0)} kW
- Spotreba: {float(analyza.get('consumption_annual_mwh', 0) or 0):.1f} MWh/rok
- Tarif source: {analyza.get('tarif_source', 'spot')}
{sug_summary}

ČO MÔŽEŠ ROBIŤ:
1. Vysvetliť dáta, čísla, dôvody
2. Navrhnúť úpravu variantov (uveďuj presné kWp/kWh + dôvody)
3. Porovnať dva varianty (NPV / risk / samospotreba trade-off)
4. Odporučiť ďalší krok (data, dokumenty, audit)
5. Spýtať sa na chýbajúce vstupy

ČO NEROBIŤ:
- Nezachádzaj do oblasti právnej alebo daňovej (len odporúč konzultáciu)
- Negarantuj konkrétne dotácie — vždy povedz "kvalifikuje sa za predpokladu..."
- Nepoužívaj emotívne slová (super, fantastic, amazing)
"""


# ============================================================
# ORCHESTRÁTOR — full analýza (Vrstvy A → G)
# ============================================================

def run_full_analysis(sb, analyza_id: str, capex_overrides: dict = None) -> dict:
    """Spustí celú AI Strategist analýzu pre analyza_id.
    capex_overrides: { capex_per_kwp, capex_per_kwh_bess, cena_nakup_eur_kwh, cena_predaj_eur_kwh }
    """
    t0 = time.time()
    
    # 1. Načítaj analyzu
    a_res = sb.table("analyza_om").select("*").eq("id", analyza_id).single().execute()
    analyza = a_res.data
    if not analyza:
        return {"ok": False, "error": "Analyza not found"}
    
    # Vrstva A: classify
    profile = classify_client_profile(analyza)
    
    # Vrstva B: smart variants (spustí engine + fallback ak treba)
    variants = generate_smart_variants(sb, analyza, profile, capex_overrides=capex_overrides)
    
    # Vrstva C: constraints
    constraints = check_constraints(analyza, variants)
    
    # Vrstva F: anomálie + opportunities
    anom = detect_anomalies_and_opportunities(analyza, profile)
    
    # Vrstva G: dotácie
    dotacie = match_dotacie(variants)
    
    # Vrstva E: similar projects
    similar = find_similar_projects(sb, profile, top_n=3)
    
    # Reasoning per variant (Claude — krátke 1-veta dôvody)
    variants_with_reasoning = _add_reasoning(variants, analyza, profile)
    
    # Top pick — najvyšší NPV s pozitívnou samospotrebou
    eligible = [v for v in variants_with_reasoning if v.get("npv_eur", 0) > 0]
    top_pick = max(eligible, key=lambda v: v["npv_eur"])["label"] if eligible else (variants_with_reasoning[1]["label"] if len(variants_with_reasoning) > 1 else None)
    
    # Default assumptions (ak overrides nezadané, ukáž defaults pre UI)
    overrides = capex_overrides or {}
    default_assumptions = {
        "capex_per_kwp": float(overrides.get("capex_per_kwp") or 760.0),  # tier mid
        "capex_per_kwh_bess": float(overrides.get("capex_per_kwh_bess") or 430.0),
        "cena_nakup_eur_kwh": float(overrides.get("cena_nakup_eur_kwh") or 0.15),
        "cena_predaj_eur_kwh": float(overrides.get("cena_predaj_eur_kwh") or 0.02),
    }
    
    payload = {
        "analyza_id": analyza_id,
        "client_classification": profile["classification"],
        "assumptions_used": default_assumptions,
        "patterns_detected": profile["patterns_detected"],
        "constraints": constraints,
        "variants": variants_with_reasoning,
        "top_pick_label": top_pick,
        "anomalies": anom["anomalies"],
        "opportunities": anom["opportunities"],
        "dotacia_match": dotacie,
        "similar_projects": similar,
        "engine_version": "0.9.5",
        "claude_model": CLAUDE_MODEL,
        "generation_ms": int((time.time() - t0) * 1000),
    }
    
    # Save do DB
    sb.table("analyza_om_ai_suggestions").insert(payload).execute()
    
    return {"ok": True, **payload}


def _add_reasoning(variants: list[dict], analyza: dict, profile: dict) -> list[dict]:
    """Pridá Claude-vygenerované 1-veta reasoning pre každý variant."""
    client = _get_client()
    
    summary_lines = []
    for v in variants:
        summary_lines.append(
            f"- {v['label']} ({v['archetype']}): {v['fve_kwp']} kWp + {v.get('bess_kwh', 0)} kWh BESS, "
            f"NPV {v.get('npv_eur', 0):.0f} €, payback {v.get('payback_years', 0):.1f} r"
        )
    
    prompt = f"""Si Senior Energy Strategist. Pre každý variant napíš 1-vetu (max 25 slov) prečo
ho odporúčaš/neodporúčaš, vecne, slovenčina, žiadny marketing.

Klient: {profile['classification']} ({profile['annual_kwh']/1000:.1f} MWh/rok)
PSČ: {analyza.get('om_psc', '—')}, Sadzba: {analyza.get('om_sadzba', 'NN')}

Varianty:
{chr(10).join(summary_lines)}

Output JSON pole 5 reasoning strings v poradí variantov:
{{"reasoning": ["...", "...", "...", "...", "..."]}}
"""
    
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text if resp.content else "{}"
        # Try parse JSON
        import re
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            data = json.loads(m.group(0))
            reasonings = data.get("reasoning", [])
            for i, v in enumerate(variants):
                if i < len(reasonings):
                    v["reasoning"] = reasonings[i]
                else:
                    v["reasoning"] = v.get("rationale_hint", "")
        else:
            for v in variants:
                v["reasoning"] = v.get("rationale_hint", "")
    except Exception as e:
        log.warning(f"Claude reasoning failed: {e}")
        for v in variants:
            v["reasoning"] = v.get("rationale_hint", "")
    
    return variants


def accept_variant(sb, analyza_id: str, variant_label: str) -> dict:
    """Akceptuje konkrétny variant z AI sugestií → uloží do analyza_om_variants."""
    s_res = sb.table("analyza_om_ai_suggestions").select("*").eq("analyza_id", analyza_id).order("created_at", desc=True).limit(1).execute()
    if not s_res.data:
        return {"ok": False, "error": "No suggestions found"}
    
    sug = s_res.data[0]
    variants = sug.get("variants") or []
    target = next((v for v in variants if v.get("label") == variant_label), None)
    if not target:
        return {"ok": False, "error": f"Variant {variant_label} not found"}
    
    # Insert do analyza_om_variants
    next_pos = sb.table("analyza_om_variants").select("position").eq("analyza_id", analyza_id).order("position", desc=True).limit(1).execute()
    pos = (next_pos.data[0]["position"] + 1) if next_pos.data else 1
    
    sb.table("analyza_om_variants").insert({
        "analyza_id": analyza_id,
        "name": target["label"],
        "position": pos,
        "fve_kwp": target["fve_kwp"],
        "fve_tilt_deg": 25,
        "fve_azimuth_deg": 180,
        "fve_topology": "south",
        "bess_kwh": target.get("bess_kwh", 0),
        "bess_kw": target.get("bess_kwh", 0) * 0.5,
        "bess_arbitrage_enabled": target.get("archetype") == "spot_arbitrage",
        "capex_eur": target.get("capex_total_eur", 0),
        "capex_source": "ai_strategist",
        "result_samosp_pct": target.get("self_consumption_pct", 0),
        "result_samostat_pct": target.get("self_sufficiency_pct", 0),
        "result_npv_eur_base": target.get("npv_eur", 0),
        "result_irr_pct_base": target.get("irr_pct", 0),
        "result_payback_y_base": target.get("payback_years", 0),
        "result_dotacia_eur": target.get("dotacia_eur", 0),
    }).execute()
    
    return {"ok": True, "variant_label": variant_label, "position": pos}
