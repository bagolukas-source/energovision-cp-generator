#!/usr/bin/env python3
"""
Ekonomika FVE/BESS — NPV, IRR, návratnosť, daňový odpis (vrátane dotácie Zelená podnikom).

KĽÚČOVÉ: Pri dotácii sa daňový odpis počíta IBA z Net CAPEX (po dotácii).
OPEX sa počíta z plnej CAPEX (servisuje sa celé zariadenie).

Dotácia Zelená podnikom: min(50 000, 0.45 × CAPEX).

Príklad:
    python economics.py --sim sim_results.json \
                        --capex "A:78321,B:91945,C:194189,D:226189" \
                        --tarif-buy 0.146 --tarif-sell 0.060 \
                        --output econ_results.json
"""
import argparse
import json
import math
import sys
from scipy.optimize import brentq


def calc_npv(capex, save_fve, dotacia=0, life=20, disc=0.06,
             deg=0.005, opex_rate=0.015, dppo=0.21):
    """NPV s 6-ročným daňovým odpisom z Net CAPEX (po dotácii).

    OPEX sa počíta z plnej CAPEX (servis celého zariadenia).
    """
    net_capex = capex - dotacia
    annual_tax = net_capex * dppo / 6
    opex = capex * opex_rate

    npv = -net_capex
    cfs = []
    for y in range(1, life + 1):
        deg_factor = (1 - deg) ** (y - 1)
        cf = save_fve * deg_factor - opex
        if y <= 6:
            cf += annual_tax
        cfs.append(cf)
        npv += cf / (1 + disc) ** y

    payback = net_capex / (save_fve + annual_tax) if (save_fve + annual_tax) > 0 else 999
    payback_no_tax = net_capex / save_fve if save_fve > 0 else 999

    def f(r):
        return -net_capex + sum(cf / (1 + r) ** i for i, cf in enumerate(cfs, 1))

    try:
        irr = brentq(f, -0.5, 1.0) * 100
    except Exception:
        irr = 0

    return dict(
        capex=capex,
        dotacia=dotacia,
        net_capex=net_capex,
        save_fve=save_fve,
        annual_tax=annual_tax,
        annual_opex=opex,
        annual_total=save_fve + annual_tax,
        npv=npv,
        payback=payback,
        payback_no_tax=payback_no_tax,
        irr=irr,
    )


def calc_dotacia(capex, max_eur=50000, pct=0.45):
    """Zelená podnikom: min(50k, 45 % × CAPEX)."""
    return min(max_eur, pct * capex)


def calc_savings(sim_var, p_buy, p_sell, arb_bonus=0):
    """Save ročne = samosp × P_BUY + export × P_SELL + arbitráž BS."""
    return (sim_var['self_use'] * 1000 * p_buy
            + sim_var['grid_export'] * 1000 * p_sell
            + arb_bonus)


def parse_capex_map(s):
    """A:78321,B:91945 → {'A': 78321, ...}"""
    out = {}
    for part in s.split(','):
        k, v = part.split(':')
        out[k.strip()] = float(v.strip())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sim', required=True, help='JSON so simulačnými výsledkami')
    ap.add_argument('--capex', required=True,
                    help='Variant:CAPEX páry, napr. "A:78321,B:91945"')
    ap.add_argument('--tarif-buy', type=float, default=0.146,
                    help='Nákupná cena VN €/kWh')
    ap.add_argument('--tarif-sell', type=float, default=0.060,
                    help='Výkupná cena prebytkov €/kWh')
    ap.add_argument('--arb-per-kwh', type=float, default=0,
                    help='BS arbitráž €/kWh BESS/rok (predvolene 0)')
    ap.add_argument('--no-dotacia', action='store_true',
                    help='Bez dotácie Zelená podnikom')
    ap.add_argument('--scenarios', action='store_true',
                    help='Tri cenové scenáre (Báza, Nízky, Spot)')
    ap.add_argument('--output', required=True)
    args = ap.parse_args()

    sim = json.load(open(args.sim))
    variants = sim['variants']
    capex_map = parse_capex_map(args.capex)

    out = {'with_dotacia': {}, 'no_dotacia': {}}
    if args.scenarios:
        out['scenarios'] = {}

    for vid, capex in capex_map.items():
        v = variants[vid]
        bess_kwh = v.get('bess_kwh', 0)
        save_base = calc_savings(v, args.tarif_buy, args.tarif_sell,
                                  arb_bonus=bess_kwh * args.arb_per_kwh)

        # Bez dotácie
        e_nodot = calc_npv(capex, save_base, dotacia=0)
        e_nodot['variant'] = vid
        out['no_dotacia'][vid] = e_nodot

        # S dotáciou
        if not args.no_dotacia:
            dot = calc_dotacia(capex)
            e_dot = calc_npv(capex, save_base, dotacia=dot)
            e_dot['variant'] = vid
            e_dot['dot_pct'] = dot / capex * 100
            out['with_dotacia'][vid] = e_dot

        # 3 cenové scenáre
        if args.scenarios:
            scen = {}
            for sn, p_sell, arb_mult in [
                ('Báza', 0.060, 1.0),
                ('Nízky výkup', 0.030, 1.0),
                ('Spot s arbitrážou', 0.080, 1.3),
            ]:
                save_s = calc_savings(v, args.tarif_buy, p_sell,
                                       arb_bonus=bess_kwh * args.arb_per_kwh * arb_mult)
                scen[sn] = calc_npv(capex, save_s, dotacia=0)
            out['scenarios'][vid] = scen

        print(f"\n[{vid}] CAPEX {capex:,.0f} € · save {save_base:,.0f} €/r", file=sys.stderr)
        print(f"  bez dotácie: návrat {e_nodot['payback']:.1f} r, NPV20 {e_nodot['npv']:+,.0f}, IRR {e_nodot['irr']:.1f}%",
              file=sys.stderr)
        if not args.no_dotacia:
            print(f"  s dotáciou:  návrat {e_dot['payback']:.1f} r, NPV20 {e_dot['npv']:+,.0f}, IRR {e_dot['irr']:.1f}%, dotácia {e_dot['dotacia']:,.0f} €",
                  file=sys.stderr)

    out['params'] = {
        'tarif_buy': args.tarif_buy,
        'tarif_sell': args.tarif_sell,
        'arb_per_kwh': args.arb_per_kwh,
        'dppo': 0.21,
        'disc': 0.06,
        'opex_rate': 0.015,
        'deg': 0.005,
        'life': 20,
        'dotacia_max': 50000,
        'dotacia_pct': 0.45,
    }

    with open(args.output, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nUložené: {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
