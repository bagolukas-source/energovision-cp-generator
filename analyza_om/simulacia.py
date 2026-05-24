#!/usr/bin/env python3
"""
Hodinová bilančná simulácia FVE + BESS.

PV model: geometrický slnečný (lat/lon), kalibrovaný na ročný výnos.
BESS dispatch: priorita FVE → load → BESS → export.

Výstup: JSON so výsledkami pre každý variant (ratio samosp, export, import, ...).

Príklad:
    python simulacia.py --profile profil_h.csv --lat 48.4 --lon 17.6 \
                        --variant "A:fve=92,tilt=25,az=0,bess_kwh=0,bess_kw=0" \
                        --variant "B:fve=92,tilt=25,az=0,bess_kwh=200,bess_kw=100" \
                        --output sim_results.json
"""
import argparse
import json
import math
import sys
from pathlib import Path
import numpy as np
import pandas as pd

# Mesačné kalibračné koeficienty pre Slovensko
MF_SK = {1: 0.50, 2: 0.65, 3: 0.85, 4: 1.05, 5: 1.15, 6: 1.20,
         7: 1.20, 8: 1.10, 9: 0.95, 10: 0.75, 11: 0.50, 12: 0.40}

# Pre Oravu (chladnejšia, viac oblačnosti)
MF_NORTH = {1: 0.40, 2: 0.55, 3: 0.80, 4: 1.00, 5: 1.10, 6: 1.18,
            7: 1.18, 8: 1.05, 9: 0.90, 10: 0.65, 11: 0.40, 12: 0.30}


def solar_position(ts, lat_deg, lon_deg):
    """Vráti (elevation_deg, azimuth_deg) — azimut: 0=juh, +západ, -východ."""
    lat_rad = math.radians(lat_deg)
    doy = ts.dayofyear
    decl = math.radians(23.45 * math.sin(math.radians(360.0 / 365.0 * (doy - 81))))
    h = ts.hour + ts.minute / 60.0
    B = math.radians(360.0 / 365.0 * (doy - 81))
    EoT = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    st = h + (lon_deg - 15) / 15 - 1 + EoT / 60
    H = math.radians(15.0 * (st - 12))
    s = math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(decl) * math.cos(H)
    elev = math.asin(max(-1, min(1, s)))
    if elev <= 0:
        return 0, 0
    cos_az = (math.sin(decl) - math.sin(elev) * math.sin(lat_rad)) / \
             (math.cos(elev) * math.cos(lat_rad))
    cos_az = max(-1, min(1, cos_az))
    az = math.degrees(math.acos(cos_az))
    return math.degrees(elev), (az if H > 0 else -az)


def panel_irradiance(elev_deg, sun_az, tilt_deg, panel_az):
    """Cosine of incidence angle. azimuth: 0=juh."""
    if elev_deg <= 0:
        return 0
    se = math.radians(elev_deg)
    sa = math.radians(sun_az)
    pt = math.radians(tilt_deg)
    pa = math.radians(panel_az)
    return max(0, math.sin(se) * math.cos(pt)
                  + math.cos(se) * math.sin(pt) * math.cos(sa - pa))


def pv_per_kWp(ts, lat, lon, tilt, azimuth, mf, pr=0.85):
    """kW na 1 kWp pre danú hodinu, orientácia, tilt."""
    elev, az = solar_position(ts, lat, lon)
    if elev <= 5:
        return 0.0
    return panel_irradiance(elev, az, tilt, azimuth) * mf[ts.month] * pr


def pv_per_kWp_ew(ts, lat, lon, tilt, mf, pr=0.85):
    """East-West konfigurácia — 50/50 mix panelov na východ a západ."""
    elev, az = solar_position(ts, lat, lon)
    if elev <= 5:
        return 0.0
    f_e = panel_irradiance(elev, az, tilt, -90)
    f_w = panel_irradiance(elev, az, tilt, +90)
    return (f_e + f_w) / 2 * mf[ts.month] * pr


def calibrate(pv_per, target_kwh_per_kwp):
    """Kalibruj PV array na cieľový ročný výnos kWh/kWp."""
    annual = pv_per.sum()
    if annual <= 0:
        return pv_per
    return pv_per * (target_kwh_per_kwp / annual)


