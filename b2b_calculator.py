"""
B2B Kalkulačka — engine pre generovanie BOM (line items) z 4 vstupov.

Vstupy (config dict):
  typ_strechy: vychod_zapad|trapez|skridla|falcovany_plech|plech_kombi_skrutka|juzna|zemne_skrutky|corab
  kwp: float
  has_bess: bool, bess_kwh: float
  has_wallbox: bool, wallbox_pocet: int
  has_optimizery: bool
  has_rapid_shutdown: bool
  has_vn_pripojenie: bool
  vzdialenost_rozvadzac: float (m, default 30)
  vzdialenost_doprava: float (km, default 20)
  preferred_vendor: sungrow|huawei|goodwe (default sungrow)
  panel_wp: int (default 430)
  margin_pct: float (default 25)

Výstup: dict s 'items' (list of BOM line items) a 'totals'.
"""
import math
import logging

log = logging.getLogger(__name__)

DC_AC_RATIO = 1.10  # overdimensioning faktor


def _eval_formula(formula: str, ctx: dict) -> float:
    """Bezpečný eval pre qty_formula. Povolené: arithmetic + ceil/floor + ctx vars."""
    if formula is None or formula == "":
        return 1.0
    # Sanitize
    safe_ctx = {
        "kwp": ctx.get("kwp", 0),
        "pocet_panelov": ctx.get("pocet_panelov", 0),
        "bess_kwh": ctx.get("bess_kwh", 0),
        "vzdialenost_rozvadzac": ctx.get("vzdialenost_rozvadzac", 30),
        "vzdialenost_doprava": ctx.get("vzdialenost_doprava", 20),
        "ceil": math.ceil,
        "floor": math.floor,
        "max": max,
        "min": min,
    }
    try:
        return float(eval(formula, {"__builtins__": {}}, safe_ctx))
    except Exception as e:
        log.warning(f"Formula eval failed: {formula} → {e}")
        return 0.0


def _load_rules(sb):
    """Načítaj všetky aktívne rules zo Supabase."""
    res = sb.table("b2b_calc_rules").select("*").eq("active", True).execute()
    return res.data or []


def _filter_rules(rules: list, rule_type: str, kwp: float = None, typ_strechy: str = None):
    """Vyfiltruj rules pre dané pásmo / typ strechy."""
    matched = []
    for r in rules:
        if r["rule_type"] != rule_type:
            continue
        if kwp is not None:
            min_k = r.get("min_kwp")
            max_k = r.get("max_kwp")
            if min_k is not None and kwp < min_k:
                continue
            if max_k is not None and kwp > max_k:
                continue
        if typ_strechy is not None:
            r_strecha = r.get("typ_strechy")
            if r_strecha and r_strecha != typ_strechy:
                continue
        matched.append(r)
    return matched


def _rule_to_item(rule: dict, ctx: dict, position: int, category: str) -> dict:
    """Konvertuje rule + kontext na BOM line item."""
    qty = _eval_formula(rule.get("qty_formula") or "1", ctx)
    if qty <= 0:
        return None
    return {
        "position": position,
        "category": category,
        "product_name": rule["product_name"],
        "qty": round(qty, 2),
        "unit": rule["unit"],
        "cost_per_unit": rule.get("cost_per_unit"),
        "price_per_unit": float(rule["price_per_unit"]),
        "total_price": round(qty * float(rule["price_per_unit"]), 2),
        "auto_generated": True,
        "rule_id": f"{rule['rule_type']}.{rule['rule_key']}",
        "raynet_product_id": rule.get("raynet_product_id"),
    }


