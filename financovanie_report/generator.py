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
    # Always full integer — no M€ abbreviation (user requirement)
    formatted = f"{v:,.0f}".replace(",", " ")
    return f"{sign}{formatted} €"


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
        _HERE / "energovision_logo.png",          # čisté logo (transparentné) — preferované
        _HERE.parent / "energovision_logo.png",
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
    """Overview cumulative cashflow chart — všetky varianty, web-kvalita."""
    if not cf_data or not variants:
        return ""

    W, H = 600, 250
    PL, PR, PT, PB = 70, 24, 16, 34

    variant_keys = [v["key"] for v in variants]
    cum: dict[str, list[float]] = {k: [] for k in variant_keys}
    years = [row["year"] for row in cf_data if "year" in row]
    for k in variant_keys:
        running = 0.0
        pts: list[float] = []
        for row in cf_data:
            if "year" not in row:
                continue
            running += float(row.get(k, 0) or 0)
            pts.append(running)
        cum[k] = pts

    all_vals = [v for lst in cum.values() for v in lst]
    if not all_vals:
        return ""
    min_v = min(all_vals) * 1.08
    max_v = max(all_vals) * 1.05
    if max_v == min_v:
        max_v = min_v + 1
    n = len(years)

    def sx(i: int) -> float:
        return PL + (i / max(n - 1, 1)) * (W - PL - PR)

    def sy(v: float) -> float:
        return PT + (1 - (v - min_v) / (max_v - min_v)) * (H - PT - PB)

    color_map = {
        "ppa10": "#6366f1", "ppa15": "#a855f7", "leas": "#f59e0b",
        "sih": "#10b981", "dot": "#ef4444", "vl": "#3b82f6",
    }

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    # horizontálne gridlines + y popisy (5 úrovní)
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        val = min_v + f * (max_v - min_v)
        y = sy(val)
        parts.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{W-PR}" y2="{y:.1f}" stroke="#eef2f7" stroke-width="1"/>')
        lab = f"{val/1000:.0f}k €" if abs(val) >= 1000 else f"{val:.0f} €"
        parts.append(f'<text x="{PL-8}" y="{y+3:.1f}" text-anchor="end" font-size="8" fill="#94a3b8">{lab}</text>')

    # vertikálne gridlines
    for i, yr in enumerate(years):
        if yr in (5, 10, 15, 20):
            parts.append(f'<line x1="{sx(i):.1f}" y1="{PT}" x2="{sx(i):.1f}" y2="{PT+(H-PT-PB):.1f}" stroke="#f4f6f9" stroke-width="1"/>')

    # nulová os
    if min_v < 0 < max_v:
        zy = sy(0)
        parts.append(f'<line x1="{PL}" y1="{zy:.1f}" x2="{W-PR}" y2="{zy:.1f}" stroke="#475569" stroke-width="1.4" stroke-dasharray="5,3"/>')
        parts.append(f'<text x="{W-PR}" y="{zy-4:.1f}" text-anchor="end" font-size="7.5" fill="#64748b">bod zvratu (0 €)</text>')

    # krivky + koncový bod
    for vc in variants:
        k = vc["key"]
        col = color_map.get(k, vc.get("color", "#94a3b8"))
        pts_str = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(cum[k]))
        parts.append(f'<polyline points="{pts_str}" fill="none" stroke="{col}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>')
        if cum[k]:
            parts.append(f'<circle cx="{sx(len(cum[k])-1):.1f}" cy="{sy(cum[k][-1]):.1f}" r="2.8" fill="{col}"/>')

    # x popisy
    for i, yr in enumerate(years):
        if yr in (5, 10, 15, 20):
            parts.append(f'<text x="{sx(i):.1f}" y="{H-PB+16}" text-anchor="middle" font-size="8" fill="#64748b">r{yr}</text>')

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
    for yr in [5, 10, 15, 20]:
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
        # Bug 9 guard: nedôveryhodné IRR (bez reálneho vstupu / extrémne) → N/A
        try:
            _irr = v.get("irr_pct")
            if _irr is not None and (float(_irr) > 100 or float(v.get("initial_investment") or 0) <= 1):
                v["irr_pct"] = None
        except (TypeError, ValueError):
            v["irr_pct"] = None
        breakdown = v.get("cf_breakdown", [])
        init_inv = float(v.get("init_inv", 0) or 0)
        color = color_map.get(v.get("key", ""), v.get("color", "#94a3b8"))
        v["mini_svg"] = _mini_svg(breakdown, init_inv, color)
        # Míľnikový kumulatív (r5/r10/r15/r20/r25) pre záverečnú porovnávaciu tabuľku
        cum_by_year = {}
        for row in breakdown:
            yy = row.get("y")
            if yy is not None:
                try:
                    cum_by_year[int(yy)] = row.get("cum")
                except (TypeError, ValueError):
                    pass
        v["cum_milestones"] = [cum_by_year.get(yr) for yr in (5, 10, 15, 20, 25)]
        v["cum_by_year"] = {int(k): val for k, val in cum_by_year.items()}

    # Záverečná tabuľka: roky 1–10 po jednom + 15/20/25
    ctx["final_years"] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20]

    # Odporúčanie + záver (vždy prítomné, deterministické z dát)
    def _n(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return -1e18
    if variants:
        npv_winner = max(variants, key=lambda v: _n(v.get("npv")))
        recommended = next((v for v in variants if v.get("key") == "sih"), None) or npv_winner
        low_up = min(variants, key=lambda v: (v.get("initial_investment") or 0))
        ctx["npv_winner"] = npv_winner
        ctx["recommended"] = recommended
        # prepíš headline odporúčanie na vyváženú voľbu (konzistentne s webom)
        ctx["best_key"] = recommended.get("key")
        ctx["best_label"] = recommended.get("label")
        ctx["best_npv"] = recommended.get("npv")
        ctx["best_payback"] = recommended.get("payback")
        _wkey = npv_winner.get("key")
        _winit = float(npv_winner.get("initial_investment") or 0)
        lines = []
        # 1) Najvyššia NPV — formulácia podľa toho, či víťaz vyžaduje vstupný kapitál
        if _wkey in ("ppa10", "ppa15", "sih") or _winit <= 1:
            lines.append(
                f"Najvyššiu čistú súčasnú hodnotu za 20 rokov dosahuje {npv_winner.get('label')} "
                f"({_fmt_eur(npv_winner.get('npv'))}) — bez vstupného kapitálu, s kladným tokom od začiatku."
            )
        else:
            lines.append(
                f"Najvyššiu čistú súčasnú hodnotu za 20 rokov dosahuje {npv_winner.get('label')} "
                f"({_fmt_eur(npv_winner.get('npv'))}); vyžaduje však jednorazový vstupný kapitál {_fmt_eur(_winit)}."
            )
        # 2) Najnižší vstupný náklad — modely bez vkladu
        lines.append(
            f"Najnižší vstupný náklad ({_fmt_eur(low_up.get('initial_investment') or 0)}) a kladný tok od "
            f"začiatku majú modely bez vstupného vkladu (PPA, SIH) — pozitívny cashflow od prvého mesiaca."
        )
        # 3) Vyvážené odporúčanie
        lines.append(
            f"Ako vyváženú voľbu odporúčame {recommended.get('label')}: nulový vstup, vlastníctvo elektrárne "
            f"od prvého dňa, fixná sadzba a kladný mesačný tok už počas splácania. Vlastná kúpa dáva najvyšší "
            f"absolútny zisk za 20 rokov, ak má firma voľný kapitál."
        )
        ctx["conclusion_lines"] = lines

    tmpl = env.get_template("report.html")
    html_str = tmpl.render(**ctx)

    css_path = str(_TEMPLATES_DIR / "styles.css")
    pdf_bytes = HTML(string=html_str, base_url=str(_HERE)).write_pdf(
        stylesheets=[CSS(filename=css_path)]
    )
    return pdf_bytes
