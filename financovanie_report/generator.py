"""Financovanie Report — Jinja2 HTML → WeasyPrint PDF.

Public API:
    generate_financovanie_pdf(context: dict) -> bytes

Context dict structure:
    project_name, project_id, client_name, site_address, report_date
    capex               float  — celkový CAPEX v EUR
    pv_kwp              float
    annual_prod_mwh     float
    scenario            str    — "base"|"optimistic"|"pessimistic"

    Per-variant dicts in `variants` list:
      key, label, color
      npv, irr_pct, payback, dscr (optional)
      monthly_payment (optional), annual_payment (optional)
      initial_investment

    Best variant: best_key, best_label, best_irr, best_npv, best_payback

    Leasing specifics (optional):
      leas_akontacia_pct, leas_akontacia_eur, leas_principal, leas_monthly, leas_yr, leas_r_pct

    SIH specifics (optional):
      sih_ann, sih_r_pct, sih_yr

    Dotacia specifics (optional):
      dot_grant, dot_own, dot_pct

    cf_data: list of dicts {year, variant_key: net_cf, ...} — for chart
"""
from __future__ import annotations

import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Undefined
from weasyprint import HTML, CSS

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"


# ──────────────────────────── helpers ────────────────────────────

def _fmt_eur(value: Any) -> str:
    if value is None or isinstance(value, Undefined):
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}{v/1_000_000:,.2f} M €".replace(",", " ").replace(".", ",")
    if v >= 1000:
        return f"{sign}{v:,.0f} €".replace(",", " ")
    return f"{sign}{v:,.0f} €"


def _fmt_pct(value: Any, decimals: int = 1) -> str:
    if value is None or isinstance(value, Undefined):
        return "—"
    try:
        return f"{float(value):.{decimals}f} %".replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value: Any, decimals: int = 0) -> str:
    if value is None or isinstance(value, Undefined):
        return "—"
    try:
        v = float(value)
        return f"{v:,.{decimals}f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def _logo_b64() -> str:
    candidates = [
        _HERE.parent / "energovision_header.png",
        _HERE.parent / "analyza_om" / "logo.png",
    ]
    for p in candidates:
        if p.exists():
            return base64.b64encode(p.read_bytes()).decode()
    return ""


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
        undefined=Undefined,
    )
    env.filters["eur"] = _fmt_eur
    env.filters["pct"] = _fmt_pct
    env.filters["num"] = _fmt_num
    env.globals["now"] = datetime.now
    return env


# ──────────────────────────── inline SVG chart ────────────────────────────

def _cumulative_svg(variants: list[dict], cf_data: list[dict]) -> str:
    """Simple inline SVG cumulative cashflow chart — no external deps."""
    if not cf_data or not variants:
        return ""

    W, H = 540, 220
    PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 16, 36

    # collect all net_cf values
    all_vals: list[float] = []
    variant_keys = [v["key"] for v in variants]
    cum: dict[str, list[float]] = {k: [] for k in variant_keys}
    years = [row["year"] for row in cf_data]

    for row in cf_data:
        for k in variant_keys:
            cum[k].append(float(row.get(k, 0) or 0))

    # cumulative
    for k in variant_keys:
        running = 0.0
        cumulative = []
        for v in cum[k]:
            running += v
            cumulative.append(running)
        cum[k] = cumulative

    for k in variant_keys:
        all_vals.extend(cum[k])

    if not all_vals:
        return ""

    min_v = min(all_vals)
    max_v = max(all_vals)
    if max_v == min_v:
        max_v = min_v + 1

    def sx(i: int) -> float:
        return PAD_L + (i / (len(years) - 1)) * (W - PAD_L - PAD_R) if len(years) > 1 else PAD_L

    def sy(v: float) -> float:
        return PAD_T + (1 - (v - min_v) / (max_v - min_v)) * (H - PAD_T - PAD_B)

    color_map = {
        "ppa10": "#22c55e", "ppa15": "#16a34a", "leas": "#f59e0b",
        "sih": "#3b82f6", "dot": "#8b5cf6", "vl": "#92D050",
    }

    lines = []
    for vc in variants:
        k = vc["key"]
        col = color_map.get(k, vc.get("color", "#94a3b8"))
        pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(cum[k]))
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" stroke-linejoin="round"/>')

    # zero line
    zy = sy(0)
    zero_line = f'<line x1="{PAD_L}" y1="{zy:.1f}" x2="{W-PAD_R}" y2="{zy:.1f}" stroke="#e5e7eb" stroke-width="1" stroke-dasharray="4,3"/>'

    # x-axis labels every 5 years
    x_labels = []
    for i, yr in enumerate(years):
        if yr % 5 == 0:
            x = sx(i)
            x_labels.append(f'<text x="{x:.1f}" y="{H-PAD_B+14}" text-anchor="middle" font-size="8" fill="#9ca3af">{yr}</text>')

    # y-axis label
    y_label = f'<text x="{PAD_L-4}" y="{sy(0):.1f}" text-anchor="end" dominant-baseline="middle" font-size="8" fill="#9ca3af">0</text>'

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="white"/>',
        zero_line,
        *lines,
        *x_labels,
        y_label,
        "</svg>",
    ]
    return "".join(svg_parts)


# ──────────────────────────── public API ────────────────────────────

def generate_financovanie_pdf(context: dict) -> bytes:
    env = _make_env()

    ctx = dict(context)
    ctx.setdefault("report_date", datetime.now().strftime("%-d. %-m. %Y"))
    ctx.setdefault("project_name", "Fotovoltická elektráreň")
    ctx.setdefault("client_name", "")
    ctx.setdefault("site_address", "")
    ctx["logo_b64"] = _logo_b64()

    variants = ctx.get("variants", [])
    cf_data = ctx.get("cf_data", [])
    ctx["cumulative_svg"] = _cumulative_svg(variants, cf_data)

    tmpl = env.get_template("report.html")
    html_str = tmpl.render(**ctx)

    css_path = str(_TEMPLATES_DIR / "styles.css")
    pdf_bytes = HTML(string=html_str, base_url=str(_HERE)).write_pdf(
        stylesheets=[CSS(filename=css_path)]
    )
    return pdf_bytes
