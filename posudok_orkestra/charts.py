"""DRAFT — toto pôjde do /sessions/magical-eager-gates/render-repo/posudok_orkestra/charts.py

Matplotlib chart generators — vracajú SVG string ktorý ide priamo do HTML.
Orkestra-style: minimal axes, žiadne ticks marks, jemné gridlines, sans-serif.
"""
from __future__ import annotations
import io
import math
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Circle
import numpy as np

# ============ ORKESTRA BRAND PALETTE ============
COLOR_SOLAR = "#FFD645"     # žltá — FVE generation, solar upfront
COLOR_BATTERY = "#16A34A"   # Energovision zelená — BESS
COLOR_GRID = "#5B7CFA"      # modrá — grid import/export
COLOR_SITE = "#B85DD8"      # fialová — site consumption
COLOR_NET_CF = "#3B82F6"    # modrá — net cashflow bars
COLOR_ACCUM = "#A5C9FF"     # light blue — accumulated cashflow line
COLOR_NEUTRAL = "#9CA3AF"   # gray — curtailed / peak load
COLOR_BG_SECTION = "#F5F6F8"
COLOR_TEXT = "#1F2937"
COLOR_AXIS = "#9CA3AF"
COLOR_GRID_LINE = "#E5E7EB"
FONT_FAMILY = "DejaVu Sans"  # WeasyPrint má v default; v HTML použijeme Inter
plt.rcParams.update({
    "font.family": FONT_FAMILY,
    "axes.edgecolor": COLOR_AXIS,
    "axes.labelcolor": COLOR_TEXT,
    "xtick.color": COLOR_AXIS,
    "ytick.color": COLOR_AXIS,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": COLOR_GRID_LINE,
    "grid.linewidth": 0.6,
    "grid.linestyle": "-",
})


def _save_svg(fig) -> str:
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    return buf.getvalue()


