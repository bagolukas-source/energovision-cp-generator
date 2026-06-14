"""
B2B Kalkulačka V2 — panely-driven + vendor stacks + AI compatibility

Vstupy:
  typ_strechy: vychod_zapad | trapez | skridla | falcovany_plech | juzna | zemne_skrutky | corab
  pocet_panelov: int  (HLAVNÝ vstup — primary)
  panel_sku: str (default "LONGI535")
  vendor_stack: 'sungrow' | 'huawei' | 'goodwe' | 'solinteg'
  has_bess: bool, bess_kwh: float
  has_wallbox: bool, wallbox_pocet: int
  has_optimizery: bool
  has_rapid_shutdown: bool
  vzdialenost_rozvadzac: float (m, default 30)
  vzdialenost_doprava: float (km, default 20)
  margin_pct: float (default 25)

Output: BOM JSON s vendor compatibility checks + AI warnings.
"""
import math
import logging
from typing import Optional

log = logging.getLogger(__name__)

DC_AC_RATIO = 1.10
# ASDR sa vyžaduje pri súčte meničov >= 100 kW AC (cena ~30k). Do MAX_KWP_NO_ASDR
# uprednostníme zostavu meničov < 100 kW (vyhne sa ASDR).
MAX_KWP_NO_ASDR = 130.0      # hranica, do ktorej sa snažíme udržať AC < 100 kW
MAX_OVERSIZE = 1.50          # max DC/AC oversizing (panely vs menič)
TARGET_OVERSIZE = 1.15       # ideálny DC/AC pomer
MAX_INVERTER_UNITS = 3       # max počet meničov v zostave (nestackovať mikro-meniče)
OVERSIZE_BAND = (0.85, 1.30)  # zdravé pásmo DC/AC; mimo neho penalizuj


def _load_vendor_stack(sb, vendor_key: str) -> Optional[dict]:
    res = sb.table("b2b_vendor_stacks").select("*").eq("vendor_key", vendor_key).single().execute()
    return res.data


def _load_konstrukcia_rule(sb, typ_strechy: str) -> list[dict]:
    """Vráti konštrukciu pre typ strechy z b2b_calc_rules."""
    res = sb.table("b2b_calc_rules").select("*").eq("rule_type", "konstrukcia").eq("typ_strechy", typ_strechy).execute()
    return res.data or []


def _load_rule(sb, rule_type: str, rule_key: str = None) -> list[dict]:
    q = sb.table("b2b_calc_rules").select("*").eq("rule_type", rule_type)
    if rule_key:
        q = q.eq("rule_key", rule_key)
    res = q.execute()
    return res.data or []


import re as _re

def _eval_qty_formula(formula, kwp, pocet_panelov) -> int:
    """Bezpečne vyhodnotí qty_formula z b2b_calc_rules (napr. 'ceil(kwp * 1.5)', 'kwp * 10', 'pocet_panelov').
    Rešpektuje koeficienty z DB (predtým ich kód ignoroval → žľab dostal kWp×7 namiesto ×1.5)."""
    f = (formula or "").strip().lower()
    if not f:
        return 1
    # whitelist: čísla, operátory, zátvorky, povolené názvy funkcií/premenných
    if not _re.fullmatch(r"[0-9.+\-*/() %a-z_]+", f):
        return 1
    env = {"ceil": math.ceil, "floor": math.floor, "round": round, "min": min, "max": max,
           "kwp": float(kwp or 0), "kwp_actual": float(kwp or 0), "pocet_panelov": float(pocet_panelov or 0),
           "panels": float(pocet_panelov or 0)}
    try:
        val = eval(f, {"__builtins__": {}}, env)
        return max(1, int(math.ceil(float(val))))
    except Exception:
        return 1


def _pick_inverters(inverters: list[dict], required_ac_kw: float) -> list[dict]:
    """Vyber kombináciu meničov (1–MAX_INVERTER_UNITS kusov), ktorá uvezie panely (kwp_actual).
    Pravidlá:
      • celkový oversizing kwp/AC v okne 0.6–MAX_OVERSIZE, cieľ ~TARGET_OVERSIZE
      • žiadny menič preťažený (DC podiel podľa AC ≤ jeho max_kwp)
      • celková kapacita meničov (Σ max_kwp) musí pokryť panely
      • do MAX_KWP_NO_ASDR uprednostni súčet AC < 100 kW (vyhne sa ASDR ~30k)
      • inak: najmenej kusov → oversizing najbližšie k cieľu → najlacnejšie
    Ak žiadna platná kombinácia (extra veľký systém) → max počet najväčších meničov."""
    import itertools
    kwp = required_ac_kw * DC_AC_RATIO
    invs = [i for i in inverters if (i.get("ac_kw") or 0) > 0]
    if not invs:
        return []
    cands = []
    for r in range(1, MAX_INVERTER_UNITS + 1):
        for combo in itertools.combinations_with_replacement(invs, r):
            ac = sum(i["ac_kw"] for i in combo)
            if ac <= 0:
                continue
            if sum((i.get("max_kwp") or 99999) for i in combo) < kwp:
                continue
            ov = kwp / ac
            if ov > MAX_OVERSIZE or ov < 0.6:
                continue
            # žiadny menič nesmie dostať viac DC než jeho max (rozdelenie podľa podielu AC)
            if any(kwp * (i["ac_kw"] / ac) > (i.get("max_kwp") or 99999) for i in combo):
                continue
            cands.append((combo, ac, r, ov, sum(float(i.get("price") or 0) for i in combo)))
    if not cands:
        big = max(invs, key=lambda x: x["ac_kw"])
        n = max(1, math.ceil(kwp / (big.get("max_kwp") or big["ac_kw"])))
        return [{"inverter": big, "qty": n}]

    def _score(x):
        _combo, ac, r, ov, cost = x
        asdr = 1 if (ac >= 100 and kwp <= MAX_KWP_NO_ASDR) else 0
        out_of_band = 0 if (OVERSIZE_BAND[0] <= ov <= OVERSIZE_BAND[1]) else 1
        # 1) vyhni sa ASDR (do MAX_KWP_NO_ASDR), 2) oversizing v zdravom pásme,
        # 3) najmenej meničov, 4) najbližšie k cieľu, 5) najlacnejšie.
        return (asdr, out_of_band, r, round(abs(ov - TARGET_OVERSIZE), 2), cost)

    best = min(cands, key=_score)[0]
    picked: list[dict] = []
    for i in best:
        k = i.get("key") or i.get("name")
        existing = next((p for p in picked if (p["inverter"].get("key") or p["inverter"].get("name")) == k), None)
        if existing:
            existing["qty"] += 1
        else:
            picked.append({"inverter": i, "qty": 1})
    return picked


