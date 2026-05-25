"""
B2B Kalkulačka V2 — panely-driven + vendor stacks + AI compatibility

Vstupy:
  typ_strechy: vychod_zapad | trapez | skridla | falcovany_plech | juzna | zemne_skrutky | corab
  pocet_panelov: int  (HLAVNÝ vstup — primary)
  panel_sku: str (default "LONGI_430")
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


def _pick_inverters(inverters: list[dict], required_ac_kw: float) -> list[dict]:
    """Greedy fill — vyber kombináciu meničov ktoré pokryjú required_ac_kw."""
    sorted_inv = sorted(inverters, key=lambda x: -x["ac_kw"])
    remaining = required_ac_kw
    picked = []
    
    for inv in sorted_inv:
        # Inverter platí pre tento kWp rozsah?
        if remaining <= 0:
            break
        min_k = inv.get("min_kwp", 0)
        max_k = inv.get("max_kwp", 99999)
        # Inverter je vhodný pre kWp range
        target_kwp = required_ac_kw * DC_AC_RATIO
        if target_kwp < min_k or target_kwp > max_k:
            continue
        
        n = math.floor(remaining / inv["ac_kw"])
        if n > 0:
            picked.append({"inverter": inv, "qty": n})
            remaining -= n * inv["ac_kw"]
    
    # Ak ostalo zvyšok — pridaj najmenší kompatibilný menič
    if remaining > 0:
        target_kwp = required_ac_kw * DC_AC_RATIO
        small = [i for i in inverters if (i.get("min_kwp") or 0) <= target_kwp <= (i.get("max_kwp") or 99999)]
        if small:
            smallest = min(small, key=lambda x: x["ac_kw"])
            existing = next((p for p in picked if p["inverter"]["key"] == smallest["key"]), None)
            if existing:
                existing["qty"] += 1
            else:
                picked.append({"inverter": smallest, "qty": 1})
    
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
    panel_sku = config.get("panel_sku", "LONGI_430")
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
        panel = panels[0] if panels else {"sku": "LONGI_430", "name": "Longi Hi-MO6 430 M", "wp": 430, "price_per_unit": 98.93, "cost": 76.10}
    
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
            "position": pos, "category": "Striedače",
            "product_name": sm["name"], "qty": 1, "unit": "ks",
            "cost_per_unit": sm["price"] * 0.77, "price_per_unit": float(sm["price"]),
            "rule_id": f"smart_manager.{vendor_key}", "vendor_stack": vendor_key,
        })
        pos += 1
    smtr = stack.get("smart_meter")
    if smtr:
        items.append({
            "position": pos, "category": "Striedače",
            "product_name": smtr["name"], "qty": 1, "unit": "ks",
            "cost_per_unit": smtr["price"] * 0.77, "price_per_unit": float(smtr["price"]),
            "rule_id": f"smart_meter.{vendor_key}", "vendor_stack": vendor_key,
        })
        pos += 1
    
    # ===== 3. KONŠTRUKCIA =====
    k_rules = _load_konstrukcia_rule(sb, typ_strechy)
    for r in k_rules:
        items.append({
            "position": pos, "category": "Konštrukcia",
            "product_name": r["product_name"],
            "qty": kwp_actual, "unit": r["unit"],
            "cost_per_unit": float(r["price_per_unit"]) * 0.77,
            "price_per_unit": float(r["price_per_unit"]),
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
            # qty formula evaluation — pocet_panelov/kwp
            if "pocet_panelov" in (r.get("qty_formula") or ""):
                qty = math.ceil(pocet_panelov * 1.5) if "1.5" in (r["qty_formula"] or "") else pocet_panelov
            elif "kwp" in (r.get("qty_formula") or ""):
                qty = math.ceil(kwp_actual * 10) if "10" in (r["qty_formula"] or "") else math.ceil(kwp_actual * 7)
            else:
                qty = 1
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
            items.append({
                "position": pos, "category": "Optimizéry",
                "product_name": opt["name"], "qty": pocet_panelov, "unit": "ks",
                "cost_per_unit": opt["price_per_panel"] * 0.77,
                "price_per_unit": float(opt["price_per_panel"]),
                "rule_id": f"optimizer.{vendor_key}.{opt['key']}",
                "vendor_stack": vendor_key,
                "ai_note": opt.get("notes", ""),
            })
            pos += 1
            # Montáž optimizérov
            items.append({
                "position": pos, "category": "Montáž",
                "product_name": "Montáž optimizér", "qty": pocet_panelov, "unit": "ks",
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
  "panel_sku": "LONGI_430" | "LONGI_580" | "JA_440" | null,
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
