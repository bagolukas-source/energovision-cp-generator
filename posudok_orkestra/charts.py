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
        ax.bar(years[1:], cf_array[1:], color=COLOR_NET_CF, width=0.6, label="Ročný peňažný tok")

    # Accumulated line
    ax.plot(years, accumulated, color=COLOR_ACCUM, linewidth=2.5, label="Kumulovaný peňažný tok")

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
    height_in: float = 4.8,
) -> str:
    """Profesionalny Sankey-style tok energie (zdroje vlavo -> odberne miesto vpravo).
    Hrubka pasov ~ MWh. Slovenske popisky. Audtorsky vzhlad."""
    from matplotlib.path import Path
    from matplotlib.patches import PathPatch, FancyBboxPatch

    fig, ax = plt.subplots(figsize=(width_in, height_in))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.axis("off")

    load = max(load_total_mwh, 1e-6)
    # vyskovy rozsah pre toky (do OM)
    H = 4.4
    y0 = 0.7
    scale = H / max(load, pv_total_mwh)  # MWh -> vyska

    x_src_r = 2.6     # prava hrana zdrojov
    x_om_l = 7.4      # lava hrana OM
    bar_w = 0.55

    def band(y_s0, y_s1, y_t0, y_t1, color, alpha=0.55):
        # filled bezier band medzi (x_src_r, y_s*) a (x_om_l, y_t*)
        cx = (x_src_r + x_om_l) / 2
        verts = [
            (x_src_r, y_s0),
            (cx, y_s0), (cx, y_t0), (x_om_l, y_t0),
            (x_om_l, y_t1),
            (cx, y_t1), (cx, y_s1), (x_src_r, y_s1),
            (x_src_r, y_s0),
        ]
        codes = [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
                 Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4, Path.CLOSEPOLY]
        ax.add_patch(PathPatch(Path(verts, codes), facecolor=color, edgecolor="none", alpha=alpha))

    def node_bar(x, y_bot, h, color, title, value_mwh, sub=None, align="left"):
        ax.add_patch(FancyBboxPatch((x - bar_w/2, y_bot), bar_w, h,
                     boxstyle="round,pad=0.02,rounding_size=0.08",
                     facecolor=color, edgecolor="none"))
        tx = x + (bar_w/2 + 0.25 if align=="left" else -(bar_w/2 + 0.25))
        ha = "left" if align=="left" else "right"
        ymid = y_bot + h/2
        ax.text(tx, ymid + 0.16, title, ha=ha, va="center", fontsize=10.5, color=COLOR_TEXT, weight="bold")
        ax.text(tx, ymid - 0.14, f"{value_mwh:,.0f} MWh".replace(",", " "), ha=ha, va="center", fontsize=9.5, color="#6B7280")
        if sub:
            ax.text(tx, ymid - 0.44, sub, ha=ha, va="center", fontsize=8, color="#9CA3AF")

    has_bess = (pv_to_bat_mwh > 0 or bat_to_load_mwh > 0)

    # ---- ZDROJE vlavo (stacked) ----
    gap = 0.35
    # vyska zdrojov: PV (cela vyroba), Siet (grid_to_load), Bateria (bat_to_load)
    h_pv = pv_total_mwh * scale
    h_grid = grid_to_load_mwh * scale
    h_bat = (bat_to_load_mwh * scale) if has_bess else 0
    total_src_h = h_pv + h_grid + h_bat + gap * (1 + (1 if has_bess else 0))
    sy = y0 + max(0, (H - total_src_h)) / 2 + (total_src_h - gap*( (1 if has_bess else 0)+1))  # top start
    # umiestnime zhora: PV hore
    cur_top = y0 + H
    pv_bot = cur_top - h_pv
    node_bar(1.6, pv_bot, h_pv, COLOR_SOLAR, "Fotovoltika", pv_total_mwh, sub="výroba", align="left")
    cur_top = pv_bot - gap
    grid_bot = cur_top - h_grid
    node_bar(1.6, grid_bot, h_grid, COLOR_GRID, "Sieť", grid_to_load_mwh, sub="import", align="left")
    cur_top = grid_bot - gap
    if has_bess:
        bat_bot = cur_top - h_bat
        node_bar(1.6, bat_bot, h_bat, COLOR_BATTERY, "Batéria", bat_to_load_mwh, sub="výstup", align="left")

    # ---- ODBERNE MIESTO vpravo ----
    h_om = load * scale
    om_bot = y0 + (H - h_om)/2
    node_bar(8.4, om_bot, h_om, COLOR_SITE, "Odberné miesto", load_total_mwh, sub="spotreba", align="right")

    # ---- EXPORT (maly uzol vpravo hore) ----
    h_exp = max(pv_to_grid_mwh * scale, 0.0)

    # ---- TOKY (poradie zhora dole na strane OM = poradie zdrojov) ----
    # zdroj PV: cast do OM (pv_to_load) + cast export (pv_to_grid)
    # rozdelime PV bar zhora: pv_to_load potom pv_to_grid (export ide hore mimo OM)
    pv_src_top = pv_bot + h_pv
    # OM prijem zhora: PV_to_load, grid_to_load, bat_to_load
    om_top = om_bot + h_om
    t_cursor = om_top
    # PV -> OM
    h_pvload = pv_to_load_mwh * scale
    s_top = pv_src_top
    band(s_top, s_top - h_pvload, t_cursor, t_cursor - h_pvload, COLOR_SOLAR, alpha=0.5)
    t_cursor -= h_pvload
    s_after_pvload = s_top - h_pvload
    # PV -> Export (zhora vpravo, mimo OM baru)
    if h_exp > 0:
        ex_y_top = om_top
        ax.add_patch(FancyBboxPatch((9.1, ex_y_top - h_exp - 0.0), 0.4, max(h_exp,0.12),
                     boxstyle="round,pad=0.02,rounding_size=0.05", facecolor=COLOR_GRID, edgecolor="none", alpha=0.85))
        ax.text(9.3, ex_y_top - h_exp/2, f"Export\n{grid_export_mwh:,.0f} MWh".replace(",", " "),
                ha="center", va="center", fontsize=7.5, color="#374151")
        # tenky tok PV spodok -> export
        band(s_after_pvload, s_after_pvload - h_exp, ex_y_top, ex_y_top - h_exp, COLOR_SOLAR, alpha=0.35)
    # Grid -> OM
    h_gl = grid_to_load_mwh * scale
    band(grid_bot + h_grid, grid_bot, t_cursor, t_cursor - h_gl, COLOR_GRID, alpha=0.45)
    t_cursor -= h_gl
    # Bat -> OM
    if has_bess and h_bat > 0:
        band(bat_bot + h_bat, bat_bot, t_cursor, t_cursor - h_bat, COLOR_BATTERY, alpha=0.5)

    return _save_svg(fig)


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