def _pick_bess(batteries: list[dict], target_kwh: float) -> list[dict]:
    """Vyber jednu alebo viac batérií."""
    if target_kwh <= 0 or not batteries:
        return []
    # Najprv skús single match
    sorted_batt = sorted(batteries, key=lambda x: abs(x["capacity_kwh"] - target_kwh))
    best = sorted_batt[0]
    if best.get("modular"):
        # Modular — qty môže byť > 1
        qty = max(1, round(target_kwh / best["capacity_kwh"]))
        return [{"battery": best, "qty": qty}]
    else:
        # Non-modular — 1 ks
        return [{"battery": best, "qty": 1}]


def calculate_bom_v2(sb, config: dict) -> dict:
    """Hlavná V2 funkcia — panely-driven + vendor stack aware."""
    typ_strechy = config.get("typ_strechy", "vychod_zapad")
    vendor_key = config.get("vendor_stack", "sungrow")
    panel_sku = config.get("panel_sku", "LONGI535")
    pocet_panelov_input = int(config.get("pocet_panelov") or 0)
    
    has_bess = bool(config.get("has_bess"))
    bess_kwh = float(config.get("bess_kwh", 0) or 0)
    has_wallbox = bool(config.get("has_wallbox"))
    wallbox_pocet = int(config.get("wallbox_pocet", 0) or 0)
    has_optimizery = bool(config.get("has_optimizery"))
    has_rapid_shutdown = bool(config.get("has_rapid_shutdown"))
    vzdialenost_doprava = float(config.get("vzdialenost_doprava", 20))
    margin_pct = float(config.get("margin_pct", 25))
    
    # Načítaj vendor stack
    stack = _load_vendor_stack(sb, vendor_key)
    if not stack:
        return {"ok": False, "error": f"Vendor stack '{vendor_key}' not found"}
    
    # Vyber panel z vendor stack
    panels = stack.get("preferred_panels") or []
    panel = next((p for p in panels if p["sku"] == panel_sku), None)
    if not panel:
        panel = panels[0] if panels else {"sku": "LONGI535", "name": "LONGi Hi-MO X10 EcoLife LR7-60HVH-535M 535 Wp", "wp": 535, "price_per_unit": 90.69, "cost": 72.55}
    
    # Ak pocet_panelov nie je zadané — odvodzuj z kWp
    if pocet_panelov_input <= 0:
        target_kwp = float(config.get("kwp", 30))
        pocet_panelov = math.ceil(target_kwp * 1000 / panel["wp"])
    else:
        pocet_panelov = pocet_panelov_input
    
    kwp_actual = round(pocet_panelov * panel["wp"] / 1000, 2)
    
    items = []
    pos = 1
    warnings = []
    
    # ===== 1. PANELY =====
    items.append({
        "position": pos, "category": "Panely",
        "product_name": panel["name"],
        "qty": pocet_panelov, "unit": "ks",
        "cost_per_unit": float(panel.get("cost") or panel["price_per_unit"] * 0.77),
        "price_per_unit": float(panel["price_per_unit"]),
        "rule_id": f"panel.{panel['sku']}",
        "vendor_stack": vendor_key,
    })
    pos += 1
    
    # ===== 2. MENIČE =====
    required_ac_kw = kwp_actual / DC_AC_RATIO
    inverters = stack.get("inverters") or []
    picked_inv = _pick_inverters(inverters, required_ac_kw)
    
    for p in picked_inv:
        items.append({
            "position": pos, "category": "Striedače",
            "product_name": p["inverter"]["name"],
            "qty": p["qty"], "unit": "ks",
            "cost_per_unit": p["inverter"]["price"] * 0.77,
            "price_per_unit": float(p["inverter"]["price"]),
            "rule_id": f"menic.{vendor_key}.{p['inverter']['key']}",
            "vendor_stack": vendor_key,
        })
        pos += 1
    
    # Smart manager + smart meter (povinné pri väčších inštaláciách)
    sm = stack.get("smart_manager")
    if sm and kwp_actual > sm.get("required_above_kwp", 0):
        items.append({
            "position": pos, "category": "Monitoring",
            "product_name": sm["name"], "qty": 1, "unit": "ks",
            "cost_per_unit": sm["price"] * 0.77, "price_per_unit": float(sm["price"]),
            "rule_id": f"smart_manager.{vendor_key}", "vendor_stack": vendor_key,
        })
        pos += 1
    smtr = stack.get("smart_meter")
    if smtr:
        items.append({
            "position": pos, "category": "Monitoring",
            "product_name": smtr["name"], "qty": 1, "unit": "ks",
            "cost_per_unit": smtr["price"] * 0.77, "price_per_unit": float(smtr["price"]),
            "rule_id": f"smart_meter.{vendor_key}", "vendor_stack": vendor_key,
        })
        pos += 1
    
    # ===== 3. KONŠTRUKCIA =====
    k_rules = _load_konstrukcia_rule(sb, typ_strechy)
    for r in k_rules:
        # Nový cenník 2026-06-12: konštrukcia sa účtuje podľa POČTU PANELOV (ks), nie kWp.
        # qty_formula z DB ('pocet_panelov' / 'kwp') sa rešpektuje; cost_per_unit z DB má prednosť pred paušálom 0.77.
        k_qty = _eval_qty_formula(r.get("qty_formula"), kwp_actual, pocet_panelov)
        k_cost = float(r["cost_per_unit"]) if r.get("cost_per_unit") is not None else float(r["price_per_unit"]) * 0.77
        items.append({
            "position": pos, "category": "Konštrukcia",
            "product_name": r["product_name"],
            "qty": k_qty, "unit": r["unit"],
            "cost_per_unit": k_cost,
            "price_per_unit": float(r["price_per_unit"]),
            "price_locked": True,  # predaj podľa cenníka 2026-06-12 — globálna marža ho neprepisuje
            "rule_id": f"konstrukcia.{r['rule_key']}",
        })
        pos += 1
    
    # ===== 4. ROZVÁDZAČ (podľa kWp pásma) =====
    r_rules = [r for r in _load_rule(sb, "rozvadzac") 
               if (r.get("min_kwp") or 0) <= kwp_actual <= (r.get("max_kwp") or 99999)]
    if r_rules:
        r = r_rules[0]
        items.append({
            "position": pos, "category": "Rozvádzač",
            "product_name": r["product_name"], "qty": 1, "unit": "ks",
            "cost_per_unit": float(r["price_per_unit"]) * 0.77,
            "price_per_unit": float(r["price_per_unit"]),
            "rule_id": f"rozvadzac.{r['rule_key']}",
        })
        pos += 1
    
    # ===== 5. VODIČE + 6. PD + 7. SPOTREBNÝ + 8. OSTATNÉ =====
    for rt, rk in [("vodice", "dc"), ("vodice", "ac"), ("spotrebny", "standard")]:
        r = next((x for x in _load_rule(sb, rt, rk)), None)
        if r:
            items.append({
                "position": pos, "category": "Vodiče" if rt == "vodice" else "Spotrebný material",
                "product_name": r["product_name"], "qty": kwp_actual, "unit": r["unit"],
                "cost_per_unit": float(r["price_per_unit"]) * 0.77,
                "price_per_unit": float(r["price_per_unit"]),
                "rule_id": f"{rt}.{rk}",
            })
            pos += 1
    
    # PD — pásmo podľa kWp
    pd_rules = [r for r in _load_rule(sb, "pd") if (r.get("min_kwp") or 0) <= kwp_actual <= (r.get("max_kwp") or 99999)]
    if pd_rules:
        r = pd_rules[0]
        items.append({
            "position": pos, "category": "Projektová dokumentácia",
            "product_name": r["product_name"], "qty": 1, "unit": r["unit"],
            "cost_per_unit": float(r["price_per_unit"]) * 0.77,
            "price_per_unit": float(r["price_per_unit"]),
            "rule_id": f"pd.{r['rule_key']}",
        })
        pos += 1
    
    # Ostatné (žľaby, chráničky)
    for rk in ["zlab_kryt_50mm", "chranicka_25mm", "chranicka_40mm"]:
        r = next((x for x in _load_rule(sb, "ostatne", rk)), None)
        if r:
            # qty z DB vzorca — reálne vyhodnotené (rešpektuje koeficient, napr. žľab kWp×1.5)
            qty = _eval_qty_formula(r.get("qty_formula"), kwp_actual, pocet_panelov)
            items.append({
                "position": pos, "category": "Ostatné",
                "product_name": r["product_name"], "qty": qty, "unit": r["unit"],
                "cost_per_unit": float(r["price_per_unit"]) * 0.77,
                "price_per_unit": float(r["price_per_unit"]),
                "rule_id": f"ostatne.{rk}",
            })
            pos += 1
    
    # ===== 9. OPTIMIZÉRY (vendor-specific!) =====
    if has_optimizery:
        # Použiť vendor-specific optimizer (Huawei → MERC, Sungrow/GoodWe/Solinteg → Tigo)
        opts = stack.get("optimizers") or []
        if opts:
            opt = opts[0]  # default first
            # počet optimizérov: panels_per_unit (Huawei MERC = 2 panely/kus, Tigo = 1 panel/kus)
            ppu = int(opt.get("panels_per_unit") or 1)
            opt_qty = math.ceil(pocet_panelov / max(1, ppu))
            items.append({
                "position": pos, "category": "Optimizéry",
                "product_name": opt["name"], "qty": opt_qty, "unit": "ks",
                "cost_per_unit": opt["price_per_panel"] * 0.77,
                "price_per_unit": float(opt["price_per_panel"]),
                "rule_id": f"optimizer.{vendor_key}.{opt['key']}",
                "vendor_stack": vendor_key,
                "ai_note": opt.get("notes", ""),
            })
            pos += 1
            # Montáž optimizérov (na kus optimizéra)
            items.append({
                "position": pos, "category": "Montáž",
                "product_name": "Montáž optimizér", "qty": opt_qty, "unit": "ks",
                "cost_per_unit": 3.90 * 0.77, "price_per_unit": 3.90,
                "rule_id": "montaz_optimizer",
            })
            pos += 1
    
    # ===== 10. RAPID SHUTDOWN =====
    if has_rapid_shutdown:
        for rk in ["bfs12", "esw12", "montaz_rs"]:
            r = next((x for x in _load_rule(sb, "rapid_shutdown", rk)), None)
            if r:
                if "ceil" in (r.get("qty_formula") or ""):
                    qty = math.ceil(pocet_panelov / 4) if "4" in r["qty_formula"] else math.ceil(pocet_panelov / 200)
                else:
                    qty = 1
                items.append({
                    "position": pos, "category": "Rapid Shutdown",
                    "product_name": r["product_name"], "qty": qty, "unit": r["unit"],
                    "cost_per_unit": float(r["price_per_unit"]) * 0.77,
                    "price_per_unit": float(r["price_per_unit"]),
                    "rule_id": f"rapid_shutdown.{rk}",
                })
                pos += 1
    
    # ===== 11. BESS (vendor-specific!) =====
    if has_bess and bess_kwh > 0:
        batteries = stack.get("batteries") or []
        picked_batt = _pick_bess(batteries, bess_kwh)
        for b in picked_batt:
            items.append({
                "position": pos, "category": "Batéria",
                "product_name": b["battery"]["name"], "qty": b["qty"], "unit": "ks",
                "cost_per_unit": b["battery"]["price"] * 0.77,
                "price_per_unit": float(b["battery"]["price"]),
                "rule_id": f"battery.{vendor_key}.{b['battery']['key']}",
                "vendor_stack": vendor_key,
            })
            pos += 1
        # Montáž batérie
        items.append({
            "position": pos, "category": "Batéria",
            "product_name": "Montáž batérie", "qty": 1, "unit": "kpl",
            "cost_per_unit": 5000 * 0.77, "price_per_unit": 5000,
            "rule_id": "battery.montaz",
        })
        pos += 1
    
    # ===== 12. WALLBOX =====
    if has_wallbox and wallbox_pocet > 0:
        wbs = stack.get("wallboxes") or []
        if wbs:
            wb = wbs[0]
            items.append({
                "position": pos, "category": "Wallbox",
                "product_name": wb["name"], "qty": wallbox_pocet, "unit": "ks",
                "cost_per_unit": wb["price"] * 0.77, "price_per_unit": float(wb["price"]),
                "rule_id": f"wallbox.{vendor_key}.{wb['key']}",
            })
            pos += 1
    
    # ===== 13. MONTÁŽ (kWp pásmo) =====
    m_rules = [r for r in _load_rule(sb, "montaz") if (r.get("min_kwp") or 0) <= kwp_actual <= (r.get("max_kwp") or 99999)]
    if m_rules:
        r = m_rules[0]
        items.append({
            "position": pos, "category": "Montáž",
            "product_name": r["product_name"], "qty": kwp_actual, "unit": r["unit"],
            "cost_per_unit": float(r["price_per_unit"]) * 0.77,
            "price_per_unit": float(r["price_per_unit"]),
            "rule_id": f"montaz.{r['rule_key']}",
        })
        pos += 1
    
    # ===== 14. DOPRAVA =====
    r = next((x for x in _load_rule(sb, "doprava", "km")), None)
    if r:
        items.append({
            "position": pos, "category": "Doprava",
            "product_name": r["product_name"], "qty": vzdialenost_doprava, "unit": "km",
            "cost_per_unit": float(r["price_per_unit"]) * 0.77,
            "price_per_unit": float(r["price_per_unit"]),
            "rule_id": "doprava.km",
        })
        pos += 1
    
    # ===== MARŽA aplikácia =====
    if margin_pct > 0:
        factor = 1 + margin_pct / 100
        for it in items:
            cost = it["cost_per_unit"]
            # price_locked (konštrukcia z cenníka): predaj z DB, marža ho neprepisuje
            if not it.get("price_locked"):
                it["price_per_unit"] = round(cost * factor, 2)
            it["total_cost"] = round(cost * it["qty"], 2)
            it["total_price"] = round(it["price_per_unit"] * it["qty"], 2)
    else:
        for it in items:
            it["total_cost"] = round(it["cost_per_unit"] * it["qty"], 2)
            it["total_price"] = round(it["price_per_unit"] * it["qty"], 2)
    
    # ===== Compatibility warnings (Vrstva F) =====
    # Huawei + Tigo bug
    if vendor_key == "huawei" and has_optimizery:
        warnings.append({
            "severity": "info",
            "kind": "vendor_match",
            "message": "✓ Huawei stack používa HUAWEI MERC-1300W (native optimizer) — NIE Tigo (nekompatibilný)",
        })
    
    if not has_bess and kwp_actual >= 100:
        warnings.append({
            "severity": "tip",
            "kind": "bess_recommendation",
            "message": f"💡 Pri {kwp_actual} kWp >> 100 odporúčam zvážiť BESS — pre arbitráž a peak shaving. Návratnosť +0.5-1 rok.",
        })
    
    if has_optimizery and not has_rapid_shutdown:
        warnings.append({
            "severity": "tip",
            "kind": "rs_recommendation",
            "message": "💡 Optimizéry + Rapid Shutdown — vyžadované pre verejné budovy podľa STN EN 50549.",
        })
    
    # Totals
    total_cost = sum(it["total_cost"] for it in items)
    total_price = sum(it["total_price"] for it in items)
    
    return {
        "ok": True,
        "config": {
            "vendor_stack": vendor_key,
            "vendor_display": stack["display_name"],
            "typ_strechy": typ_strechy,
            "panel": panel,
            "pocet_panelov": pocet_panelov,
            "kwp_actual": kwp_actual,
        },
        "items": items,
        "totals": {
            "pocet_panelov": pocet_panelov,
            "pocet_menicov": sum(p["qty"] for p in picked_inv),
            "kwp": kwp_actual,
            "panel_wp": panel["wp"],
            "total_cost": round(total_cost, 2),
            "total_price": round(total_price, 2),
            "total_margin_eur": round(total_price - total_cost, 2),
            "margin_pct_effective": round((total_price - total_cost) / total_price * 100, 2) if total_price > 0 else 0,
            "items_count": len(items),
        },
        "warnings": warnings,
    }


