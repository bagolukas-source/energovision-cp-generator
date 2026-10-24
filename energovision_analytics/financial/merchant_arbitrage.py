"""Merchant (grid-to-grid) batériová arbitráž — podpora bilančnej skupiny.

Batéria sa NEdimenzuje na samospotrebu, ale obchoduje komoditu cez bilančnú skupinu:
nabíja z gridu v lacných hodinách (ohraničené RK), vybíja DO gridu v drahých (ohraničené
max exportom). Hodnota = spotový spread × účinnosť, mínus marža organizátora a odchýlka.
Opotrebenie článkov je len PRAH obchodu (obchod, ktorý ho nepokryje, sa nespraví), ako náklad
sa od výnosu neodpočítava — batériu zaplatil CAPEX, ekonomika sa posudzuje návratnosťou
investície (rozhodnutie majiteľa 2026-10-24; rezerva od 9/2026 platila články druhýkrát).

Čistý, samostatný model — needituje EMS samospotreby, takže žiadne dvojité počítanie.

Dispatch: po DENNÝCH blokoch (window intervalov), každý deň samostatný MILP s CHRONOLOGICKÝM
SoC (audit 2026-10-24 M1, prevzaté z Codex 1.0.0rc1). Deň začína aj končí s prázdnou batériou
(SoC = soc_min), takže sa nedá predať energia, ktorá sa ešte nenabila, a nič neprechádza
nezaplatené do ďalšieho dňa. Binárna premenná na interval zakazuje súčasné nabíjanie
a vybíjanie (pri zápornom spote by inak model „pálil" energiu cez straty). Riešenie je
perfektná predpoveď v rámci dňa — horný odhad pre operátora, nie garantovaný výnos.
Predchádzajúci greedy (najlacnejšie × najdrahšie hodiny bez chronológie) pri > 1 cykle denne
nadhodnotil výnos o 43–100 %.

Počet cyklov za deň riadi max_cycles_per_day: strop DC energie uloženej za deň = usable × cykly.
1.0 = jedno plné nabitie, None = bez stropu (obmedzí len výkon, sieť a chronológia SoC).

PRAH ZISKOVOSTI (oprava 9/2026 — kalibrácia voči Energe):
Do 9/2026 sa pár testoval len na round-trip stratu (`p_dis*√rte > p_chg/√rte`) a degradácia
s odchýlkou sa odpočítali až od SÚČTU na konci. Engine teda uzavrel aj obchody, ktoré po
započítaní opotrebenia batérie boli stratové, a vykázal ich ako zisk. Reálny obchodník
(potvrdené Energe 4.9.2026: „rátame aj s capexom baterky a pod nejaký spread už nejdeme")
takýto obchod neuzavrie. Teraz sa každý pár testuje na ČISTÝ marginálny zisk:

    zisk_dc = (p_vyboj·√rte − p_nabij/√rte)·(1 − fee) − odchylka·√rte − degradacia

a páruje sa len kým `zisk_dc > min_margin_eur_mwh`. Výstup `breakeven_spread_eur_mwh`
hovorí, aký cenový rozdiel musí deň ponúknuť, aby sa obchod vôbec oplatil.

MRK/RK headroom: nabíjanie ide cez to isté odberné miesto ako výroba, takže sa SČÍTAVA
so súbežným odberom. Bez `load_kw` sa strop berie ako celá RK — to pri OM s vysokou
základnou záťažou (napr. 24/7 prevádzka s odberom 2 100 kW pri RK 3 400 kW) nadhodnotí
obchodovaný objem aj výnos, a v realite by znamenalo prekročenie MRK s penále.
S `load_kw` sa v každom intervale nabíja len do voľnej kapacity (rk_kw − load_kw[i]).

Energetická bilancia (DC = energia v batérii):
  - uloženie e_dc → AC odber z gridu = e_dc / sqrt(rte)   (strata pri nabíjaní)
  - dodávka z e_dc → AC export do gridu = e_dc * sqrt(rte) (strata pri vybíjaní)
"""
from __future__ import annotations
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

# Značka metódy vo výstupe — posudok aj CRM vedia, z akého dispatchu je číslo.
MODEL = "denny_milp_chronologicky_soc"


