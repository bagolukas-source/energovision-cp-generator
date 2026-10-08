"""
B2B Kalkulačka V2 — panely-driven + vendor stacks + AI compatibility

Vstupy:
  typ_strechy: vychod_zapad | trapez | skridla | falcovany_plech | juzna | zemne_skrutky | corab
  pocet_panelov: int  (HLAVNÝ vstup — primary); bez neho sa panely odvodia z kwp
  kwp: float (voliteľné; použije sa len ak nie je pocet_panelov)
  panel_sku: str (default "LONGI535")
  vendor_stack: 'sungrow' | 'huawei' | 'goodwe' | 'solinteg'
  has_bess: bool, bess_kwh: float, bess_count: int, bess_sku/bess_key: str, bess_class: residential|industrial
  has_wallbox: bool, wallbox_pocet: int
  has_optimizery: bool
  has_rapid_shutdown: bool
  has_janitza: bool (default true; Janitza sa ponúka len pri Huawei nad prahom), janitza_key: str
  has_dc_rozvadzac: bool (default true)
  distribucka: 'ZSD' | 'SSD' | 'VSD' | None  (dispečing pri Σ AC >= 100 kW; None → ZSD + varovanie)
  vzdialenost_doprava: float (km; 0/chýba = bez riadku dopravy + varovanie)
  margin_pct: float (marža Z PREDAJA v %, default 22; platí 0 <= m < 100)

Režim LEN BESS (Fáza 1, 2026-10; F1-SPEC "Jadro BESS"): pocet_panelov 0, bez kwp, has_bess + kWh/počet/model.
  bess_kwh: cieľová kapacita (model + počet skríň vyberie jadro: kombinácia jedného modelu, ceil, odchýlka > 5 % = varovanie)
  bess_kw: cieľový AC výkon batérie/PCS v kW (voliteľný) — základ pre AC rozvádzač, PD, dispečing;
           bez neho výkon zo stacku (max_power_kw x ks), inak kapacita / 2 + varovanie
  has_ems: bool (default: len BESS od 100 kWh zapnuté; pri FVE+BESS vypnuté), ems_typ: 'compact' (default) | 'full'
  bess_kabel_m: dĺžka AC kabeláže batéria ↔ rozvádzač v m (default 30 + varovanie)
  Skladba: batéria, montáž za skriňu, AC rozvádzač (pásmo podľa kW), kabeláž (kabelaz_bess), PD (max(kWp, kW), min PD50),
  EMS, statika + PBS (>= 100 kWh), dispečing (AC >= 100 kW podľa distribučky), doprava. Bez panelov, meničov FVE,
  konštrukcie, vodičov DC, rozvádzača DC a montáže FVE. totals.ac_kw_total = výkon batérie/PCS.

Cenotvorba (Fáza 0, 2026-10): predaj = nákup / (1 - m/100) jednotne pre VŠETKY položky.
Nákup sa berie z DB pravidiel (cost_per_unit) a zo stacku (cost); ak chýba, odhad predaj x 0,77
a varovanie `cost_estimated`.

Output: BOM JSON — items[], jediný zoznam warnings[] ({severity: info|warning|error, kind, message}),
totals{}. Chyba vstupu (marža mimo rozsahu, neznámy výrobca, 0 panelov bez batérie) → ok:false.
"""
import math
import logging
import re as _re
from typing import Optional

log = logging.getLogger(__name__)

DC_AC_RATIO = 1.10
# ASDR sa vyžaduje pri súčte meničov >= 100 kW AC (cena ~30k). Do MAX_KWP_NO_ASDR
# uprednostníme zostavu meničov < 100 kW (vyhne sa ASDR).
MAX_KWP_NO_ASDR = 130.0      # hranica, do ktorej sa snažíme udržať AC < 100 kW
MAX_OVERSIZE = 1.50          # max DC/AC oversizing (panely vs menič)
TARGET_OVERSIZE = 1.15       # ideálny DC/AC pomer
MAX_INVERTER_UNITS = 3       # max počet meničov v zostave (nestackovať mikro-meniče)
OVERSIZE_BAND = (0.85, 1.35)  # zdravé pásmo DC/AC; mimo neho penalizuj (1.35 = bežný komerčný oversizing)

# --- Fáza 0 (2026-10): jednotná cenotvorba a pravidlá ponuky (F0-SPEC) ---
DEFAULT_MARGIN_PCT = 22.0     # marža Z PREDAJA v %; predaj = nákup / (1 - m/100)
COST_FALLBACK_FACTOR = 0.77   # odhad nákupu z predajnej ceny, ak chýba cost (+ varovanie cost_estimated)
ASDR_MIN_AC_KW = 100.0        # Σ AC >= 100 kW → dispečerské riadenie podľa distribučky
MTP_MIN_AC_KW = 30.0          # Σ AC > 30 kW → 3 ks MTP
STATIKA_MIN_KWP = 100.0       # kWp >= 100 → statický posudok + projekt požiarnej bezpečnosti
TIGO_CCA_PER_SET = 150        # 1 sada CCA Kit + TAP na 150 optimizérov
JANITZA_DEFAULT_ABOVE_KW = 30.0  # Janitza (len Huawei) nad týmto výkonom, ak stack nemá offer_above_kw
BESS_KWH_TOLERANCE = 0.05     # odchýlka efektívnej kapacity od cieľa, nad ktorú varujeme
SCOPE_MAX_KWP = 500.0         # nad týmto výkonom je ponuka mimo bežného rozsahu kalkulačky (len varovanie)
SCOPE_MAX_BESS_KWH = 250.0    # C&I batéria nad touto kapacitou (alebo Huawei LUNA C&I) je mimo bežného rozsahu
GROUND_ROOFS = ("zemne_skrutky", "zemna_ramming")
DISTRIBUCKY = ("ZSD", "SSD", "VSD")

# --- Fáza 1 (2026-10): plný režim len BESS (F1-SPEC) ---
BESS_ONLY_MIN_PD_KW = 50.0    # PD v režime len BESS: pásmo podľa max(kWp, kW), najmenej PD50 (Raynet R5)
BESS_EMS_DEFAULT_KWH = 100.0  # len BESS od tejto kapacity je EMS predvolene zapnutý (has_ems ho prebíja)
BESS_STATIKA_MIN_KWH = 100.0  # len BESS od tejto kapacity: statický posudok + projekt požiarnej bezpečnosti
BESS_KABEL_DEFAULT_M = 30.0   # predvolená dĺžka AC kabeláže, ak bess_kabel_m nie je zadané (+ varovanie)
BESS_KW_PER_KWH = 0.5         # odhad výkonu (0,5C = kWh / 2), ak chýba bess_kw aj výkon batérie v dátach
BESS_KW_TOLERANCE = 0.10      # požadovaný bess_kw môže prevyšovať výkon batérií v dátach najviac o toľko bez varovania
BESS_INDUSTRIAL_FROM_KWH = 60.0  # bez zvolenej triedy (bess_class) sa od tejto cieľovej kapacity vyberá z priemyselných skríň
BESS_MANY_UNITS = 12          # výber podľa kWh, ktorý vyjde na viac kusov, je podozrivý (varovanie bess_many_units)


def _num(v, default: float = 0.0) -> float:
    """Bezpečné číslo: None/''/nečíselné/NaN → default."""
    try:
        if v is None or v == "":
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _flag(v, default: bool = True) -> bool:
    """Bezpečný boolean: None/'' → default; 'false'/'0'/'no'/'nie' → False."""
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return default
        return s not in ("0", "false", "no", "nie", "off")
    return bool(v)