# ============================================================
# AI helpers (Claude Sonnet 4.5)
# ============================================================

def ai_smart_configurator(sb, user_text: str) -> dict:
    """Text → form fill: '30 kWp obchod rovná strecha Sungrow' → {kwp, typ_strechy, vendor, ...}"""
    import os
    import json as _json
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    
    prompt = f"""Si Senior Energy Strategist. Z užívateľského textu vyparsuj parametre FVE projektu.

VSTUP: {user_text}

OUTPUT JSON:
{{
  "typ_strechy": "vychod_zapad" | "trapez" | "skridla" | "falcovany_plech" | "juzna" | "zemne_skrutky" | "corab",
  "kwp": float,
  "pocet_panelov": int (alebo null ak nie je v texte),
  "panel_sku": "LONGI535" | "LONGI_580" | "JA_440" | null,
  "vendor_stack": "sungrow" | "huawei" | "goodwe" | "solinteg" (default sungrow),
  "has_bess": bool,
  "bess_kwh": float (0 ak nie),
  "has_optimizery": bool,
  "has_rapid_shutdown": bool,
  "has_wallbox": bool,
  "wallbox_pocet": int,
  "client_type_hint": "priemysel" | "obchod" | "kancelaria" | "obec" | "polnohospodar" | null,
  "reasoning": "1-veta prečo som zvolil tieto hodnoty"
}}

Pravidlá:
- "30 kWp" → kwp = 30, pocet_panelov = null (ráta sa z kWp / Wp_panel)
- "70 panelov" → pocet_panelov = 70
- "obchod / kancelária" → kwp ~10-30, panel 430Wp, vendor Sungrow
- "priemysel" → kwp 100+, panel 580Wp, vendor Sungrow (alebo Huawei pri Premium)
- "rovná strecha / E-W" → typ_strechy = vychod_zapad
- "trapéz / plech" → trapez
- "škridla / šindeľ" → skridla
- "pozemná / zem" → zemne_skrutky
- "verejné budovy / škola / obec" → has_rapid_shutdown = true
- "tienenie / lesné okolie" → has_optimizery = true
- "Huawei" v texte → vendor_stack = huawei
"""
    
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text if resp.content else "{}"
        import re
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            data = _json.loads(m.group(0))
            return {"ok": True, **data}
        return {"ok": False, "error": "AI nevrátil JSON"}
    except Exception as e:
        log.exception("ai_smart_configurator")
        return {"ok": False, "error": str(e)[:200]}