def calculate_bom(sb, config: dict) -> dict:
    """Hlavná funkcia — generuje BOM."""
    rules = _load_rules(sb)
    
    # Vstupy
    kwp = float(config["kwp"])
    typ_strechy = config.get("typ_strechy", "vychod_zapad")
    panel_wp = int(config.get("panel_wp", 430 if kwp <= 100 else 580))
    has_bess = config.get("has_bess", False)
    bess_kwh = float(config.get("bess_kwh", 0) or 0)
    has_optimizery = config.get("has_optimizery", False)
    has_rapid_shutdown = config.get("has_rapid_shutdown", False)
    vzdialenost_doprava = float(config.get("vzdialenost_doprava", 20))
    vzdialenost_rozvadzac = float(config.get("vzdialenost_rozvadzac", 30))
    preferred_vendor = config.get("preferred_vendor", "sungrow")
    margin_pct = float(config.get("margin_pct", 25))
    
    # Výpočet panelov
    pocet_panelov = math.ceil(kwp * 1000 / panel_wp)
    
    ctx = {
        "kwp": kwp,
        "pocet_panelov": pocet_panelov,
        "bess_kwh": bess_kwh,
        "vzdialenost_rozvadzac": vzdialenost_rozvadzac,
        "vzdialenost_doprava": vzdialenost_doprava,
    }
    
    items = []
    pos = 1
    
    # 1. PANELY (vyber podľa kwp)
    panel_rules = _filter_rules(rules, "panel", kwp=kwp)
    if panel_rules:
        # Vyber podľa panel_wp
        target_panel = next((r for r in panel_rules if str(panel_wp) in r["product_name"]), panel_rules[0])
        # Override qty na presný počet
        ctx["pocet_panelov"] = pocet_panelov
        item = _rule_to_item(target_panel, ctx, pos, "Panely")
        if item:
            items.append(item); pos += 1
    
    # 2. MENIČE
    if preferred_vendor == "huawei" and kwp <= 15:
        # Huawei pre malé
        for key in ["huawei.sun2000_10ktl", "huawei.smartmeter_dtsu"]:
            r = next((x for x in rules if x["rule_type"] == "menic" and x["rule_key"] == key), None)
            if r:
                item = _rule_to_item(r, ctx, pos, "Striedače")
                if item: items.append(item); pos += 1
    else:
        # Sungrow — greedy fill od najväčších meničov
        required_ac_kw = kwp / DC_AC_RATIO
        zvysok = required_ac_kw
        
        # Greedy fill 125 → 110 → 50 → 33
        for menic_key, menic_kw in [("sungrow.sg125cx", 125), ("sungrow.sg50cx", 50), ("sungrow.sg33cx", 33)]:
            if zvysok <= 0:
                break
            r = next((x for x in rules if x["rule_type"] == "menic" and x["rule_key"] == menic_key), None)
            if not r:
                continue
            # Aplikuj prahy: SG125 len pre >=250 kWp, SG50 pre 30-250, SG33 pre 15-50
            min_k = r.get("min_kwp") or 0
            max_k = r.get("max_kwp") or 99999
            if kwp < min_k or kwp > max_k:
                continue
            n = math.floor(zvysok / menic_kw)
            if n > 0:
                item = {
                    "position": pos,
                    "category": "Striedače",
                    "product_name": r["product_name"],
                    "qty": n,
                    "unit": r["unit"],
                    "cost_per_unit": r.get("cost_per_unit"),
                    "price_per_unit": float(r["price_per_unit"]),
                    "total_price": round(n * float(r["price_per_unit"]), 2),
                    "auto_generated": True,
                    "rule_id": f"menic.{menic_key}",
                }
                items.append(item); pos += 1
                zvysok -= n * menic_kw
        
        # Doplň zvyšok 33kW meničom ak treba
        if zvysok > 0:
            r = next((x for x in rules if x["rule_type"] == "menic" and x["rule_key"] == "sungrow.sg33cx"), None)
            if r:
                n = max(1, math.ceil(zvysok / 33))
                item = {
                    "position": pos, "category": "Striedače",
                    "product_name": r["product_name"], "qty": n, "unit": r["unit"],
                    "cost_per_unit": r.get("cost_per_unit"),
                    "price_per_unit": float(r["price_per_unit"]),
                    "total_price": round(n * float(r["price_per_unit"]), 2),
                    "auto_generated": True, "rule_id": "menic.sungrow.sg33cx",
                }
                items.append(item); pos += 1
        
        # Gateway + smartmeter (povinné pri >30 kWp)
        if kwp > 30:
            for key in ["sungrow.com100e", "sungrow.smartmeter"]:
                r = next((x for x in rules if x["rule_type"] == "menic" and x["rule_key"] == key), None)
                if r:
                    item = _rule_to_item(r, ctx, pos, "Striedače")
                    if item: items.append(item); pos += 1
    
    # 3. KONŠTRUKCIA (podľa typu strechy)
    k_rules = _filter_rules(rules, "konstrukcia", typ_strechy=typ_strechy)
    for r in k_rules:
        item = _rule_to_item(r, ctx, pos, "Konštrukcia")
        if item: items.append(item); pos += 1
    
    # 4. ROZVÁDZAČ (podľa kWp pásma)
    r_rules = _filter_rules(rules, "rozvadzac", kwp=kwp)
    if r_rules:
        # Pick exact match
        r = r_rules[0]
        item = _rule_to_item(r, ctx, pos, "Rozvádzač")
        if item: items.append(item); pos += 1
    
    # 5. VODIČE (DC + AC)
    for key in ["dc", "ac"]:
        r = next((x for x in rules if x["rule_type"] == "vodice" and x["rule_key"] == key), None)
        if r:
            item = _rule_to_item(r, ctx, pos, "Vodiče")
            if item: items.append(item); pos += 1
    
    # 6. PROJEKTOVÁ DOKUMENTÁCIA
    pd_rules = _filter_rules(rules, "pd", kwp=kwp)
    if pd_rules:
        item = _rule_to_item(pd_rules[0], ctx, pos, "Projektová dokumentácia")
        if item: items.append(item); pos += 1
    
    # 7. SPOTREBNÝ MATERIÁL
    r = next((x for x in rules if x["rule_type"] == "spotrebny" and x["rule_key"] == "standard"), None)
    if r:
        item = _rule_to_item(r, ctx, pos, "Spotrebný material")
        if item: items.append(item); pos += 1
    
    # 8. OSTATNÉ (žľaby, chráničky)
    for key in ["zlab_kryt_50mm", "chranicka_25mm", "chranicka_40mm"]:
        r = next((x for x in rules if x["rule_type"] == "ostatne" and x["rule_key"] == key), None)
        if r:
            item = _rule_to_item(r, ctx, pos, "Ostatné")
            if item: items.append(item); pos += 1
    
    # 9. OPTIMIZÉRY (voliteľne)
    if has_optimizery:
        for key in ["tigo_ts4", "montaz_optimizer"]:
            r = next((x for x in rules if x["rule_type"] == "optimizery" and x["rule_key"] == key), None)
            if r:
                item = _rule_to_item(r, ctx, pos, "Optimizéry")
                if item: items.append(item); pos += 1
    
    # 10. RAPID SHUTDOWN (voliteľne)
    if has_rapid_shutdown:
        for key in ["bfs12", "esw12", "montaz_rs"]:
            r = next((x for x in rules if x["rule_type"] == "rapid_shutdown" and x["rule_key"] == key), None)
            if r:
                item = _rule_to_item(r, ctx, pos, "Rapid Shutdown")
                if item: items.append(item); pos += 1
    
    # 11. BESS (voliteľne)
    if has_bess and bess_kwh > 0:
        # Vyber batériu — Pylontech pre malé (<100), Huawei LUNA pre veľké
        bess_key = "huawei_luna2000_200" if bess_kwh >= 100 else "pylontech_force"
        r = next((x for x in rules if x["rule_type"] == "batteria" and x["rule_key"] == bess_key), None)
        if r:
            item = _rule_to_item(r, ctx, pos, "Batéria")
            if item: items.append(item); pos += 1
        # Montáž batérie
        r = next((x for x in rules if x["rule_type"] == "batteria" and x["rule_key"] == "montaz_baterie"), None)
        if r:
            item = _rule_to_item(r, ctx, pos, "Batéria")
            if item: items.append(item); pos += 1
    
    # 12. MONTÁŽ (podľa kWp pásma)
    m_rules = _filter_rules(rules, "montaz", kwp=kwp)
    if m_rules:
        item = _rule_to_item(m_rules[0], ctx, pos, "Montáž")
        if item: items.append(item); pos += 1
    
    # 13. DOPRAVA
    r = next((x for x in rules if x["rule_type"] == "doprava" and x["rule_key"] == "km"), None)
    if r:
        item = _rule_to_item(r, ctx, pos, "Doprava")
        if item: items.append(item); pos += 1
    
    # ===== SUMARIZÁCIA + MARŽA =====
    total_cost = sum(((it.get("cost_per_unit") or 0) * it["qty"]) for it in items)
    
    # Aplikuj maržu na price_per_unit ak je margin_pct != 0
    # Logika: price_per_unit už je *predajná* z Raynet, takže cost = price * (1 - margin)
    # Ale tu rátame: ak má rule price 100€ a margin 25%, potom predaj = 100, cost = 75
    # Marža slider mení LEN predaj — nákupka je fixed z dát.
    if margin_pct > 0:
        margin_factor = 1.0 + (margin_pct / 100.0)
        for it in items:
            # Ak cost_per_unit nie je explicitný, odvodzuj z price
            if not it.get("cost_per_unit"):
                # predpoklad: súčasná price obsahuje 30% maržu (Raynet default)
                # použijem to ako baseline
                it["cost_per_unit"] = round(it["price_per_unit"] / 1.30, 2)
            # Override price = cost * (1 + margin)
            new_price = round(float(it["cost_per_unit"]) * margin_factor, 2)
            it["price_per_unit"] = new_price
            it["total_price"] = round(new_price * it["qty"], 2)
    
    total_price = sum(it["total_price"] for it in items)
    total_cost_recalc = sum(((it.get("cost_per_unit") or 0) * it["qty"]) for it in items)
    
    return {
        "items": items,
        "totals": {
            "pocet_panelov": pocet_panelov,
            "pocet_menicov": sum(it["qty"] for it in items if it["category"] == "Striedače" and "smart" not in it["product_name"].lower() and "com" not in it["rule_id"].lower()),
            "kwp": kwp,
            "panel_wp": panel_wp,
            "total_cost": round(total_cost_recalc, 2),
            "total_price": round(total_price, 2),
            "total_margin_eur": round(total_price - total_cost_recalc, 2),
            "margin_pct_effective": round((total_price - total_cost_recalc) / total_price * 100, 2) if total_price > 0 else 0,
            "items_count": len(items),
        }
    }


