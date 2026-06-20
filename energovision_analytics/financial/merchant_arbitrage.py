"""Merchant (grid-to-grid) batériová arbitráž — podpora bilančnej skupiny.

Batéria sa NEdimenzuje na samospotrebu, ale obchoduje komoditu cez bilančnú skupinu:
nabíja z gridu v lacných hodinách (ohraničené RK), vybíja DO gridu v drahých (ohraničené
max exportom). Hodnota = spotový spread × účinnosť, mínus marža organizátora.

Čistý, samostatný model — needituje EMS samospotreby, takže žiadne dvojité počítanie.

Dispatch: po DENNÝCH blokoch (window intervalov). V rámci dňa páruje NAJLACNEJŠIE hodiny
(nabíjanie) s NAJDRAHŠÍMI (vybíjanie) — greedy podľa cenového poradia, nie chronologicky.
Tým je zisk monotónny v export/RK/výkon limitoch (viac kapacity = viac zisku) a zodpovedá
tomu, ako reálny operátor vyberá najlepšie hodiny. Batéria sa každý deň vyprázdni (denný cyklus).

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
    soc_min_frac: float = 0.05,
    soc_max_frac: float = 0.95,
    window: int = 96,        # dĺžka denného bloku (intervalov); 96=15-min, 24=hodinové
) -> dict:
    spot = np.asarray(spot_eur_mwh, dtype=float)
    n = len(spot)
    empty = {"annual_profit_eur": 0.0, "throughput_mwh": 0.0, "equiv_cycles": 0.0,
             "sell_eur": 0.0, "buy_eur": 0.0, "fee_pct": organizer_fee_pct, "gross_eur": 0.0}
    if n == 0 or bess_kwh <= 0 or power_kw_ac <= 0:
        return empty

    sqrt_rte = rte ** 0.5
    usable = bess_kwh * (soc_max_frac - soc_min_frac)              # DC kWh/deň max

    # Per-interval kapacity (konštantné):
    chg_ac_cap = min(power_kw_ac, rk_kw) * dt_h                    # AC odber / interval
    dis_ac_cap = min(power_kw_ac, export_kw) * dt_h               # AC export / interval
    if chg_ac_cap <= 0 or dis_ac_cap <= 0:
        return empty
    chg_dc_per = chg_ac_cap * sqrt_rte                            # DC uložené / nabíjací interval
    dis_dc_per = dis_ac_cap / sqrt_rte                           # DC odobraté / vybíjací interval

    sell_eur = 0.0; buy_eur = 0.0
    ac_export_total = 0.0; dc_throughput = 0.0

    for start in range(0, n, window):
        w = spot[start:start + window]
        if len(w) < 2:
            continue
        # zostav nabíjacie a vybíjacie kandidátne hodiny (oddelené množiny — extrémy)
        order = np.argsort(w)                                     # rastúco podľa ceny
        # nabíjanie: najlacnejšie hodiny (cena ≥ 0); vybíjanie: najdrahšie hodiny
        charge_cand = [(float(w[k]), chg_dc_per) for k in order if w[k] >= 0]
        discharge_cand = [(float(w[k]), dis_dc_per) for k in order[::-1]]

        ci = 0; di = 0
        rem_usable = usable
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
    net = gross * (1.0 - organizer_fee_pct / 100.0)              # (100−fee)% zákazníkovi
    equiv_cycles = (dc_throughput / bess_kwh) if bess_kwh > 0 else 0.0
    return {"annual_profit_eur": round(net, 0),
            "throughput_mwh": round(ac_export_total / 1000.0, 1),
            "equiv_cycles": round(equiv_cycles, 0),
            "sell_eur": round(sell_eur, 0), "buy_eur": round(buy_eur, 0),
            "fee_pct": organizer_fee_pct, "gross_eur": round(gross, 0)}
