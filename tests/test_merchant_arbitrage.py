"""Sanity testy merchant arbitráže — bežia bez pytestu: `python3 tests/test_merchant_arbitrage.py`.

Chránia opravu z 9/2026 (kalibrácia voči Energe): prah ziskovosti páru musí obsahovať
degradáciu, odchýlku aj maržu organizátora, nie len round-trip stratu.
"""
import sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from energovision_analytics.financial.merchant_arbitrage import (  # noqa: E402
    compute_merchant_arbitrage, degradation_reserve_eur_mwh)

# denný profil: 8 h lacno (20 €), 8 h stredne (100 €), 8 h draho (200 €)
DAY = [20.0] * 32 + [100.0] * 32 + [200.0] * 32
SPOT = np.array(DAY * 365, dtype=float)
BASE = dict(spot_eur_mwh=SPOT, dt_h=0.25, bess_kwh=1000.0, power_kw_ac=500.0,
            rk_kw=2000.0, export_kw=2000.0, organizer_fee_pct=15.0, window=96)

fails = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


# 1) rezerva na opotrebenie — z ceny ČLÁNKOV (40 % BESS capexu), nie celej inštalácie
d6 = degradation_reserve_eur_mwh(318, 6000, 0.90)
check("degradačná rezerva 318 €/kWh @6000 cy ≈ 23,6 €/MWh", abs(d6 - 23.6) < 0.2, f"{d6:.1f}")
check("dvojnásobná záruka = polovičná rezerva",
      abs(degradation_reserve_eur_mwh(318, 12000, 0.90) - d6 / 2) < 0.1)
check("nulové vstupy nespadnú", degradation_reserve_eur_mwh(0, 0, 0) == 0.0)

# 2) degradácia znižuje výnos aj obchodovaný objem (nielen odpočet na konci)
r0 = compute_merchant_arbitrage(**BASE, degradation_cost_eur_mwh=0.0, max_cycles_per_day=None)
r1 = compute_merchant_arbitrage(**BASE, degradation_cost_eur_mwh=d6, max_cycles_per_day=None)
check("degradácia znižuje čistý výnos", r1["merchant_net_eur"] < r0["merchant_net_eur"],
      f"{r0['merchant_net_eur']:,.0f} → {r1['merchant_net_eur']:,.0f}")
check("degradácia znižuje aj throughput (prah, nie len odpočet)",
      r1["dc_throughput_mwh"] <= r0["dc_throughput_mwh"],
      f"{r0['dc_throughput_mwh']:,.0f} → {r1['dc_throughput_mwh']:,.0f} MWh")

# 2b) pri TESNOM spreade prah reálne odreže obchody (nie len zníži zisk na konci)
# striedavo: úzky deň (50→85, po stratách a marži nepokryje opotrebenie) a široký (50→200)
_narrow = [50.0] * 48 + [85.0] * 48
_wide = [50.0] * 48 + [200.0] * 48
TIGHT = np.array((_narrow + _wide) * 182 + _narrow, dtype=float)
tb = dict(BASE); tb["spot_eur_mwh"] = TIGHT
t0 = compute_merchant_arbitrage(**tb, degradation_cost_eur_mwh=0.0, max_cycles_per_day=None)
t1 = compute_merchant_arbitrage(**tb, degradation_cost_eur_mwh=d6, max_cycles_per_day=None)
check("tesný spread: degradácia odreže objem", t1["dc_throughput_mwh"] < t0["dc_throughput_mwh"],
      f"{t0['dc_throughput_mwh']:,.0f} → {t1['dc_throughput_mwh']:,.0f} MWh")
check("tesný spread: menej obchodovaných dní", t1["days_traded"] < t0["days_traded"],
      f"{t0['days_traded']} → {t1['days_traded']} dní")

# 3) neúnosná degradácia zastaví obchodovanie úplne
r2 = compute_merchant_arbitrage(**BASE, degradation_cost_eur_mwh=500.0, max_cycles_per_day=None)
check("degradácia nad spread → žiadny obchod",
      r2["dc_throughput_mwh"] == 0.0 and r2["merchant_net_eur"] == 0.0)

# 4) breakeven spread rastie s nákladmi a je 0 pri nulových nákladoch
check("breakeven pri nulových nákladoch = 0", r0["breakeven_spread_eur_mwh"] == 0.0)
check("breakeven rastie s degradáciou",
      r1["breakeven_spread_eur_mwh"] > r0["breakeven_spread_eur_mwh"],
      f"{r1['breakeven_spread_eur_mwh']:.1f} €/MWh")

# 5) monotónnosť — väčšia batéria nesmie zarobiť menej
big = dict(BASE); big["bess_kwh"] = 2000.0
rb = compute_merchant_arbitrage(**big, degradation_cost_eur_mwh=d6, max_cycles_per_day=None)
check("2× kapacita nezarobí menej", rb["merchant_net_eur"] >= r1["merchant_net_eur"],
      f"{r1['merchant_net_eur']:,.0f} → {rb['merchant_net_eur']:,.0f}")

# 6) MRK headroom — plná záťaž pod RK zastaví nabíjanie
full = compute_merchant_arbitrage(**BASE, degradation_cost_eur_mwh=0.0, max_cycles_per_day=None,
                                  load_kw=np.full(len(SPOT), 2000.0))
check("odber na úrovni RK → nulový headroom → žiadny obchod",
      full["dc_throughput_mwh"] == 0.0 and full["mrk_aware"] is True)

# 7) odchýlka sa správa ako degradácia (vstupuje do prahu)
ri = compute_merchant_arbitrage(**BASE, imbalance_cost_eur_mwh=30.0, max_cycles_per_day=None)
check("odchýlka znižuje výnos", ri["merchant_net_eur"] < r0["merchant_net_eur"],
      f"{ri['merchant_net_eur']:,.0f}")

# 8) spätná kompatibilita — bez nákladov je výsledok ako pred opravou
check("bez nákladov obchoduje každý deň", r0["days_traded"] == r0["days_total"] == 365)

print()
if fails:
    print(f"NEPREŠLO {len(fails)}: {', '.join(fails)}")
    sys.exit(1)
print("Všetky kontroly prešli.")