def save_quote(sb, config: dict, items: list, totals: dict, customer_id: str = None,
               lead_id: str = None, project_id: str = None, user_id: str = None) -> str:
    """Uloží cenovku do b2b_quotes + items."""
    quote_data = {
        "typ_strechy": config["typ_strechy"],
        "kwp": float(config["kwp"]),
        "has_bess": config.get("has_bess", False),
        "bess_kwh": float(config.get("bess_kwh", 0)) if config.get("bess_kwh") else None,
        "has_wallbox": config.get("has_wallbox", False),
        "wallbox_pocet": int(config.get("wallbox_pocet", 0) or 0),
        "has_optimizery": config.get("has_optimizery", False),
        "has_rapid_shutdown": config.get("has_rapid_shutdown", False),
        "has_vn_pripojenie": config.get("has_vn_pripojenie", False),
        "vzdialenost_rozvadzac": float(config.get("vzdialenost_rozvadzac", 30)),
        "vzdialenost_doprava": float(config.get("vzdialenost_doprava", 20)),
        "preferred_vendor": config.get("preferred_vendor", "sungrow"),
        "panel_wp": int(config.get("panel_wp", 430)),
        "pocet_panelov": totals["pocet_panelov"],
        "pocet_menicov": int(totals["pocet_menicov"]),
        "total_excl_vat": totals["total_price"],
        "total_cost": totals["total_cost"],
        "total_margin_eur": totals["total_margin_eur"],
        "margin_pct": float(config.get("margin_pct", 25)),
        "customer_id": customer_id,
        "lead_id": lead_id,
        "project_id": project_id,
        "created_by": user_id,
    }
    res = sb.table("b2b_quotes").insert(quote_data).execute()
    quote = res.data[0]
    
    # Save items
    items_data = []
    for it in items:
        items_data.append({
            "quote_id": quote["id"],
            "position": it["position"],
            "category": it["category"],
            "product_name": it["product_name"],
            "qty": it["qty"],
            "unit": it["unit"],
            "cost_per_unit": it.get("cost_per_unit"),
            "price_per_unit": it["price_per_unit"],
            "auto_generated": it.get("auto_generated", True),
            "rule_id": it.get("rule_id"),
            "raynet_product_id": it.get("raynet_product_id"),
        })
    if items_data:
        sb.table("b2b_quote_items").insert(items_data).execute()
    
    return quote


