"""S6 STRES CYKLOV: max_efc [50,300,1000] na 2-cenovom dni (arbitrazne idealne).
Throughput ma skalovat s budgetom (engine reze pri 90 % budgetu), EUR/MWh >= min spread 30."""
import numpy as np
from harness import run_ems, batt_value

N = 8760
load = np.full(N, 50.0)
pv = np.zeros(N)
spot = np.tile(np.array([20.0]*12 + [220.0]*12), 365)

print("=== S6 STRES CYKLOV (2-cenovy den, BESS 96/48) ===")
usable = 96 * 0.9
res = []
for efc in [50, 300, 1000]:
    iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=96.0, max_efc_per_year=float(efc))
    v = batt_value(s)
    thr_mwh = s.bat_discharge_total_kwh / 1000
    eur_mwh = v / thr_mwh if thr_mwh > 0 else 0
    exp_efc = min(0.9 * efc, 365)  # engine stopne pri <10 % budgetu; max 1 cyklus/den
    res.append((efc, s.bat_efc, thr_mwh, v, eur_mwh))
    print(f"max_efc={efc:>5}: efc_used={s.bat_efc:7.1f} (ocak ~{exp_efc:.0f})"
          f"  discharge={thr_mwh:7.2f} MWh  value={v:8.0f} EUR  value/MWh={eur_mwh:7.1f} EUR")

ok_scale = res[0][1] < res[1][1] < res[2][1] and res[0][2] < res[1][2] < res[2][2]
ok_value = all(r[4] >= 30.0 for r in res)
print("throughput skaluje s budgetom:", "OK" if ok_scale else "CHYBA")
print("value/MWh >= min spread 30:", "OK" if ok_value else "CHYBA")
print("VERDIKT:", "OK" if (ok_scale and ok_value) else "CHYBA")
