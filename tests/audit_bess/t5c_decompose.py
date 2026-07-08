"""S5c IZOLACIA: bottom-up rozklad delta_total po streamoch pre PV 50/100/150,
s DVOMA baselinami: legacy (bez curtailu — ako doterajsi t5/harness) vs parity
(curtail neg. spot + MRK clip — ako fixnuty generator._build_pv_only_summary).
+ kontrola (b): PV->bat pri spot<0, MRK clip, peak stream."""
import numpy as np
from harness import run_ems, pv_only_baseline, real_spot, ADD_ON

N = 8760
hours = np.arange(N) % 24
load = np.where((hours >= 18) & (hours <= 23), 60.0, 8.0)
shape = np.clip(np.sin(np.pi * (hours - 6) / 13.0), 0, None) * 0.8
shape[(hours < 6) | (hours > 19)] = 0.0
spot = real_spot()

def baseline_parity(load, pv, spot, mrk_kw=200.0, export_price=0.06):
    """Replika FIXNUTEHO _build_pv_only_summary (curtail spot<0 + MRK clip)."""
    pv_to_load = np.minimum(pv, load)
    pv_to_grid = np.maximum(pv - load, 0.0).copy()
    curt = float(pv_to_grid[spot < 0].sum())
    pv_to_grid[spot < 0] = 0.0
    over = np.maximum(pv_to_grid - mrk_kw, 0.0)
    curt += float(over.sum())
    pv_to_grid = np.minimum(pv_to_grid, mrk_kw)
    tarif = (spot + ADD_ON) / 1000.0
    return {"sav_solar_self": float((pv_to_load * tarif).sum()),
            "sav_export": float(pv_to_grid.sum() * export_price),
            "sav_total": float((pv_to_load * tarif).sum() + pv_to_grid.sum() * export_price),
            "export_kwh": float(pv_to_grid.sum()), "curtailed_kwh": curt}

print("=== S5c ROZKLAD DELTY PO STREAMOCH (BESS 100 kWh, realny spot 2025) ===")
d_leg, d_par = [], []
for kwp in [50, 100, 150]:
    pv = shape * kwp
    b_leg = pv_only_baseline(load, pv, spot)   # legacy replika (bez curtailu)
    b_par = baseline_parity(load, pv, spot)    # parity replika (= fixnuty generator)
    iv, s, bat, ems = run_ems(load, pv, spot, bess_kwh=100.0)

    d_self = s.sav_solar_self_cons_eur - b_leg["sav_solar_self"]
    d_exp_leg = s.sav_solar_export_eur - b_leg["sav_export"]
    d_exp_par = s.sav_solar_export_eur - b_par["sav_export"]
    streams = dict(bess=s.sav_bess_self_cons_eur, arb=s.sav_arbitrage_eur,
                   peak=s.sav_peak_shaving_eur, mrk=s.sav_mrk_penalty_avoided_eur)
    tot_leg = d_self + d_exp_leg + sum(streams.values())
    tot_par = d_self + d_exp_par + sum(streams.values())
    d_leg.append(tot_leg); d_par.append(tot_par)

    # (b) kontroly: PV->bat pocas spot<0; curtail v BESS behu vs v baseline
    neg = spot < 0
    pv2bat_neg = sum(i.pv_to_bat_kwh for i, m in zip(iv, neg) if m)
    curt_engine = float(getattr(s, "pv_curtailed_kwh", 0.0) or 0.0)

    print(f"\nPV {kwp} kWp:")
    print(f"  d_solar_self = {d_self:+9.2f}  (ocak ~0)")
    print(f"  d_export legacy = {d_exp_leg:+9.0f}   d_export parity = {d_exp_par:+9.0f}")
    print(f"  bess_self={streams['bess']:8.0f}  arb={streams['arb']:7.0f}  peak={streams['peak']:5.0f}  mrk={streams['mrk']:5.0f}")
    print(f"  DELTA_TOTAL legacy = {tot_leg:8.0f}   DELTA_TOTAL parity = {tot_par:8.0f}")
    print(f"  krizova kontrola vs summary: legacy={s.sav_total_eur - b_leg['sav_total']:8.0f} parity={s.sav_total_eur - b_par['sav_total']:8.0f}")
    print(f"  (b) curtail: baseline={b_par['curtailed_kwh']/1000:6.2f} MWh  engine={curt_engine/1000:6.2f} MWh"
          f"  PV->bat pri spot<0 = {pv2bat_neg/1000:5.2f} MWh")

def mono(v): return [(round(a), round(b)) for a, b in zip(v, v[1:]) if b < a - 1]
print("\nlegacy baseline delta:", [round(x) for x in d_leg], "->", "POKLES" if mono(d_leg) else "monotonne OK")
print("parity baseline delta:", [round(x) for x in d_par], "->", "POKLES" if mono(d_par) else "monotonne OK")
