"""S2 UCEBNICOVA ARBITRAZ: 12h @20, 12h @220 €/MWh, load 50 kW, no PV, BESS 96/48.
Manual: AC_out/den=usable*eta_dis, AC_in=usable/eta_ch; profit=AC_out*ret220 - AC_in*ret20; x365.
Tolerancia 15 %."""
import numpy as np
from harness import run_ems, batt_value, retail_kwh, actual_cost, pv_only_baseline

N = 8760
load = np.full(N, 50.0)
pv = np.zeros(N)
day = np.array([20.0]*12 + [220.0]*12)
spot = np.tile(day, 365)

iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=96.0, max_efc_per_year=450)

# manual analytic (base RTE 0.88, bez degradacie)
rte = 0.88; eta = rte ** 0.5
usable = 96 * 0.9
ac_out = usable * eta          # kWh/den
ac_in = usable / eta
profit_day = ac_out * retail_kwh(220.0) - ac_in * retail_kwh(20.0)
expected = profit_day * 365

measured = batt_value(s)
base = pv_only_baseline(load, pv, spot)
cost_delta = base['cost_eur'] - actual_cost(iv)

print("=== S2 UCEBNICOVA ARBITRAZ ===")
print(f"manual: AC_out/d={ac_out:.1f} AC_in/d={ac_in:.1f} profit/d={profit_day:.2f} -> rok={expected:.0f} EUR")
print(f"engine: sav_arb={s.sav_arbitrage_eur:.0f} sav_bess_self={s.sav_bess_self_cons_eur:.0f} spolu={measured:.0f} EUR")
print(f"engine: charge={s.bat_charge_total_kwh/1000:.1f} MWh discharge={s.bat_discharge_total_kwh/1000:.1f} MWh efc={s.bat_efc:.1f}")
print(f"engine: n_charge_grid={s.n_state_charge_grid} n_discharge={s.n_state_discharge}")
print(f"real bill delta (base - bess) = {cost_delta:.0f} EUR")
dev = (measured - expected) / expected * 100
dev_cost = (cost_delta - expected) / expected * 100
print(f"odchylka claimed vs manual: {dev:+.1f} %   real-bill vs manual: {dev_cost:+.1f} %")
print("VERDIKT:", "OK" if abs(dev) <= 15 else "CHYBA")