def ai_explain_bom_item(sb, item: dict, config: dict) -> str:
    """1-veta vysvetlenie prečo je tento item v BOM."""
    import os
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    
    prompt = f"""Položka: {item['product_name']} ({item['qty']} {item['unit']}, kategória: {item['category']})
Projekt: {config.get('kwp_actual', 0)} kWp, vendor: {config.get('vendor_stack')}, strecha: {config.get('typ_strechy')}

Vysvetli 1-vetou (max 20 slov) prečo je táto položka v cenovke. Slovenčina, vecne, žiadny marketing."""
    
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text if resp.content else ""
    except Exception:
        return ""


# ============================================================
# AI FEATURES — Vendor Recommender / Compatibility / Sanity / Validator
# ============================================================

# Raynet patterns (z analýzy 2000 ponúk) — fallback heuristics
RAYNET_VENDOR_DISTRIBUTION = {
    "sungrow": 0.65,   # dominantný pri >30 kWp, hala/priemysel
    "huawei":  0.20,   # < 15 kWp, prémiové projekty, FusionSolar
    "goodwe":  0.10,   # menšie residential/komerčné
    "solinteg": 0.05,  # výnimočne, tieto starty
}

# Priemerné €/kWp z Raynet ponúk (predaj bez DPH)
RAYNET_AVG_EUR_PER_KWP = {
    "do_30":    1150.0,   # do 30 kWp
    "30_60":     950.0,   # 30-60 kWp
    "60_100":    830.0,
    "nad_100":   720.0,
}