def degradation_reserve_eur_mwh(capex_bess_eur_per_kwh: float,
                                warranty_cycles: int,
                                usable_frac: float = 0.90,
                                cells_share: float = 0.40) -> float:
    """Opotrebenie batérie prepočítané na €/MWh energie prevedenej článkami (DC).

    Cyklovaním sa spotrebúvajú ČLÁNKY, nie celá inštalácia — pri obnove sa menia moduly,
    nie trafo, rozvádzače, projekt a montáž. Preto sa berie len `cells_share` z ceny BESS
    (default 0,40 — rovnaká sadzba, akou CashflowBuilder oceňuje výmenu článkov, aby boli
    obidve cesty konzistentné). Počítanie z plnej predajnej ceny systému by opotrebenie
    nadhodnotilo 2,5-násobne a merchant výnos umelo zrazilo.

    Články za `capex × cells_share` €/kWh zvládnu `warranty_cycles` cyklov, v každom prejde
    `usable_frac` kapacity. Priepustnosť na 1 kWh inštalovanej kapacity je teda
    `warranty_cycles × usable_frac` kWh a cena rozpočítaná na ňu je cena prevedenej MWh.

    Príklad (BESS 318 €/kWh, články 40 %, 6 000 cyklov, 90 % DoD):
        318 × 0,40 / (6 000 × 0,90) × 1000 = 23,6 €/MWh

    Toto je REZERVA na budúcu výmenu — ak sa v cashflow súčasne účtuje explicitná výmena
    článkov (`count_battery_replacement`), rezerva sa nesmie použiť, inak sa opotrebenie
    započíta dvakrát.
    """
    cap = float(capex_bess_eur_per_kwh or 0.0) * float(cells_share or 0.0)
    cyc = float(warranty_cycles or 0.0)
    frac = float(usable_frac or 0.0)
    if cap <= 0 or cyc <= 0 or frac <= 0:
        return 0.0
    return cap / (cyc * frac) * 1000.0


def _solve_day(w, cap_c, cap_d, usable, e_cap, eta, fee, imb, dc_cost):
    """Denný MILP s chronologickým SoC → (c, d) v AC kWh/interval, None = obchod sa neoplatí.

    Premenné [c, d, s, z] po k intervaloch: c = AC nabíjanie (≤ voľné miesto pod RK),
    d = AC vybíjanie (≤ min(výkon, export)), s = DC energia nad soc_min na konci intervalu
    (≤ usable, posledný interval = 0), z = 1 nabíja / 0 vybíja.
    """
    k = len(w)
    j = np.arange(k)
    p = w / 1000.0 * (1.0 - fee)
    obj = np.r_[p + eta * dc_cost, -p + imb / 1000.0, np.zeros(2 * k)]
    # s_j − s_{j−1} − η·c_j + d_j/η = 0 (s_{−1} = 0: deň začína prázdny)
    rows = [j, j, j, j[1:]]
    cols = [j, k + j, 2 * k + j, 2 * k + j[1:] - 1]
    vals = [np.full(k, -eta), np.full(k, 1.0 / eta), np.ones(k), -np.ones(k - 1)]
    # c_j ≤ cap_c_j·z_j ;  d_j ≤ cap_d·(1 − z_j)
    rows += [k + j, k + j, 2 * k + j, 2 * k + j]
    cols += [j, 3 * k + j, k + j, 3 * k + j]
    vals += [np.ones(k), -np.asarray(cap_c, dtype=float), np.ones(k), np.full(k, cap_d)]
    lo = np.r_[np.zeros(k), np.full(2 * k, -np.inf)]
    hi = np.r_[np.zeros(2 * k), np.full(k, cap_d)]
    m = 3 * k
    if e_cap is not None:                          # denný strop DC energie uloženej
        rows.append(np.full(k, m)); cols.append(j); vals.append(np.full(k, eta))
        lo = np.r_[lo, -np.inf]; hi = np.r_[hi, e_cap]; m += 1
    a = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                   shape=(m, 4 * k)).tocsc()
    ub = np.r_[cap_c, np.full(k, cap_d), np.full(k, usable), np.ones(k)]
    ub[3 * k - 1] = 0.0                            # koniec dňa prázdny
    sol = milp(obj, integrality=np.r_[np.zeros(3 * k), np.ones(k)],
               bounds=Bounds(np.zeros(4 * k), ub), constraints=LinearConstraint(a, lo, hi),
               options={"mip_rel_gap": 1e-9, "time_limit": 30})
    if not sol.success or sol.x is None:
        raise RuntimeError(f"Merchant solver zlyhal: {sol.message}")
    if sol.fun >= -1e-8:                           # nič ziskové → batéria stojí
        return None
    return np.maximum(0.0, sol.x[:k]), np.maximum(0.0, sol.x[k:2 * k])


