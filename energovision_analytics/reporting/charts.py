"""Plotly grafy pre Energovision posudky — v0.3 redesign.

Vlastný dizajn inšpirovaný industry-standard UX patternmi (donut s pravou legendou,
spaghetti load plot, energy metrics area, stacked bars). Žiadny external kód.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

try:
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False


@dataclass
class EnergovisionTheme:
    """Brand farby — v0.3 (modrá ako primary accent pre čísla)."""
    # Brand
    primary: str = "#7AB835"
    primary_dark: str = "#4D8121"
    primary_lighter: str = "#EAF6D5"

    # Industry energy palette (NOT brand)
    solar: str = "#F2C744"
    solar_light: str = "#FCE9A0"
    grid: str = "#6092F5"
    grid_light: str = "#CCDBF9"
    battery: str = "#9B6FE0"
    battery_light: str = "#E0D4F5"
    load_after: str = "#5A8DEE"

    # Numbers accent (modrá pre big stats)
    accent_blue: str = "#5A8DEE"

    # Neutrals
    ink: str = "#0F1419"
    ink_muted: str = "#5C6470"
    ink_subtle: str = "#9AA3AE"
    border: str = "#EAEDF0"
    grid_color: str = "#F5F6F8"

    bg_card: str = "#FFFFFF"
    bg_app: str = "#F8F9FB"

    # Status
    danger: str = "#DC2626"
    success: str = "#15803D"
    warning: str = "#D97706"

    # Typography
    font_sans: str = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"


THEME = EnergovisionTheme()


def _layout(title: Optional[str] = None, height: int = 360, show_legend: bool = True,
            legend_pos: str = "bottom") -> dict:
    legend_cfg = {
        "orientation": "h",
        "yanchor": "top", "y": -0.18,
        "xanchor": "center", "x": 0.5,
        "font": {"size": 11, "color": THEME.ink_muted, "family": THEME.font_sans},
        "bgcolor": "rgba(255,255,255,0)",
        "itemwidth": 30,
    }
    return {
        "plot_bgcolor": "white", "paper_bgcolor": "white",
        "font": {"family": THEME.font_sans, "color": THEME.ink, "size": 11},
        "height": height,
        "showlegend": show_legend,
        "legend": legend_cfg,
        "margin": {"l": 56, "r": 24, "t": 30 if not title else 42, "b": 80 if show_legend else 40},
        "hoverlabel": {
            "bgcolor": "white", "bordercolor": THEME.border,
            "font": {"family": THEME.font_sans, "color": THEME.ink, "size": 11},
        },
        "xaxis": {
            "gridcolor": THEME.grid_color, "showgrid": True, "zeroline": False,
            "linecolor": THEME.border, "tickcolor": THEME.border,
            "tickfont": {"size": 10, "color": THEME.ink_muted},
        },
        "yaxis": {
            "gridcolor": THEME.grid_color, "showgrid": True, "zeroline": True,
            "zerolinecolor": THEME.border, "zerolinewidth": 1,
            "linecolor": "rgba(0,0,0,0)", "tickcolor": THEME.border,
            "tickfont": {"size": 10, "color": THEME.ink_muted},
        },
    }


# ============================================================================
# 1. PV CONSUMPTION DONUT — legenda vpravo (custom layout)
# ============================================================================
def chart_pv_consumption_donut(
    pv_to_load: float, pv_to_bat: float, pv_to_grid: float, pv_clipped: float = 0,
    title: str = "Využitie FVE výroby",
) -> "go.Figure":
    """Donut s legendou vpravo s value+kWh."""
    raw = [
        ("Vlastná spotreba", pv_to_load, THEME.solar),
        ("Nabíjanie BESS", pv_to_bat, THEME.battery),
        ("Export do siete", pv_to_grid, THEME.grid),
        ("Orezané", pv_clipped, THEME.ink_subtle),
    ]
    keep = [(l, v, c) for l, v, c in raw if v > 0.5]
    if not keep:
        keep = [("Žiadna výroba", 1, THEME.ink_subtle)]
    labels, vals, colors = zip(*keep)
    total = sum(vals)
    self_cons_pct = (pv_to_load + pv_to_bat) / total * 100 if total > 0 else 0

    fig = go.Figure(data=[go.Pie(
        labels=list(labels), values=list(vals), hole=0.66,
        marker={"colors": list(colors), "line": {"color": "white", "width": 3}},
        textinfo="none",
        hovertemplate="<b>%{label}</b>: %{value:,.0f} kWh (%{percent})<extra></extra>",
        sort=False, direction="clockwise",
        showlegend=False,
    )])
    fig.update_layout(
        plot_bgcolor="white", paper_bgcolor="white",
        font={"family": THEME.font_sans, "color": THEME.ink, "size": 11},
        height=300, margin={"l": 20, "r": 20, "t": 20, "b": 20},
        annotations=[
            {"text": "<b>Samospotreba</b>", "x": 0.5, "y": 0.58,
             "showarrow": False, "font": {"size": 11, "color": THEME.ink_muted}},
            {"text": f"<b style='font-size:32px;color:{THEME.ink}'>{self_cons_pct:.0f}%</b>",
             "x": 0.5, "y": 0.42, "showarrow": False, "font": {"size": 32}},
        ],
    )
    return fig


def render_donut_legend(
    pv_to_load: float, pv_to_bat: float, pv_to_grid: float, pv_clipped: float = 0,
) -> str:
    """HTML legenda vpravo od donutu — value + kWh per položku."""
    total = pv_to_load + pv_to_bat + pv_to_grid + pv_clipped
    items = [
        ("Vlastná spotreba", pv_to_load, THEME.solar, True),
        ("z toho priamo do záťaže", pv_to_load, THEME.solar_light, False),
        ("z toho do BESS", pv_to_bat, THEME.battery, False),
        ("Export do siete", pv_to_grid, THEME.grid, True),
        ("Orezané (clipping)", pv_clipped, THEME.ink_subtle, True),
    ]
    rows = []
    for label, val, color, main in items:
        if val < 0.5 and not main:
            continue
        if val < 0.5 and label == "Orezané (clipping)":
            continue
        pct = (val / total * 100) if total > 0 else 0
        indent = "" if main else "ml-md"
        rows.append(f"""
            <div class="legend-row {indent}">
                <span class="dot" style="background:{color}"></span>
                <span class="lbl">{label}</span>
                <span class="val"><b>{pct:.1f}%</b> · {val:,.0f} kWh</span>
            </div>""".replace(",", " "))
    return f'<div class="legend-list">{"".join(rows)}</div>'


# ============================================================================
# 2. SITE ENERGY CONSUMPTION DONUT (Energia tab)
# ============================================================================
def chart_site_consumption_donut(
    solar_to_load: float, bat_to_load: float, grid_to_load: float,
) -> "go.Figure":
    total = solar_to_load + bat_to_load + grid_to_load
    raw = [
        ("FVE", solar_to_load, THEME.solar),
        ("BESS", bat_to_load, THEME.battery),
        ("Sieť", grid_to_load, THEME.grid),
    ]
    keep = [(l, v, c) for l, v, c in raw if v > 0.5]
    labels, vals, colors = zip(*keep) if keep else (("Sieť",), (1,), (THEME.grid,))

    fig = go.Figure(data=[go.Pie(
        labels=list(labels), values=list(vals), hole=0.66,
        marker={"colors": list(colors), "line": {"color": "white", "width": 3}},
        textinfo="none",
        hovertemplate="<b>%{label}</b>: %{value:,.0f} kWh (%{percent})<extra></extra>",
        sort=False, showlegend=False,
    )])
    fig.update_layout(
        plot_bgcolor="white", paper_bgcolor="white",
        font={"family": THEME.font_sans, "color": THEME.ink, "size": 11},
        height=300, margin={"l": 20, "r": 20, "t": 20, "b": 20},
        annotations=[
            {"text": "<b>Spotreba</b>", "x": 0.5, "y": 0.58,
             "showarrow": False, "font": {"size": 11, "color": THEME.ink_muted}},
            {"text": f"<b style='font-size:24px;color:{THEME.ink}'>{total:,.0f}</b><br>"
                     f"<span style='font-size:11px;color:{THEME.ink_muted}'>kWh</span>",
             "x": 0.5, "y": 0.42, "showarrow": False},
        ],
    )
    return fig


def render_site_consumption_legend(
    solar_to_load: float, bat_to_load: float, grid_to_load: float,
) -> str:
    total = solar_to_load + bat_to_load + grid_to_load
    rows = []
    for label, val, color in [
        ("FVE", solar_to_load, THEME.solar),
        ("BESS", bat_to_load, THEME.battery),
        ("Sieť (import)", grid_to_load, THEME.grid),
    ]:
        pct = val / total * 100 if total > 0 else 0
        rows.append(f"""
            <div class="legend-row">
                <span class="dot" style="background:{color}"></span>
                <span class="lbl">{label}</span>
                <span class="val"><b>{pct:.1f}%</b> · {val:,.0f} kWh</span>
            </div>""".replace(",", " "))
    return f'<div class="legend-list">{"".join(rows)}</div>'


# ============================================================================
# 3. SPAGHETTI LOAD PROFILE — všetky dni v pozadí + priemer
# ============================================================================
def chart_spaghetti_load(
    timestamps: pd.DatetimeIndex,
    load_kw: np.ndarray,
    title: str = "Denný profil — kvantily cez všetky dni v roku",
    ylabel: str = "kW",
) -> "go.Figure":
    """Kvantilový profil — P10/P25/P50/P75/P90 napríč všetkými dňami v roku.

    Lepšia alternatíva k 365 individuálnym čiaram (Plotly perf + čitateľnosť).
    Stredná modrá = medián, svetlé area = P10-P90 range, dotted = max.
    """
    df = pd.DataFrame({"ts": pd.DatetimeIndex(timestamps), "v": np.asarray(load_kw, dtype=float)})
    df["hour"] = df["ts"].dt.hour

    grouped = df.groupby("hour")["v"]
    hours = list(range(24))
    p10 = [float(grouped.get_group(h).quantile(0.10)) if h in grouped.groups else 0 for h in hours]
    p25 = [float(grouped.get_group(h).quantile(0.25)) if h in grouped.groups else 0 for h in hours]
    p50 = [float(grouped.get_group(h).quantile(0.50)) if h in grouped.groups else 0 for h in hours]
    p75 = [float(grouped.get_group(h).quantile(0.75)) if h in grouped.groups else 0 for h in hours]
    p90 = [float(grouped.get_group(h).quantile(0.90)) if h in grouped.groups else 0 for h in hours]
    peak = [float(grouped.get_group(h).max()) if h in grouped.groups else 0 for h in hours]
    mean = [float(grouped.get_group(h).mean()) if h in grouped.groups else 0 for h in hours]

    fig = go.Figure()
    # P10-P90 band (svetlá výplň)
    fig.add_trace(go.Scatter(
        x=hours, y=p90, mode="lines", line={"color": "rgba(0,0,0,0)"},
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=hours, y=p10, mode="lines", line={"color": "rgba(0,0,0,0)"},
        fill="tonexty", fillcolor="rgba(90,141,238,0.12)",
        name="P10–P90 rozsah",
        hoverinfo="skip",
    ))
    # P25-P75 band (tmavšia výplň)
    fig.add_trace(go.Scatter(
        x=hours, y=p75, mode="lines", line={"color": "rgba(0,0,0,0)"},
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=hours, y=p25, mode="lines", line={"color": "rgba(0,0,0,0)"},
        fill="tonexty", fillcolor="rgba(90,141,238,0.25)",
        name="P25–P75 rozsah",
        hoverinfo="skip",
    ))
    # Medián
    fig.add_trace(go.Scatter(
        x=hours, y=p50, name="Medián (P50)",
        mode="lines",
        line={"color": THEME.accent_blue, "width": 3},
        hovertemplate="%{x}:00 — medián <b>%{y:.1f} " + ylabel + "</b><extra></extra>",
    ))
    # Peak
    fig.add_trace(go.Scatter(
        x=hours, y=peak, name="Peak (max)",
        mode="lines",
        line={"color": THEME.ink_muted, "width": 1, "dash": "dot"},
        hovertemplate="%{x}:00 — peak <b>%{y:.1f} " + ylabel + "</b><extra></extra>",
    ))

    lay = _layout(title, height=380)
    lay["xaxis"].update({"title": "Hodina dňa", "dtick": 4, "range": [-0.5, 23.5],
                          "ticksuffix": ":00"})
    lay["yaxis"]["title"] = ylabel
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 4. CASHFLOW (čistá verzia)
# ============================================================================
def chart_cashflow(
    years: list[int], cf_yearly: list[float], cf_accumulated: list[float],
    title: Optional[str] = None,
) -> "go.Figure":
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=years, y=cf_accumulated, name="Kumulatívne",
        mode="lines",
        line={"color": THEME.accent_blue, "width": 2.5},
        fill="tozeroy", fillcolor=f"rgba(90,141,238,0.10)",
        hovertemplate="Rok %{x}<br>Kumulatívne <b>%{y:,.0f} €</b><extra></extra>",
    ))
    colors = [THEME.danger if v < 0 else THEME.accent_blue for v in cf_yearly]
    fig.add_trace(go.Scatter(
        x=years, y=cf_yearly, name="Ročný cashflow",
        mode="markers",
        marker={"size": 8, "color": colors,
                "line": {"color": "white", "width": 1.5}},
        hovertemplate="Rok %{x}<br><b>%{y:,.0f} €</b><extra></extra>",
    ))
    lay = _layout(title, height=380)
    lay["xaxis"].update({"title": "Rok projektu", "dtick": 2})
    lay["yaxis"]["title"] = "EUR"
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 5. MONTHLY EARNINGS STACKED
# ============================================================================
def chart_monthly_earnings(
    months: list[str], streams: dict[str, list[float]], title: Optional[str] = None,
) -> "go.Figure":
    fig = go.Figure()
    color_map = {
        "Samospotreba FVE":  THEME.solar,
        "Export FVE":        THEME.solar_light,
        "Samospotreba BESS": THEME.battery,
        "Arbitráž":          THEME.grid,
        "Peak shaving":      THEME.warning,
        "MRK penalty avoid": THEME.ink_subtle,
    }
    for name, vals in streams.items():
        if any(abs(v) > 0.01 for v in vals):
            fig.add_trace(go.Bar(
                x=months, y=vals, name=name,
                marker={"color": color_map.get(name, THEME.primary),
                        "line": {"color": "white", "width": 1}},
                hovertemplate=f"<b>{name}</b><br>%{{x}}: %{{y:,.0f}} €<extra></extra>",
            ))
    lay = _layout(title, height=340)
    lay["barmode"] = "relative"
    lay["yaxis"]["title"] = "€ / mesiac"
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 6. INTERVAL ACTIVITY (Intervaly tab) — bars + lines, opravený bug
# ============================================================================
def chart_weekly_earnings(
    weeks_iso: list[str],
    streams: dict[str, list[float]],
    highlighted_week: Optional[str] = None,
    title: Optional[str] = None,
) -> "go.Figure":
    """Týždenný stacked bar chart — € zarobené per týždeň, rozdelené per value stream.

    Args:
        weeks_iso: list ISO dátumov začiatkov týždňov (napr. "2025-04-06")
        streams: dict {nazov_streamu: [€ values per týždeň]}
        highlighted_week: ISO dátum týždňa ktorý sa má zvýrazniť tučnejším okrajom
    """
    color_map = {
        "Samospotreba FVE":  THEME.solar,
        "Export FVE":        THEME.solar_light,
        "BESS úspory":       THEME.battery,
        "Samospotreba BESS": THEME.battery,
        "Arbitráž (spot)":   THEME.accent_blue,
        "Peak shaving":      THEME.warning,
        "MRK penalty avoid": THEME.ink_subtle,
    }
    fig = go.Figure()
    for name, vals in streams.items():
        if any(abs(v) > 0.01 for v in vals):
            # Pre highlighted week zvýrazni stĺpec hrubším borderom
            line_widths = [3 if w == highlighted_week else 0 for w in weeks_iso]
            line_colors = [THEME.ink if w == highlighted_week else "white" for w in weeks_iso]
            fig.add_trace(go.Bar(
                x=weeks_iso, y=vals, name=name,
                marker={
                    "color": color_map.get(name, THEME.primary),
                    "line": {"color": line_colors, "width": line_widths},
                },
                hovertemplate=f"<b>{name}</b><br>Týždeň %{{x}}<br><b>%{{y:,.0f}} €</b><extra></extra>",
            ))
    lay = _layout(title, height=380)
    lay["barmode"] = "relative"
    lay["xaxis"]["type"] = "date"
    lay["xaxis"]["tickformat"] = "%d. %m"
    lay["xaxis"]["dtick"] = "M1"
    lay["yaxis"]["title"] = "€ / týždeň"
    fig.update_layout(**lay)
    return fig


def render_week_detail_panel(
    week_label: str, total_eur: float, streams: dict[str, float],
) -> str:
    """HTML side panel pre detail týždňa — celkové € + breakdown per stream."""
    color_map = {
        "Samospotreba FVE":  THEME.solar,
        "Export FVE":        THEME.solar_light,
        "BESS úspory":       THEME.battery,
        "Samospotreba BESS": THEME.battery,
        "Arbitráž (spot)":   THEME.accent_blue,
        "Peak shaving":      THEME.warning,
        "MRK penalty avoid": THEME.ink_subtle,
    }
    rows = []
    for name, val in sorted(streams.items(), key=lambda kv: -abs(kv[1])):
        if abs(val) < 0.5:
            continue
        color = color_map.get(name, THEME.primary)
        val_str = f"{val:,.0f}".replace(",", " ") + " €"
        rows.append(f"""
            <div class="wd-row">
                <span class="wd-dot" style="background:{color}"></span>
                <span class="wd-lbl">{name}</span>
                <span class="wd-val">{val_str}</span>
            </div>""")
    return f"""
        <div class="week-detail">
            <div class="wd-header">Týždeň od</div>
            <div class="wd-week">{week_label}</div>
            <div class="wd-total">{total_eur:,.0f} €</div>
            <div class="wd-streams">{"".join(rows)}</div>
        </div>""".replace(",", " ")


def chart_bess_activity_breakdown(
    sample_index: pd.DatetimeIndex,
    pv_to_bat_kw: np.ndarray, grid_to_bat_kw: np.ndarray, bat_to_load_kw: np.ndarray,
    spot_eur_mwh: np.ndarray, title: Optional[str] = None,
) -> "go.Figure":
    """BESS aktivita rozdelená: žltá nabíjanie z PV, modrá nabíjanie zo siete (arbitráž),
    fialová vybíjanie do load. Plus spot cena ako overlay na druhej Y osi.

    Toto je kľúčový graf pre Intervaly tab — ukazuje jednoznačne kde ide arbitráž
    (modré bars v lacných hodinách) a kde ide PV samospotreba (žlté bars).
    """
    x_list = [t.strftime("%Y-%m-%d %H:%M") for t in pd.DatetimeIndex(sample_index)]
    pv_chg = (-np.asarray(pv_to_bat_kw, dtype=float)).tolist()        # negative (nabíjanie)
    grid_chg = (-np.asarray(grid_to_bat_kw, dtype=float)).tolist()    # negative (nabíjanie zo siete)
    discharge = np.asarray(bat_to_load_kw, dtype=float).tolist()      # positive (vybíjanie)
    spot = np.asarray(spot_eur_mwh, dtype=float).tolist()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=x_list, y=pv_chg, name="Nabíjanie z FVE",
        marker={"color": THEME.solar, "line": {"width": 0}},
        hovertemplate="%{x}<br>Z FVE <b>%{y:.1f} kW</b><extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=x_list, y=grid_chg, name="Nabíjanie zo siete (arbitráž)",
        marker={"color": THEME.accent_blue, "line": {"width": 0}},
        hovertemplate="%{x}<br>Zo siete <b>%{y:.1f} kW</b><extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=x_list, y=discharge, name="Vybíjanie do záťaže",
        marker={"color": THEME.battery, "line": {"width": 0}},
        hovertemplate="%{x}<br>Do záťaže <b>%{y:.1f} kW</b><extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x_list, y=spot, name="OKTE spot",
        line={"color": THEME.ink, "width": 1.5, "dash": "dot"},
        yaxis="y2", opacity=0.6,
        hovertemplate="%{x}<br>Spot <b>%{y:.0f} €/MWh</b><extra></extra>",
    ))
    lay = _layout(title, height=400)
    lay["barmode"] = "relative"
    lay["xaxis"]["type"] = "date"
    lay["xaxis"]["tickformat"] = "%d. %m"
    lay["yaxis"]["title"] = "BESS kW (− nabíja / + vybíja)"
    lay["yaxis2"] = {
        "title": {"text": "OKTE spot (€/MWh)", "font": {"color": THEME.ink_muted, "size": 11}},
        "overlaying": "y", "side": "right",
        "gridcolor": "rgba(0,0,0,0)",
        "tickfont": {"size": 10, "color": THEME.ink_muted},
    }
    fig.update_layout(**lay)
    return fig


def chart_interval_activity(
    sample_index: pd.DatetimeIndex,
    pv_kw: np.ndarray, load_before_kw: np.ndarray, grid_import_kw: np.ndarray,
    bat_net_kw: np.ndarray, title: Optional[str] = None,
) -> "go.Figure":
    """Týždenný dispatch — všetky Scatter (žiadny Bar). Žltá fill area pre PV,
    modrá hrubá pre load_after, sivá dotted pre load_before, fialová pre BESS net.

    Forced datetime axis + ISO timestamps cez to_pydatetime() pre rendering safety.
    """
    # Convert DatetimeIndex → list of ISO strings (avoid Plotly index-as-integer bug)
    x_list = [t.strftime("%Y-%m-%d %H:%M") for t in pd.DatetimeIndex(sample_index)]

    # Numeric arrays (defensive cast)
    pv_y = np.asarray(pv_kw, dtype=float).tolist()
    load_b_y = np.asarray(load_before_kw, dtype=float).tolist()
    grid_y = np.asarray(grid_import_kw, dtype=float).tolist()
    bat_y = np.asarray(bat_net_kw, dtype=float).tolist()

    fig = go.Figure()
    # PV — fill area (žltá)
    fig.add_trace(go.Scatter(
        x=x_list, y=pv_y, name="FVE výroba",
        mode="lines",
        line={"color": THEME.solar, "width": 1.5, "shape": "hv"},
        fill="tozeroy", fillcolor="rgba(242,199,68,0.30)",
        hovertemplate="%{x}<br>FVE <b>%{y:.1f} kW</b><extra></extra>",
    ))
    # Záťaž pred (sivá tenká dotted)
    fig.add_trace(go.Scatter(
        x=x_list, y=load_b_y, name="Záťaž — pred",
        mode="lines",
        line={"color": THEME.ink_subtle, "width": 1.2, "dash": "dot", "shape": "hv"},
        hovertemplate="%{x}<br>Pred <b>%{y:.1f} kW</b><extra></extra>",
    ))
    # Záťaž po (modrá hrubá)
    fig.add_trace(go.Scatter(
        x=x_list, y=grid_y, name="Záťaž — po (zo siete)",
        mode="lines",
        line={"color": THEME.accent_blue, "width": 2.5, "shape": "hv"},
        hovertemplate="%{x}<br>Po BESS <b>%{y:.1f} kW</b><extra></extra>",
    ))
    # BESS net (fialová)
    fig.add_trace(go.Scatter(
        x=x_list, y=bat_y, name="BESS net (+ vybíja / − nabíja)",
        mode="lines",
        line={"color": THEME.battery, "width": 2, "shape": "hv"},
        hovertemplate="%{x}<br>BESS <b>%{y:+.1f} kW</b><extra></extra>",
    ))
    lay = _layout(title, height=400)
    lay["yaxis"]["title"] = "kW"
    lay["xaxis"]["type"] = "date"  # FORCE datetime axis
    lay["xaxis"]["tickformat"] = "%d. %m"
    fig.update_layout(**lay)
    return fig


def chart_interval_soc(
    sample_index: pd.DatetimeIndex, soc_pct: np.ndarray, title: Optional[str] = None,
) -> "go.Figure":
    x_list = [t.strftime("%Y-%m-%d %H:%M") for t in pd.DatetimeIndex(sample_index)]
    y_list = np.asarray(soc_pct, dtype=float).tolist()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x_list, y=y_list, name="SoC",
        mode="lines",
        line={"color": THEME.battery, "width": 2, "shape": "spline", "smoothing": 0.3},
        fill="tozeroy", fillcolor="rgba(155,111,224,0.14)",
        hovertemplate="%{x}<br>SoC <b>%{y:.0f}%</b><extra></extra>",
    ))
    lay = _layout(title, height=240, show_legend=False)
    lay["yaxis"].update({"title": "SoC %", "range": [0, 100]})
    lay["xaxis"]["type"] = "date"
    lay["xaxis"]["tickformat"] = "%d. %m"
    fig.update_layout(**lay)
    return fig


def chart_interval_spot(
    sample_index: pd.DatetimeIndex, spot_eur_mwh: np.ndarray, title: Optional[str] = None,
) -> "go.Figure":
    x_list = [t.strftime("%Y-%m-%d %H:%M") for t in pd.DatetimeIndex(sample_index)]
    y_list = np.asarray(spot_eur_mwh, dtype=float).tolist()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x_list, y=y_list, name="OKTE spot",
        mode="lines",
        line={"color": THEME.accent_blue, "width": 1.8, "shape": "hv"},
        fill="tozeroy", fillcolor="rgba(90,141,238,0.12)",
        hovertemplate="%{x}<br>Spot <b>%{y:.0f} €/MWh</b><extra></extra>",
    ))
    avg = float(np.mean(y_list))
    fig.add_hline(y=avg, line_dash="dot", line_color=THEME.ink_muted, line_width=1,
                  annotation_text=f"priemer {avg:.0f}",
                  annotation_position="top right",
                  annotation_font={"color": THEME.ink_muted, "size": 10})
    lay = _layout(title, height=240, show_legend=False)
    lay["yaxis"]["title"] = "€ / MWh"
    lay["xaxis"]["type"] = "date"
    lay["xaxis"]["tickformat"] = "%d. %m"
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 7. ENERGY METRICS AREA — Energy independence + Solar utilisation
# ============================================================================
def chart_energy_metric_area(
    monthly_values: list[float], title: str, color: str = None,
    avg_pct: float = None,
) -> "go.Figure":
    """Mesačný horizontálny area chart pre %-metrickú KPI."""
    color = color or THEME.accent_blue
    months_lbl = ["Apr", "Máj", "Jún", "Júl", "Aug", "Sep", "Okt", "Nov", "Dec",
                  "Jan", "Feb", "Mar"]
    avg = avg_pct if avg_pct is not None else float(np.mean(monthly_values))

    # Build fill color from hex
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    fill_rgba = f"rgba({r},{g},{b},0.25)"
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=months_lbl[:len(monthly_values)], y=monthly_values,
        mode="lines",
        line={"color": color, "width": 0, "shape": "spline"},
        fill="tozeroy", fillcolor=fill_rgba,
        hovertemplate="<b>%{x}</b>: %{y:.0f}%<extra></extra>",
    ))

    fig.update_layout(
        plot_bgcolor="white", paper_bgcolor="white",
        font={"family": THEME.font_sans, "color": THEME.ink, "size": 10},
        height=120, showlegend=False,
        margin={"l": 36, "r": 70, "t": 8, "b": 24},
        xaxis={"showgrid": False, "tickfont": {"size": 9, "color": THEME.ink_muted},
               "zeroline": False, "linecolor": THEME.border, "tickcolor": THEME.border},
        yaxis={"range": [0, 100], "showgrid": True, "gridcolor": THEME.grid_color,
               "tickfont": {"size": 9, "color": THEME.ink_muted}, "ticksuffix": "%",
               "dtick": 50, "zeroline": False, "linecolor": "rgba(0,0,0,0)"},
        annotations=[{
            "text": f"<b style='font-size:22px;color:{color}'>{avg:.0f}%</b><br>"
                    f"<span style='font-size:9px;color:{THEME.ink_muted}'>{title}</span>",
            "xref": "paper", "yref": "paper",
            "x": 1.02, "y": 0.5, "xanchor": "left", "yanchor": "middle",
            "showarrow": False, "align": "left",
        }],
    )
    return fig


# ============================================================================
# 8. SOC HEATMAP — týždne × hodiny
# ============================================================================
def chart_soc_heatmap(
    timestamps: pd.DatetimeIndex, soc_pct: np.ndarray, title: Optional[str] = None,
) -> "go.Figure":
    df = pd.DataFrame({"ts": pd.DatetimeIndex(timestamps), "soc": np.asarray(soc_pct, dtype=float) * 100})
    df["week"] = df["ts"].dt.isocalendar().week.astype(int)  # FIX: np.uint32 → int
    df["hour"] = df["ts"].dt.hour.astype(int)
    pivot = df.pivot_table(index="hour", columns="week", values="soc", aggfunc="mean")

    fig = go.Figure(data=go.Heatmap(
        z=pivot.values.tolist(),
        x=[int(c) for c in pivot.columns],  # FIX: cast na int pre Plotly
        y=[int(i) for i in pivot.index],
        colorscale=[
            [0.0, "#FEE2E2"], [0.30, THEME.warning],
            [0.65, THEME.solar], [1.0, THEME.primary_dark],
        ],
        zmin=0, zmax=100,
        colorbar={
            "title": {"text": "SoC %", "font": {"family": THEME.font_sans, "size": 11}},
            "tickfont": {"family": THEME.font_sans, "size": 10, "color": THEME.ink_muted},
            "outlinewidth": 0, "thickness": 12, "len": 0.9,
        },
        hovertemplate="Týždeň %{x} · %{y}:00<br>SoC <b>%{z:.0f}%</b><extra></extra>",
    ))
    lay = _layout(title, height=280, show_legend=False)
    lay["xaxis"].update({"title": "Týždeň v roku", "dtick": 4})
    lay["yaxis"].update({"title": "Hodina", "dtick": 6, "autorange": "reversed"})
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 9. CARBON YEARLY (clean line+bar)
# ============================================================================
def chart_carbon_summary(
    annual_co2_t: float, horizon_years: int = 20,
    annual_degradation_pct: float = 1.0,
) -> "go.Figure":
    years = list(range(1, horizon_years + 1))
    annual = [annual_co2_t * (1 - annual_degradation_pct/100) ** (y - 1) for y in years]
    cum = list(np.cumsum(annual))

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=years, y=annual, name="Ročne",
        marker={"color": THEME.primary, "line": {"width": 0}},
        hovertemplate="Rok %{x} — <b>%{y:.1f} t CO₂</b><extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=years, y=cum, name="Kumulatívne",
        mode="lines", line={"color": THEME.accent_blue, "width": 2.5},
        yaxis="y2",
        hovertemplate="Rok %{x} — kum <b>%{y:.0f} t</b><extra></extra>",
    ))
    lay = _layout("Vyhnuté CO₂ počas životnosti", height=320)
    lay["xaxis"].update({"title": "Rok", "dtick": 2})
    lay["yaxis"]["title"] = "t CO₂ / rok"
    lay["yaxis2"] = {
        "title": {"text": "Kumulatívne (t)", "font": {"color": THEME.accent_blue}},
        "overlaying": "y", "side": "right",
        "gridcolor": "rgba(0,0,0,0)",
        "tickfont": {"size": 10, "color": THEME.accent_blue},
    }
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 10. BATTERY DEGRADATION
# ============================================================================
def chart_battery_degradation(
    horizon_years: int = 20, annual_degradation_pct: float = 2.0,
    first_year_drop_pct: float = 2.0, eol_soh: float = 0.80,
    replacement_year: Optional[int] = 12,
) -> "go.Figure":
    years, soh = [0], [1.0]
    for y in range(1, horizon_years + 1):
        if replacement_year and y == replacement_year:
            soh.append(1.0)
        elif replacement_year and y > replacement_year:
            yr = y - replacement_year
            val = (1 - first_year_drop_pct/100) if yr == 1 else \
                  (1 - first_year_drop_pct/100) * (1 - annual_degradation_pct/100) ** (yr - 1)
            soh.append(val)
        elif y == 1:
            soh.append(1 - first_year_drop_pct/100)
        else:
            soh.append((1 - first_year_drop_pct/100) * (1 - annual_degradation_pct/100) ** (y - 1))
        years.append(y)

    soh_pct = [s * 100 for s in soh]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=years, y=soh_pct, name="BESS SoH",
        mode="lines",
        line={"color": THEME.battery, "width": 2.5},
        fill="tozeroy", fillcolor="rgba(155,111,224,0.10)",
        hovertemplate="Rok %{x} · SoH <b>%{y:.1f}%</b><extra></extra>",
    ))
    fig.add_hline(y=eol_soh * 100, line_dash="dash", line_color=THEME.danger, line_width=1,
                  annotation_text=f"EOL ({eol_soh*100:.0f}%)",
                  annotation_position="bottom right",
                  annotation_font={"color": THEME.danger, "size": 10})
    if replacement_year and replacement_year < horizon_years:
        fig.add_vline(x=replacement_year, line_color=THEME.warning, line_width=1, line_dash="dot",
                      annotation_text="Výmena",
                      annotation_position="top",
                      annotation_font={"color": THEME.warning, "size": 10})
    lay = _layout("Degradácia BESS počas životnosti", height=300, show_legend=False)
    lay["xaxis"].update({"title": "Rok projektu", "dtick": 2})
    lay["yaxis"].update({"title": "SoH (%)", "range": [60, 102]})
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 11. MONTHLY PV
# ============================================================================
def chart_monthly_pv(monthly_kwh: list[float], title: Optional[str] = None) -> "go.Figure":
    months_lbl = ["Apr", "Máj", "Jún", "Júl", "Aug", "Sep", "Okt", "Nov", "Dec",
                  "Jan", "Feb", "Mar"]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=months_lbl[:len(monthly_kwh)], y=[v / 1000 for v in monthly_kwh],
        marker={"color": THEME.solar, "line": {"width": 0}},
        hovertemplate="<b>%{x}</b>: %{y:.1f} MWh<extra></extra>",
    ))
    lay = _layout(title, height=280, show_legend=False)
    lay["yaxis"]["title"] = "MWh / mesiac"
    fig.update_layout(**lay)
    return fig


# ============================================================================
# 12. TORNADO SENSITIVITY
# ============================================================================
def chart_tornado_sensitivity(
    tornado_data: list[dict], baseline_npv: float, title: Optional[str] = None,
) -> "go.Figure":
    names = [t["variable"].replace("_", " ").replace("factor", "").strip().capitalize()
             for t in tornado_data]
    lo = [t["delta_low_eur"] for t in tornado_data]
    hi = [t["delta_high_eur"] for t in tornado_data]
    fig = go.Figure()
    fig.add_trace(go.Bar(y=names, x=lo, name="Pesimistický",
        orientation="h", marker={"color": THEME.danger, "line": {"color": "white", "width": 1}}))
    fig.add_trace(go.Bar(y=names, x=hi, name="Optimistický",
        orientation="h", marker={"color": THEME.primary, "line": {"color": "white", "width": 1}}))
    lay = _layout(title, height=320)
    lay["barmode"] = "overlay"
    lay["xaxis"]["title"] = f"Δ NPV vs baseline ({baseline_npv:,.0f} €)"
    lay["yaxis"]["autorange"] = "reversed"
    fig.update_layout(**lay)
    return fig