# Toleranica per kategória (±%) — mimo = warning
RAYNET_PRICE_TOLERANCE = {
    "Konštrukcia": 0.20,
    "Menič": 0.15,
    "Batéria": 0.18,
    "Panel": 0.10,
    "Káble": 0.30,
    "Práca": 0.25,
    "Projektová dokumentácia": 0.30,
}

# Povinné kategórie pre kompletnú FVE
ESSENTIAL_CATEGORIES = [
    "Panel", "Menič", "Konštrukcia",
    "Káble - DC", "Káble - AC",
    "Práca - montáž", "Projektová dokumentácia",
]


def _eur_per_kwp_bucket(kwp: float) -> str:
    if kwp <= 30:
        return "do_30"
    if kwp <= 60:
        return "30_60"
    if kwp <= 100:
        return "60_100"
    return "nad_100"


def ai_vendor_recommender(sb, kwp: float, client_type_hint: Optional[str] = None,
                            has_bess: bool = False, has_optimizery: bool = False,
                            has_rapid_shutdown: bool = False) -> dict:
    """
    Odporučí vendor stack na základe kWp + projektových atribútov.
    Vracia ranked list (top 3) s vysvetlením a Raynet share %.
    """
    scored = {}
    for v, share in RAYNET_VENDOR_DISTRIBUTION.items():
        scored[v] = {"vendor_key": v, "score": share * 100, "reasons": [f"{int(share*100)}% historický podiel v Raynet ponukách"]}

    # Pravidlá z Raynet patterns
    if kwp >= 100:
        scored["sungrow"]["score"] += 25; scored["sungrow"]["reasons"].append(f"Priemysel {kwp:.0f} kWp — Sungrow SG110CX/SG125CX dominuje")
        scored["huawei"]["score"] += 5
        scored["goodwe"]["score"] -= 10
        scored["solinteg"]["score"] -= 15
    elif kwp >= 30:
        scored["sungrow"]["score"] += 20; scored["sungrow"]["reasons"].append(f"Komerčný projekt {kwp:.0f} kWp — Sungrow SG33CX/SG50CX štandard")
        scored["huawei"]["score"] += 10
    elif kwp >= 15:
        scored["sungrow"]["score"] += 5
        scored["huawei"]["score"] += 15; scored["huawei"]["reasons"].append("Stredné projekty — Huawei SUN2000 vhodný")
        scored["goodwe"]["score"] += 10
    else:
        scored["huawei"]["score"] += 20; scored["huawei"]["reasons"].append(f"Malé {kwp:.0f} kWp — Huawei SUN2000-10/15KTL prémium")
        scored["goodwe"]["score"] += 15

    if has_bess:
        scored["sungrow"]["score"] += 10; scored["sungrow"]["reasons"].append("BESS — Sungrow SBR HV battery preferovaná")
        scored["huawei"]["score"] += 8;  scored["huawei"]["reasons"].append("BESS — Huawei LUNA2000 series kompatibilná")
        scored["solinteg"]["score"] -= 5

    if has_optimizery and not has_rapid_shutdown:
        scored["huawei"]["score"] += 15; scored["huawei"]["reasons"].append("Optimizéry — Huawei MERC-1300W native (lepšie ako Tigo)")
        scored["sungrow"]["score"] += 0  # vyžaduje Tigo external

    if has_rapid_shutdown:
        scored["sungrow"]["score"] += 5; scored["sungrow"]["reasons"].append("Rapid Shutdown — Sungrow + Tigo MLPE")

    if client_type_hint == "priemysel":
        scored["sungrow"]["score"] += 15
    elif client_type_hint == "obchod":
        scored["sungrow"]["score"] += 8
        scored["huawei"]["score"] += 5

    ranked = sorted(scored.values(), key=lambda x: -x["score"])
    # normalizuj confidence na 0–100
    total = sum(max(0, r["score"]) for r in ranked) or 1
    for r in ranked:
        r["confidence_pct"] = round(max(0, r["score"]) / total * 100, 1)

    return {
        "ok": True,
        "ranked": ranked[:3],
        "recommended": ranked[0]["vendor_key"],
        "rationale": "; ".join(ranked[0]["reasons"][:3]),
    }


