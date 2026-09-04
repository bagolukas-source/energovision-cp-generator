"""Merchant (grid-to-grid) batériová arbitráž — podpora bilančnej skupiny.

Batéria sa NEdimenzuje na samospotrebu, ale obchoduje komoditu cez bilančnú skupinu:
nabíja z gridu v lacných hodinách (ohraničené RK), vybíja DO gridu v drahých (ohraničené
max exportom). Hodnota = spotový spread × účinnosť, mínus marža organizátora.

Čistý, samostatný model — needituje EMS samospotreby, takže žiadne dvojité počítanie.

Dispatch: po DENNÝCH blokoch (window intervalov). V rámci dňa páruje NAJLACNEJŠIE hodiny
(nabíjanie) s NAJDRAHŠÍMI (vybíjanie) — greedy podľa cenového poradia, nie chronologicky.
Tým je zisk monotónny v export/RK/výkon limitoch (viac kapacity = viac zisku) a zodpovedá
tomu, ako reálny operátor vyberá najlepšie hodiny. Batéria sa každý deň vyprázdni (denný cyklus).

Počet cyklov za deň riadi max_cycles_per_day: 1.0 = konzervatívne (jedno nabitie/vybitie),
vyššia hodnota = viac obchodovaných dvojíc, None = bez limitu (obmedzí len výkon a sieť).
Volajúci si musí zvýšené cyklovanie premietnuť do degradácie a výmeny článkov — inak by
sa tá istá kapacita započítala dvakrát.

MRK/RK headroom: nabíjanie ide cez to isté odberné miesto ako výroba, takže sa SČÍTAVA
so súbežným odberom. Bez `load_kw` sa strop berie ako celá RK — to pri OM s vysokou
základnou záťažou (napr. 24/7 prevádzka s odberom 2 100 kW pri RK 3 400 kW) nadhodnotí
obchodovaný objem aj výnos, a v realite by znamenalo prekročenie MRK s penále.
S `load_kw` sa v každom intervale nabíja len do voľnej kapacity (rk_kw − load_kw[i]).

Energetická bilancia (DC = energia v batérii):
  - uloženie e_dc → AC odber z gridu = e_dc / sqrt(rte)   (strata pri nabíjaní)
  - dodávka z e_dc → AC export do gridu = e_dc * sqrt(rte) (strata pri vybíjaní)
  pár je ziskový keď  p_vyboj * sqrt(rte) > p_nabij / sqrt(rte)  (t.j. p_vyboj/p_nabij > 1/rte).
"""
from __future__ import annotations
import numpy as np


