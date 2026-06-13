"""Financovanie Report — Jinja2 HTML → WeasyPrint PDF.

Public API:
    generate_financovanie_pdf(context: dict) -> bytes
"""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Undefined, ChainableUndefined
from weasyprint import HTML, CSS


class _SafeUndefined(ChainableUndefined):
    def __str__(self) -> str:
        return ""
    def __iter__(self):
        return iter([])
    def __len__(self) -> int:
        return 0
    def __bool__(self) -> bool:
        return False


_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"

# ──────────────────── helpers ────────────────────

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
        s = f"{v/1_000_000:,.3f}".replace(",", " ").replace(".", ",")
        # trim trailing zeros after comma
        parts = s.split(",")
        dec = parts[1].rstrip("0") if len(parts) > 1 else ""
        if dec:
            return f"{sign}{parts[0]},{dec} M €"
        return f"{sign}{parts[0]} M €"
    if v >= 1000:
        return f"{sign}{v:,.0f} €".replace(",", " ")
    return f"{sign}{v:,.0f} €"


def _fmt_pct(value: Any, decimals: int = 1) -> str:
    if value is None or isinstance(value, Undefined):
        return "N/A"
    try:
        v = float(value)
        if v <= -99.9:
            return "N/A"
        return f"{v:.{decimals}f} %".replace(".", ",")
    except (TypeError, ValueError):
        return "N/A"


def _fmt_num(value: Any, decimals: int = 0) -> str:
    if value is None or isinstance(value, Undefined):
        return "—"
    try:
        v = float(value)
        return f"{v:,.{decimals}f}".replace(",", " ").replace(".", ",")
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
        undefined=_SafeUndefined,
    )
    env.filters["eur"] = _fmt_eur
    env.filters["pct"] = _fmt_pct
    env.filters["num"] = _fmt_num
    env.globals["now"] = datetime.now
    return env

# ──────────────────── SVG charts ────────────────────

