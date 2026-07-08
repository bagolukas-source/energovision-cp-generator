"""S5b IZOLACIA PRICINY: S5 znovu s negative_spot_curtail=False + kvantifikacia
curtailed PV v S5/S4 behoch (PV energia co 'zmizne' pri spot<0)."""
import numpy as np
from harness import run_ems, pv_only_baseline, batt_value, real_spot

N = 8760
hours = np.arange(N) % 24
load = np.where((hours >= 18) & (hours <= 23), 60.0, 8.0)
shape = np.clip(np.sin(np.pi * (hours - 6) / 13.0), 0, None) * 0.8
shape[(hours < 6) | (hours > 19)] = 0.0
spot = real_spot()
print("negative spot hodin v 2025 CSV:", int((spot < 0).sum()), " min:", spot.min())

for curtail in (True, False):
    print(f"--- negative_spot_curtail={curtail} ---")
    vals_t = []
    for kwp in [0, 50, 100, 150]:
        pv = shape * kwp
        base = pv_only_baseline(load, pv, spot)
        iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=100.0, negative_spot_curtail=curtail)
        lost_pv = s.pv_total_kwh - s.pv_to_load_kwh - s.pv_to_bat_kwh - s.pv_to_grid_kwh
        v_total = s.sav_total_eur - base['sav_total']
        vals_t.append(v_total)
        print(f"PV {kwp:>3}: delta_total={v_total:8.0f}  sav_bess+arb={batt_value(s):8.0f}"
              f"  curtailed_PV={lost_pv/1000:7.2f} MWh (={lost_pv*0.06:6.0f} EUR exportu v baseline)")
    bad = [(a, b) for a, b in zip(vals_t, vals_t[1:]) if b < a - 1.0]
    print("delta_total monotonne:", "OK" if not bad else f"POKLES {bad}")
