#!/usr/bin/env python3
"""
Generátor 6 grafov pre Energovision posudok.

Vyrába: heatmapa, hodinový profil (work/sat/sun), mesačná spotreba,
LDC krivka, porovnanie variantov, energetická bilancia.

Príklad:
    python charts.py --profile profil_h.csv --sim sim_results.json \
                     --econ econ_results.json --output-dir pracovne/
"""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Energovision farby
GRN = '#92D050'
BLK = '#1A1A1A'
DGR = '#2C2C2C'
MGR = '#8C8C8C'
LGR = '#F5F5F5'
LGRN = '#E8F4D5'
RED = '#E74C3C'
ORN = '#F39200'
BLU = '#0F4C81'

plt.rcParams.update({
    'font.family': 'DejaVu Sans', 'font.size': 10,
    'axes.edgecolor': MGR, 'axes.labelcolor': DGR,
    'xtick.color': DGR, 'ytick.color': DGR,
    'axes.titlecolor': BLK, 'axes.titleweight': 'bold',
    'figure.facecolor': 'white', 'savefig.facecolor': 'white',
    'savefig.bbox': 'tight', 'savefig.dpi': 150,
})


def graf_heatmapa(load, out):
    df_h = pd.DataFrame({'load': load})
    df_h['date'] = df_h.index.date
    df_h['hour'] = df_h.index.hour
    piv = df_h.pivot_table(index='hour', columns='date', values='load', aggfunc='mean')
    fig, ax = plt.subplots(figsize=(11, 5))
    im = ax.imshow(piv.values, aspect='auto', cmap='YlGnBu',
                   interpolation='nearest', origin='upper', vmin=0)
    ax.set_title('Heatmapa hodinového odberu (kW)', loc='left')
    ax.set_ylabel('Hodina dňa')
    ax.set_xlabel('Dátum')
    ax.set_yticks(np.arange(0, 24, 2))
    ax.set_yticklabels([f'{h:02d}:00' for h in range(0, 24, 2)])
    xt = np.linspace(0, piv.shape[1] - 1, 13).astype(int)
    ax.set_xticks(xt)
    ax.set_xticklabels([str(piv.columns[i]) for i in xt], rotation=45, ha='right')
    plt.colorbar(im, ax=ax, label='kW')
    plt.savefig(out)
    plt.close()


def graf_profil(load, out):
    work = load[load.index.weekday < 5].groupby(
        load[load.index.weekday < 5].index.hour).mean()
    sat = load[load.index.weekday == 5].groupby(
        load[load.index.weekday == 5].index.hour).mean()
    sun = load[load.index.weekday == 6].groupby(
        load[load.index.weekday == 6].index.hour).mean()
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(work.index, work.values, color=BLK, linewidth=2.5, label='Pracovný deň')
    ax.plot(sat.index, sat.values, color=ORN, linewidth=2, label='Sobota')
    ax.plot(sun.index, sun.values, color=BLU, linewidth=2, linestyle='--', label='Nedeľa')
    ax.fill_between(work.index, 0, work.values, color=GRN, alpha=0.12)
    ax.set_title('Priemerný hodinový profil odberu', loc='left')
    ax.set_xlabel('Hodina')
    ax.set_ylabel('Priemerný odber [kW]')
    ax.set_xticks(range(0, 24, 2))
    ax.legend(loc='best')
    ax.grid(axis='y', alpha=0.3)
    ax.set_xlim(0, 23)
    plt.savefig(out)
    plt.close()


def graf_mesacna(load, out):
    m = load.groupby([load.index.year, load.index.month]).sum() / 1000
    months_str = [f"{y % 100:02d}/{mn:02d}" for (y, mn) in m.index]
    fig, ax = plt.subplots(figsize=(11, 4.5))
    bars = ax.bar(months_str, m.values, color=GRN, edgecolor=BLK, linewidth=0.5)
    for b, v in zip(bars, m.values):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 1,
                f'{v:.0f}', ha='center', va='bottom', fontsize=9, fontweight='bold')
    ax.set_title('Mesačná spotreba (MWh)', loc='left')
    ax.set_ylabel('MWh')
    ax.grid(axis='y', alpha=0.3)
    ax.set_ylim(0, max(m.values) * 1.15)
    plt.xticks(rotation=45)
    plt.savefig(out)
    plt.close()


def graf_ldc(load, out):
    sorted_kw = np.sort(load.values)[::-1]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(np.arange(len(sorted_kw)), sorted_kw, color=BLU, linewidth=1.8)
    ax.fill_between(np.arange(len(sorted_kw)), 0, sorted_kw, color=BLU, alpha=0.15)
    p99 = np.percentile(load, 99)
    p95 = np.percentile(load, 95)
    p50 = np.percentile(load, 50)
    for v, c, lbl in [(p99, RED, f'P99 = {p99:.0f} kW'),
                      (p95, ORN, f'P95 = {p95:.0f} kW'),
                      (p50, MGR, f'Medián = {p50:.0f} kW')]:
        ax.axhline(v, color=c, linestyle='--', linewidth=1, alpha=0.7)
        ax.text(8500, v, ' ' + lbl, va='center', ha='right', color=c, fontsize=9)
    ax.set_title('Krivka trvania výkonu (LDC)', loc='left')
    ax.set_xlabel('Hodín za rok')
    ax.set_ylabel('Výkon [kW]')
    ax.grid(alpha=0.3)
    ax.set_xlim(0, 8760)
    ax.set_ylim(0, sorted_kw.max() * 1.05)
    plt.savefig(out)
    plt.close()