def ai_compatibility_checker(sb, config: dict) -> dict:
    """
    Real-time compatibility check pre vybraný vendor + komponenty.
    Vracia warnings (severity: error/warning/info) ešte pred preview.
    """
    vendor = (config.get("vendor_stack") or "").lower()
    typ_strechy = config.get("typ_strechy") or ""
    has_bess = bool(config.get("has_bess"))
    bess_kwh = float(config.get("bess_kwh") or 0)
    has_optim = bool(config.get("has_optimizery"))
    has_rs = bool(config.get("has_rapid_shutdown"))
    has_wb = bool(config.get("has_wallbox"))
    pocet_panelov = int(config.get("pocet_panelov") or 0)
    panel_sku = config.get("panel_sku") or "LONGI535"

    issues = []

    # Vendor × optimizer
    if vendor == "huawei" and has_optim:
        issues.append({"severity": "info", "kind": "vendor_match",
                       "message": "Huawei + optimizéry → použijem HUAWEI MERC-1300W (native), nie Tigo."})
    if vendor in ("sungrow", "goodwe", "solinteg") and has_optim:
        issues.append({"severity": "info", "kind": "vendor_match",
                       "message": f"{vendor.title()} + optimizéry → external Tigo TS4-A-O (Huawei MERC inkompatibilný)."})

    # BESS sanity
    if has_bess and bess_kwh <= 0:
        issues.append({"severity": "warning", "kind": "bess_missing_kwh",
                       "message": "Označená batéria ale 0 kWh — nastavte kapacitu (default 10 kWh)."})
    if has_bess and bess_kwh > 0:
        if vendor == "solinteg" and bess_kwh < 5:
            issues.append({"severity": "warning", "kind": "vendor_bess",
                           "message": "Solinteg HV: minimum 5 kWh. Pre menšie použite Sungrow alebo Huawei."})
        if vendor == "huawei" and (bess_kwh < 5 or bess_kwh > 30):
            issues.append({"severity": "info", "kind": "vendor_bess",
                           "message": "Huawei LUNA2000: optimal 5–30 kWh modulárne (5/10/15)."})

    # Pre flat (E-W) rapid_shutdown býva povinný pre verejné budovy
    if typ_strechy == "vychod_zapad" and not has_rs and pocet_panelov > 100:
        issues.append({"severity": "info", "kind": "code_check",
                       "message": "Veľká E-W hala (>100 panelov) — zvážte Rapid Shutdown ak je to verejná budova (norma)."})

    # Panel sanity
    if pocet_panelov > 0 and (pocet_panelov % 2 == 1 and typ_strechy == "vychod_zapad"):
        issues.append({"severity": "info", "kind": "panel_count",
                       "message": "Nepárny počet panelov na E-W streche — symetrické rozloženie odporúčam zaokrúhliť hore."})

    # Wallbox
    if has_wb and int(config.get("wallbox_pocet") or 0) <= 0:
        issues.append({"severity": "warning", "kind": "wb_qty",
                       "message": "Wallbox označený ale počet 0 — nastavte aspoň 1 ks."})

    severity_rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda x: severity_rank.get(x["severity"], 9))

    return {"ok": True, "issues": issues, "count": len(issues)}