# ============ CHART 1: CASHFLOW (P2) ============
def chart_cashflow(
    cf_array: list[float],
    capex_pv: float,
    capex_bess: float,
    capex_other: float = 0.0,
    width_in: float = 9.5,
    height_in: float = 4.5,
) -> str:
    """Stacked bars (net cashflow + capex) + accumulated line.

    cf_array[0] je rok 0 (negatívny capex), cf_array[1..N] sú ročné net cashflows.
    """
    years = list(range(len(cf_array)))
    accumulated = np.cumsum(cf_array)

    fig, ax = plt.subplots(figsize=(width_in, height_in))

    # Capex bars na rok 0
    ax.bar(0, -capex_pv, color=COLOR_SOLAR, width=0.6, label="FVE CAPEX")
    if capex_bess > 0:
        ax.bar(0, -capex_bess, bottom=-capex_pv, color=COLOR_BATTERY, width=0.6, label="BESS CAPEX")
    if capex_other > 0:
        ax.bar(0, -capex_other, bottom=-capex_pv-capex_bess, color=COLOR_NEUTRAL, width=0.6, label="Ostatné CAPEX")

    # Net cashflow bars roky 1..N
    if len(years) > 1:
        ax.bar(years[1:], cf_array[1:], color=COLOR_NET_CF, width=0.6, label="Ročný cash flow")

    # Accumulated line
    ax.plot(years, accumulated, color=COLOR_ACCUM, linewidth=2.5, label="Kumulovaný cash flow")

    # Format y-axis as currency
    def fmt(x, _):
        if abs(x) >= 1_000_000:
            return f"{x/1_000_000:.1f} M€"
        if abs(x) >= 1000:
            return f"{x/1000:.0f}k€"
        return f"{x:.0f}€"
    ax.yaxis.set_major_formatter(plt.FuncFormatter(fmt))
    ax.set_xlabel("Rok", fontsize=10, color=COLOR_TEXT)
    ax.set_xticks(years[::max(1, len(years)//12)])
    ax.legend(loc="upper left", frameon=False, fontsize=9, ncol=2)
    ax.axhline(0, color=COLOR_AXIS, linewidth=0.8)

    return _save_svg(fig)


# ============ CHART 2: ENERGY FLOW DIAGRAM (P3) ============
def chart_energy_flow(
    pv_total_mwh: float,
    pv_to_load_mwh: float,
    pv_to_grid_mwh: float,
    pv_to_bat_mwh: float,
    grid_to_load_mwh: float,
    bat_to_load_mwh: float,
    grid_to_bat_mwh: float,
    load_total_mwh: float,
    grid_export_mwh: float,
    width_in: float = 9.5,
    height_in: float = 5.5,
) -> str:
    """4-circle diagram s arrowmi medzi nimi (replika Orkestra P3).
    Solar PV (top-right, yellow) → Site / Battery / Grid Export
    Grid (left, blue) ↔ Site / Battery
    Battery (bottom-right, green) ↔ Site
    Site (center, purple) — consumption
    """
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.set_aspect("equal")
    ax.axis("off")

    # Circle positions (x, y, radius, color, label_top, value, unit)
    nodes = {
        "Solar PV": (8.0, 4.5, 1.0, COLOR_SOLAR, "Solar PV", f"{pv_total_mwh:.0f}", "MWh\ngenerácia"),
        "Site":     (5.0, 3.0, 1.1, COLOR_SITE,  "Site",     f"{load_total_mwh:.0f}", "MWh\nspotreba"),
        "Grid":     (2.0, 3.0, 1.1, COLOR_GRID,  "Grid",     f"{grid_to_load_mwh + grid_to_bat_mwh:.0f}", f"MWh\nExport: {grid_export_mwh:.0f}"),
        "Battery":  (8.0, 1.5, 0.9, COLOR_BATTERY, "Battery", f"{bat_to_load_mwh:.0f}", "MWh\nvýstup"),
    }

    for name, (x, y, r, color, label, value, unit) in nodes.items():
        # Circle background — light tint
        circ_bg = Circle((x, y), r, facecolor=color, alpha=0.20, edgecolor=color, linewidth=2.5)
        ax.add_patch(circ_bg)
        # Label top
        ax.text(x, y + r * 0.5, label, ha="center", va="center", fontsize=10, color=COLOR_TEXT, weight="bold")
        # Value (big)
        ax.text(x, y - 0.05, value, ha="center", va="center", fontsize=22, color=COLOR_TEXT, weight="bold")
        # Unit
        ax.text(x, y - r * 0.55, unit, ha="center", va="center", fontsize=8, color=COLOR_TEXT)

    # Helper to draw arrow with label
    def arrow(from_node, to_node, value_mwh, label_offset=(0, 0), color=None):
        x1, y1, r1, c1, *_ = nodes[from_node]
        x2, y2, r2, c2, *_ = nodes[to_node]
        # Compute arrow start/end on circle edges
        dx, dy = x2 - x1, y2 - y1
        dist = math.hypot(dx, dy)
        ux, uy = dx / dist, dy / dist
        sx, sy = x1 + ux * r1, y1 + uy * r1
        ex, ey = x2 - ux * r2, y2 - uy * r2
        arrow_color = color or c1
        arr = FancyArrowPatch((sx, sy), (ex, ey), arrowstyle="->,head_length=10,head_width=8",
                              color=arrow_color, linewidth=2.5, alpha=0.85)
        ax.add_patch(arr)
        # Label uprostred
        mx, my = (sx + ex) / 2 + label_offset[0], (sy + ey) / 2 + label_offset[1]
        ax.text(mx, my, f"{value_mwh:.0f}", ha="center", va="center",
                fontsize=11, color=COLOR_TEXT, weight="bold",
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none"))

    if pv_to_load_mwh > 0:
        arrow("Solar PV", "Site", pv_to_load_mwh, label_offset=(0, 0.15))
    if pv_to_grid_mwh > 0:
        arrow("Solar PV", "Grid", pv_to_grid_mwh, label_offset=(0, 0.3))
    if pv_to_bat_mwh > 0:
        arrow("Solar PV", "Battery", pv_to_bat_mwh, label_offset=(0.3, 0))
    if grid_to_load_mwh > 0:
        arrow("Grid", "Site", grid_to_load_mwh, label_offset=(0, 0.2))
    if bat_to_load_mwh > 0:
        arrow("Battery", "Site", bat_to_load_mwh, label_offset=(0.2, 0))
    if grid_to_bat_mwh > 0:
        arrow("Grid", "Battery", grid_to_bat_mwh, label_offset=(0, -0.3))

    return _save_svg(fig)


# ============ CHART 3: DAILY LOAD PROFILE (P4) ============
def chart_daily_load_profile(
    hourly_load_kw: list[float],  # 24 values for avg day
    hourly_load_kw_after: list[float] | None = None,
    width_in: float = 9.5,
    height_in: float = 4.0,
) -> str:
    """Line chart 24h before/after."""
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    hours = list(range(24))
    ax.plot(hours, hourly_load_kw, color=COLOR_NEUTRAL, linewidth=2, label="Pred FVE/BESS", alpha=0.7)
    if hourly_load_kw_after:
        ax.plot(hours, hourly_load_kw_after, color=COLOR_NET_CF, linewidth=2.5, label="Po FVE/BESS")
    ax.set_xticks([0, 4, 8, 12, 16, 20, 23])
    ax.set_xticklabels(["00:00", "04:00", "08:00", "12:00", "16:00", "20:00", "23:00"])
    ax.set_ylabel("Výkon (kW)", fontsize=10, color=COLOR_TEXT)
    ax.axhline(0, color=COLOR_AXIS, linewidth=0.8)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    return _save_svg(fig)


# ============ CHART 4: SOLAR CONSUMPTION DONUT (P5) ============
def chart_solar_consumption_donut(
    self_consumed_pct: float,
    direct_to_load_pct: float,
    charging_battery_pct: float,
    exported_pct: float,
    curtailed_pct: float,
    width_in: float = 7.5,
    height_in: float = 5.0,
) -> str:
    """Donut chart so 4 segmentmi + centrálnym percentom."""
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    sizes = [direct_to_load_pct, charging_battery_pct, exported_pct, curtailed_pct]
    colors = [COLOR_SOLAR, COLOR_BATTERY, COLOR_GRID, COLOR_NEUTRAL]
    wedges, _ = ax.pie(
        sizes, colors=colors, startangle=90, counterclock=False,
        wedgeprops=dict(width=0.32, edgecolor="white", linewidth=2),
    )
    # Center text
    ax.text(0, 0.08, "Samospotreba", ha="center", va="center", fontsize=10, color=COLOR_TEXT)
    ax.text(0, -0.12, f"{self_consumed_pct:.0f}%", ha="center", va="center", fontsize=28, color=COLOR_TEXT, weight="bold")
    return _save_svg(fig)


# ============ CHART 5: MONTHLY EARNINGS (P6) ============
def chart_monthly_earnings(
    months: list[str],
    solar_to_load: list[float],
    solar_export: list[float],
    arbitrage: list[float],
    peak_reduction: list[float] | None = None,
    width_in: float = 9.5,
    height_in: float = 4.5,
) -> str:
    """Stacked vertical bars 12 mesiacov."""
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    x = np.arange(len(months))
    bottoms = np.zeros(len(months))
    ax.bar(x, solar_to_load, color=COLOR_SOLAR, label="Samospotreba FVE", bottom=bottoms)
    bottoms += np.array(solar_to_load)
    ax.bar(x, solar_export, color=COLOR_GRID, label="Predaj do siete", bottom=bottoms)
    bottoms += np.array(solar_export)
    if arbitrage:
        ax.bar(x, arbitrage, color=COLOR_BATTERY, label="Arbitráž BESS", bottom=bottoms)
        bottoms += np.array(arbitrage)
    if peak_reduction:
        ax.bar(x, peak_reduction, color=COLOR_SITE, label="Peak shaving", bottom=bottoms)
    ax.set_xticks(x)
    ax.set_xticklabels(months, fontsize=9)
    ax.legend(loc="upper left", frameon=False, fontsize=9, ncol=2)
    def fmt(y, _):
        return f"{y/1000:.0f}k€" if abs(y) >= 1000 else f"{y:.0f}€"
    ax.yaxis.set_major_formatter(plt.FuncFormatter(fmt))
    return _save_svg(fig)


# ============ CHART 6: UPFRONT COSTS (P7) ============
def chart_upfront_costs(
    capex_pv: float, capex_bess: float, capex_other: float = 0.0,
    width_in: float = 5.0, height_in: float = 5.0,
) -> str:
    """Jedna stacked vertical column + total label nad ňou."""
    fig, ax = plt.subplots(figsize=(width_in, height_in))
    total = capex_pv + capex_bess + capex_other
    bottoms = 0
    if capex_pv > 0:
        ax.bar(0, capex_pv, color=COLOR_SOLAR, width=0.5)
        ax.text(0, capex_pv / 2, f"{capex_pv:,.0f} €".replace(",", " "),
                ha="center", va="center", fontsize=10, color=COLOR_TEXT, weight="bold")
        bottoms += capex_pv
    if capex_bess > 0:
        ax.bar(0, capex_bess, bottom=bottoms, color=COLOR_BATTERY, width=0.5)
        ax.text(0, bottoms + capex_bess / 2, f"{capex_bess:,.0f} €".replace(",", " "),
                ha="center", va="center", fontsize=10, color=COLOR_TEXT, weight="bold")
        bottoms += capex_bess
    if capex_other > 0:
        ax.bar(0, capex_other, bottom=bottoms, color=COLOR_NEUTRAL, width=0.5)
        ax.text(0, bottoms + capex_other / 2, f"{capex_other:,.0f} €".replace(",", " "),
                ha="center", va="center", fontsize=10, color=COLOR_TEXT)
        bottoms += capex_other
    # Total label
    ax.text(0, total + total * 0.04, f"{total:,.0f} €".replace(",", " "),
            ha="center", va="center", fontsize=14, color=COLOR_TEXT, weight="bold")
    ax.set_xlim(-1, 1)
    ax.set_xticks([])
    def fmt(y, _):
        return f"{y/1000:.0f}k €" if abs(y) >= 1000 else f"{y:.0f} €"
    ax.yaxis.set_major_formatter(plt.FuncFormatter(fmt))
    ax.spines["bottom"].set_visible(False)
    return _save_svg(fig)