def graf_porovnanie(econ_dict, out):
    """3-panel: CAPEX/Saving, Návratnosť, NPV."""
    keys = list(econ_dict.keys())
    if len(keys) == 0:
        return
    capex = [econ_dict[k]['capex'] / 1000 for k in keys]
    saving = [econ_dict[k]['annual_total'] / 1000 for k in keys]
    payback = [econ_dict[k]['payback'] for k in keys]
    npv = [econ_dict[k]['npv'] / 1000 for k in keys]

    fig, axes = plt.subplots(1, 3, figsize=(13, 4.5))
    x = np.arange(len(keys))
    w = 0.38

    ax1 = axes[0]
    ax1.bar(x - w / 2, capex, w, color=DGR, edgecolor=BLK, linewidth=0.5, label='CAPEX')
    ax1.bar(x + w / 2, saving, w, color=GRN, edgecolor=BLK, linewidth=0.5,
            label='Úspora rok 1')
    for i, (c, s) in enumerate(zip(capex, saving)):
        ax1.text(i - w / 2, c + 5, f'{c:.0f}', ha='center', fontsize=9, fontweight='bold')
        ax1.text(i + w / 2, s + 0.5, f'{s:.0f}', ha='center', fontsize=9, fontweight='bold')
    ax1.set_xticks(x)
    ax1.set_xticklabels(keys, fontsize=10)
    ax1.set_ylabel('tis. €')
    ax1.set_title('CAPEX vs ročná úspora', loc='left')
    ax1.legend(loc='upper left')
    ax1.grid(axis='y', alpha=0.3)

    ax2 = axes[1]
    bars = ax2.bar(x, payback, color=ORN, edgecolor=BLK, linewidth=0.5)
    for i, p in enumerate(payback):
        ax2.text(i, p + 0.1, f'{p:.1f} r', ha='center', fontsize=10, fontweight='bold')
    ax2.set_xticks(x)
    ax2.set_xticklabels(keys, fontsize=10)
    ax2.set_ylabel('Návratnosť [r]')
    ax2.set_title('Návratnosť', loc='left')
    ax2.grid(axis='y', alpha=0.3)

    ax3 = axes[2]
    colors_npv = [GRN if n > 0 else RED for n in npv]
    bars = ax3.bar(x, npv, color=colors_npv, edgecolor=BLK, linewidth=0.5)
    for i, n in enumerate(npv):
        ax3.text(i, n + (2 if n >= 0 else -3), f'{n:+.0f}',
                 ha='center', va='bottom' if n >= 0 else 'top',
                 fontsize=10, fontweight='bold')
    ax3.axhline(0, color=BLK, linewidth=0.8)
    ax3.set_xticks(x)
    ax3.set_xticklabels(keys, fontsize=10)
    ax3.set_ylabel('NPV 20 r. [tis. €]')
    ax3.set_title('NPV 20 r.', loc='left')
    ax3.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def graf_bilancia(sim_dict, out):
    """Stacked bar: samosp + export pre každý variant."""
    keys = list(sim_dict.keys())
    if len(keys) == 0:
        return
    samosp = [sim_dict[k]['self_use'] for k in keys]
    export = [sim_dict[k]['grid_export'] for k in keys]
    total = [s + e for s, e in zip(samosp, export)]

    fig, ax = plt.subplots(figsize=(11, 4.5))
    x = np.arange(len(keys))
    ax.bar(x, samosp, color=GRN, edgecolor=BLK, linewidth=0.5,
           label='Samospotreba (priamo + BESS)')
    ax.bar(x, export, bottom=samosp, color=BLU, edgecolor=BLK, linewidth=0.5,
           label='Export do siete')
    for i, (s, e) in enumerate(zip(samosp, export)):
        ax.text(i, s / 2, f'{s:.0f}', ha='center', va='center',
                fontsize=10, fontweight='bold', color='white')
        if e > 1:
            ax.text(i, s + e / 2, f'{e:.0f}', ha='center', va='center',
                    fontsize=10, fontweight='bold', color='white')
        ax.text(i, s + e + 1, f'{s + e:.0f} MWh', ha='center', va='bottom',
                fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(keys, fontsize=10)
    ax.set_ylabel('MWh/rok')
    ax.set_title('Energetická bilancia variantov', loc='left')
    ax.legend(loc='upper right')
    ax.grid(axis='y', alpha=0.3)
    plt.savefig(out)
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', required=True, help='CSV s hodinovým profilom')
    ap.add_argument('--sim', required=False, help='JSON so simulačnými výsledkami')
    ap.add_argument('--econ', required=False, help='JSON s ekonomikou (no_dotacia)')
    ap.add_argument('--output-dir', required=True)
    args = ap.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(args.profile, parse_dates=[0], index_col=0)
    load = df.iloc[:, 0]

    print(f"Spotreba: {load.sum() / 1000:.1f} MWh/rok ({len(load)} h)")

    graf_heatmapa(load, out / 'graf_heatmapa.png')
    print(f"  ✓ heatmapa")
    graf_profil(load, out / 'graf_profil_hodinovy.png')
    print(f"  ✓ profil hodinový")
    graf_mesacna(load, out / 'graf_mesacna.png')
    print(f"  ✓ mesačná")
    graf_ldc(load, out / 'graf_ldc.png')
    print(f"  ✓ LDC")

    if args.sim:
        sim = json.load(open(args.sim))
        graf_bilancia(sim['variants'], out / 'graf_bilancia.png')
        print(f"  ✓ bilancia")

    if args.econ:
        econ = json.load(open(args.econ))
        # Použij no_dotacia ako default
        if 'no_dotacia' in econ:
            graf_porovnanie(econ['no_dotacia'], out / 'graf_porovnanie.png')
            print(f"  ✓ porovnanie")


if __name__ == '__main__':
    main()
