"""S3 CISTY PV POSUN: PV 60 kW @10-15h (360 kWh/d), load 70 kW @18-23h, flat spot.
Bateria 400 kWh (usable 360) pojme dennu vyrobu. Ocak: export ~0, samospotreba ~ RTE."""
import numpy as np
from harness import run_ems

N = 8760
hours = np.arange(N) % 24
load = np.where((hours >= 18) & (hours <= 23), 70.0, 0.0)
pv = np.where((hours >= 10) & (hours <= 15), 60.0, 0.0)
spot = np.full(N, 100.0)

iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=400.0, max_efc_per_year=500)

print("=== S3 CISTY PV POSUN ===")
print(f"PV total = {s.pv_total_kwh/1000:.1f} MWh  load = {s.load_total_kwh/1000:.1f} MWh")
print(f"pv_to_load(direct) = {s.pv_to_load_kwh:.1f} kWh  pv_to_bat = {s.pv_to_bat_kwh/1000:.2f} MWh  pv_to_grid(export) = {s.pv_to_grid_kwh:.1f} kWh")
print(f"bat_discharge = {s.bat_discharge_total_kwh/1000:.2f} MWh  pv_via_bat = {s.pv_to_load_via_bat_kwh/1000:.2f} MWh")
print(f"samospotreba = {s.samospotreba_pct:.2f} %  samostatnost = {s.samostatnost_pct:.2f} %  efc = {s.bat_efc:.1f}")
exp_pct = s.pv_to_grid_kwh / s.pv_total_kwh * 100
print(f"export share = {exp_pct:.2f} % PV")
# ocakavanie: samospotreba priblizne RTE (85-90 %), export < 2 %
ok = (80.0 <= s.samospotreba_pct <= 92.0) and exp_pct < 2.0
print("VERDIKT:", "OK" if ok else "CHYBA")