def _warn(severity: str, kind: str, message: str, **extra) -> dict:
    w = {"severity": severity, "kind": kind, "message": message}
    w.update(extra)
    return w


def _error_result(kind: str, message: str) -> dict:
    """Neplatný vstup → ok:false (UI zablokuje Uložiť/Tlač); chyba je aj v jedinom poli warnings."""
    return {"ok": False, "error": message, "warnings": [_warn("error", kind, message)]}


def _load_vendor_stack(sb, vendor_key: str) -> Optional[dict]:
    # .limit(1) namiesto .single(): neznámy výrobca nesmie hodiť výnimku (→ ok:false v calculate_bom_v2)
    res = sb.table("b2b_vendor_stacks").select("*").eq("vendor_key", vendor_key).limit(1).execute()
    rows = res.data or []
    return rows[0] if rows else None


def _sort_rules(rows: list[dict]) -> list[dict]:
    """Stabilné zoradenie pravidiel: priority, potom min_kwp (rovnako ako ORDER BY v DB)."""
    return sorted(rows or [], key=lambda r: (_num(r.get("priority"), 100.0), _num(r.get("min_kwp"), 0.0)))


def _load_konstrukcia_rule(sb, typ_strechy: str) -> list[dict]:
    """Vráti AKTÍVNE pravidlá konštrukcie pre typ strechy z b2b_calc_rules (podľa priority, min_kwp)."""
    res = (sb.table("b2b_calc_rules").select("*")
             .eq("rule_type", "konstrukcia").eq("typ_strechy", typ_strechy)
             .eq("active", True).order("priority").execute())
    return _sort_rules(res.data or [])


def _load_rule(sb, rule_type: str, rule_key: str = None) -> list[dict]:
    """Vráti AKTÍVNE pravidlá daného typu (podľa priority, min_kwp)."""
    q = sb.table("b2b_calc_rules").select("*").eq("rule_type", rule_type).eq("active", True)
    if rule_key:
        q = q.eq("rule_key", rule_key)
    res = q.order("priority").execute()
    return _sort_rules(res.data or [])


def _eval_qty_formula(formula, kwp, pocet_panelov, exact: bool = False):
    """Bezpečne vyhodnotí qty_formula z b2b_calc_rules (napr. 'ceil(kwp * 1.5)', 'kwp * 10', 'pocet_panelov').
    Rešpektuje koeficienty z DB (predtým ich kód ignoroval → žľab dostal kWp×7 namiesto ×1.5).
    exact=False: celé číslo nahor (ks); exact=True: presná hodnota na 2 desatinné (jednotka kWp)."""
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
        val = float(eval(f, {"__builtins__": {}}, env))
        if exact:
            return max(0.0, round(val, 2))
        return max(1, int(math.ceil(val)))
    except Exception:
        return 1


def _rule_qty(r: dict, kwp, pocet_panelov, default=1):
    """Množstvo z pravidla: qty_formula z DB (jednotka kWp → presne, inak celé nahor); bez vzorca → default."""
    formula = r.get("qty_formula")
    if not formula or not str(formula).strip():
        return default
    exact = str(r.get("unit") or "").strip().lower() == "kwp"
    return _eval_qty_formula(formula, kwp, pocet_panelov, exact=exact)


def _pick_band(rules: list[dict], value: float) -> Optional[dict]:
    """Prvé pravidlo (v poradí priority, min_kwp), ktorého pásmo [min_kwp, max_kwp] obsahuje value.
    Dolná hranica má toleranciu 0,011 (pásma 10 | 10,01 nemajú medzeru pri neceločíselnom value)."""
    for r in rules:
        lo = _num(r.get("min_kwp"), 0.0)
        hi = r.get("max_kwp")
        hi = 99999.0 if hi is None else _num(hi, 99999.0)
        if lo - 0.011 <= value <= hi:
            return r
    return None


def _select_band(rules: list[dict], value: float):
    """(pravidlo, násobok, nad_rozsahom). Nad najvyšším pásmom vráti najvyššie pásmo a násobok ceil(value / max_kwp).
    Bez pravidiel alebo bez zhody vo vnútri rozsahu vráti (None, 1, False)."""
    if not rules:
        return None, 1, False
    band = _pick_band(rules, value)
    if band:
        return band, 1, False
    top = max(rules, key=lambda r: _num(r.get("max_kwp"), 0.0))
    top_max = _num(top.get("max_kwp"), 0.0)
    if top_max > 0 and value > top_max:
        return top, max(1, math.ceil(value / top_max)), True
    return None, 1, False


def _pick_inverters(inverters: list[dict], required_ac_kw: float, require_hybrid: bool = False) -> list[dict]:
    """Vyber kombináciu meničov (1–MAX_INVERTER_UNITS kusov), ktorá uvezie panely (kwp_actual).
    Pravidlá:
      • celkový oversizing kwp/AC v okne 0.6–MAX_OVERSIZE, cieľ ~TARGET_OVERSIZE
      • žiadny menič preťažený (DC podiel podľa AC ≤ jeho max_kwp)
      • celková kapacita meničov (Σ max_kwp) musí pokryť panely
      • do MAX_KWP_NO_ASDR uprednostni súčet AC < 100 kW (vyhne sa ASDR ~30k)
      • inak: najmenej kusov → oversizing najbližšie k cieľu → najlacnejšie
    Ak žiadna platná kombinácia (extra veľký systém) → n × najväčší menič; výsledok nesie "fallback": True
    (calculate_bom_v2 z toho urobí varovanie inverter_fallback)."""
    import itertools
    kwp = required_ac_kw * DC_AC_RATIO
    invs_all = [i for i in inverters if (i.get("ac_kw") or 0) > 0]
    # Staršie modely (legacy: true) neponúkať automaticky, ak existuje aktuálny model (Raynet 10/2026: MC0, M2HT, V21)
    invs = [i for i in invs_all if not i.get("legacy")] or invs_all
    if not invs:
        return []
    # BESS → len hybridné (battery-ready). Bez BESS → len stringové (lacnejšie, neplytvať hybridom).
    if require_hybrid:
        pool = [i for i in invs if i.get("hybrid")]
    else:
        pool = [i for i in invs if not i.get("hybrid")]
    invs = pool or invs  # fallback ak by jedna skupina chýbala (napr. vendor bez hybridu)
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
            # Kapacita: panely sa medzi meniče rozdelia voľne (nie proporcionálne podľa AC),
            # takže combo uvezie kwp, ak Σmax_kwp >= kwp (overené vyššie). Žiadny ďalší
            # per-menič proporcionálny limit — inak by sme zbytočne zamietli napr. 50+40,
            # kde 50KTL dostane viac panelov (do svojho max) a 40KTL menej.
            cands.append((combo, ac, r, ov, sum(float(i.get("price") or 0) for i in combo)))
    if not cands:
        big = max(invs, key=lambda x: x["ac_kw"])
        n = max(1, math.ceil(kwp / (big.get("max_kwp") or big["ac_kw"])))
        return [{"inverter": big, "qty": n, "fallback": True}]

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


def _is_luna_ci(battery: dict) -> bool:
    """Huawei LUNA2000-200/241 (C&I) — samostatný PCS, Raynet ho doteraz nepridával."""
    key = battery.get("key") or ""
    return battery.get("battery_class") == "industrial" and ("luna2000_200" in key or "luna2000_241" in key)


