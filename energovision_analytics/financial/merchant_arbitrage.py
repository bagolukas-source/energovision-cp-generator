"""Merchant (grid-to-grid) batériová arbitráž — podpora bilančnej skupiny.

Batéria sa NEdimenzuje na samospotrebu, ale obchoduje komoditu cez bilančnú skupinu:
nabíja z gridu v lacných hodinách (ohraničené RK), vybíja DO gridu v drahých (ohraničené
max exportom). Hodnota = spotový spread × účinnosť, mínus marža organizátora.

Čistý, samostatný model — needituje EMS samospotreby, takže žiadne dvojité počítanie.
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
    window: int = 96,        # look-ahead okno (1 deň pri 15-min)
) -> dict:
    spot = np.asarray(spot_eur_mwh, dtype=float)
    n = len(spot)
    if n == 0 or bess_kwh <= 0 or power_kw_ac <= 0:
        return {"annual_profit_eur": 0.0, "throughput_mwh": 0.0, "equiv_cycles": 0.0,
                "sell_eur": 0.0, "buy_eur": 0.0, "fee_pct": organizer_fee_pct}

    usable = bess_kwh * (soc_max_frac - soc_min_frac)
    soc = bess_kwh * soc_min_frac                       # kWh nad min
    soc_floor = bess_kwh * soc_min_frac
    soc_cap = bess_kwh * soc_max_frac
    charge_e_cap = power_kw_ac * dt_h                    # kWh / interval (výkon)
    rk_e_cap = rk_kw * dt_h                              # import limit / interval
    exp_e_cap = export_kw * dt_h                         # export limit / interval

    sqrt_rte = rte ** 0.5
    sell_eur = 0.0; buy_eur = 0.0; throughput = 0.0
    for i in range(n):
        lo = i; hi = min(n, i + window)
        w = spot[lo:hi]
        p_lo = float(np.percentile(w, 25)); p_hi = float(np.percentile(w, 75))
        s = spot[i]
        if s <= p_lo and soc < soc_cap and s >= 0:
            # nabíjaj z gridu (lacno) — do výkonu aj RK
            room = soc_cap - soc
            chg = min(charge_e_cap, rk_e_cap, room / sqrt_rte)   # AC odber väčší kvôli strate
            stored = chg * sqrt_rte
            soc += stored
            buy_eur += chg * s / 1000.0
        elif s >= p_hi and soc > soc_floor:
            # vybíjaj DO gridu (draho) — do výkonu aj export limitu
            avail = soc - soc_floor
            dis_dc = min(charge_e_cap, avail)
            dis_ac = dis_dc * sqrt_rte                            # AC dodávka menšia kvôli strate
            dis_ac = min(dis_ac, exp_e_cap)
            dis_dc = dis_ac / sqrt_rte
            soc -= dis_dc
            sell_eur += dis_ac * s / 1000.0
            throughput += dis_ac

    gross = sell_eur - buy_eur
    net = gross * (1.0 - organizer_fee_pct / 100.0)      # 85 % zákazníkovi
    equiv_cycles = (throughput / bess_kwh) if bess_kwh > 0 else 0.0
    return {"annual_profit_eur": round(net, 0), "throughput_mwh": round(throughput / 1000.0, 1),
            "equiv_cycles": round(equiv_cycles, 0), "sell_eur": round(sell_eur, 0),
            "buy_eur": round(buy_eur, 0), "fee_pct": organizer_fee_pct,
            "gross_eur": round(gross, 0)}
