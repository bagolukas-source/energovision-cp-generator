"""S7 INVARIANT KONZERVACIE: PV+import = load+export+dSoC+straty. Odchylka > 0.5 % = nalez.
Scenare: A=arbitraz(2-cenovy), B=PV posun, C=realny spot+prebytky (100 kWh).
Bonus D: to iste ako B ale timestep 15 min (podozrenie kW/kWh mix v rule_based.py)."""
import numpy as np
from harness import run_ems, conservation, real_spot

def report(name, load, pv, spot, kwh, ts_min=60, **cfg):
    iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=kwh, timestep_min=ts_min, **cfg)
    init_soc = kwh * 0.5
    c = conservation(iv, s, bat, init_soc)
    print(f"[{name}] PV={s.pv_total_kwh/1000:9.2f} imp={s.grid_import_kwh/1000:9.2f} "
          f"load={s.load_total_kwh/1000:9.2f} exp={s.grid_export_kwh/1000:9.2f} "
          f"dSoC={c['d_soc']:8.1f} straty={c['losses']/1000:7.2f} MWh")
    print(f"     resid={c['resid']:10.2f} kWh = {c['resid_pct']:8.4f} %  "
          f"flow_resid_pv={c['flow_resid_pv']:10.2f} kWh  flow_resid_load={c['flow_resid_load']:10.2f} kWh")
    verdict = "OK" if abs(c['resid_pct']) <= 0.5 else "CHYBA"
    print(f"     VERDIKT: {verdict}")
    return c

N = 8760
hours = np.arange(N) % 24
print("=== S7 INVARIANT KONZERVACIE ===")

# A: arbitraz
report("A arbitraz 2-cen", np.full(N, 50.0), np.zeros(N),
       np.tile(np.array([20.0]*12 + [220.0]*12), 365), 96.0, max_efc_per_year=450)

# B: PV posun
loadB = np.where((hours >= 18) & (hours <= 23), 70.0, 0.0)
pvB = np.where((hours >= 10) & (hours <= 15), 60.0, 0.0)
report("B PV posun      ", loadB, pvB, np.full(N, 100.0), 400.0, max_efc_per_year=500)

# C: realny spot + prebytky
day = (hours >= 8) & (hours <= 17)
report("C real spot     ", np.where(day, 40.0, 10.0), np.where(day, 80.0, 0.0),
       real_spot(), 100.0)

# D: BONUS — scenar B pri 15-min timestepe (kontrola jednotiek kW vs kWh)
N4 = 8760 * 4
hours4 = (np.arange(N4) // 4) % 24
loadD = np.where((hours4 >= 18) & (hours4 <= 23), 70.0, 0.0)
pvD = np.where((hours4 >= 10) & (hours4 <= 15), 60.0, 0.0)
report("D = B @ 15 min  ", loadD, pvD, np.full(N4, 100.0), 400.0, ts_min=15, max_efc_per_year=500)
