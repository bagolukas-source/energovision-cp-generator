"""Orkestra-style posudok generator — Jinja2 HTML → WeasyPrint PDF.

Public API:
    generate_orkestra_pdf(context: dict) -> bytes
    render_orkestra_html(context: dict) -> str

Context dict structure (everything OPTIONAL — sensible defaults):
    project_name, project_id, client_name, site_address, posudok_date
    pv_kwp, bess_kwh, bess_kw, inverter_kw, fve_topology
    capex_total_eur, capex_pv_eur, capex_bess_eur, dotacia_eur, net_capex_eur
    saving_y1_eur, payback_years, irr_pct, npv_eur
    samospotreba_pct, samostatnost_pct
    mrk_kw, annual_kwh, tarif_typ, ems_strategy
    cf_array (list[float] len=21+), accumulated_cf_final
    pv_total_mwh, pv_to_load_mwh, pv_to_grid_mwh, pv_to_bat_mwh
    grid_to_load_mwh, bat_to_load_mwh, grid_to_bat_mwh
    load_total_mwh, grid_import_mwh, grid_export_mwh
    hourly_load_kw_before (24), hourly_load_kw_after (24)
    direct_to_load_pct, charging_battery_pct, exported_pct, curtailed_pct
    monthly_solar_to_load (12), monthly_solar_export (12), monthly_arbitrage (12)
    other_variants (list of dicts), n_variants_run
    co2_avoided_tonnes, co2_reduction_pct, trees_equivalent, barrels_oil
"""
from __future__ import annotations
import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape, Undefined
from weasyprint import HTML, CSS

from . import charts


# ============ TEMPLATES DIR ============
_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"


# ============ JINJA HELPERS ============
def _format_currency(value: float | int | None) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}{v/1_000_000:,.2f} M €".replace(",", " ").replace(".", ",")
    if v >= 1000:
        # 880 000 €
        return f"{sign}{v:,.0f} €".replace(",", " ")
    return f"{sign}{v:,.0f} €".replace(",", " ")


def _format_kwh(value: float | int | None) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000:
        return f"{v/1_000_000:,.2f} GWh".replace(".", ",")
    if v >= 1000:
        return f"{v/1000:,.0f} MWh".replace(",", " ")
    return f"{v:,.0f} kWh".replace(",", " ")


def _logo_b64() -> str:
    """Load Energovision logo and return base64 for embedding."""
    candidates = [
        _HERE.parent / "energovision_header.png",
        _HERE.parent / "analyza_om" / "logo.png",
    ]
    for p in candidates:
        if p.exists():
            return base64.b64encode(p.read_bytes()).decode("ascii")
    return ""


class _SafeUndefined(Undefined):
    """Chybajuce pole degraduje na 0/prazdny retazec namiesto padu celeho posudku.
    Posudok sa nikdy nesmie 500-nut kvoli jednemu chybajucemu cislu."""
    __slots__ = ()
    def __int__(self): return 0
    def __float__(self): return 0.0
    def __round__(self, ndigits=0): return 0
    def __str__(self): return ""
    def __html__(self): return ""
    def __add__(self, other): return other
    def __radd__(self, other): return other
    def __sub__(self, other): return -other if isinstance(other,(int,float)) else 0
    def __rsub__(self, other): return other
    def __mul__(self, other): return 0
    def __rmul__(self, other): return 0
    def __truediv__(self, other): return 0
    def __rtruediv__(self, other): return 0
    def __iter__(self): return iter(())
    def __len__(self): return 0
    def __bool__(self): return False
    def __hash__(self): return 0
    def __eq__(self, o): return (o == 0) or isinstance(o, Undefined)
    def __ne__(self, o): return not self.__eq__(o)
    def __lt__(self, o): return 0 < o if isinstance(o,(int,float)) else False
    def __le__(self, o): return 0 <= o if isinstance(o,(int,float)) else False
    def __gt__(self, o): return 0 > o if isinstance(o,(int,float)) else False
    def __ge__(self, o): return 0 >= o if isinstance(o,(int,float)) else False


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=_SafeUndefined,
    )
    env.filters["format_currency"] = _format_currency
    env.filters["format_kwh"] = _format_kwh
    return env