def ai_price_sanity_check(sb, items: list[dict], kwp: float) -> dict:
    """
    Porovná predajné ceny per kategória voči Raynet histórii.
    Flag-uje extrémne odchýlky (>tolerance%).
    """
    if not items or kwp <= 0:
        return {"ok": True, "flags": [], "total_eur_per_kwp": 0, "raynet_avg_eur_per_kwp": 0}

    total_sell = sum((it.get("price_per_unit") or 0) * (it.get("qty") or 0) for it in items)
    eur_per_kwp = total_sell / kwp
    bucket = _eur_per_kwp_bucket(kwp)
    avg = RAYNET_AVG_EUR_PER_KWP[bucket]
    dev = (eur_per_kwp - avg) / avg

    flags = []
    if dev > 0.25:
        flags.append({"severity": "warning", "kind": "overall_high",
                      "message": f"Cena {eur_per_kwp:.0f} €/kWp je o {dev*100:+.0f}% nad Raynet priemerom ({avg:.0f} €/kWp pre {bucket.replace('_', '–')} kWp).",
                      "metric": "eur_per_kwp"})
    elif dev < -0.25:
        flags.append({"severity": "warning", "kind": "overall_low",
                      "message": f"Cena {eur_per_kwp:.0f} €/kWp je o {dev*100:+.0f}% pod Raynet priemerom ({avg:.0f} €/kWp) — overiť maržu.",
                      "metric": "eur_per_kwp"})

    # Per item: výrazne anomálne ceny
    for it in items:
        cat = (it.get("category") or "").strip()
        price = it.get("price_per_unit") or 0
        cost = it.get("cost_per_unit") or 0
        margin = (price - cost) / cost if cost > 0 else 0
        # Negatívna marža = error
        if cost > 0 and price < cost:
            flags.append({"severity": "error", "kind": "negative_margin",
                          "message": f"{it.get('product_name','?')} — predaj {price:.2f}€ < nákup {cost:.2f}€ (strata).",
                          "position": it.get("position")})
        # Extrémne nízka marža (<5%) v komponentoch
        elif cost > 0 and margin < 0.05 and cat not in ("Doprava", "Spotrebný materiál"):
            flags.append({"severity": "info", "kind": "low_margin",
                          "message": f"{it.get('product_name','?')} — marža {margin*100:.1f}% (málo).",
                          "position": it.get("position")})

    return {
        "ok": True,
        "flags": flags,
        "total_eur_per_kwp": round(eur_per_kwp, 1),
        "raynet_avg_eur_per_kwp": round(avg, 1),
        "deviation_pct": round(dev * 100, 1),
        "bucket": bucket,
    }