# ============ CHART 7: POROVNANIE — CAPEX vs NPV (Porovnávací súhrn) ============
def chart_capex_vs_npv(
    names: list[str],
    capex: list[float],
    npv: list[float],
    highlight_idx: Optional[int] = None,
    dominated_mask: Optional[list[bool]] = None,
    width_in: float = 9.5,
    height_in: float = 4.6,
) -> str:
    """Zoskupené stĺpce CAPEX (vstup) vs NPV 20r (zisk) pre každú porovnávanú ponuku.
    Dominované ponuky (horšie vo všetkom než iná ponuka v tomto porovnaní) sú stlmené sivou."""
    n = len(names)
    x = np.arange(n)
    w = 0.36
    dominated_mask = dominated_mask or [False] * n

    fig, ax = plt.subplots(figsize=(width_in, height_in))
    capex_colors = [COLOR_NEUTRAL if d else COLOR_GRID for d in dominated_mask]
    npv_colors = [COLOR_NEUTRAL if d else COLOR_BATTERY for d in dominated_mask]
    ax.bar(x - w / 2, capex, width=w, color=capex_colors, label="CAPEX (vstup)")
    ax.bar(x + w / 2, npv, width=w, color=npv_colors, label="NPV 20 r (zisk)")

    ax.set_xticks(x)
    tick_labels = ax.set_xticklabels(names, fontsize=8.5, rotation=14, ha="right")
    if highlight_idx is not None and 0 <= highlight_idx < n:
        tick_labels[highlight_idx].set_fontweight("bold")
        tick_labels[highlight_idx].set_color(COLOR_TEXT)
    fig.subplots_adjust(bottom=0.22)

    def fmt(y, _):
        if abs(y) >= 1000:
            return f"{y/1000:.0f}k€"
        return f"{y:.0f}€"
    ax.yaxis.set_major_formatter(plt.FuncFormatter(fmt))
    ax.legend(loc="upper right", frameon=False, fontsize=9)
    ax.axhline(0, color=COLOR_AXIS, linewidth=0.8)
    return _save_svg(fig)


# ============ CHART 8: POROVNANIE — NÁVRATNOSŤ RANKING (Porovnávací súhrn) ============
def chart_payback_ranking(
    names: list[str],
    payback: list[float],
    highlight_idx: Optional[int] = None,
    dominated_mask: Optional[list[bool]] = None,
    width_in: float = 9.5,
    height_in: float = 4.0,
) -> str:
    """Horizontálne stĺpce návratnosti (roky), zoradené od najrýchlejšej po najpomalšiu."""
    n = len(names)
    dominated_mask = dominated_mask or [False] * n
    order = sorted(range(n), key=lambda i: payback[i] if payback[i] is not None else 1e9)
    names_o = [names[i] for i in order]
    payback_o = [payback[i] or 0 for i in order]
    colors_o = []
    for i in order:
        if dominated_mask[i]:
            colors_o.append(COLOR_NEUTRAL)
        elif i == highlight_idx:
            colors_o.append(COLOR_BATTERY)
        else:
            colors_o.append(COLOR_GRID)

    fig, ax = plt.subplots(figsize=(width_in, height_in))
    y = np.arange(n)
    ax.barh(y, payback_o, color=colors_o, height=0.55)
    ax.set_yticks(y)
    ax.set_yticklabels(names_o, fontsize=8.5)
    ax.invert_yaxis()  # najrýchlejšia hore
    max_pb = max(payback_o) if payback_o else 1
    for yi, v in zip(y, payback_o):
        ax.text(v + max_pb * 0.015, yi, f"{v:.1f} r", va="center", fontsize=8.5, color=COLOR_TEXT)
    ax.set_xlabel("Návratnosť (roky) — kratšia je lepšia", fontsize=9, color=COLOR_TEXT)
    ax.spines["left"].set_visible(False)
    ax.tick_params(left=False)
    return _save_svg(fig)