def compute_merchant_arbitrage(
    spot_eur_mwh,            # list/array €/MWh, krok dt_h
    dt_h: float,
    bess_kwh: float,
    power_kw_ac: float,
    rk_kw: float,            # rezervovaná kapacita (import limit) kW
    export_kw: float,        # max export do gridu kW
    rte: float = 0.88,       # round-trip účinnosť
    organizer_fee_pct: float = 15.0,   # marža organizátora bilančnej skupiny
    imbalance_cost_eur_mwh: float = 0.0,    # BOD 3: odchýlková (imbalance) cena na obchodovaný objem; 0=nastaviť per kontrakt
    degradation_cost_eur_mwh: float = 0.0,  # BOD 3: cyklová degradačná rezerva na DC throughput; 0=nastaviť per projekt
    revenue_share_pct: float = 1.0,         # R2 #6: podiel klienta/Energovision z čistého merchant výnosu (1.0=celé)
    soc_min_frac: float = 0.05,
    soc_max_frac: float = 0.95,
    window: int = 96,        # dĺžka denného bloku (intervalov); 96=15-min, 24=hodinové
    max_cycles_per_day: float | None = 1.0,  # koľko plných cyklov denne; None = bez limitu
    load_kw=None,            # súbežný odber OM (kW) — nabíjanie sa vojde len do RK MÍNUS odber
) -> dict:
    spot = np.asarray(spot_eur_mwh, dtype=float)
    n = len(spot)
    empty = {"annual_profit_eur": 0.0, "throughput_mwh": 0.0, "dc_throughput_mwh": 0.0,
             "grid_charge_mwh": 0.0, "mrk_aware": False, "intervals_blocked_by_mrk": 0,
             "max_cycles_per_day": max_cycles_per_day, "equiv_cycles": 0.0,
             "sell_eur": 0.0, "buy_eur": 0.0, "fee_pct": organizer_fee_pct, "gross_eur": 0.0,
             "organizer_fee_eur": 0.0, "imbalance_eur": 0.0, "degradation_eur": 0.0,
             "merchant_net_eur": 0.0, "revenue_share_pct": revenue_share_pct}
    if n == 0 or bess_kwh <= 0 or power_kw_ac <= 0:
        return empty

    sqrt_rte = rte ** 0.5
    usable = bess_kwh * (soc_max_frac - soc_min_frac)              # DC kWh/deň max

    # Vybíjanie (export) je konštantné — limituje ho menič a max export.
    dis_ac_cap = min(power_kw_ac, export_kw) * dt_h               # AC export / interval
    if dis_ac_cap <= 0:
        return empty
    dis_dc_per = dis_ac_cap / sqrt_rte                           # DC odobraté / vybíjací interval

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
        return empty
    chg_dc_arr = chg_ac_arr * sqrt_rte                            # DC uložené / nabíjací interval

    sell_eur = 0.0; buy_eur = 0.0
    ac_export_total = 0.0; dc_throughput = 0.0
    ac_charge_total = 0.0     # koľko sa reálne odobralo zo siete na nabíjanie
    blocked_intervals = 0     # intervaly, kde headroom nestačil na plný výkon

    for start in range(0, n, window):
        w = spot[start:start + window]
        if len(w) < 2:
            continue
        # zostav nabíjacie a vybíjacie kandidátne hodiny (oddelené množiny — extrémy)
        order = np.argsort(w)                                     # rastúco podľa ceny
        # nabíjanie: najlacnejšie hodiny VRÁTANE záporného spotu (pri cene < 0 grid platí za
        # odber → náklad je záporné číslo, znižuje buy_eur); vybíjanie: najdrahšie hodiny.
        # Nezisk. páry aj tak zastaví profitability-break v cykle nižšie.
        # Kapacita nabíjania je per-interval (voľné miesto pod RK), preto sa berie z poľa.
        chg_w = chg_dc_arr[start:start + window]
        charge_cand = [(float(w[k]), float(chg_w[k])) for k in order if chg_w[k] > 1e-9]
        blocked_intervals += int((chg_w <= 1e-9).sum())
        if not charge_cand:
            continue
        discharge_cand = [(float(w[k]), dis_dc_per) for k in order[::-1]]

        ci = 0; di = 0
        # Denný energetický strop. Doteraz bol natvrdo 1 cyklus (rem_usable = usable),
        # takže batéria 482 kWh/206 kW, ktorá sa nabije za 2,3 h, obchodovala raz denne
        # a limit cyklov zadaný v UI sa úplne ignoroval.
        # None = bez limitu → strop dá už len výkon meniča, RK a max export.
        if max_cycles_per_day is None:
            rem_usable = dis_dc_per * len(w)
        else:
            rem_usable = usable * float(max_cycles_per_day)
        chg_left = charge_cand[ci][1] if charge_cand else 0.0
        dis_left = discharge_cand[di][1] if discharge_cand else 0.0
        while ci < len(charge_cand) and di < len(discharge_cand) and rem_usable > 1e-9:
            p_chg = charge_cand[ci][0]; p_dis = discharge_cand[di][0]
            # ziskovosť páru po stratách
            if p_dis * sqrt_rte <= p_chg / sqrt_rte:
                break                                            # ďalšie páry už nie sú ziskové
            move_dc = min(rem_usable, chg_left, dis_left)
            if move_dc <= 1e-9:
                break
            buy_eur  += (move_dc / sqrt_rte) * p_chg / 1000.0    # AC odber × cena
            sell_eur += (move_dc * sqrt_rte) * p_dis / 1000.0    # AC export × cena
            ac_charge_total += move_dc / sqrt_rte
            ac_export_total += move_dc * sqrt_rte
            dc_throughput   += move_dc
            rem_usable -= move_dc
            chg_left   -= move_dc
            dis_left   -= move_dc
            if chg_left <= 1e-9:
                ci += 1
                if ci < len(charge_cand): chg_left = charge_cand[ci][1]
            if dis_left <= 1e-9:
                di += 1
                if di < len(discharge_cand): dis_left = discharge_cand[di][1]

    gross = sell_eur - buy_eur
    # R2 #6 FIX: organizer_fee (marža organizátora BS) a revenue_share (podiel klienta) sú
    # ODDELENÉ koncepty. BOD 3: imbalance (na AC obchod) + degradačná rezerva (na DC throughput).
    organizer_fee = gross * (organizer_fee_pct / 100.0)
    imbalance = (ac_export_total / 1000.0) * float(imbalance_cost_eur_mwh)
    degradation = (dc_throughput / 1000.0) * float(degradation_cost_eur_mwh)
    merchant_net = gross - organizer_fee - imbalance - degradation      # čistý merchant výnos
    client_value = merchant_net * float(revenue_share_pct)              # podiel klienta (default 1.0=celé)
    equiv_cycles = (dc_throughput / bess_kwh) if bess_kwh > 0 else 0.0
    # DC throughput je podklad pre degradáciu a plán výmeny článkov
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
            "merchant_net_eur": round(merchant_net, 0), "revenue_share_pct": revenue_share_pct}