def simulate(load_arr, pv_arr, bess_kwh=0, bess_kw=0):
    """Hodinová bilancia."""
    eta_rt = 0.92
    eta_ch = math.sqrt(eta_rt)
    eta_dis = math.sqrt(eta_rt)
    cap_min = bess_kwh * 0.05
    cap_max = bess_kwh * 0.95
    soc = bess_kwh * 0.5
    L = load_arr.values
    PV = pv_arr
    n = len(L)
    sud = bch = bdis = exp_ = imp_ = 0.0
    for i in range(n):
        load_i = L[i]
        pv_i = PV[i]
        direct = min(load_i, pv_i)
        sud += direct
        rl = load_i - direct
        rp = pv_i - direct
        if rp > 0 and bess_kwh > 0:
            cp = min(rp, bess_kw)
            ce = min(cp * eta_ch, cap_max - soc)
            cf = ce / eta_ch
            soc += ce
            rp -= cf
            bch += cf
        if rp > 0:
            exp_ += rp
        if rl > 0 and bess_kwh > 0:
            dp = min(rl, bess_kw)
            da = (soc - cap_min) * eta_dis
            du = min(dp, da)
            soc -= du / eta_dis
            rl -= du
            bdis += du
        imp_ += rl
    fp = PV.sum()
    tl = L.sum()
    su = sud + bdis
    return dict(
        fve_prod=fp / 1000,
        self_use_direct=sud / 1000,
        bess_charge=bch / 1000,
        bess_discharge=bdis / 1000,
        grid_export=exp_ / 1000,
        grid_import=imp_ / 1000,
        self_use=su / 1000,
        self_use_ratio=su / fp if fp > 0 else 0,
        coverage=su / tl if tl > 0 else 0,
        total_load=tl / 1000,
    )


def parse_variant(spec):
    """fve=92,tilt=25,az=0,bess_kwh=0,bess_kw=0"""
    name, params_str = spec.split(':', 1)
    params = {}
    for p in params_str.split(','):
        k, v = p.split('=')
        params[k.strip()] = float(v.strip())
    return name.strip(), params


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', required=True, help='CSV s hodinovým profilom')
    ap.add_argument('--lat', type=float, default=48.7, help='Zemepisná šírka')
    ap.add_argument('--lon', type=float, default=18.5, help='Zemepisná dĺžka')
    ap.add_argument('--mf', choices=['sk', 'north'], default='sk',
                    help='Mesačné koeficienty (sk default, north pre Oravu)')
    ap.add_argument('--variant', action='append', required=True,
                    help='name:fve=X,tilt=Y,az=Z,bess_kwh=A,bess_kw=B (az: 0=juh, ew=East-West, fasada=90)')
    ap.add_argument('--target-yield', type=float, default=1050,
                    help='Cieľový ročný výnos kWh/kWp pre kalibráciu')
    ap.add_argument('--output', required=True, help='Výstupný JSON')
    args = ap.parse_args()

    df = pd.read_csv(args.profile, parse_dates=[0], index_col=0)
    load = df.iloc[:, 0]
    print(f"Profil: {load.sum() / 1000:.1f} MWh/rok ({len(load)} h)", file=sys.stderr)

    mf = MF_SK if args.mf == 'sk' else MF_NORTH

    results = {}
    for spec in args.variant:
        name, p = parse_variant(spec)
        fve_kwp = p.get('fve', 0)
        tilt = p.get('tilt', 25)
        az = p.get('az', 0)  # 0 = juh
        bess_kwh = p.get('bess_kwh', 0)
        bess_kw = p.get('bess_kw', 0)

        # PV array
        ts_index = load.index
        if az == 999:  # E-W flag
            pv_per = np.array([pv_per_kWp_ew(ts, args.lat, args.lon, tilt, mf)
                               for ts in ts_index])
        else:
            pv_per = np.array([pv_per_kWp(ts, args.lat, args.lon, tilt, az, mf)
                               for ts in ts_index])
        pv_per = calibrate(pv_per, args.target_yield)

        pv = pv_per * fve_kwp if fve_kwp > 0 else np.zeros(len(ts_index))
        r = simulate(load, pv, bess_kwh, bess_kw)
        r['fve_kwp'] = fve_kwp
        r['bess_kwh'] = bess_kwh
        r['bess_kw'] = bess_kw
        r['tilt'] = tilt
        r['azimuth'] = az
        results[name] = r

        print(f"\n[{name}] FVE {fve_kwp:.1f} kWp tilt {tilt}° az {az}° + BESS {bess_kwh}/{bess_kw}",
              file=sys.stderr)
        print(f"  Výroba {r['fve_prod']:.1f} MWh, samosp {r['self_use_ratio'] * 100:.1f}% "
              f"({r['self_use']:.1f} MWh)", file=sys.stderr)
        print(f"  Pokrytie {r['coverage'] * 100:.1f}%, export {r['grid_export']:.1f} MWh, "
              f"import {r['grid_import']:.1f} MWh", file=sys.stderr)

    with open(args.output, 'w') as f:
        json.dump({
            'profile_MWh': float(load.sum() / 1000),
            'lat': args.lat, 'lon': args.lon,
            'target_yield': args.target_yield,
            'variants': results,
        }, f, indent=2, default=str)
    print(f"\nUložené: {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