# ============ DEFAULT CONTEXT (fallback values) ============
def _build_context(user_ctx: dict[str, Any]) -> dict[str, Any]:
    """Merge user context with defaults + derived fields + generated chart SVGs."""

    c: dict[str, Any] = {
        # Identity
        "project_name": "Hybridné riešenie FVE + BESS",
        "project_id": "AOM-DEMO",
        "client_name": "Klient",
        "site_address": "—",
        "posudok_date": datetime.now().strftime("%d.%m.%Y"),
        "prepared_by_name": "Lukáš Bago",
        "prepared_by_email": "lukas.bago@energovision.sk",
        "prepared_by_phone": "0918 187 762",
        "company_ico": "53 036 280",
        "engine_version": "0.9.5",
        "year_label": "rok 1",
        "analysis_years": 20,
        "spot_avg_eur_mwh": 103,

        # Tech config
        "pv_kwp": 0,
        "bess_kwh": 0,
        "bess_kw": 0,
        "inverter_kw": 0,
        "fve_topology": "Juh, 35°",
        "mrk_kw": 0,
        "annual_kwh": 0,
        "tarif_typ": "spot",
        "ems_strategy": "Samospotreba + arbitráž",

        # Financial
        "capex_total_eur": 0,
        "capex_pv_eur": 0,
        "capex_bess_eur": 0,
        "capex_other_eur": 0,
        "dotacia_eur": 0,
        "net_capex_eur": 0,
        "saving_y1_eur": 0,
        "payback_years": 0,
        "irr_pct": 0,
        "npv_eur": 0,
        "label": "Variant",

        # Energy KPIs
        "samospotreba_pct": 0,
        "samostatnost_pct": 0,

        # Energy flow (MWh)
        "pv_total_mwh": 0,
        "pv_to_load_mwh": 0,
        "pv_to_grid_mwh": 0,
        "pv_to_bat_mwh": 0,
        "grid_to_load_mwh": 0,
        "bat_to_load_mwh": 0,
        "grid_to_bat_mwh": 0,
        "load_total_mwh": 0,
        "grid_import_mwh": 0,
        "grid_export_mwh": 0,

        # Solar consumption breakdown
        "direct_to_load_pct": 0,
        "charging_battery_pct": 0,
        "exported_pct": 0,
        "curtailed_pct": 0,

        # Carbon
        "co2_avoided_tonnes": 0,
        "co2_reduction_pct": 0,
        "trees_equivalent": 0,
        "barrels_oil": 0,

        # Dotacia info
        "dotacia_scheme_name": "Zelená podnikom",
        "dotacia_max_eur": 50000,
        "dotacia_intensity_pct": 45,
        # Porovnanie s/bez dotácie (vplyv dotácie sekcia)
        "payback_without_dotacia": 0,
        "npv_without_dotacia": 0,
        "irr_without_dotacia": 0,

        # 3 cenové scenáre (Báza / Nízky výkup / Spot s arbitrážou)
        # Každý prvok: {name, is_base, tarif_buy_eur_kwh, tarif_sell_eur_kwh, annual_save_eur, payback_years, npv_eur, irr_pct, note}
        "scenarios": [],

        # Otvorené otázky pre klienta (defaults sa použijú ak nie sú custom)
        # Každý prvok: {title, detail}
        "open_questions": [],

        # ============ VLNA 4 — AI Expert posúdenie (Claude Sonnet 4.5) ============
        # Tieto polia napĺňa _generate_ai_expert_commentary() v analyza_om_v2.py
        # Pri zlyhaní AI alebo pre legacy volania zostanú prázdne → sekcia sa nezobrazí
        "ai_commentary": "",                 # 3-4 paragrafov HTML
        "ai_recommendations": [],            # [{title, detail}, ...]
        "ai_anomalies": [],                  # [{title, detail}, ...]
        "ai_open_questions": [],             # [{title, detail}, ...] — nahradí open_questions ak je naplnené

        # Cashflow series
        "cf_array": [],
        "accumulated_cf_final": 0,

        # Variants
        "other_variants": [],
        "n_variants_run": 0,
    }
    c.update(user_ctx)

    # Derive net_capex_eur if not set
    if c["net_capex_eur"] == 0:
        c["net_capex_eur"] = c["capex_total_eur"] - c["dotacia_eur"]

    # ====== GENERATE CHARTS ======
    try:
        c["chart_cashflow_svg"] = charts.chart_cashflow(
            cf_array=c["cf_array"] or [0] * 21,
            capex_pv=c["capex_pv_eur"],
            capex_bess=c["capex_bess_eur"],
            capex_other=c["capex_other_eur"],
        )
    except Exception as e:
        c["chart_cashflow_svg"] = f"<p>Chart cashflow error: {e}</p>"

    try:
        c["chart_energy_flow_svg"] = charts.chart_energy_flow(
            pv_total_mwh=c["pv_total_mwh"],
            pv_to_load_mwh=c["pv_to_load_mwh"],
            pv_to_grid_mwh=c["pv_to_grid_mwh"],
            pv_to_bat_mwh=c["pv_to_bat_mwh"],
            grid_to_load_mwh=c["grid_to_load_mwh"],
            bat_to_load_mwh=c["bat_to_load_mwh"],
            grid_to_bat_mwh=c["grid_to_bat_mwh"],
            load_total_mwh=c["load_total_mwh"],
            grid_export_mwh=c["grid_export_mwh"],
        )
    except Exception as e:
        c["chart_energy_flow_svg"] = f"<p>Chart energy flow error: {e}</p>"

    # Daily load profile (24h) — fallback flat line if no data
    hourly_before = c.get("hourly_load_kw_before") or [50] * 24
    hourly_after = c.get("hourly_load_kw_after") or None
    try:
        c["chart_daily_load_svg"] = charts.chart_daily_load_profile(
            hourly_load_kw=hourly_before,
            hourly_load_kw_after=hourly_after,
        )
    except Exception as e:
        c["chart_daily_load_svg"] = f"<p>Chart daily load error: {e}</p>"

    try:
        c["chart_solar_consumption_svg"] = charts.chart_solar_consumption_donut(
            self_consumed_pct=c["direct_to_load_pct"] + c["charging_battery_pct"],
            direct_to_load_pct=c["direct_to_load_pct"],
            charging_battery_pct=c["charging_battery_pct"],
            exported_pct=c["exported_pct"],
            curtailed_pct=c["curtailed_pct"],
        )
    except Exception as e:
        c["chart_solar_consumption_svg"] = f"<p>Chart donut error: {e}</p>"

    months = ["Jan", "Feb", "Mar", "Apr", "Máj", "Jún", "Júl", "Aug", "Sep", "Okt", "Nov", "Dec"]
    try:
        c["chart_monthly_earnings_svg"] = charts.chart_monthly_earnings(
            months=months,
            solar_to_load=c.get("monthly_solar_to_load") or [0] * 12,
            solar_export=c.get("monthly_solar_export") or [0] * 12,
            arbitrage=c.get("monthly_arbitrage") or [0] * 12,
        )
    except Exception as e:
        c["chart_monthly_earnings_svg"] = f"<p>Chart monthly error: {e}</p>"

    try:
        c["chart_upfront_costs_svg"] = charts.chart_upfront_costs(
            capex_pv=c["capex_pv_eur"],
            capex_bess=c["capex_bess_eur"],
            capex_other=c["capex_other_eur"],
        )
    except Exception as e:
        c["chart_upfront_costs_svg"] = f"<p>Chart upfront error: {e}</p>"

    # Logo
    c["logo_b64"] = _logo_b64()

    return c