def _kwh_deviation(eff_kwh: float, target_kwh: float) -> float:
    """Relatívna odchýlka efektívnej kapacity od cieľa (+ = viac než cieľ)."""
    return (eff_kwh - target_kwh) / target_kwh if target_kwh > 0 else 0.0


def _kwh_off_target(eff_kwh: float, target_kwh: float) -> bool:
    """Odchýlka kapacity od cieľa nad toleranciou BESS_KWH_TOLERANCE (5 %)."""
    return abs(_kwh_deviation(eff_kwh, target_kwh)) > BESS_KWH_TOLERANCE + 1e-9


def _best_bess_for_kwh(batteries: list[dict], target_kwh: float) -> dict:
    """Model a počet skríň pre cieľ v kWh. Kombinácia je vždy z JEDNÉHO modelu: qty = ceil(cieľ / kapacita).
    Z modelov v tolerancii (BESS_KWH_TOLERANCE) vyberie ten s najmenším počtom kusov, potom najlacnejší;
    ak žiadny nie je v tolerancii, ten s najmenšou odchýlkou od cieľa (potom menej kusov, potom lacnejší).
    Modely, pri ktorých by qty prekročilo max_units, sa preskočia (ak by nezostal žiadny, rozhodujú všetky
    a limit orežú calculate_bom_v2 s varovaním bess_limit)."""
    cands = []
    for b in batteries:
        cap = _num(b.get("capacity_kwh"), 0.0)
        if cap <= 0:
            continue
        # round(…, 6): 20,48 / 10,24 nesmie dať 2,0000000000000004 → 3 ks
        qty = max(1, math.ceil(round(target_kwh / cap, 6)))
        cands.append({"battery": b, "qty": qty, "dev": abs(_kwh_deviation(cap * qty, target_kwh)),
                      "cost": _num(b.get("cost"), _num(b.get("price"), 0.0)) * qty})
    if not cands:
        return {"battery": batteries[0], "qty": 1}
    in_limit = [c for c in cands
                if not (c["battery"].get("max_units") and c["qty"] > int(_num(c["battery"]["max_units"], 0)))]
    pool = in_limit or cands
    in_tol = [c for c in pool if c["dev"] <= BESS_KWH_TOLERANCE + 1e-9]
    if in_tol:
        best = min(in_tol, key=lambda c: (c["qty"], c["cost"], c["dev"]))
    else:
        best = min(pool, key=lambda c: (c["dev"], c["qty"], c["cost"]))
    return {"battery": best["battery"], "qty": best["qty"]}


def _pick_bess(batteries: list[dict], target_kwh: float, count: int = 0) -> list[dict]:
    """Vyber batérie. Ak count>0 → počet KUSOV modulov/skríň (zadáva user priamo; primárny modulárny modul,
    inak prvý model — výber bez cieľa kWh hlási calculate_bom_v2 ako bess_model_missing).
    Inak cieľová kapacita v kWh: kombinácia skríň jedného modelu (viď _best_bess_for_kwh), qty = ceil(cieľ / kapacita)
    pre modulárne AJ nemodulárne batérie (odchýlku kapacity od cieľa hlási calculate_bom_v2)."""
    if not batteries:
        return []
    # Režim POČET KUSOV — vyber primárny modulárny modul a vynásob počtom
    if count and int(count) > 0:
        modular = [b for b in batteries if b.get("modular")]
        base = min(modular, key=lambda x: x["capacity_kwh"]) if modular else batteries[0]
        return [{"battery": base, "qty": int(count)}]
    # Režim KAPACITA kWh
    if target_kwh <= 0:
        return []
    return [_best_bess_for_kwh(batteries, target_kwh)]


