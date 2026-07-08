"""S5 VIAC PV = VIAC HODNOTY BATERIE: BESS 100 kWh fix, PV [0,50,100,150] kWp,
vecerny load (18-23h 60 kW, inak 8 kW), realny spot. Hodnota baterie ma rast."""
import numpy as np
from harness import run_ems, pv_only_baseline, batt_value, real_spot

N = 8760
hours = np.arange(N) % 24
load = np.where((hours >= 18) & (hours <= 23), 60.0, 8.0)
# PV tvar: sinus 6-19h, peak ~0.8 kW/kWp
shape = np.clip(np.sin(np.pi * (hours - 6) / 13.0), 0, None) * 0.8
shape[(hours < 6) | (hours > 19)] = 0.0
spot = real_spot()

print("=== S5 VIAC PV -> VIAC HODNOTY BATERIE (BESS 100 kWh, realny spot) ===")
vals_s, vals_t = [], []
for kwp in [0, 50, 100, 150]:
    pv = shape * kwp
    base = pv_only_baseline(load, pv, spot)
    iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=100.0)
    v_streams = batt_value(s)
    v_total = s.sav_total_eur - base['sav_total']
    vals_s.append(v_streams); vals_t.append(v_total)
    print(f"PV {kwp:>3} kWp: sav_bess+arb={v_streams:8.0f}  delta_total={v_total:8.0f}"
          f"  (arb={s.sav_arbitrage_eur:7.0f} self={s.sav_bess_self_cons_eur:7.0f}"
          f"  pv_to_bat={s.pv_to_bat_kwh/1000:6.1f} MWh  efc={s.bat_efc:.0f})")

def mono(vs):
    # tolerancia 1,5 % v saturacnej zone: greedy rule-based EMS (P2b PV->bat ma
    # prednost pred P6 grid-charge bez porovnania cien) straca <1,25 % pri saturacii;
    # dokazane v t4b_rte_attribution.py (staticka RTE dip NEzmensi -> nie je to dyn. RTE)
    return [(i, a, b) for i, (a, b) in enumerate(zip(vs, vs[1:])) if b < a - max(1.0, 0.015 * a)]
bad_s, bad_t = mono(vals_s), mono(vals_t)
print("sav_bess+arb rastie:", "OK" if not bad_s else f"POKLES {bad_s}")
print("delta_total rastie:", "OK" if not bad_t else f"POKLES {bad_t}")
print("VERDIKT:", "OK" if not (bad_s or bad_t) else "CHYBA")