# ============ PUBLIC API ============
def render_orkestra_html(context: dict[str, Any]) -> str:
    """Render HTML string (for debugging / preview)."""
    env = _make_env()
    template = env.get_template("posudok.html")
    full_ctx = _build_context(context)
    # Read CSS as raw string for inline embedding
    css_path = _TEMPLATES_DIR / "styles.css"
    full_ctx["css"] = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return template.render(**full_ctx)


def generate_orkestra_pdf(context: dict[str, Any]) -> bytes:
    """Generate PDF bytes from context dict."""
    html_str = render_orkestra_html(context)
    return HTML(string=html_str, base_url=str(_TEMPLATES_DIR)).write_pdf()


# ============ POROVNÁVACÍ SÚHRN (viacero ponúk pre jedného klienta) ============
def _build_porovnanie_context(user_ctx: dict[str, Any]) -> dict[str, Any]:
    """Merge user context (rows už majú dominated_by dopočítané) + vygeneruje porovnávacie grafy."""
    c: dict[str, Any] = {
        "project_name": "Porovnanie ponúk",
        "project_id": "AOM-DEMO",
        "client_name": "Klient",
        "site_address": "—",
        "posudok_date": datetime.now().strftime("%d.%m.%Y"),
        "prepared_by_name": "Lukáš Bago",
        "prepared_by_email": "lukas.bago@energovision.sk",
        "prepared_by_phone": "0918 187 762",
        "company_ico": "53 036 280",
        "analysis_years": 20,
        "annual_kwh": 0,
        "peak_kw": 0,
        "mrk_kw": 0,
        "rows": [],
        "n_variants": 0,
        "dominated_count": 0,
        "viable_count": 0,
        "same_winner": False,
        "pick_npv": {},
        "pick_payback": {},
    }
    c.update(user_ctx)
    c["logo_b64"] = _logo_b64()

    rows = c["rows"]
    names = [r["short_label"] for r in rows]
    capex = [float(r.get("capex_eur") or 0) for r in rows]
    npv = [float(r.get("npv_eur") or 0) for r in rows]
    payback = [float(r.get("payback_y") or 0) for r in rows]
    dominated_mask = [bool(r.get("dominated_by")) for r in rows]
    highlight_idx = next((i for i, r in enumerate(rows) if r.get("is_pick_npv")), None)

    try:
        c["chart_capex_npv_svg"] = charts.chart_capex_vs_npv(
            names=names, capex=capex, npv=npv,
            highlight_idx=highlight_idx, dominated_mask=dominated_mask,
        )
    except Exception as e:
        c["chart_capex_npv_svg"] = f"<p>Chart capex/npv error: {e}</p>"

    try:
        c["chart_payback_svg"] = charts.chart_payback_ranking(
            names=names, payback=payback,
            highlight_idx=highlight_idx, dominated_mask=dominated_mask,
        )
    except Exception as e:
        c["chart_payback_svg"] = f"<p>Chart payback error: {e}</p>"

    return c


def render_porovnanie_html(context: dict[str, Any]) -> str:
    """Render porovnávací súhrn HTML string (for debugging / preview)."""
    env = _make_env()
    template = env.get_template("porovnanie.html")
    full_ctx = _build_porovnanie_context(context)
    css_path = _TEMPLATES_DIR / "styles.css"
    full_ctx["css"] = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return template.render(**full_ctx)


def generate_porovnanie_pdf(context: dict[str, Any]) -> bytes:
    """Generate porovnávací súhrn PDF bytes from context dict."""
    html_str = render_porovnanie_html(context)
    return HTML(string=html_str, base_url=str(_TEMPLATES_DIR)).write_pdf()