def calculate_bom_v2(sb, config: dict) -> dict:
    """Hlavná V2 funkcia — panely-driven + vendor stack aware.

    Cenotvorba: predaj = nákup / (1 - m/100) pre všetky položky (bez výnimiek). Neplatný vstup → ok:false;
    všetko ostatné sú varovania v jedinom zozname `warnings`."""
    config = config or {}

    # ===== VSTUPY =====
    typ_strechy = config.get("typ_strechy", "vychod_zapad")
    vendor_key = str(config.get("vendor_stack") or "sungrow").strip().lower()
    panel_sku = str(config.get("panel_sku") or "LONGI535").strip()
    pocet_panelov_input = int(_num(config.get("pocet_panelov"), 0))
    kwp_input = _num(config.get("kwp"), 0.0)

    has_bess = _flag(config.get("has_bess"), False)
    bess_kwh = _num(config.get("bess_kwh"), 0.0)
    bess_count = int(_num(config.get("bess_count"), 0))
    bess_sku = str(config.get("bess_sku") or config.get("bess_key") or "").strip()
    has_wallbox = _flag(config.get("has_wallbox"), False)
    wallbox_pocet = int(_num(config.get("wallbox_pocet"), 0))
    has_optimizery = _flag(config.get("has_optimizery"), False)
    has_rapid_shutdown = _flag(config.get("has_rapid_shutdown"), False)
    vzdialenost_doprava = _num(config.get("vzdialenost_doprava"), 0.0)
    bess_kw_input = _num(config.get("bess_kw"), 0.0)        # cieľový AC výkon batérie/PCS (režim len BESS)
    bess_kabel_m = _num(config.get("bess_kabel_m"), 0.0)    # dĺžka AC kabeláže batéria ↔ rozvádzač (režim len BESS)

    # Marža Z PREDAJA: predaj = nákup / (1 - m/100). Chýba → 22 %. Mimo 0 <= m < 100 → chyba.
    raw_margin = config.get("margin_pct")
    if raw_margin is None or (isinstance(raw_margin, str) and not raw_margin.strip()):
        margin_pct = DEFAULT_MARGIN_PCT
    else:
        try:
            margin_pct = float(str(raw_margin).replace(",", ".")) if isinstance(raw_margin, str) else float(raw_margin)
        except (TypeError, ValueError):
            return _error_result("margin_invalid", f"Marža '{raw_margin}' nie je číslo.")
    if not math.isfinite(margin_pct) or margin_pct < 0 or margin_pct >= 100:
        return _error_result("margin_range",
                             f"Marža {margin_pct:g} % je mimo rozsahu — povolené je 0 ≤ marža < 100 % (z predaja).")

    distribucka = str(config.get("distribucka") or "").strip().upper()
    if distribucka not in DISTRIBUCKY:
        distribucka = ""

    bess_requested = has_bess and (bess_kwh > 0 or bess_count > 0 or bool(bess_sku))

    # Načítaj vendor stack
    stack = _load_vendor_stack(sb, vendor_key)
    if not stack:
        return _error_result("unknown_vendor", f"Neznámy výrobca '{vendor_key}' — vendor stack neexistuje.")

    items: list[dict] = []
    warnings: list[dict] = []
    estimated: list[str] = []   # položky s odhadnutým nákupom (predaj × 0,77)

    def warn(severity, kind, message, **extra):
        warnings.append(_warn(severity, kind, message, **extra))

    def add(category, name, qty, unit, cost, price_hint, rule_id, **extra):
        """Pridá riadok BOM. cost=None → odhad price_hint × 0,77 + zápis do `estimated`.
        price_per_unit a total_* sa dopočítajú nižšie jednotnou funkciou marže."""
        if cost is None:
            cost = round(_num(price_hint, 0.0) * COST_FALLBACK_FACTOR, 4)
            if name not in estimated:
                estimated.append(name)
        item = {"position": len(items) + 1, "category": category, "product_name": name,
                "qty": qty, "unit": unit, "cost_per_unit": float(cost), "rule_id": rule_id}
        item.update(extra)
        items.append(item)
        return item

    _rule_cache: dict[str, list[dict]] = {}

    def rules(rule_type, rule_key=None):
        """Aktívne pravidlá typu (1 dopyt na typ, potom filter v pamäti)."""
        if rule_type not in _rule_cache:
            _rule_cache[rule_type] = _load_rule(sb, rule_type)
        rows = _rule_cache[rule_type]
        return rows if rule_key is None else [r for r in rows if r.get("rule_key") == rule_key]

    def rule(rule_type, rule_key):
        found = rules(rule_type, rule_key)
        return found[0] if found else None

    def missing_rule(rule_type, rule_key, what):
        warn("warning", "missing_rule",
             f"V cenníku chýba pravidlo '{what}' ({rule_type}/{rule_key}) — položka nie je v ponuke, doplň ju ručne.")

    def add_from_rule(category, r, qty, rule_id, **extra):
        return add(category, r["product_name"], qty, r.get("unit") or "ks",
                   r.get("cost_per_unit"), r.get("price_per_unit"), rule_id, **extra)

    # ===== VSTUPNÝ REŽIM: panely / len BESS / chyba =====
    panel = None
    pocet_panelov = 0
    bess_only = False
    if pocet_panelov_input > 0 or kwp_input > 0:
        # Vyber panel z vendor stack
        panels = stack.get("preferred_panels") or []
        panel = next((p for p in panels if p.get("sku") == panel_sku), None)
        if not panel:
            warn("warning", "panel_unknown",
                 f"Panel '{panel_sku}' nie je v katalógu výrobcu — použitý predvolený panel; over počet panelov a kWp.")
            panel = panels[0] if panels else {"sku": "LONGI535", "name": "LONGi Hi-MO X10 EcoLife LR7-60HVH-535M 535 Wp", "wp": 535, "price_per_unit": 90.69, "cost": 72.55}
        if pocet_panelov_input > 0:
            pocet_panelov = pocet_panelov_input
        else:
            # round(…, 6): 59,92 kWp / 535 Wp nesmie vyjsť 112,0000000001 → 113 panelov
            pocet_panelov = math.ceil(round(kwp_input * 1000 / float(panel["wp"]), 6))
    elif bess_requested:
        bess_only = True   # 0 panelov, bez kWp, má batériu → zjednodušená vetva "len BESS"
    else:
        return _error_result("no_panels",
                             "Nulový počet panelov (ani kWp) a žiadna batéria — nie je čo kalkulovať. "
                             "Zadaj počet panelov alebo zapni batériu s kWh/počtom.")

    kwp_actual = round(pocet_panelov * float(panel["wp"]) / 1000, 2) if panel else 0.0

    # ===== SPOLOČNÉ BLOKY (FVE aj len BESS): rozvádzač AC, PD, statika + PBS, dispečing, EMS =====
    def build_rozvadzac_ac(ac_value: float):
        """Rozvádzač AC — pásmo podľa Σ AC kW (nad najvyšším pásmom: ceil(AC / max) ks najvyššieho + varovanie)."""
        band, mult, over = _select_band(rules("rozvadzac"), ac_value)
        if not band:
            missing_rule("rozvadzac", f"pásmo {ac_value:g} kW AC", "Rozvádzač AC")
            return
        add_from_rule("Rozvádzač", band, mult, f"rozvadzac.{band['rule_key']}")
        if over:
            warn("warning", "rozvadzac_over_range",
                 f"Σ AC {ac_value:g} kW je nad najvyšším pásmom rozvádzača ({_num(band.get('max_kwp')):g} kW) — "
                 f"účtovaných {mult} ks najvyššieho pásma, individuálne preveriť.")

    def build_pd(ac_value: float):
        """Projektová dokumentácia — pásmo podľa Σ AC kW (režim len BESS: podľa max(kW, 50) = najmenej PD50)."""
        band, mult, over = _select_band(rules("pd"), ac_value)
        if not band:
            missing_rule("pd", f"pásmo {ac_value:g} kW AC", "Projektová dokumentácia")
            return
        add_from_rule("Projektová dokumentácia", band, 1, f"pd.{band['rule_key']}")
        if over:
            warn("warning", "pd_over_range",
                 f"Σ AC {ac_value:g} kW je nad najvyšším pásmom PD ({_num(band.get('max_kwp')):g} kW) — "
                 f"účtované najvyššie pásmo, individuálne preveriť.")

    def build_statika_pbs():
        """Statický posudok + projekt požiarnej bezpečnosti (FVE od 100 kWp, len BESS od 100 kWh)."""
        for rk, label in (("statika", "Statický posudok"), ("ppbs", "Projekt požiarnej bezpečnosti")):
            r = rule("statika", rk)
            if r:
                add_from_rule("Statika a PBS", r, _rule_qty(r, kwp_actual, pocet_panelov, default=1), rk)
            else:
                missing_rule("statika", rk, label)

    def build_dispecing(ac_value: float):
        """Dispečerské riadenie podľa distribučky pri Σ AC >= 100 kW (bez distribučky: ZSD + varovanie)."""
        if ac_value < ASDR_MIN_AC_KW:
            return
        dkey = distribucka or "ZSD"
        if not distribucka:
            warn("warning", "distribucka",
                 "Zvoľ distribučku (ZSD/SSD/VSD) — dispečerské riadenie je počítané pre ZSD.")
        r = rule("dispecing", dkey)
        if r:
            add_from_rule("Dispečerské riadenie", r, _rule_qty(r, kwp_actual, pocet_panelov, default=1),
                          f"dispecing.{dkey}")
        else:
            missing_rule("dispecing", dkey, f"Dispečerské riadenie {dkey}")

    def build_ems(default_on: bool):
        """EMS (riadenie spotreby, EnergoStation): has_ems prebíja default_on; typ compact (predvolený) | full."""
        if not _flag(config.get("has_ems"), default_on):
            return
        typ = "full" if str(config.get("ems_typ") or "").strip().lower() == "full" else "compact"
        r = rule("ems", typ)
        if r:
            add_from_rule("EMS", r, _rule_qty(r, kwp_actual, pocet_panelov, default=1),
                          "ems" if typ == "compact" else f"ems.{typ}")
        else:
            missing_rule("ems", typ, "EMS (riadenie spotreby energie)")

    # ===== BATÉRIA (vendor-specific + trieda rez/priemysel + limity) =====
    def build_battery():
        """Pridá batériu + montáž. Vráti (vybrané_batérie, efektívna kapacita kWh)."""
        batteries = list(stack.get("batteries") or [])
        # Filter podľa triedy (residential / industrial) — toggle z UI
        bess_class = str(config.get("bess_class") or "").strip().lower()
        if bess_class in ("residential", "industrial"):
            _cls = [b for b in batteries if (b.get("battery_class") or "residential") == bess_class]
            batteries = _cls or batteries
        # Explicitne zvolený model (key) z UI
        sku_ok = False
        if bess_sku:
            _chosen = [b for b in batteries if b.get("key") == bess_sku]
            if _chosen:
                batteries = _chosen
                sku_ok = True
            else:
                warn("warning", "bess_sku_unknown",
                     f"Model batérie '{bess_sku}' nie je v katalógu výrobcu — vybraný podľa kapacity/počtu.")
        eff_count = bess_count
        if bess_sku and eff_count <= 0 and bess_kwh <= 0:
            eff_count = 1       # zvolený model bez počtu/kWh → default 1 ks
        elif not sku_ok and bess_kwh > 0:
            # Cieľ kWh bez (platného) modelu: model aj počet skríň vyberie jadro podľa cieľa. Počet z UI (predvolené
            # 1–2 ks) by inak cieľ potichu prebil a vybral by sa model "podľa poradia v stacku" (N-19).
            eff_count = 0
        kwh_mode = eff_count <= 0 and bess_kwh > 0
        if kwh_mode and not sku_ok and bess_class not in ("residential", "industrial") and bess_kwh >= BESS_INDUSTRIAL_FROM_KWH:
            # trieda nezvolená a cieľ na úrovni C&I skríň → drobné rezidenčné moduly (desiatky kusov) sú zlá skladba
            batteries = [b for b in batteries if (b.get("battery_class") or "residential") == "industrial"] or batteries
        picked = _pick_bess(batteries, bess_kwh, eff_count)
        if not picked:
            warn("warning", "bess_unavailable", "Výrobca nemá v katalógu vhodnú batériu — batéria nie je v ponuke.")
            return [], 0.0
        # N-19: počet kusov bez modelu aj bez cieľa kWh → model je len "prvý v poradí", user ho má vybrať
        if not bess_sku and not kwh_mode and len(batteries) > 1:
            warn("warning", "bess_model_missing",
                 f"Nie je zvolený model batérie ani cieľová kapacita (kWh) — použitý {picked[0]['battery'].get('name')}; "
                 f"vyber model batérie alebo zadaj kapacitu v kWh.")
        # Limit ks na menič (napr. Solinteg max 2)
        for pb in picked:
            _mx = pb["battery"].get("max_units")
            if _mx and pb["qty"] > int(_mx):
                warn("warning", "bess_limit",
                     f"{pb['battery']['name']}: max {int(_mx)} ks na menič — znížené z {pb['qty']} na {int(_mx)}.")
                pb["qty"] = int(_mx)
        if kwh_mode:
            for pb in picked:
                if pb["qty"] > BESS_MANY_UNITS:
                    warn("warning", "bess_many_units",
                         f"Pre cieľ {bess_kwh:g} kWh vychádza {pb['qty']}× {pb['battery'].get('name')} — veľa kusov; "
                         f"zváž priemyselnú triedu batérie (skrine) alebo iný model.")
        # efektívna kapacita (kWh) z reálne vybraných modulov/skríň
        eff_kwh = round(sum(_num(p["battery"].get("capacity_kwh")) * p["qty"] for p in picked), 2)
        if kwh_mode and bess_kwh > 0 and _kwh_off_target(eff_kwh, bess_kwh):
            warn("warning", "bess_kwh_deviation",
                 f"Požadovaná kapacita {bess_kwh:g} kWh, ponúkaná {eff_kwh:g} kWh "
                 f"({_kwh_deviation(eff_kwh, bess_kwh) * 100:+.1f} %) — over výber skríň/modulov.")
        for b in picked:
            _bat = b["battery"]
            add("Batéria", _bat["name"], b["qty"], "ks", _bat.get("cost"), _bat.get("price"),
                f"battery.{_bat['key']}", vendor_stack=vendor_key)
            # C&I batéria, ktorá by mohla potrebovať samostatný PCS (Huawei LUNA-200/241)
            if _is_luna_ci(_bat):
                warn("info", "pcs_required",
                     f"{_bat['name']}: samostatný PCS nie je v ponuke — Raynet PCS doteraz nepridával — over u dodávateľa.")
        # Montáž batérie: industrial = za skriňu (× počet kusov), residential = za systém (× 1)
        ind_qty = sum(p["qty"] for p in picked if (p["battery"].get("battery_class") or "residential") == "industrial")
        has_res = any((p["battery"].get("battery_class") or "residential") != "industrial" for p in picked)
        if ind_qty > 0:
            r = rule("batteria", "montaz_baterie")
            if r:
                add_from_rule("Batéria", r, ind_qty, "battery.montaz")
            else:
                warn("warning", "missing_rule",
                     "V cenníku chýba pravidlo 'Montáž batériového úložiska' (batteria/montaz_baterie) — "
                     "použitá záložná cena 2 000 € predaj / 1 750 € nákup za skriňu.")
                add("Batéria", "Montáž batériového úložiska", ind_qty, "ks", 1750.0, 2000.0, "battery.montaz")
        if has_res:
            rid = "battery.montaz" if ind_qty == 0 else "battery.montaz_rez"
            r = rule("batteria", "montaz_baterie_rez")
            if r:
                add_from_rule("Batéria", r, 1, rid)
            else:
                warn("warning", "missing_rule",
                     "V cenníku chýba pravidlo 'Montáž batérie (rezidenčná sada)' (batteria/montaz_baterie_rez) — "
                     "použitá záložná cena 500 € predaj / 300 € nákup.")
                add("Batéria", "Montáž batérie (rezidenčná sada)", 1, "kpl", 300.0, 500.0, rid)
        return picked, eff_kwh

    # ===== LEN BESS: výkon batérie a AC kabeláž =====
    def bess_ac_kw(picked: list[dict], eff_kwh: float) -> float:
        """AC výkon batérie/PCS v kW: bess_kw zo vstupu → inak Σ max_power_kw × ks zo stacku → inak kWh / 2 + varovanie."""
        powers = [_num(p["battery"].get("max_power_kw"), 0.0) for p in picked]
        stack_kw = round(sum(pw * p["qty"] for pw, p in zip(powers, picked)), 2) if powers and all(pw > 0 for pw in powers) else 0.0
        if bess_kw_input > 0:
            if stack_kw > 0 and bess_kw_input > stack_kw * (1 + BESS_KW_TOLERANCE):
                warn("warning", "bess_kw_below",
                     f"Požadovaný výkon {bess_kw_input:g} kW je vyšší než výkon vybraných batérií {stack_kw:g} kW — "
                     f"over PCS/menič (v režime len BESS ich kalkulačka nepridáva).")
            return round(bess_kw_input, 2)
        if stack_kw > 0:
            return stack_kw
        est = round(eff_kwh * BESS_KW_PER_KWH, 2)
        warn("warning", "bess_kw_odhad",
             f"Výkon batérie nie je zadaný (bess_kw) ani uvedený v dátach výrobcu — odhad {est:g} kW (kapacita / 2); "
             f"AC rozvádzač, PD a dispečing sú počítané z neho. Zadaj výkon batérie.")
        return est

    def build_kabelaz_bess(ac_value: float):
        """AC kabeláž batéria ↔ rozvádzač: m z bess_kabel_m (default 30 m + varovanie), cena z pravidla kabelaz_bess
        (AYKY-J; ak DB definuje pásma podľa AC kW, vyberie sa pásmo, inak platí jediné pravidlo)."""
        meters = bess_kabel_m
        if meters <= 0:
            meters = BESS_KABEL_DEFAULT_M
            warn("warning", "bess_kabel_default",
                 f"Dĺžka AC kabeláže batérie (bess_kabel_m) nie je zadaná — použitých {meters:g} m. "
                 f"Zadaj vzdialenosť batérie od rozvádzača.")
        meters = int(meters) if float(meters).is_integer() else round(meters, 2)
        krules = rules("kabelaz_bess")
        r = _pick_band(krules, ac_value) or (krules[-1] if krules else None)
        if r:
            add_from_rule("Vodiče", r, meters, "kabelaz_bess")
        else:
            missing_rule("kabelaz_bess", "ayky_3x150_70", "Kabeláž AC batérie (AYKY-J)")

    picked_inv: list[dict] = []
    picked_batt: list[dict] = []
    ac_kw_total = 0.0
    bess_kwh_effective = 0.0

    if bess_only:
        # ===== LEN BESS: batéria + montáž, AC rozvádzač, kabeláž, PD, EMS, statika + PBS, dispečing (bez panelov,
        # meničov FVE, konštrukcie, vodičov DC, rozvádzača DC a montáže FVE); doprava nižšie =====
        picked_batt, bess_kwh_effective = build_battery()
        if not picked_batt:
            return _error_result("no_battery", "Výrobca nemá v katalógu vhodnú batériu — nie je čo kalkulovať.")
        ac_kw_total = bess_ac_kw(picked_batt, bess_kwh_effective)
        build_rozvadzac_ac(ac_kw_total)
        build_kabelaz_bess(ac_kw_total)
        build_pd(max(ac_kw_total, BESS_ONLY_MIN_PD_KW))
        build_ems(default_on=bess_kwh_effective >= BESS_EMS_DEFAULT_KWH)
        if bess_kwh_effective >= BESS_STATIKA_MIN_KWH:
            build_statika_pbs()
        build_dispecing(ac_kw_total)
        warn("info", "bess_only",
             f"Ponuka len pre batériu (bez FVE): {bess_kwh_effective:g} kWh / {ac_kw_total:g} kW. "
             f"Menič/PCS kalkulačka nepridáva — over, či je PCS súčasťou batérie, inak ho doplň ručne.")
    else:
        # ===== 1. PANELY =====
        add("Panely", panel["name"], pocet_panelov, "ks", panel.get("cost"), panel.get("price_per_unit"),
            f"panel.{panel['sku']}", vendor_stack=vendor_key)

        # ===== 2. MENIČE =====
        required_ac_kw = kwp_actual / DC_AC_RATIO
        picked_inv = _pick_inverters(stack.get("inverters") or [], required_ac_kw, require_hybrid=has_bess)
        if not picked_inv:
            warn("warning", "no_inverter", "Výrobca nemá v katalógu žiadny menič — ponuka je bez meničov.")
        for p in picked_inv:
            inv = p["inverter"]
            add("Striedače", inv["name"], p["qty"], "ks", inv.get("cost"), inv.get("price"),
                f"menic.{vendor_key}.{inv.get('key') or inv.get('name')}", vendor_stack=vendor_key,
                sku=inv.get("key"), ac_kw=_num(inv.get("ac_kw")))
        # Σ AC výkon vybraných meničov — základ pre rozvádzač AC, PD, MTP a dispečing
        ac_kw_total = round(sum(_num(p["inverter"].get("ac_kw")) * p["qty"] for p in picked_inv), 2)
        # E-12: žiadna kombinácia do MAX_INVERTER_UNITS kusov nevyhovela → n × najväčší menič (výber bez signálu by klamal)
        _fb = next((p for p in picked_inv if p.get("fallback")), None)
        if _fb:
            _fb_inv = _fb["inverter"]
            warn("warning", "inverter_fallback",
                 f"Zostava meničov sa nezmestila do {MAX_INVERTER_UNITS} ks — vybraných {_fb['qty']}× {_fb_inv.get('name')} "
                 f"({ac_kw_total:g} kW AC, DC/AC {kwp_actual / ac_kw_total if ac_kw_total else 0:.2f}); "
                 f"over zostavu meničov (typ, počet, oversizing) a rozsah ponuky.")

        # Smart manager + smart meter (povinné pri väčších inštaláciách)
        # Väčšie inštalácie (napr. Huawei >10 kWp) vyžadujú Smart Logger namiesto dongle.
        sm = stack.get("smart_manager")
        sm_large = stack.get("smart_manager_large")
        if sm_large and kwp_actual > sm_large.get("required_above_kwp", 10):
            sm = sm_large
        if sm and kwp_actual > sm.get("required_above_kwp", 0):
            add("Monitoring", sm["name"], 1, "ks", sm.get("cost"), sm.get("price"),
                f"smart_manager.{vendor_key}", vendor_stack=vendor_key)
        smtr = stack.get("smart_meter")
        if smtr:
            add("Monitoring", smtr["name"], 1, "ks", smtr.get("cost"), smtr.get("price"),
                f"smart_meter.{vendor_key}", vendor_stack=vendor_key)

        # ===== 2b. SIEŤOVÝ ANALYZÁTOR (Janitza) — len Huawei nad prahom; has_janitza:false ho vypne =====
        accessories = stack.get("accessories") or []
        if vendor_key == "huawei" and _flag(config.get("has_janitza"), True):
            _nas = [a for a in accessories if a.get("category") == "network_analyzer"]
            if _nas:
                _jk = config.get("janitza_key")
                _ja = next((a for a in _nas if _jk and a.get("key") == _jk), None)
                if _ja is None:   # uprednostni UMG 103-CBM
                    _ja = next((a for a in _nas if "103" in f"{a.get('key') or ''} {a.get('name') or ''}"), _nas[0])
                _thr = _num(_ja.get("offer_above_kw"), JANITZA_DEFAULT_ABOVE_KW)
                if kwp_actual > _thr:
                    add("Diagnostika siete", _ja["name"], 1, "ks", _ja.get("cost"), _ja.get("price"),
                        f"accessory.{_ja['key']}", vendor_stack=vendor_key,
                        ai_note=f"Auto pri >{_thr:g} kW; kompatibilné so všetkými meničmi")

        # ===== 3. KONŠTRUKCIA (+ záťaž pri východ-západ) =====
        k_rules = _load_konstrukcia_rule(sb, typ_strechy) if typ_strechy else []
        if not k_rules:
            warn("warning", "konstrukcia_missing",
                 f"Pre typ strechy '{typ_strechy}' nie je v cenníku konštrukcia — ponuka je bez konštrukcie, doplň ju ručne.")
        for r in k_rules:
            # qty_formula a jednotka z DB (konštrukcia je od F0 na kWp; staré dáta: ks podľa počtu panelov)
            _kwp_unit = str(r.get("unit") or "").strip().lower() == "kwp"
            k_qty = _rule_qty(r, kwp_actual, pocet_panelov, default=kwp_actual if _kwp_unit else pocet_panelov)
            add_from_rule("Konštrukcia", r, k_qty, f"konstrukcia.{r['rule_key']}")
        if typ_strechy == "vychod_zapad":
            r = rule("zatiaz", "vz")
            if r:
                add_from_rule("Konštrukcia", r, _rule_qty(r, kwp_actual, pocet_panelov, default=kwp_actual), "zatiaz.vz")
            else:
                missing_rule("zatiaz", "vz", "Záťaž konštrukcie (V-Z)")

        # ===== 4. ROZVÁDZAČ DC (pri FVE vždy, vypínateľný has_dc_rozvadzac) =====
        if _flag(config.get("has_dc_rozvadzac"), True):
            r = rule("rozvadzac_dc", "r_dc")
            if r:
                add_from_rule("Rozvádzač", r, _rule_qty(r, kwp_actual, pocet_panelov, default=kwp_actual), "rozvadzac_dc")
            else:
                missing_rule("rozvadzac_dc", "r_dc", "Rozvádzač DC")

        # ===== 4b. ROZVÁDZAČ AC (pásmo podľa Σ AC kW meničov) =====
        build_rozvadzac_ac(ac_kw_total)

        # ===== 5. VODIČE + SPOTREBNÝ + KÁBLOVÉ ŽĽABY =====
        for rt, rk, cat in [("vodice", "dc", "Vodiče"), ("vodice", "ac", "Vodiče"), ("spotrebny", "standard", "Spotrebný materiál")]:
            r = rule(rt, rk)
            if r:
                add_from_rule(cat, r, kwp_actual, f"{rt}.{rk}")
            else:
                missing_rule(rt, rk, "Vodiče DC" if rk == "dc" else ("Vodiče AC" if rk == "ac" else "Spotrebný materiál"))
        r = rule("ostatne", "kablove_zlaby")
        if r:
            _zl_kwp = str(r.get("unit") or "").strip().lower() == "kwp"
            add_from_rule("Káblové žľaby", r,
                          _rule_qty(r, kwp_actual, pocet_panelov, default=kwp_actual if _zl_kwp else pocet_panelov),
                          "kablove_zlaby")
        else:
            # staré dáta (pred F0): žľab + chráničky ako tri pravidlá; v nových dátach sú neaktívne
            _legacy = [(rk, rule("ostatne", rk)) for rk in ("zlab_kryt_50mm", "chranicka_25mm", "chranicka_40mm")]
            _legacy = [(rk, lr) for rk, lr in _legacy if lr]
            for rk, lr in _legacy:
                add_from_rule("Káblové žľaby", lr, _eval_qty_formula(lr.get("qty_formula"), kwp_actual, pocet_panelov),
                              f"ostatne.{rk}")
            if not _legacy:
                missing_rule("ostatne", "kablove_zlaby", "Káblové žľaby")

        # ===== 6. PD — pásmo podľa Σ AC kW =====
        build_pd(ac_kw_total)

        # ===== 6b. STATIKA + PBS (kWp >= 100) =====
        if kwp_actual >= STATIKA_MIN_KWP:
            build_statika_pbs()

        # ===== 6c. MTP (Σ AC > 30 kW, 3 ks) =====
        if ac_kw_total > MTP_MIN_AC_KW:
            r = rule("mtp", "mtp3")
            if r:
                add_from_rule("Meranie", r, _rule_qty(r, kwp_actual, pocet_panelov, default=3), "mtp")
            else:
                missing_rule("mtp", "mtp3", "Merací transformátor prúdu (MTP)")

        # ===== 6d. DISPEČING / ASDR (Σ AC >= 100 kW) podľa distribučky =====
        build_dispecing(ac_kw_total)

        # ===== 7. OPTIMIZÉRY (vendor-specific!) =====
        if has_optimizery:
            # Huawei → MERC, Sungrow/GoodWe/Solinteg → Tigo
            opts = stack.get("optimizers") or []
            if opts:
                opt = opts[0]  # default first
                # počet optimizérov: panels_per_unit (Huawei MERC = 2 panely/kus, Tigo = 1 panel/kus)
                ppu = int(_num(opt.get("panels_per_unit"), 1)) or 1
                opt_qty = math.ceil(pocet_panelov / max(1, ppu))
                opt_cost = next((opt[k] for k in ("cost", "cost_per_unit", "cost_per_panel") if opt.get(k) is not None), None)
                add("Optimizéry", opt["name"], opt_qty, "ks", opt_cost, opt.get("price_per_panel"),
                    f"optimizer.{vendor_key}.{opt['key']}", vendor_stack=vendor_key, ai_note=opt.get("notes", ""))
                # Tigo → povinný CCA Kit + TAP (1 sada na 150 optimizérov); bez "Montáž optimizér" (od F0 zrušená)
                if "tigo" in f"{opt.get('key') or ''} {opt.get('name') or ''}".lower():
                    cca_items = [a for a in accessories if a.get("category") == "tigo_cca"]
                    if not cca_items:
                        warn("warning", "tigo_cca_missing",
                             "Tigo optimizéry vyžadujú CCA Kit + TAP, v dátach výrobcu nie sú — doplň ich ručne "
                             "(1 sada na 150 optimizérov).")
                    else:
                        sets = max(1, math.ceil(opt_qty / TIGO_CCA_PER_SET))
                        for a in cca_items:
                            add("Optimizéry", a["name"], sets * max(1, int(_num(a.get("qty_per_set"), 1))), "ks",
                                a.get("cost"), a.get("price"),
                                "tigo_cca" if len(cca_items) == 1 else f"tigo_cca.{a.get('key')}",
                                vendor_stack=vendor_key)
                        if opt_qty > TIGO_CCA_PER_SET:
                            warn("warning", "tigo_cca_multi",
                                 f"{opt_qty} optimizérov Tigo je nad {TIGO_CCA_PER_SET} — pridaných {sets} sád CCA Kit + TAP "
                                 f"(1 sada na {TIGO_CCA_PER_SET} optimizérov); over počet u dodávateľa.")
            else:
                warn("warning", "optimizer_missing", "Výrobca nemá v katalógu optimizér — riadok optimizérov chýba.")

        # ===== 8. RAPID SHUTDOWN =====
        if has_rapid_shutdown:
            for rk in ["bfs12", "esw12", "montaz_rs"]:
                r = rule("rapid_shutdown", rk)
                if r:
                    if "ceil" in (r.get("qty_formula") or ""):
                        qty = math.ceil(pocet_panelov / 4) if "4" in r["qty_formula"] else math.ceil(pocet_panelov / 200)
                    else:
                        qty = 1
                    add_from_rule("Rapid Shutdown", r, qty, f"rapid_shutdown.{rk}")
                else:
                    missing_rule("rapid_shutdown", rk, "Rapid Shutdown")

        # ===== 9. BESS (EMS len na výslovné has_ems) =====
        if bess_requested:
            picked_batt, bess_kwh_effective = build_battery()
            if picked_batt:
                build_ems(default_on=False)
        elif has_bess:
            warn("warning", "bess_missing_qty",
                 "Batéria je zapnutá, ale bez počtu, kapacity (kWh) alebo modelu — batéria nie je v ponuke.")

    # ===== 10. WALLBOX =====
    if has_wallbox and wallbox_pocet > 0:
        wbs = stack.get("wallboxes") or []
        if wbs:
            wb = wbs[0]
            add("Wallbox", wb["name"], wallbox_pocet, "ks", wb.get("cost"), wb.get("price"),
                f"wallbox.{vendor_key}.{wb['key']}")
        else:
            warn("warning", "wallbox_missing", "Výrobca nemá v katalógu wallbox — riadok wallboxu chýba.")

    # ===== 11. MONTÁŽ FVE (kWp pásmo) =====
    if not bess_only:
        m_band = _pick_band(rules("montaz"), kwp_actual)
        if m_band:
            add_from_rule("Montáž", m_band, kwp_actual, f"montaz.{m_band['rule_key']}")
        else:
            missing_rule("montaz", f"pásmo {kwp_actual:g} kWp", "Montáž FVE")

    # ===== 12. DOPRAVA =====
    if vzdialenost_doprava > 0:
        r = rule("doprava", "km")
        if r:
            add_from_rule("Doprava", r, vzdialenost_doprava, "doprava.km")
        else:
            missing_rule("doprava", "km", "Doprava")
    else:
        warn("warning", "doprava_km",
             "Vzdialenosť dopravy nie je zadaná (0 km) — riadok dopravy nie je v ponuke. Zadaj vzdialenosť v km.")

    # ===== CENOTVORBA: predaj = nákup / (1 - m/100) pre VŠETKY položky =====
    for it in items:
        cost = it["cost_per_unit"]
        it["price_per_unit"] = round(cost / (1 - margin_pct / 100), 2)
        it["total_cost"] = round(cost * it["qty"], 2)
        it["total_price"] = round(it["price_per_unit"] * it["qty"], 2)

    # ===== Varovania (jediný zoznam) =====
    if estimated:
        warn("warning", "cost_estimated",
             "Nákup nie je v dátach, odhadnutý ako 77 % z cenníkovej ceny pre: " + "; ".join(estimated)
             + ". Doplň nákupné ceny.", items=list(estimated))

    # Huawei + Tigo bug
    if vendor_key == "huawei" and has_optimizery and not bess_only:
        warn("info", "vendor_match",
             "✓ Huawei stack používa natívne optimizéry HUAWEI MERC — nie Tigo (nekompatibilné)")

    if not has_bess and kwp_actual >= 100:
        warn("info", "bess_recommendation",
             f"💡 Pri {kwp_actual} kWp >> 100 odporúčam zvážiť BESS — pre arbitráž a peak shaving. Návratnosť +0.5-1 rok.")

    if has_optimizery and not has_rapid_shutdown and not bess_only:
        warn("info", "rs_recommendation",
             "💡 Optimizéry + Rapid Shutdown — vyžadované pre verejné budovy podľa STN EN 50549.")

    # Batéria bez hybridného meniča (napr. Sungrow nemá hybrid) — varovanie, nie ticho
    if picked_batt and picked_inv and not all(p["inverter"].get("hybrid") for p in picked_inv):
        warn("warning", "no_hybrid",
             "Pri batérii nebol nájdený vhodný hybridný menič pre tento výkon — vybraný stringový. "
             "Skontroluj zostavu/doplň hybrid model.")

    # Mimo bežného rozsahu kalkulačky — kalkulačka pustí všetko, len varuje
    if kwp_actual > SCOPE_MAX_KWP:
        warn("warning", "out_of_scope",
             f"Výkon {kwp_actual:g} kWp je nad {SCOPE_MAX_KWP:g} kWp — mimo bežného rozsahu kalkulačky, "
             f"výsledok ber ako orientačný.")
    if typ_strechy in GROUND_ROOFS and not bess_only:
        warn("warning", "out_of_scope",
             "Zemná konštrukcia je mimo bežného rozsahu kalkulačky — over položky, ktoré kalkulačka nepokrýva "
             "(výkopy, kabeláž, oplotenie).")
    # (režim len BESS C&I batérie rieši priamo: AC rozvádzač, kabeláž, EMS aj dispečing sú v ponuke)
    if not bess_only and any((p["battery"].get("battery_class") or "residential") == "industrial" for p in picked_batt) and (
            bess_kwh_effective > SCOPE_MAX_BESS_KWH or any(_is_luna_ci(p["battery"]) for p in picked_batt)):
        warn("warning", "out_of_scope",
             f"C&I batéria ({bess_kwh_effective:g} kWh) je mimo bežného rozsahu kalkulačky — "
             f"over PCS/menič, AC rozvádzač, kabeláž a EMS.")

    # Totals
    total_cost = sum(it["total_cost"] for it in items)
    total_price = sum(it["total_price"] for it in items)

    return {
        "ok": True,
        "config": {
            "vendor_stack": vendor_key,
            "vendor_display": stack.get("display_name"),
            "typ_strechy": typ_strechy,
            "panel": panel,
            "pocet_panelov": pocet_panelov,
            "kwp_actual": kwp_actual,
        },
        "items": items,
        "warnings": warnings,
        "totals": {
            "pocet_panelov": pocet_panelov,
            "pocet_menicov": sum(p["qty"] for p in picked_inv),
            "kwp": kwp_actual,
            "panel_wp": panel["wp"] if panel else 0,
            "ac_kw_total": ac_kw_total,
            "requires_asdr": ac_kw_total >= ASDR_MIN_AC_KW,
            "bess_kwh_effective": bess_kwh_effective,
            "margin_pct_input": margin_pct,
            "margin_pct_effective": round((total_price - total_cost) / total_price * 100, 2) if total_price > 0 else 0,
            "total_cost": round(total_cost, 2),
            "total_price": round(total_price, 2),
            "total_margin_eur": round(total_price - total_cost, 2),
            "items_count": len(items),
        },
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

# Podiel výrobcov v Raynet ponukách (audit 2026-10-08, Fáza 0) — fallback heuristics
RAYNET_VENDOR_DISTRIBUTION = {
    "huawei":   0.50,
    "solinteg": 0.33,
    "sungrow":  0.17,
    "goodwe":   0.0,   # v Raynete bez cenníka
}

# Priemerné €/kWp z Raynet ponúk (predaj bez DPH; audit 2026-10-08, Fáza 0)
RAYNET_AVG_EUR_PER_KWP = {
    "do_30":    790.0,    # do 30 kWp
    "30_60":    745.0,    # 30-60 kWp
    "60_100":   680.0,
    "nad_100":  700.0,
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
    bess_count = int(_num(config.get("bess_count"), 0))
    bess_selected = bool(config.get("bess_sku") or config.get("bess_key"))
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

    # BESS sanity — UI zadáva batériu počtom kusov (bess_count) alebo modelom, nie kWh
    if has_bess and bess_kwh <= 0 and bess_count <= 0 and not bess_selected:
        issues.append({"severity": "warning", "kind": "bess_missing_kwh",
                       "message": "Označená batéria ale 0 kWh — nastavte kapacitu (default 10 kWh)."})
    # Režim len BESS (výslovne 0 panelov, bez kWp): menič/PCS kalkulačka nepridáva (varuje bess_only) → hybrid sa nehlási
    bess_only_cfg = "pocet_panelov" in config and pocet_panelov == 0 and _num(config.get("kwp"), 0.0) <= 0
    # Batéria vyžaduje hybridný menič: výrobca bez hybridu v katalógu (napr. Sungrow) → nekompatibilná zostava
    if has_bess and (bess_kwh > 0 or bess_count > 0 or bess_selected) and vendor and not bess_only_cfg:
        try:
            _stack = _load_vendor_stack(sb, vendor)
        except Exception:
            log.exception("ai_compatibility_checker: načítanie stacku zlyhalo")
            _stack = None
        _invs = (_stack or {}).get("inverters") or []
        if _invs and not any(i.get("hybrid") for i in _invs):
            issues.append({"severity": "warning", "kind": "no_hybrid",
                           "message": f"{vendor.title()}: v katalógu nie je hybridný menič — batéria sa so stringovým "
                                      f"meničom nedá zapojiť. Zvoľ iného výrobcu alebo doplň hybrid ručne."})
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
    # (aliasy zodpovedajú kategóriám, ktoré vracia calculate_bom_v2: Panely, Striedače, Vodiče)
    grouped = {
        "Panel": ["Panel", "Panely", "Fotovoltický panel"],
        "Menič": ["Menič", "Striedač", "Striedače", "Invertor"],
        "Konštrukcia": ["Konštrukcia"],
        "Káble - DC": ["Káble - DC", "Vodiče", "DC kábel", "Solárny kábel"],
        "Káble - AC": ["Káble - AC", "Vodiče", "AC kábel", "CYKY"],
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