# ============================================================
# REFACTOR — zápis do quote_bundles (B2C-zdieľaná architektúra)
# Namiesto separátnych b2b_quotes generuje 1 bundle so 4 variantmi A/B/C/D.
# ============================================================

ARCHETYPES_B2B = [
    {"key": "A", "label": "Konzervatívny", "fve_factor": 0.70, "bess_factor": 0.0,
     "description": "Iba samospotreba, žiadne BESS. Low risk."},
    {"key": "B", "label": "Optimal NPV", "fve_factor": 1.00, "bess_factor": 0.30,
     "description": "Engine-optimal NPV pri tomto profile."},
    {"key": "C", "label": "Energy Independence", "fve_factor": 1.30, "bess_factor": 0.60,
     "description": "Max samostatnosť ≥85% pre stabilný outage comfort."},
    {"key": "D", "label": "Spot arbitráž", "fve_factor": 0.70, "bess_factor": 0.80,
     "description": "Menšie FVE, väčšie BESS — arbitráž medzi nočnou a poludňajšou cenou."},
]


def build_4_variants(base_config: dict) -> list[dict]:
    """Z 1 user config (kwp, typ_strechy, ...) vygeneruj 4 archetypy-config."""
    base_kwp = float(base_config.get("kwp", 30))
    annual_kwh_estimate = base_kwp * 1000  # heuristika ~1000 kWh/kWp
    
    variants = []
    for arch in ARCHETYPES_B2B:
        v_kwp = round(base_kwp * arch["fve_factor"], 0)
        v_bess = round(annual_kwh_estimate / 365 * arch["bess_factor"], 0)  # ~30/60/80% denného priemeru
        v_bess = min(v_bess, 500)  # cap
        
        v_config = {**base_config, "kwp": v_kwp, "has_bess": v_bess > 0, "bess_kwh": v_bess}
        v_config["_archetype_key"] = arch["key"]
        v_config["_archetype_label"] = arch["label"]
        v_config["_archetype_desc"] = arch["description"]
        variants.append(v_config)
    
    return variants


