"""S1 NULOVY TEST: plochý spot 100 €/MWh, žiadne PV, flat load 50 kW.
Batéria 96 kWh nesmie nič zarobiť ani pokaziť vs bess=0. Očak. |delta| <= 1 €."""
import numpy as np
from harness import run_ems, pv_only_baseline, actual_cost, batt_value

N = 8760
load = np.full(N, 50.0)
pv = np.zeros(N)
spot = np.full(N, 100.0)

base = pv_only_baseline(load, pv, spot)  # bez batérie: úspora=0, cost = load*retail
iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=96.0)

cost_bess = actual_cost(iv)
print("=== S1 NULOVY TEST (flat spot 100, no PV) ===")
print(f"baseline(no BESS): sav_total={base['sav_total']:.2f} EUR  cost={base['cost_eur']:.2f} EUR")
print(f"BESS96: sav_total={s.sav_total_eur:.2f}  sav_bess={s.sav_bess_self_cons_eur:.2f}  sav_arb={s.sav_arbitrage_eur:.2f}")
print(f"BESS96: charge={s.bat_charge_total_kwh:.2f} kWh  discharge={s.bat_discharge_total_kwh:.2f} kWh  efc={s.bat_efc:.3f}")
print(f"BESS96: cost={cost_bess:.2f} EUR  n_charge_grid={s.n_state_charge_grid}  n_discharge={s.n_state_discharge}")
print(f"DELTA sav (claimed batt value) = {s.sav_total_eur - base['sav_total']:.2f} EUR")
print(f"DELTA cost (real bill diff, base-bess) = {base['cost_eur'] - cost_bess:.2f} EUR")
print(f"init SoC = {96*0.5:.1f} kWh, final SoC = {bat.soc_kwh:.2f} kWh")
verdict = "OK" if abs(s.sav_total_eur - base['sav_total']) <= 1.0 else "CHYBA"
print("VERDIKT:", verdict)