def compute_merchant_arbitrage(
    spot_eur_mwh,            # list/array €/MWh, krok dt_h
    dt_h: float,
    bess_kwh: float,
    power_kw_ac: float,
    rk_kw: float,            # rezervovaná kapacita (import limit) kW
    export_kw: float,        # max export do gridu kW
    rte: float = 0.88,       # round-trip účinnosť
    organizer_fee_pct: float = 15.0,   # marža organizátora bilančnej skupiny
    imbalance_cost_eur_mwh: float = 0.0,    # odchýlková (imbalance) cena na obchodovaný AC objem
    degradation_cost_eur_mwh: float = 0.0,  # cyklová degradačná rezerva na DC throughput
    revenue_share_pct: float = 1.0,         # podiel klienta/Energovision z čistého merchant výnosu
    min_margin_eur_mwh: float = 0.0,        # minimálna čistá marža na DC MWh, pod ktorou sa neobchoduje
    soc_min_frac: float = 0.05,
    soc_max_frac: float = 0.95,
    window: int = 96,        # dĺžka denného bloku (intervalov); 96=15-min, 24=hodinové
    max_cycles_per_day: float | None = 1.0,  # koľko plných cyklov denne; None = bez limitu
    load_kw=None,            # súbežný odber OM (kW) — nabíjanie sa vojde len do RK MÍNUS odber
) -> dict:
    spot = np.asarray(spot_eur_mwh, dtype=float)
    n = len(spot)
    fee_frac = float(organizer_fee_pct) / 100.0
    imb = float(imbalance_cost_eur_mwh or 0.0)
    degr = float(degradation_cost_eur_mwh or 0.0)
    min_margin = float(min_margin_eur_mwh or 0.0)
    sqrt_rte = rte ** 0.5

    # Aký hrubý cenový rozdiel (p_vyboj − p_nabij pri strednej cenovej hladine) musí deň
    # ponúknuť, aby pár prešiel prahom — pre transparentnosť v posudku.
    # Odvodené z podmienky: (p_dis·√rte − p_chg/√rte)·(1−fee) − imb·√rte − degr > min_margin
    _cost_side = (imb * sqrt_rte + degr + min_margin) / max(1e-9, (1.0 - fee_frac))
    breakeven = _cost_side / max(1e-9, sqrt_rte)

    empty = {"annual_profit_eur": 0.0, "throughput_mwh": 0.0, "dc_throughput_mwh": 0.0,
             "grid_charge_mwh": 0.0, "mrk_aware": False, "intervals_blocked_by_mrk": 0,
             "max_cycles_per_day": max_cycles_per_day, "equiv_cycles": 0.0,
             "sell_eur": 0.0, "buy_eur": 0.0, "fee_pct": organizer_fee_pct, "gross_eur": 0.0,
             "organizer_fee_eur": 0.0, "imbalance_eur": 0.0, "degradation_eur": 0.0,
             "degradation_cost_eur_mwh": round(degr, 2),
             "imbalance_cost_eur_mwh": round(imb, 2),
             "min_margin_eur_mwh": round(min_margin, 2),
             "breakeven_spread_eur_mwh": round(breakeven, 1),
             "days_traded": 0, "days_total": 0,
             "merchant_net_eur": 0.0, "revenue_share_pct": revenue_share_pct,
             "model": MODEL}
    if n == 0 or bess_kwh <= 0 or power_kw_ac <= 0:
        return empty

    usable = bess_kwh * (soc_max_frac - soc_min_frac)              # DC kWh/deň max

    # Vybíjanie (export) je konštantné — limituje ho menič a max export.
    dis_ac_cap = min(power_kw_ac, export_kw) * dt_h               # AC export / interval
    if dis_ac_cap <= 0:
        return empty

    # Nabíjanie je per-interval: batéria a odber OM zdieľajú tú istú prípojku, takže
    # strop je RK MÍNUS súbežný odber. Bez profilu záťaže sa (kompatibilne so starým
    # správaním) berie celá RK, ale výsledok sa označí ako neoverený voči MRK.
    if load_kw is not None and len(load_kw) >= n:
        _load = np.asarray(load_kw[:n], dtype=float)
        headroom_kw = np.maximum(0.0, rk_kw - _load)
        chg_ac_arr = np.minimum(power_kw_ac, headroom_kw) * dt_h
        mrk_aware = True
    else:
        chg_ac_arr = np.full(n, min(power_kw_ac, rk_kw) * dt_h, dtype=float)
        mrk_aware = False
    if float(chg_ac_arr.max()) <= 0:
        # Odber vyčerpáva celú RK — pod limitom nezostáva miesto na nabíjanie.
        # Výsledok je nula, ale musí byť rozoznateľné, že ju spôsobila prípojka,
        # nie chýbajúce dáta: inak posudok tvrdí „batéria nezarobí" bez dôvodu.
        blocked = dict(empty)
        blocked["mrk_aware"] = mrk_aware
        blocked["intervals_blocked_by_mrk"] = int(n)
        blocked["days_total"] = int(np.ceil(n / max(1, window)))
        return blocked
    sell_eur = 0.0; buy_eur = 0.0
    ac_export_total = 0.0; dc_throughput = 0.0
    ac_charge_total = 0.0     # koľko sa reálne odobralo zo siete na nabíjanie
    blocked_intervals = 0     # intervaly bez voľného miesta pod RK
    days_total = 0; days_traded = 0
    # Prah na DC kWh uloženú: opotrebenie článkov + minimálna marža. Oboje len rozhoduje,
    # či sa obchod oplatí — do výsledku sa neúčtuje (opotrebenie je informatívne nižšie).
    dc_cost = (degr + min_margin) / 1000.0
    e_cap = None if max_cycles_per_day is None else usable * float(max_cycles_per_day)

    for start in range(0, n, window):
        w = spot[start:start + window]
        if len(w) < 2:
            continue
        days_total += 1
        cap_c = chg_ac_arr[start:start + len(w)]
        blocked_intervals += int((cap_c <= 1e-9).sum())
        if float(cap_c.max()) <= 1e-9:
            continue
        x = _solve_day(w, cap_c, dis_ac_cap, usable, e_cap, sqrt_rte, fee_frac, imb, dc_cost)
        if x is None:
            continue
        c, d = x
        day_dc = float(c.sum()) * sqrt_rte        # = DC vybité (deň končí prázdny)
        if day_dc <= 1e-9:
            continue
        buy_eur += float(c @ w) / 1000.0          # AC odber × cena (záporný spot znižuje náklad)
        sell_eur += float(d @ w) / 1000.0         # AC export × cena
        ac_charge_total += float(c.sum())
        ac_export_total += float(d.sum())
        dc_throughput += day_dc
        days_traded += 1

    gross = sell_eur - buy_eur
    # organizer_fee (marža organizátora BS) a revenue_share (podiel klienta) sú ODDELENÉ
    # koncepty. imbalance ide na AC obchod. Opotrebenie (degradation) sa NEodpočítava —
    # články zaplatil CAPEX; číslo ostáva len informatívne (koľko by stála rezerva).
    organizer_fee = gross * fee_frac
    imbalance = (ac_export_total / 1000.0) * imb
    degradation = (dc_throughput / 1000.0) * degr
    merchant_net = gross - organizer_fee - imbalance                    # čistý merchant výnos
    client_value = merchant_net * float(revenue_share_pct)              # podiel klienta
    equiv_cycles = (dc_throughput / bess_kwh) if bess_kwh > 0 else 0.0
    dc_throughput_mwh = dc_throughput / 1000.0
    return {"annual_profit_eur": round(client_value, 0),
            # koľko sa NAOZAJ odobralo zo siete na nabíjanie — to je energia, ktorá
            # ide cez fakturačné meranie OM navyše k spotrebe (posudok to musí ukázať)
            "grid_charge_mwh": round(ac_charge_total / 1000.0, 1),
            "mrk_aware": mrk_aware,
            "intervals_blocked_by_mrk": blocked_intervals,
            "throughput_mwh": round(ac_export_total / 1000.0, 1),
            "dc_throughput_mwh": round(dc_throughput_mwh, 1),
            "max_cycles_per_day": max_cycles_per_day,
            "equiv_cycles": round(equiv_cycles, 0),
            "sell_eur": round(sell_eur, 0), "buy_eur": round(buy_eur, 0),
            "fee_pct": organizer_fee_pct, "gross_eur": round(gross, 0),
            "organizer_fee_eur": round(organizer_fee, 0),
            "imbalance_eur": round(imbalance, 0), "degradation_eur": round(degradation, 0),
            "degradation_deducted": False,      # opotrebenie len prah obchodu, nie náklad
            # vstupy prahu — posudok aj CRM ich musia vedieť ukázať, inak je číslo neoveriteľné
            "degradation_cost_eur_mwh": round(degr, 2),
            "imbalance_cost_eur_mwh": round(imb, 2),
            "min_margin_eur_mwh": round(min_margin, 2),
            "breakeven_spread_eur_mwh": round(breakeven, 1),
            "days_traded": days_traded, "days_total": days_total,
            "merchant_net_eur": round(merchant_net, 0), "revenue_share_pct": revenue_share_pct,
            "model": MODEL}
