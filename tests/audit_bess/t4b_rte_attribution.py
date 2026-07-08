"""S4b ATRIBUCIA mikropoklesu 200->400 kWh v t4: dyn. RTE vs staticka RTE.
+ to iste pre parity dip 100->150 kWp v t5 (BESS 100)."""
import numpy as np
from harness import run_ems, batt_value, real_spot, pv_only_baseline

N = 8760
hours = np.arange(N) % 24
spot = real_spot()

# t4 profil
day = (hours >= 8) & (hours <= 17)
load4 = np.where(day, 40.0, 10.0)
pv4 = np.where(day, 80.0, 0.0)
print("=== S4b: t4 profil, BESS 200 vs 400 kWh ===")
for dyn in (True, False):
    vals = []
    for kwh in (200.0, 400.0):
        iv, s, bat, ems = run_ems(load4, pv4, spot, bess_kwh=kwh, use_dynamic_rte=dyn)
        vals.append(batt_value(s))
    d = vals[1] - vals[0]
    print(f"dynamic_rte={dyn}: 200kWh={vals[0]:8.1f}  400kWh={vals[1]:8.1f}  delta={d:+8.1f} ({d/vals[0]*100:+.2f} %)")

# t5 parity dip 100 -> 150 kWp
load5 = np.where((hours >= 18) & (hours <= 23), 60.0, 8.0)
shape = np.clip(np.sin(np.pi * (hours - 6) / 13.0), 0, None) * 0.8
shape[(hours < 6) | (hours > 19)] = 0.0
print("=== S4b: t5 profil, PV 100 vs 150 kWp (BESS 100, parity baseline) ===")
for dyn in (True, False):
    vals = []
    for kwp in (100, 150):
        pv = shape * kwp
        b = pv_only_baseline(load5, pv, spot)
        iv, s, bat, ems = run_ems(load5, pv, spot, bess_kwh=100.0, use_dynamic_rte=dyn)
        vals.append(s.sav_total_eur - b["sav_total"])
    d = vals[1] - vals[0]
    print(f"dynamic_rte={dyn}: 100kWp={vals[0]:8.1f}  150kWp={vals[1]:8.1f}  delta={d:+8.1f} ({d/vals[0]*100:+.2f} %)")