def save_quote_as_bundle(sb, base_config: dict, customer_id: str = None,
                          lead_id: str = None, user_id: str = None) -> dict:
    """Generuje 4-varianty bundle do quote_bundles (workspace='b2b')."""
    # Zostav 4 varianty
    variants = build_4_variants(base_config)
    
    # Spustí BOM kalkuláciu pre každý
    bundle_data = {
        "vykon_kwp": float(base_config.get("kwp", 0)),
        "typ_ponuky": "b2b_konfigurator",
        "lead_id": lead_id,
        "customer_id": customer_id,
        "created_by": user_id,
        "status": "draft",
        "payment_terms": (base_config.get("payment_terms") if base_config.get("payment_terms") in ("60_30_10", "60_40", "50_50", "30_70") else "60_30_10"),
        "workspace": "b2b",
    }
    
    margin_pct = float(base_config.get("margin_pct", 25))
    
    for v_config in variants:
        key = v_config["_archetype_key"].lower()  # a, b, c, d
        v_config_for_calc = {**v_config, "margin_pct": margin_pct}
        
        try:
            result = calculate_bom(sb, v_config_for_calc)
        except Exception as e:
            import logging
            logging.warning(f"BOM calc failed for variant {key}: {e}")
            continue
        
        items = result.get("items") or []
        totals = result.get("totals") or {}
        
        # Mapovať items do bundle BOM JSON formátu (zhoda s B2C variant_X_bom)
        bom_array = []
        for it in items:
            bom_array.append({
                "sku": it.get("rule_id", ""),
                "name": it.get("product_name", ""),
                "category": it.get("category", ""),
                "qty": float(it.get("qty", 0)),
                "unit": it.get("unit", ""),
                "unit_purchase": float(it.get("cost_per_unit") or 0),
                "unit_sale": float(it.get("price_per_unit") or 0),
                "total_purchase": float(it.get("cost_per_unit") or 0) * float(it.get("qty") or 0),
                "total_sale": float(it.get("total_price") or 0),
            })
        
        bundle_data[f"variant_{key}_active"] = True
        bundle_data[f"variant_{key}_marza_pct"] = int(round(margin_pct))
        bundle_data[f"variant_{key}_cost"] = totals.get("total_cost", 0)
        bundle_data[f"variant_{key}_price_no_vat"] = totals.get("total_price", 0)
        bundle_data[f"variant_{key}_price_with_vat"] = round(totals.get("total_price", 0) * 1.23, 2)
        bundle_data[f"variant_{key}_bom"] = bom_array
    
    # Insert do quote_bundles (DB má auto-numbering trigger pre bundle_number)
    res = sb.table("quote_bundles").insert(bundle_data).execute()
    if not res.data:
        raise RuntimeError("Bundle insert failed")
    
    return res.data[0]