def _cumulative_svg(variants: list[dict], cf_data: list[dict]) -> str:
    """Overview cumulative cashflow chart — all variants."""
    if not cf_data or not variants:
        return ""

    W, H = 540, 200
    PL, PR, PT, PB = 60, 16, 14, 36

    variant_keys = [v["key"] for v in variants]
    cum: dict[str, list[float]] = {k: [] for k in variant_keys}
    years = [row["year"] for row in cf_data if "year" in row]

    # build running cumulative from net CF
    for k in variant_keys:
        running = 0.0
        pts: list[float] = []
        for row in cf_data:
            if "year" not in row:
                continue
            if row["year"] == 0:
                # initial investment already captured in init_inv
                running += float(row.get(k, 0) or 0)
            else:
                running += float(row.get(k, 0) or 0)
            pts.append(running)
        cum[k] = pts

    all_vals = [v for lst in cum.values() for v in lst]
    if not all_vals:
        return ""

    min_v = min(all_vals)
    max_v = max(all_vals)
    if max_v == min_v:
        max_v = min_v + 1

    n = len(years)

    def sx(i: int) -> float:
        return PL + (i / max(n - 1, 1)) * (W - PL - PR)

    def sy(v: float) -> float:
        return PT + (1 - (v - min_v) / (max_v - min_v)) * (H - PT - PB)

    color_map = {
        "ppa10": "#6366f1", "ppa15": "#a855f7", "leas": "#f59e0b",
        "sih": "#3b82f6", "dot": "#8b5cf6", "vl": "#92D050",
    }

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    # zero line
    if min_v < 0 < max_v:
        zy = sy(0)
        parts.append(f'<line x1="{PL}" y1="{zy:.1f}" x2="{W-PR}" y2="{zy:.1f}" stroke="#e5e7eb" stroke-width="1" stroke-dasharray="4,3"/>')
        parts.append(f'<text x="{PL-4}" y="{zy:.1f}" text-anchor="end" dominant-baseline="middle" font-size="8" fill="#9ca3af">0</text>')

    for vc in variants:
        k = vc["key"]
        col = color_map.get(k, vc.get("color", "#94a3b8"))
        pts_str = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(cum[k]))
        parts.append(f'<polyline points="{pts_str}" fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')

    # x-axis labels
    for i, yr in enumerate(years):
        if yr % 5 == 0:
            x = sx(i)
            parts.append(f'<text x="{x:.1f}" y="{H-PB+14}" text-anchor="middle" font-size="8" fill="#9ca3af">{yr}</text>')

    # y-axis: max label
    parts.append(f'<text x="{PL-4}" y="{PT+4}" text-anchor="end" font-size="8" fill="#9ca3af">{_fmt_eur(max_v)}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _mini_svg(cf_breakdown: list[dict], init_inv: float, color: str) -> str:
    """Per-variant mini cumulative chart."""
    if not cf_breakdown:
        return ""

    W, H = 260, 120
    PL, PR, PT, PB = 46, 8, 8, 24

    # build pts: start from init_inv, then add each row's cumulative
    pts: list[float] = [init_inv]
    for row in cf_breakdown:
        pts.append(float(row.get("cum", 0) or 0))

    min_v = min(pts)
    max_v = max(pts)
    if max_v == min_v:
        max_v = min_v + 1

    n = len(pts)

    def sx(i: int) -> float:
        return PL + (i / max(n - 1, 1)) * (W - PL - PR)

    def sy(v: float) -> float:
        return PT + (1 - (v - min_v) / (max_v - min_v)) * (H - PT - PB)

    # fill area
    fill_pts = [f"{sx(0):.1f},{sy(0):.1f}"]
    for i, v in enumerate(pts):
        fill_pts.append(f"{sx(i):.1f},{sy(v):.1f}")
    fill_pts.append(f"{sx(n-1):.1f},{sy(0):.1f}")
    fill_str = " ".join(fill_pts)

    line_pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(pts))

    # axis labels
    min_label = _fmt_eur(min_v)
    max_label = _fmt_eur(max_v)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    # zero line
    if min_v < 0:
        zy = sy(0)
        parts.append(f'<line x1="{PL}" y1="{zy:.1f}" x2="{W-PR}" y2="{zy:.1f}" stroke="#e5e7eb" stroke-width="1" stroke-dasharray="3,2"/>')
        parts.append(f'<text x="{PL-3}" y="{zy:.1f}" text-anchor="end" dominant-baseline="middle" font-size="7" fill="#9ca3af">0</text>')

    # fill + line
    parts.append(f'<polygon points="{fill_str}" fill="{color}18"/>')
    parts.append(f'<polyline points="{line_pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    # x labels at r5, r10, r15, r20, r25
    for yr in [5, 10, 15, 20, 25]:
        i = yr  # index = year number (pts[0]=init, pts[1]=yr1, ..., pts[25]=yr25)
        if i < n:
            x = sx(i)
            parts.append(f'<text x="{x:.1f}" y="{H-PB+12}" text-anchor="middle" font-size="7" fill="#9ca3af">r{yr}</text>')

    # y labels
    parts.append(f'<text x="{PL-3}" y="{PT+5}" text-anchor="end" font-size="7" fill="#9ca3af">{max_label}</text>')
    parts.append(f'<text x="{PL-3}" y="{H-PB-1}" text-anchor="end" font-size="7" fill="#9ca3af">{min_label}</text>')

    parts.append("</svg>")
    return "".join(parts)

# ──────────────────── public API ────────────────────

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

    # overview chart
    ctx["cumulative_svg"] = _cumulative_svg(variants, cf_data)

    # per-variant mini SVG
    color_map = {
        "ppa10": "#6366f1", "ppa15": "#a855f7", "leas": "#f59e0b",
        "sih": "#3b82f6", "dot": "#8b5cf6", "vl": "#92D050",
    }
    for v in variants:
        breakdown = v.get("cf_breakdown", [])
        init_inv = float(v.get("init_inv", 0) or 0)
        color = color_map.get(v.get("key", ""), v.get("color", "#94a3b8"))
        v["mini_svg"] = _mini_svg(breakdown, init_inv, color)

    tmpl = env.get_template("report.html")
    html_str = tmpl.render(**ctx)

    css_path = str(_TEMPLATES_DIR / "styles.css")
    pdf_bytes = HTML(string=html_str, base_url=str(_HERE)).write_pdf(
        stylesheets=[CSS(filename=css_path)]
    )
    return pdf_bytes