def ai_bom_validator(sb, items: list[dict], config: dict) -> dict:
    """
    Skontroluje či BOM obsahuje všetky podstatné kategórie.
    Flag-uje chýbajúce komponenty (Panel/Menič/Konštrukcia/Káble/Práca/PD).
    """
    if not items:
        return {"ok": True, "missing": [], "warnings": [{"severity": "warning", "message": "BOM je prázdny."}]}

    present_cats = {(it.get("category") or "").strip() for it in items}
    missing = []
    warnings = []

    # Skupiny ktoré sú "OK ak existuje aspoň jeden"
    grouped = {
        "Panel": ["Panel", "Fotovoltický panel"],
        "Menič": ["Menič", "Striedač", "Invertor"],
        "Konštrukcia": ["Konštrukcia"],
        "Káble - DC": ["Káble - DC", "DC kábel", "Solárny kábel"],
        "Káble - AC": ["Káble - AC", "AC kábel", "CYKY"],
        "Práca - montáž": ["Práca - montáž", "Práca", "Montáž"],
        "Projektová dokumentácia": ["Projektová dokumentácia", "PD", "Projekt"],
    }
    for label, aliases in grouped.items():
        if not any(a in present_cats for a in aliases):
            missing.append(label)

    if missing:
        warnings.append({
            "severity": "warning",
            "kind": "missing_categories",
            "message": "Chýba(jú): " + ", ".join(missing),
            "items": missing,
        })

    # BESS check ak je v configu zapnutý
    if config.get("has_bess") and not any("Batéria" in (it.get("category") or "") for it in items):
        warnings.append({"severity": "error", "kind": "bess_in_config_not_bom",
                         "message": "Config má has_bess=true ale BOM neobsahuje batériu."})

    # Wallbox check
    if config.get("has_wallbox") and not any("Wallbox" in (it.get("category") or "") for it in items):
        warnings.append({"severity": "warning", "kind": "wallbox_missing",
                         "message": "Wallbox označený v configu ale chýba v BOM."})

    # Konzistencia: počet meničov vs počet panelov
    pocet_p = int(config.get("pocet_panelov") or 0)
    if pocet_p > 0:
        menic_qty = sum((it.get("qty") or 0) for it in items if "Menič" in (it.get("category") or "") or "Striedač" in (it.get("category") or ""))
        if menic_qty == 0:
            warnings.append({"severity": "error", "kind": "no_inverter",
                             "message": "BOM neobsahuje menič / striedač."})

    return {"ok": True, "missing": missing, "warnings": warnings, "present_categories": sorted(present_cats)}


# ============================================================
# SAVE V2 — perzistujeme final items (incl. inline edits + custom items)
# ============================================================

def save_bundle_v2(sb, payload: dict) -> dict:
    """
    payload:
      config: { vendor_stack, typ_strechy, pocet_panelov, panel_sku, has_bess, bess_kwh, has_wallbox, wallbox_pocet, has_optimizery, has_rapid_shutdown, margin_pct, kwp_actual }
      final_items: list[item]  (output BOM rows AFTER user edits + custom items)
      customer_id, lead_id, user_id
      payment_terms (optional)
    """
    config = payload.get("config") or {}
    items = payload.get("final_items") or []

    if not items:
        raise RuntimeError("save_bundle_v2: final_items je prázdne — uložiť nemôžem.")

    total_cost = sum((it.get("cost_per_unit") or 0) * (it.get("qty") or 0) for it in items)
    total_sell = sum((it.get("price_per_unit") or 0) * (it.get("qty") or 0) for it in items)
    margin_pct = float(config.get("margin_pct") or 25)
    kwp = float(config.get("kwp_actual") or 0)

    bom_array = []
    for it in items:
        qty = float(it.get("qty") or 0)
        cost = float(it.get("cost_per_unit") or 0)
        sell = float(it.get("price_per_unit") or 0)
        bom_array.append({
            "sku": it.get("rule_id") or it.get("sku") or "",
            "name": it.get("product_name") or "",
            "category": it.get("category") or "",
            "qty": qty,
            "unit": it.get("unit") or "ks",
            "unit_purchase": cost,
            "unit_sale": sell,
            "total_purchase": cost * qty,
            "total_sale": sell * qty,
            "is_custom": bool(it.get("is_custom")),
        })

    bundle_data = {
        "vykon_kwp": kwp,
        "typ_ponuky": "b2b_kalkulator_v2",
        "lead_id": payload.get("lead_id"),
        "customer_id": payload.get("customer_id"),
        "created_by": payload.get("user_id"),
        "status": "draft",
        "payment_terms": payload.get("payment_terms") or "30% pri objednávke / 30% pri dodávke / 40% pri odovzdaní",
        "workspace": "b2b",
        # Variant A = vypočítaný BOM s editmi
        "variant_a_active": True,
        "variant_a_marza_pct": margin_pct,
        "variant_a_cost": round(total_cost, 2),
        "variant_a_price_no_vat": round(total_sell, 2),
        "variant_a_price_with_vat": round(total_sell * 1.23, 2),
        "variant_a_bom": bom_array,
        # Meta — config snapshot pre re-open editora
        "b2b_v2_config": config,
    }

    res = sb.table("quote_bundles").insert(bundle_data).execute()
    if not res.data:
        raise RuntimeError("Bundle insert failed")
    return res.data[0]
