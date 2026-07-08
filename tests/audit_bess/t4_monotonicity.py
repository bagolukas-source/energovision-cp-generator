"""S4 MONOTONNOST KAPACITY: realny spot 2025, load 40 kW den (8-17) / 10 kW noc,
PV 80 kW den (2x load). Baterie [0,25,50,100,200,400]. Hodnota bat neklesajuca."""
import numpy as np
from harness import run_ems, pv_only_baseline, batt_value, real_spot

N = 8760
hours = np.arange(N) % 24
day = (hours >= 8) & (hours <= 17)
load = np.where(day, 40.0, 10.0)
pv = np.where(day, 80.0, 0.0)
spot = real_spot()

base = pv_only_baseline(load, pv, spot)
print("=== S4 MONOTONNOST KAPACITY (realny spot) ===")
print(f"baseline PV-only sav_total = {base['sav_total']:.0f} EUR")
rows = []
for kwh in [25, 50, 100, 200, 400]:
    iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=float(kwh))
    v_streams = batt_value(s)
    v_total = s.sav_total_eur - base['sav_total']
    rows.append((kwh, v_streams, v_total, s.sav_arbitrage_eur, s.sav_bess_self_cons_eur,
                 s.sav_solar_export_eur, s.bat_efc))
    print(f"BESS {kwh:>4} kWh: sav_bess+arb={v_streams:8.0f}  delta_total_vs_PVonly={v_total:8.0f}"
          f"  (arb={s.sav_arbitrage_eur:7.0f} self={s.sav_bess_self_cons_eur:7.0f}"
          f" export={s.sav_solar_export_eur:6.0f} efc={s.bat_efc:.0f})")

vals_streams = [0.0] + [r[1] for r in rows]
vals_total = [0.0] + [r[2] for r in rows]
def check(vs, name):
    bad = [(a, b) for a, b in zip(vs, vs[1:]) if b < a - 1.0]
    print(f"{name}: {'neklesajuca OK' if not bad else 'POKLES: ' + str(bad)}")
    return not bad
ok1 = check(vals_streams, "sav_bess+sav_arb")
ok2 = check(vals_total, "delta sav_total vs PV-only")
print("VERDIKT:", "OK" if (ok1 and ok2) else "CHYBA")
