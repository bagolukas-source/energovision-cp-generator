"""HTMLDashboard v0.3 — premium redesign s vlastným SVG energy flow diagramom.

Vylepšenia oproti v0.2:
    - Vlastný SVG node-link diagram (kruhy + šípky) namiesto Sankey
    - Donut grafy s pravou legendou (value + kWh per položka)
    - Energy metrics ako horizontálne area cards
    - Spaghetti load profile (všetky dni v pozadí + priemer)
    - Interval activity ako stĺpce + línie
    - Modrá ako accent farba pre big stats
    - Kompaktnejšia typografia
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from energovision_analytics.core.models import BESSInput, PVInput, SiteInput
from energovision_analytics.ems.dispatch_state import DispatchInterval, DispatchSummary
from energovision_analytics.financial.cashflow import FinancialResult
from energovision_analytics.reporting.charts import (
    THEME,
    chart_battery_degradation,
    chart_bess_activity_breakdown,
    chart_carbon_summary,
    chart_cashflow,
    chart_energy_metric_area,
    chart_interval_activity,
    chart_interval_soc,
    chart_interval_spot,
    chart_monthly_earnings,
    chart_monthly_pv,
    chart_pv_consumption_donut,
    chart_site_consumption_donut,
    chart_soc_heatmap,
    chart_spaghetti_load,
    chart_weekly_earnings,
    render_donut_legend,
    render_site_consumption_legend,
    render_week_detail_panel,
)


def _eur(v: float, digits: int = 0) -> str:
    return f"{v:,.{digits}f}".replace(",", " ")


def _pct(v: float, digits: int = 1) -> str:
    return f"{v:.{digits}f}%"


def _co2_eqs(t: float) -> list[dict]:
    return [
        {"value": f"{t / 2.0:,.0f}".replace(",", " "),
         "label": "áut menej na cestách za rok",
         "desc": "1 priemerné auto ≈ 2 t CO₂/rok"},
        {"value": f"{t / 6.0:,.0f}".replace(",", " "),
         "label": "hektárov vyrasteného lesa",
         "desc": "1 ha mladý les ≈ 6 t CO₂/rok"},
        {"value": f"{t / 0.8:,.0f}".replace(",", " "),
         "label": "rodinných domov ročne",
         "desc": "1 dom (4 MWh) ≈ 0,8 t CO₂"},
        {"value": f"{t / 37.5:,.0f}".replace(",", " "),
         "label": "vagónov uhlia (15 t)",
         "desc": "1 vagón uhlia ≈ 37,5 t CO₂"},
    ]


class HTMLDashboard:
    """Premium HTML report generator v0.3."""

    def __init__(
        self,
        site: SiteInput,
        pv: Optional[PVInput],
        bess: Optional[BESSInput],
        summary: DispatchSummary,
        financial: FinancialResult,
        intervals: list[DispatchInterval],
        capex_solar_eur: float = 0,
        capex_bess_eur: float = 0,
        client_name: str = "",
        scenario_name: str = "Posudok",
    ) -> None:
        self.site = site
        self.pv = pv
        self.bess = bess
        self.summary = summary
        self.financial = financial
        self.intervals = intervals
        self.capex_solar = capex_solar_eur
        self.capex_bess = capex_bess_eur
        self.client_name = client_name or site.nazov
        self.scenario_name = scenario_name

        self.df = pd.DataFrame([
            {
                "timestamp": iv.timestamp,
                "load_kw": iv.load_kw, "pv_kw": iv.pv_kw,
                "pv_to_load_kwh": iv.pv_to_load_kwh,
                "pv_to_bat_kwh": iv.pv_to_bat_kwh,
                "pv_to_grid_kwh": iv.pv_to_grid_kwh,
                "grid_to_load_kwh": iv.grid_to_load_kwh,
                "grid_to_bat_kwh": iv.grid_to_bat_kwh,
                "bat_to_load_kwh": iv.bat_to_load_kwh,
                "bat_soc_pct": (iv.bat_soc_kwh_end / bess.nominal_kwh * 100) if bess else 0,
                "spot_eur_mwh": iv.spot_eur_mwh,
                "tarif_buy_eur_kwh": iv.tarif_buy_eur_kwh,
                "sav_solar_self_cons_eur": iv.sav_solar_self_cons_eur,
                "sav_solar_export_eur": iv.sav_solar_export_eur,
                "sav_bess_self_cons_eur": iv.sav_bess_self_cons_eur,
                "sav_arbitrage_eur": iv.sav_arbitrage_eur,
                "sav_peak_shaving_eur": iv.sav_peak_shaving_eur,
                "sav_mrk_penalty_avoided_eur": iv.sav_mrk_penalty_avoided_eur,
            }
            for iv in intervals
        ])
        if len(self.df) > 0:
            self.df["timestamp"] = pd.to_datetime(self.df["timestamp"])
            self.df.set_index("timestamp", inplace=True)

    def render(self) -> str:
        return self._template(self._kpi_grid(), self._tabs_html())

    def save(self, path: str) -> None:
        from pathlib import Path
        Path(path).write_text(self.render(), encoding="utf-8")

    # ------------------------------------------------------------------ KPI
    def _kpi_grid(self) -> str:
        f = self.financial
        s = self.summary
        items = [
            ("CAPEX brutto", f"{_eur(f.capex_gross_eur)} €", "Celková investícia"),
            ("Dotácia", f"{_eur(f.dotacia_eur)} €", "Zelená podnikom"),
            ("Net CAPEX", f"{_eur(f.capex_net_eur)} €", "Po dotácii", True),
            ("Úspora rok 1", f"{_eur(f.annual_saving_y1_eur)} €", "Prvý prevádzkový rok"),
            ("NPV 20 r", f"{_eur(f.npv_eur)} €", "Diskont 6 %", True),
            ("IRR", f"{f.irr_pct:.1f}%" if f.irr_pct else "—", "Vnútorná miera"),
            ("Návratnosť",
             f"{f.payback_simple_y:.1f} r" if f.payback_simple_y < 50 else "—",
             "Simple payback"),
            ("Samospotreba", _pct(s.samospotreba_pct), "FVE využitá na mieste"),
            ("Samostatnosť", _pct(s.samostatnost_pct), "Nezávislosť od siete"),
        ]
        cards = []
        for it in items:
            label, value, sub = it[0], it[1], it[2]
            hi = "kpi hi" if len(it) > 3 else "kpi"
            cards.append(f"""
                <div class="{hi}">
                    <div class="kpi-l">{label}</div>
                    <div class="kpi-v">{value}</div>
                    <div class="kpi-s">{sub}</div>
                </div>""")
        return f'<div class="kpi-grid">{"".join(cards)}</div>'

    # ------------------------------------------------------------------ Tabs
    def _tabs_html(self) -> str:
        tabs = [
            ("suhrn", "Súhrn", self._tab_suhrn()),
            ("faktura", "Faktúra", self._tab_faktura()),
            ("ekonomika", "Ekonomika", self._tab_ekonomika()),
            ("energia", "Energia", self._tab_energia()),
            ("co2", "CO₂", self._tab_co2()),
            ("intervaly", "Intervaly", self._tab_intervaly()),
        ]
        nav = "".join([
            f'<button class="tab{" active" if i==0 else ""}" '
            f'onclick="showTab(\'{slug}\',this)">{title}</button>'
            for i, (slug, title, _) in enumerate(tabs)
        ])
        panels = "".join([
            f'<div id="t-{slug}" class="panel{" active" if i==0 else ""}">{body}</div>'
            for i, (slug, _, body) in enumerate(tabs)
        ])
        return f'<div class="tabs-wrap"><div class="tab-nav">{nav}</div>{panels}</div>'

    # ------------------------------------------------------------------
    # Vlastný SVG node-link energy flow diagram (kruhy + šípky)
    # ------------------------------------------------------------------
    def _svg_energy_flow(self) -> str:
        """Custom SVG diagram so 4 nodes: Solar, BESS, Site, Grid."""
        s = self.summary
        # MWh hodnoty
        pv_tot = s.pv_total_kwh / 1000
        pv_to_load = s.pv_to_load_kwh / 1000
        pv_to_bat = s.pv_to_bat_kwh / 1000
        pv_to_grid = s.pv_to_grid_kwh / 1000
        bat_to_load = s.bat_discharge_total_kwh / 1000
        grid_import = s.grid_import_kwh / 1000
        grid_to_bat = max(0, (s.bat_charge_total_kwh - s.pv_to_bat_kwh)) / 1000
        grid_to_load = max(0, grid_import - grid_to_bat)
        load = s.load_total_kwh / 1000
        grid_export = s.grid_export_kwh / 1000

        # Helper: render edge label
        def lbl(v): return f"{v:.0f}" if v >= 1 else f"{v:.1f}"

        # Show only existing nodes
        has_bat = self.bess is not None
        has_solar = self.pv is not None

        # Compose SVG
        # 4 node layout: solar (top-left), grid (top-right), bess (middle), site (bottom)
        # Or simpler: 3 columns — Grid (left), Site (middle), Solar+BESS (right)
        svg = f"""
        <svg viewBox="0 0 900 320" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;max-height:340px;">
            <defs>
                <marker id="ar-y" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">
                    <path d="M0,0 L10,5 L0,10 Z" fill="{THEME.solar}"/>
                </marker>
                <marker id="ar-b" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">
                    <path d="M0,0 L10,5 L0,10 Z" fill="{THEME.grid}"/>
                </marker>
                <marker id="ar-p" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">
                    <path d="M0,0 L10,5 L0,10 Z" fill="{THEME.battery}"/>
                </marker>
            </defs>

            <!-- GRID node (left) -->
            <g>
                <circle cx="140" cy="160" r="70" fill="white" stroke="{THEME.grid}" stroke-width="3"/>
                <text x="140" y="135" text-anchor="middle" font-family="{THEME.font_sans}" font-size="13" font-weight="700" fill="{THEME.ink}">Sieť</text>
                <text x="140" y="155" text-anchor="middle" font-family="{THEME.font_sans}" font-size="11" fill="{THEME.ink_muted}">⚡</text>
                <text x="140" y="180" text-anchor="middle" font-family="{THEME.font_sans}" font-size="22" font-weight="800" fill="{THEME.ink}">{lbl(grid_import)}</text>
                <text x="140" y="198" text-anchor="middle" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.ink_muted}">MWh import</text>
                <text x="140" y="218" text-anchor="middle" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.ink_subtle}">export: {lbl(grid_export)} MWh</text>
            </g>

            <!-- SITE node (middle) -->
            <g>
                <circle cx="450" cy="160" r="70" fill="white" stroke="{THEME.battery}" stroke-width="3"/>
                <text x="450" y="135" text-anchor="middle" font-family="{THEME.font_sans}" font-size="13" font-weight="700" fill="{THEME.ink}">Odberné miesto</text>
                <text x="450" y="155" text-anchor="middle" font-family="{THEME.font_sans}" font-size="11" fill="{THEME.ink_muted}">🏭</text>
                <text x="450" y="180" text-anchor="middle" font-family="{THEME.font_sans}" font-size="22" font-weight="800" fill="{THEME.ink}">{lbl(load)}</text>
                <text x="450" y="198" text-anchor="middle" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.ink_muted}">MWh spotreba</text>
            </g>

            <!-- SOLAR node (right top) -->
            {f'''
            <g>
                <circle cx="760" cy="90" r="60" fill="white" stroke="{THEME.solar}" stroke-width="3"/>
                <text x="760" y="70" text-anchor="middle" font-family="{THEME.font_sans}" font-size="13" font-weight="700" fill="{THEME.ink}">FVE</text>
                <text x="760" y="87" text-anchor="middle" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.ink_muted}">☀</text>
                <text x="760" y="108" text-anchor="middle" font-family="{THEME.font_sans}" font-size="20" font-weight="800" fill="{THEME.ink}">{lbl(pv_tot)}</text>
                <text x="760" y="125" text-anchor="middle" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.ink_muted}">MWh výroba</text>
            </g>''' if has_solar else ''}

            <!-- BESS node (right bottom) -->
            {f'''
            <g>
                <circle cx="760" cy="240" r="60" fill="white" stroke="{THEME.battery}" stroke-width="3"/>
                <text x="760" y="220" text-anchor="middle" font-family="{THEME.font_sans}" font-size="13" font-weight="700" fill="{THEME.ink}">BESS</text>
                <text x="760" y="237" text-anchor="middle" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.ink_muted}">🔋</text>
                <text x="760" y="258" text-anchor="middle" font-family="{THEME.font_sans}" font-size="20" font-weight="800" fill="{THEME.ink}">{lbl(bat_to_load)}</text>
                <text x="760" y="275" text-anchor="middle" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.ink_muted}">MWh discharge</text>
            </g>''' if has_bat else ''}

            <!-- Arrows + labels -->
            <!-- Grid → Site (load) -->
            <line x1="210" y1="160" x2="380" y2="160" stroke="{THEME.grid}" stroke-width="2.5" marker-end="url(#ar-b)"/>
            <text x="295" y="152" text-anchor="middle" font-family="{THEME.font_sans}" font-size="11" font-weight="600" fill="{THEME.grid}">{lbl(grid_to_load)} MWh</text>

            <!-- Solar → Site (priame) -->
            {f'''<path d="M 705,115 Q 580,135 520,150" stroke="{THEME.solar}" stroke-width="2.5" fill="none" marker-end="url(#ar-y)"/>
            <text x="605" y="120" text-anchor="middle" font-family="{THEME.font_sans}" font-size="11" font-weight="600" fill="{THEME.solar}">{lbl(pv_to_load)} MWh</text>''' if has_solar and pv_to_load > 0.1 else ''}

            <!-- Solar → Grid (export) -->
            {f'''<path d="M 705,75 Q 425,30 175,100" stroke="{THEME.solar}" stroke-width="2" fill="none" stroke-dasharray="5,3" marker-end="url(#ar-y)" opacity="0.7"/>
            <text x="440" y="22" text-anchor="middle" font-family="{THEME.font_sans}" font-size="11" font-weight="600" fill="{THEME.solar}" opacity="0.85">export {lbl(pv_to_grid)} MWh</text>''' if has_solar and pv_to_grid > 0.1 else ''}

            <!-- BESS → Site -->
            {f'''<path d="M 705,225 Q 580,210 520,180" stroke="{THEME.battery}" stroke-width="2.5" fill="none" marker-end="url(#ar-p)"/>
            <text x="610" y="245" text-anchor="middle" font-family="{THEME.font_sans}" font-size="11" font-weight="600" fill="{THEME.battery}">{lbl(bat_to_load)} MWh</text>''' if has_bat and bat_to_load > 0.1 else ''}

            <!-- Solar → BESS -->
            {f'''<path d="M 760,150 L 760,180" stroke="{THEME.solar}" stroke-width="2" fill="none" marker-end="url(#ar-y)" opacity="0.6"/>
            <text x="780" y="170" font-family="{THEME.font_sans}" font-size="10" fill="{THEME.solar}" opacity="0.7">{lbl(pv_to_bat)}</text>''' if has_solar and has_bat and pv_to_bat > 0.1 else ''}
        </svg>
        """
        return svg

    # ------------------------------------------------------------------ Tab 1: Súhrn
    def _tab_suhrn(self) -> str:
        s = self.summary
        years = [cy.year for cy in self.financial.yearly_cashflows]
        cf = [cy.net_cashflow for cy in self.financial.yearly_cashflows]
        cum = list(np.cumsum(cf))
        fig_cf = chart_cashflow(years, cf, cum)

        fig_donut = chart_pv_consumption_donut(
            s.pv_to_load_kwh, s.pv_to_bat_kwh, s.pv_to_grid_kwh,
        )
        donut_legend = render_donut_legend(s.pv_to_load_kwh, s.pv_to_bat_kwh, s.pv_to_grid_kwh)

        return f"""
        <div class="grid-3">
            {self._card_site_info()}
            {self._card_solution_info()}
            <div class="card hi">
                <h4>Hlavné zistenia</h4>
                <ul class="findings">{self._findings_list()}</ul>
            </div>
        </div>

        <div class="card mt-md">
            <h4>Ročný tok energie</h4>
            <p class="note mb-sm">Schéma zobrazuje hlavné energetické toky za rok v MWh.
                Šípky idú od zdrojov k spotrebe; export do siete je čiarkovaný.</p>
            {self._svg_energy_flow()}
        </div>

        <div class="grid-2 mt-md">
            <div class="chart-card">
                <h4>Využitie FVE výroby</h4>
                <div class="donut-row">
                    <div class="donut-chart">{fig_donut.to_html(include_plotlyjs=False, full_html=False, div_id='d-pv-s')}</div>
                    <div class="donut-legend">{donut_legend}</div>
                </div>
            </div>
            <div class="chart-card">
                <h4>Cashflow projektu</h4>
                {fig_cf.to_html(include_plotlyjs=False, full_html=False, div_id='c-cf-s')}
            </div>
        </div>
        """

    def _card_site_info(self) -> str:
        return f"""
        <div class="card">
            <h4>Odberné miesto</h4>
            <dl class="dl">
                <dt>Klient</dt><dd>{self.client_name}</dd>
                <dt>Distribútor</dt><dd>{self.site.distribuutor.value} · {self.site.sadzba.value}</dd>
                <dt>Tarif</dt><dd>{self.site.typ_tarify.value}</dd>
                <dt>RK / MRK</dt><dd>{self.site.rk_kw} / {self.site.mrk_kw} kW</dd>
                <dt>Ročná spotreba</dt><dd><b>{_eur(self.site.rocna_spotreba_kwh)} kWh</b></dd>
            </dl>
        </div>"""

    def _card_solution_info(self) -> str:
        pv_rows = ""
        if self.pv:
            pv_rows = f"""
                <dt>FVE výkon</dt><dd><b>{self.pv.instalovany_kwp} kWp</b></dd>
                <dt>Moduly</dt><dd>{self.pv.pocet_modulov} × {self.pv.modul_wp} Wp</dd>
                <dt>Invertor</dt><dd>{self.pv.inverter_kw_ac} kW AC</dd>
                <dt>Orientácia</dt><dd>{self.pv.sklon_stupne}° / {self.pv.azimut_stupne}°</dd>"""
        bess_rows = ""
        if self.bess:
            bess_rows = f"""
                <dt>BESS kapacita</dt><dd><b>{self.bess.nominal_kwh} kWh</b></dd>
                <dt>BESS výkon</dt><dd>{self.bess.power_kw_ac} kW AC</dd>
                <dt>Výrobca</dt><dd>{self.bess.vyrobca.value} {self.bess.typ}</dd>
                <dt>RTE</dt><dd>{self.bess.rte_ac_ac*100:.0f}% (field)</dd>"""
        return f"""
        <div class="card">
            <h4>Navrhované riešenie</h4>
            <dl class="dl">{pv_rows}{bess_rows}</dl>
        </div>"""

    def _findings_list(self) -> str:
        f = self.financial
        s = self.summary
        items = [
            f"Úspora <b>{_eur(f.annual_saving_y1_eur)} € / rok</b>",
            f"NPV za 20 r: <b>{_eur(f.npv_eur)} €</b>",
            f"Návratnosť: <b>{f.payback_simple_y:.1f} roka</b>",
            f"Samospotreba FVE: <b>{_pct(s.samospotreba_pct)}</b>",
            f"BESS cyklov za rok: <b>{s.bat_efc:.0f}</b>",
            f"Vyhnuté CO₂: <b>{s.co2_avoided_t:.1f} t / rok</b>",
        ]
        return "".join([f"<li>{i}</li>" for i in items])

    # ------------------------------------------------------------------ Tab 2: Faktúra
    def _tab_faktura(self) -> str:
        s = self.summary
        baseline = s.load_total_kwh
        after = s.grid_import_kwh
        avg_spot = float(self.df["spot_eur_mwh"].mean())
        avg_tarif = float(self.df["tarif_buy_eur_kwh"].mean())
        silova = avg_spot / 1000
        obchodnik = 0.025
        regulovane = avg_tarif - silova - obchodnik

        def row(label, before, after):
            saving = before - after
            cls = "pos" if saving > 0 else ("neg" if saving < -0.5 else "")
            return (f'<tr><td>{label}</td>'
                    f'<td class="num">{_eur(before)} €</td>'
                    f'<td class="num">{_eur(after)} €</td>'
                    f'<td class="num {cls}">{_eur(saving)} €</td></tr>')

        rows = [
            row("Silová zložka (priemer spot)", baseline * silova, after * silova),
            row("Marža obchodník", baseline * obchodnik, after * obchodnik),
            row("Regulované (TPS, dist., NJF, ...)", baseline * regulovane, after * regulovane),
            row("Výkup z exportu", 0, -s.sav_solar_export_eur),
        ]
        total_b = baseline * avg_tarif
        total_a = after * avg_tarif - s.sav_solar_export_eur

        return f"""
        <div class="card mb-md">
            <h4>Ročná faktúra — pred / po inštalácii FVE+BESS</h4>
            <table class="dt">
                <thead><tr><th>Zložka</th><th class="num">Pred (€/r)</th><th class="num">Po (€/r)</th><th class="num">Úspora (€/r)</th></tr></thead>
                <tbody>{"".join(rows)}
                <tr class="total"><th>Celkom</th>
                    <th class="num">{_eur(total_b)} €</th>
                    <th class="num">{_eur(total_a)} €</th>
                    <th class="num pos">{_eur(total_b - total_a)} €</th></tr></tbody>
            </table>
            <p class="note">Priemerný OKTE spot {avg_spot:.1f} €/MWh.
                Mesačné fakturácie sa líšia podľa hodinovej volatility cien.</p>
        </div>
        <div class="card">
            <h4>Úspory rozdelené podľa zdroja (value streams)</h4>
            <table class="dt">
                <thead><tr><th>Zdroj úspory</th><th class="num">€ / rok</th><th class="num">Podiel</th></tr></thead>
                <tbody>{self._sav_rows()}</tbody>
            </table>
        </div>
        """

    def _sav_rows(self) -> str:
        s = self.summary
        items = [
            ("Samospotreba FVE (PV → vlastná záťaž)", s.sav_solar_self_cons_eur),
            ("Export FVE (PV → sieť)", s.sav_solar_export_eur),
            ("Samospotreba BESS (BAT → záťaž)", s.sav_bess_self_cons_eur),
            ("Wholesale arbitráž (load-shifting)", s.sav_arbitrage_eur),
            ("Peak shaving (zníženie ¼-h MRK)", s.sav_peak_shaving_eur),
            ("Vyhnutie MRK export penalty (SK 2026)", s.sav_mrk_penalty_avoided_eur),
        ]
        total = sum(v for _, v in items) or 1
        rows = []
        for label, v in items:
            pct = v / total * 100
            rows.append(f'<tr><td>{label}</td><td class="num">{_eur(v)} €</td><td class="num">{pct:.1f}%</td></tr>')
        rows.append(f'<tr class="total"><th>Celkom</th><th class="num">{_eur(sum(v for _,v in items))} €</th><th class="num">100%</th></tr>')
        return "".join(rows)

    # ------------------------------------------------------------------ Tab 3: Ekonomika
    def _tab_ekonomika(self) -> str:
        years = [cy.year for cy in self.financial.yearly_cashflows]
        cf = [cy.net_cashflow for cy in self.financial.yearly_cashflows]
        cum = list(np.cumsum(cf))
        fig_cf = chart_cashflow(years, cf, cum)

        months_idx = self.df.index.month if len(self.df) > 0 else []
        streams = {}
        for col, name in [
            ("sav_solar_self_cons_eur", "Samospotreba FVE"),
            ("sav_solar_export_eur", "Export FVE"),
            ("sav_bess_self_cons_eur", "Samospotreba BESS"),
            ("sav_arbitrage_eur", "Arbitráž"),
            ("sav_peak_shaving_eur", "Peak shaving"),
        ]:
            if col in self.df.columns:
                m = self.df.groupby(months_idx)[col].sum()
                streams[name] = [float(m.get(i, 0)) for i in range(1, 13)]
        month_lbl = ["Jan", "Feb", "Mar", "Apr", "Máj", "Jún", "Júl", "Aug", "Sep", "Okt", "Nov", "Dec"]
        fig_m = chart_monthly_earnings(month_lbl, streams)

        cf_rows = []
        for cy in self.financial.yearly_cashflows[:11]:
            cls = "pos" if cy.net_cashflow >= 0 else "neg"
            cf_rows.append(
                f'<tr><td>{cy.year}</td>'
                f'<td class="num">{_eur(cy.revenue_total)}</td>'
                f'<td class="num">{_eur(cy.opex_total)}</td>'
                f'<td class="num">{_eur(cy.tax_shield)}</td>'
                f'<td class="num">{_eur(cy.dotacia_zelena)}</td>'
                f'<td class="num {cls}">{_eur(cy.net_cashflow)}</td></tr>')
        lcos = f"{self.financial.lcos_eur_mwh:.1f} €/MWh" if self.financial.lcos_eur_mwh else "—"

        return f"""
        <div class="grid-2">
            <div class="chart-card">
                <h4>Cashflow projektu — všetky roky</h4>
                {fig_cf.to_html(include_plotlyjs=False, full_html=False, div_id='c-cf-e')}
            </div>
            <div class="card">
                <h4>Finančné metriky</h4>
                <dl class="dl big">
                    <dt>NPV (20 r, 6 %)</dt><dd class="big-num">{_eur(self.financial.npv_eur)} €</dd>
                    <dt>IRR</dt><dd class="big-num">{f"{self.financial.irr_pct:.1f}%" if self.financial.irr_pct else "—"}</dd>
                    <dt>Návratnosť (simple)</dt><dd>{self.financial.payback_simple_y:.1f} rokov</dd>
                    <dt>Návratnosť (diskontovaná)</dt><dd>{self.financial.payback_discounted_y:.1f} rokov</dd>
                    <dt>LCOS</dt><dd>{lcos}</dd>
                    <dt>Priem. úspora / rok</dt><dd>{_eur(self.financial.annual_saving_lifetime_avg_eur)} €</dd>
                    <dt>Celkový príjem 20 r</dt><dd><b>{_eur(self.financial.total_lifetime_revenue_eur)} €</b></dd>
                </dl>
            </div>
        </div>
        <div class="chart-card mt-md">
            <h4>Mesačné úspory podľa zdroja</h4>
            {fig_m.to_html(include_plotlyjs=False, full_html=False, div_id='c-mon-e')}
        </div>
        <div class="card mt-md">
            <h4>Cashflow — prvých 10 rokov</h4>
            <table class="dt">
                <thead><tr><th>Rok</th><th class="num">Príjem</th><th class="num">OPEX</th><th class="num">Daň. shield</th><th class="num">Dotácia</th><th class="num">Net</th></tr></thead>
                <tbody>{"".join(cf_rows)}</tbody>
            </table>
            <p class="note">Rok 0 zahŕňa investíciu (CAPEX v negatíve, dotácia v pozitíve).
                Daňový shield: 6-r odpis z Net CAPEX × 21 % DPPO (SK legislatíva).</p>
        </div>
        """

    # ------------------------------------------------------------------ Tab 4: Energia
    def _tab_energia(self) -> str:
        s = self.summary
        soc_pct = self.df["bat_soc_pct"].to_numpy() if "bat_soc_pct" in self.df.columns else np.zeros(len(self.df))

        # Donuts s legendou vpravo
        fig_pv_donut = chart_pv_consumption_donut(s.pv_to_load_kwh, s.pv_to_bat_kwh, s.pv_to_grid_kwh)
        pv_legend = render_donut_legend(s.pv_to_load_kwh, s.pv_to_bat_kwh, s.pv_to_grid_kwh)

        fig_site_donut = chart_site_consumption_donut(
            s.pv_to_load_kwh, s.bat_discharge_total_kwh,
            s.grid_import_kwh - max(0, s.bat_charge_total_kwh - s.pv_to_bat_kwh),
        )
        site_legend = render_site_consumption_legend(
            s.pv_to_load_kwh, s.bat_discharge_total_kwh,
            s.grid_import_kwh - max(0, s.bat_charge_total_kwh - s.pv_to_bat_kwh),
        )

        # Spaghetti load profile
        fig_spaghetti = chart_spaghetti_load(self.df.index, self.df["load_kw"].to_numpy())

        # Monthly PV bar
        monthly_pv = self.df.groupby(self.df.index.month)["pv_kw"].sum().tolist()
        fig_pv_m = chart_monthly_pv(monthly_pv)

        # SoC heatmap
        fig_soc = chart_soc_heatmap(self.df.index, soc_pct / 100)

        # Battery degradation
        fig_deg_html = ""
        if self.bess:
            fig_deg = chart_battery_degradation(
                horizon_years=self.financial.horizon_years,
                annual_degradation_pct=2.0, first_year_drop_pct=2.0,
                eol_soh=self.bess.warranty_eol_soh, replacement_year=12,
            )
            fig_deg_html = f'<div class="chart-card mt-md"><h4>Degradácia batérie</h4>{fig_deg.to_html(include_plotlyjs=False, full_html=False, div_id="c-deg-e")}</div>'

        # Energy metrics area charts
        monthly = self.df.copy()
        monthly["month"] = monthly.index.month
        monthly["self_used_pv"] = monthly["pv_to_load_kwh"] + monthly["pv_to_bat_kwh"]
        monthly["from_load"] = monthly["pv_to_load_kwh"] + monthly["bat_to_load_kwh"]
        sample_months = monthly.groupby("month").agg(
            pv_total=("pv_kw", "sum"),
            self_used=("self_used_pv", "sum"),
            load=("load_kw", "sum"),
            from_load=("from_load", "sum"),
        )
        # Re-order Apr-Mar (energy year)
        month_order = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]
        ei_monthly = []  # Energy independence
        su_monthly = []  # Solar utilisation
        for m in month_order:
            if m in sample_months.index:
                r = sample_months.loc[m]
                ei = r["from_load"] / r["load"] * 100 if r["load"] > 0 else 0
                su = r["self_used"] / r["pv_total"] * 100 if r["pv_total"] > 0 else 0
            else:
                ei = su = 0
            ei_monthly.append(min(100, ei))
            su_monthly.append(min(100, su))

        fig_ei = chart_energy_metric_area(ei_monthly, "Energetická nezávislosť",
                                            color=THEME.accent_blue,
                                            avg_pct=s.samostatnost_pct)
        fig_su = chart_energy_metric_area(su_monthly, "Využitie FVE",
                                            color=THEME.solar,
                                            avg_pct=s.samospotreba_pct)

        return f"""
        <div class="grid-2 mb-md">
            <div class="card">
                <h4>Energetické KPI</h4>
                <dl class="dl">
                    <dt>Ročná spotreba</dt><dd>{_eur(s.load_total_kwh)} kWh</dd>
                    <dt>Ročná FVE výroba</dt><dd>{_eur(s.pv_total_kwh)} kWh</dd>
                    <dt>Špecifický výnos</dt>
                        <dd>{(s.pv_total_kwh / self.pv.instalovany_kwp if self.pv else 0):.0f} kWh/kWp</dd>
                    <dt>Samospotreba FVE</dt><dd><b>{_pct(s.samospotreba_pct)}</b></dd>
                    <dt>Samostatnosť od siete</dt><dd><b>{_pct(s.samostatnost_pct)}</b></dd>
                </dl>
            </div>
            <div class="card">
                <h4>BESS prevádzkové KPI</h4>
                <dl class="dl">
                    <dt>Cyklov (EFC) za rok</dt><dd><b>{s.bat_efc:.0f}</b></dd>
                    <dt>Nabité do BAT</dt><dd>{_eur(s.bat_charge_total_kwh)} kWh</dd>
                    <dt>Vybité z BAT</dt><dd>{_eur(s.bat_discharge_total_kwh)} kWh</dd>
                    <dt>SoH na konci roka</dt><dd>{s.bat_soh_end*100:.1f}%</dd>
                    <dt>Výmeny počas životnosti</dt><dd>{s.n_replacements}</dd>
                </dl>
            </div>
        </div>

        <div class="card mb-md">
            <h4>Ročný tok energie</h4>
            {self._svg_energy_flow()}
        </div>

        <div class="grid-2 mb-md">
            <div class="chart-card">
                <h4>Využitie FVE výroby</h4>
                <div class="donut-row">
                    <div class="donut-chart">{fig_pv_donut.to_html(include_plotlyjs=False, full_html=False, div_id='d-pv-e')}</div>
                    <div class="donut-legend">{pv_legend}</div>
                </div>
            </div>
            <div class="chart-card">
                <h4>Pokrytie spotreby</h4>
                <div class="donut-row">
                    <div class="donut-chart">{fig_site_donut.to_html(include_plotlyjs=False, full_html=False, div_id='d-site-e')}</div>
                    <div class="donut-legend">{site_legend}</div>
                </div>
            </div>
        </div>

        <div class="card mb-md">
            <h4>Energetické metriky — mesačný priebeh</h4>
            <div class="metrics-row">
                <div class="metric-chart">{fig_ei.to_html(include_plotlyjs=False, full_html=False, div_id='c-ei')}</div>
                <div class="metric-chart">{fig_su.to_html(include_plotlyjs=False, full_html=False, div_id='c-su')}</div>
            </div>
        </div>

        <div class="grid-2 mb-md">
            <div class="chart-card">
                <h4>Denný profil spotreby — celý rok</h4>
                {fig_spaghetti.to_html(include_plotlyjs=False, full_html=False, div_id='c-spag-e')}
            </div>
            <div class="chart-card">
                <h4>Mesačná výroba FVE</h4>
                {fig_pv_m.to_html(include_plotlyjs=False, full_html=False, div_id='c-pvm-e')}
            </div>
        </div>

        <div class="chart-card mb-md">
            <h4>BESS stav nabitia (SoC) — heatmap roka</h4>
            {fig_soc.to_html(include_plotlyjs=False, full_html=False, div_id='c-soc-e')}
        </div>

        {fig_deg_html}
        """

    # ------------------------------------------------------------------ Tab 5: CO2
    def _tab_co2(self) -> str:
        annual = self.summary.co2_avoided_t
        horizon = self.financial.horizon_years
        cum = annual * horizon * 0.95
        fig = chart_carbon_summary(annual, horizon, 1.0)
        eq_y = _co2_eqs(annual)
        eq_life = _co2_eqs(cum)

        def render_eq(items, life=False):
            return "".join([f"""
                <div class="big-num-row {'life' if life else ''}">
                    <div class="big-num-val">{e['value']}</div>
                    <div class="big-num-meta">
                        <div class="big-num-label">{e['label']}</div>
                        <div class="big-num-desc">{e['desc']}</div>
                    </div>
                </div>""" for e in items])

        return f"""
        <div class="grid-2 mb-md">
            <div class="card hi">
                <h4>Vyhnuté CO₂ ročne</h4>
                <div class="hero-num">{annual:.1f} <span>t CO₂ / rok</span></div>
                <p class="note">Pri SK gridovom mixe ~250 g CO₂/kWh (zdroj: SEPS).</p>
            </div>
            <div class="card hi">
                <h4>Životnosť projektu ({horizon} rokov)</h4>
                <div class="hero-num">{cum:.0f} <span>t CO₂ vyhnutých</span></div>
                <p class="note">Kumulatívne počas celej životnosti (priemer 1 %/r degradácia).</p>
            </div>
        </div>

        <div class="chart-card mb-md">
            <h4>Vyhnuté CO₂ počas životnosti</h4>
            {fig.to_html(include_plotlyjs=False, full_html=False, div_id='c-co2')}
        </div>

        <div class="grid-2">
            <div class="card">
                <h4>Ročný prínos — ekvivalenty</h4>
                {render_eq(eq_y)}
            </div>
            <div class="card">
                <h4>Celoživotný prínos — ekvivalenty</h4>
                {render_eq(eq_life, life=True)}
            </div>
        </div>
        """

    # ------------------------------------------------------------------ Tab 6: Intervaly
    def _tab_intervaly(self) -> str:
        if len(self.df) == 0:
            return "<p>Žiadne dáta.</p>"
        weekly = self.df["spot_eur_mwh"].resample("W").mean()
        top = weekly.idxmax()
        ws = top - pd.Timedelta(days=6)
        sample = self.df.loc[(self.df.index >= ws) & (self.df.index <= top)].copy()
        if len(sample) < 24:
            sample = self.df.iloc[:168].copy()

        bat_net = (sample["bat_to_load_kwh"]
                    - sample["grid_to_bat_kwh"]
                    - sample["pv_to_bat_kwh"]).to_numpy()
        pv_a = sample["pv_kw"].to_numpy()
        load_a = sample["load_kw"].to_numpy()
        grid_a = sample["grid_to_load_kwh"].to_numpy()
        soc_a = sample["bat_soc_pct"].to_numpy()
        spot_a = sample["spot_eur_mwh"].to_numpy()
        pv_to_bat = sample["pv_to_bat_kwh"].to_numpy()
        grid_to_bat = sample["grid_to_bat_kwh"].to_numpy()
        bat_to_load = sample["bat_to_load_kwh"].to_numpy()

        # Spočítaj arbitráž zisk za týždeň pre kontext
        grid_charge_kwh = grid_to_bat.sum()
        avg_charge_spot = (sample["grid_to_bat_kwh"] * sample["spot_eur_mwh"]).sum() / grid_charge_kwh if grid_charge_kwh > 0 else 0
        discharge_kwh = bat_to_load.sum()
        avg_discharge_spot = (sample["bat_to_load_kwh"] * sample["spot_eur_mwh"]).sum() / discharge_kwh if discharge_kwh > 0 else 0
        arb_spread = avg_discharge_spot - avg_charge_spot

        f_act = chart_interval_activity(sample.index, pv_a, load_a, grid_a, bat_net)
        f_breakdown = chart_bess_activity_breakdown(
            sample.index, pv_to_bat, grid_to_bat, bat_to_load, spot_a,
        )
        f_soc = chart_interval_soc(sample.index, soc_a)
        f_sp = chart_interval_spot(sample.index, spot_a)

        # Weekly earnings — celý rok per týždeň
        # POZNÁMKA: arbitráž + samospotreba BESS spojené do jednej kategórie "BESS úspory",
        # lebo per-timestep alokácia môže rozdeliť cost a benefit do rôznych streamov
        # (engine nikdy nedispatchuje arbitráž do mínusu, je to účtovný artefakt).
        weekly_streams_df = self.df.copy()
        weekly_streams_df["week"] = weekly_streams_df.index.to_period("W").start_time
        weekly_streams_df["bess_uspory_eur"] = (
            weekly_streams_df["sav_bess_self_cons_eur"]
            + weekly_streams_df["sav_arbitrage_eur"]
        )
        stream_cols = {
            "Samospotreba FVE": "sav_solar_self_cons_eur",
            "Export FVE": "sav_solar_export_eur",
            "BESS úspory": "bess_uspory_eur",
            "Peak shaving": "sav_peak_shaving_eur",
        }
        weekly_streams = {}
        weeks_grouped = weekly_streams_df.groupby("week")
        weeks_iso = sorted(weekly_streams_df["week"].unique())
        weeks_iso_str = [pd.Timestamp(w).strftime("%Y-%m-%d") for w in weeks_iso]
        for name, col in stream_cols.items():
            if col in weekly_streams_df.columns:
                weekly_streams[name] = [
                    float(weeks_grouped[col].sum().get(w, 0)) for w in weeks_iso
                ]

        # Highlighted = najvolatilnejší týždeň (matchni s sample)
        highlighted_iso = pd.Timestamp(sample.index[0]).to_period("W").start_time.strftime("%Y-%m-%d")
        f_weekly = chart_weekly_earnings(weeks_iso_str, weekly_streams, highlighted_iso)

        # Week detail (highlighted week sumáre)
        week_streams_detail = {}
        highlighted_ts = pd.Timestamp(highlighted_iso)
        for name, col in stream_cols.items():
            if col in weekly_streams_df.columns:
                mask = weekly_streams_df["week"] == highlighted_ts
                week_streams_detail[name] = float(weekly_streams_df.loc[mask, col].sum())
        week_total = sum(week_streams_detail.values())
        week_label_str = sample.index[0].strftime("%d. %m. %Y")
        week_detail_html = render_week_detail_panel(week_label_str, week_total, week_streams_detail)

        return f"""
        <div class="card mb-md">
            <p class="note">
                <b>Najvolatilnejší týždeň roka:</b> {sample.index[0].strftime('%d. %m. %Y')}
                — {sample.index[-1].strftime('%d. %m. %Y')}
                (priemer OKTE spot <b>{spot_a.mean():.0f} €/MWh</b>, peak <b>{spot_a.max():.0f} €/MWh</b>).
            </p>
            <p class="note">
                <b>Arbitráž za týždeň:</b> BESS nabité {grid_charge_kwh:.0f} kWh zo siete pri priemernej cene
                <b>{avg_charge_spot:.0f} €/MWh</b>, vybité {discharge_kwh:.0f} kWh pri priemernej cene
                <b>{avg_discharge_spot:.0f} €/MWh</b>. Spread <b>{arb_spread:+.0f} €/MWh</b>
                × {discharge_kwh:.0f} kWh × RTE 0.88 ≈ <b>{(arb_spread/1000 * discharge_kwh * 0.88):.0f} €</b> čistý zisk z arbitráže.
            </p>
        </div>
        <div class="chart-card mb-md">
            <h4>Týždenné úspory — celý rok</h4>
            <div class="weekly-row">
                <div class="weekly-chart">
                    {f_weekly.to_html(include_plotlyjs=False, full_html=False, div_id='c-int-wk')}
                </div>
                {week_detail_html}
            </div>
            <p class="note">
                Každý stĺpec = jeden týždeň v roku, výška = celkové úspory v €. Farba = zdroj úspory.
                Zvýraznený stĺpec (čierny okraj) = vybraný najvolatilnejší týždeň, ktorého detail vidno vpravo.
                <b>BESS úspory</b> zlučujú samospotrebu BESS aj arbitráž (cenový rozdiel medzi lacným nabíjaním
                a drahým vybíjaním) — vždy net pozitívny príspevok pretože engine nedispatchuje BESS do straty.
            </p>
        </div>
        <div class="chart-card mb-md">
            <h4>BESS aktivita rozdelená — odkiaľ ide nabíjanie?</h4>
            {f_breakdown.to_html(include_plotlyjs=False, full_html=False, div_id='c-int-brk')}
            <p class="note">
                Modré stĺpce = nabíjanie <b>zo siete</b> (čistá arbitráž v lacných hodinách).
                Žlté stĺpce = nabíjanie <b>z FVE</b> (samospotreba prebytkov).
                Fialové stĺpce = vybíjanie <b>do záťaže</b> (v drahých hodinách).
                Bodkovaná čierna čiara = OKTE spot cena pre kontext.
            </p>
        </div>
        <div class="chart-card mb-md">
            <h4>Všetky toky energie</h4>
            {f_act.to_html(include_plotlyjs=False, full_html=False, div_id='c-int-a')}
        </div>
        <div class="chart-card mb-md">
            <h4>BESS stav nabitia — vidno cykly</h4>
            {f_soc.to_html(include_plotlyjs=False, full_html=False, div_id='c-int-s')}
        </div>
        <div class="chart-card">
            <h4>OKTE spot cena — koreluje s dispatchom</h4>
            {f_sp.to_html(include_plotlyjs=False, full_html=False, div_id='c-int-sp')}
        </div>
        """

    # ------------------------------------------------------------------ HTML shell
    def _template(self, kpi: str, tabs: str) -> str:
        now = datetime.now().strftime("%d. %m. %Y")
        return f"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{self.client_name} — Posudok FVE+BESS</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:{THEME.font_sans};color:{THEME.ink};background:{THEME.bg_app};line-height:1.5;-webkit-font-smoothing:antialiased;font-size:14px}}
.wrap{{max-width:1320px;margin:0 auto;padding:24px}}

/* HEADER */
.hero{{background:linear-gradient(135deg,#2F5210 0%,#4D8121 70%,#7AB835 100%);color:white;padding:32px 36px;border-radius:14px;margin-bottom:20px;box-shadow:0 6px 24px rgba(77,129,33,.15);position:relative;overflow:hidden}}
.hero::before{{content:"";position:absolute;top:-50%;right:-15%;width:55%;height:200%;background:radial-gradient(circle,rgba(255,255,255,.08) 0%,transparent 70%);pointer-events:none}}
.hero h1{{margin:0 0 6px;font-size:26px;font-weight:800;letter-spacing:-.02em}}
.hero .sub{{font-size:14px;opacity:.95;font-weight:500}}
.hero .meta{{font-size:12px;opacity:.85;margin-top:10px;display:flex;gap:14px;flex-wrap:wrap}}
.hero .meta span::before{{content:"·";margin-right:14px;opacity:.5}}
.hero .meta span:first-child::before{{display:none}}

/* KPI */
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:20px}}
.kpi{{background:white;border-radius:10px;padding:14px 16px;border:1px solid {THEME.border};transition:transform .15s ease,border-color .15s ease}}
.kpi:hover{{transform:translateY(-1px);border-color:{THEME.accent_blue}}}
.kpi.hi{{background:linear-gradient(135deg,rgba(90,141,238,.06) 0%,white 100%);border-color:rgba(90,141,238,.25)}}
.kpi-l{{font-size:11px;font-weight:600;color:{THEME.ink_muted};text-transform:uppercase;letter-spacing:.04em}}
.kpi-v{{font-size:20px;font-weight:800;margin:3px 0 1px;letter-spacing:-.01em;font-variant-numeric:tabular-nums}}
.kpi.hi .kpi-v{{color:{THEME.accent_blue}}}
.kpi-s{{font-size:11px;color:{THEME.ink_subtle}}}

/* TABS */
.tabs-wrap{{background:white;border-radius:14px;overflow:hidden;border:1px solid {THEME.border}}}
.tab-nav{{display:flex;background:white;border-bottom:1px solid {THEME.border};position:sticky;top:0;z-index:50;padding:0 8px;overflow-x:auto}}
.tab{{padding:14px 24px;cursor:pointer;border:none;background:transparent;font-size:14px;font-weight:600;color:{THEME.ink_muted};border-bottom:3px solid transparent;transition:all .15s ease;font-family:inherit;white-space:nowrap;margin-bottom:-1px}}
.tab:hover{{color:{THEME.ink};background:rgba(90,141,238,.04)}}
.tab.active{{color:{THEME.accent_blue};border-bottom-color:{THEME.accent_blue}}}
.panel{{display:none;padding:28px 32px;animation:fade .2s ease}}
.panel.active{{display:block}}
@keyframes fade{{from{{opacity:0}}to{{opacity:1}}}}

/* CARDS */
.card{{background:white;border-radius:10px;padding:18px 22px;border:1px solid {THEME.border}}}
.card.hi{{background:linear-gradient(135deg,rgba(90,141,238,.05) 0%,white 100%);border-color:rgba(90,141,238,.20)}}
.card h4{{margin:0 0 14px;font-size:13px;font-weight:700;color:{THEME.ink};text-transform:uppercase;letter-spacing:.04em}}
.chart-card{{background:white;border-radius:10px;padding:18px 22px;border:1px solid {THEME.border}}}
.chart-card h4{{margin:0 0 8px;font-size:13px;font-weight:700;color:{THEME.ink};text-transform:uppercase;letter-spacing:.04em}}

/* DATA LIST */
.dl{{margin:0;display:grid;grid-template-columns:1fr auto;row-gap:7px;column-gap:14px;font-size:13px}}
.dl dt{{color:{THEME.ink_muted};font-weight:500}}
.dl dd{{margin:0;font-variant-numeric:tabular-nums;text-align:right}}
.dl.big dt,.dl.big dd{{padding:5px 0}}
.dl.big{{font-size:13px}}
.big-num{{font-size:20px !important;font-weight:800 !important;color:{THEME.accent_blue} !important}}

/* TABLES */
.dt{{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}}
.dt th{{background:{THEME.bg_app};padding:10px 12px;text-align:left;font-weight:600;font-size:11px;color:{THEME.ink_muted};text-transform:uppercase;letter-spacing:.03em;border-bottom:1px solid {THEME.border}}}
.dt td{{padding:9px 12px;border-bottom:1px solid {THEME.border}}}
.dt .num,.dt th.num{{text-align:right}}
.dt .pos{{color:{THEME.success};font-weight:600}}
.dt .neg{{color:{THEME.danger};font-weight:500}}
.dt .total th{{background:rgba(90,141,238,.08);color:{THEME.ink};border-top:2px solid {THEME.accent_blue}}}

/* FINDINGS */
.findings{{margin:0;padding:0;list-style:none;font-size:13px}}
.findings li{{padding:6px 0 6px 22px;position:relative;border-bottom:1px dashed {THEME.border}}}
.findings li:last-child{{border-bottom:0}}
.findings li::before{{content:"✓";position:absolute;left:0;top:6px;color:{THEME.accent_blue};font-weight:800;font-size:13px}}

/* GRIDS */
.grid-2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:14px}}
.grid-3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}}
.mt-md{{margin-top:14px}}.mt-md{{margin-top:14px}}.mb-md{{margin-bottom:14px}}.mb-sm{{margin-bottom:8px}}

/* DONUT ROW (donut left, legend right) */
.donut-row{{display:grid;grid-template-columns:auto 1fr;gap:20px;align-items:center}}
.donut-chart{{width:260px;flex-shrink:0}}
.donut-legend{{padding-left:8px}}
.legend-list{{display:flex;flex-direction:column;gap:8px}}
.legend-row{{display:grid;grid-template-columns:14px 1fr auto;gap:10px;align-items:baseline;font-size:13px}}
.legend-row .dot{{width:12px;height:12px;border-radius:3px;display:inline-block;margin-top:2px}}
.legend-row .lbl{{color:{THEME.ink_muted}}}
.legend-row .val{{color:{THEME.ink};font-variant-numeric:tabular-nums;text-align:right;font-size:13px}}
.legend-row.ml-md .lbl{{padding-left:14px;color:{THEME.ink_subtle};font-size:12px}}

/* METRICS ROW */
.metrics-row{{display:flex;flex-direction:column;gap:8px}}
.metric-chart{{}}

/* WEEKLY EARNINGS — chart left + week detail panel right */
.weekly-row{{display:grid;grid-template-columns:1fr 220px;gap:18px;align-items:start}}
.weekly-chart{{min-width:0}}
.week-detail{{background:{THEME.bg_app};border:1px solid {THEME.border};border-radius:10px;padding:18px 16px}}
.wd-header{{font-size:11px;font-weight:600;color:{THEME.ink_muted};text-transform:uppercase;letter-spacing:.04em}}
.wd-week{{font-size:14px;font-weight:700;color:{THEME.ink};margin-top:2px}}
.wd-total{{font-size:24px;font-weight:800;color:{THEME.accent_blue};margin:10px 0 14px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}}
.wd-streams{{display:flex;flex-direction:column;gap:8px;border-top:1px solid {THEME.border};padding-top:12px}}
.wd-row{{display:grid;grid-template-columns:10px 1fr auto;gap:8px;align-items:center;font-size:12px}}
.wd-dot{{width:10px;height:10px;border-radius:2px}}
.wd-lbl{{color:{THEME.ink_muted};white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.wd-val{{color:{THEME.ink};font-variant-numeric:tabular-nums;font-weight:500}}
@media (max-width:760px){{.weekly-row{{grid-template-columns:1fr}}}}

/* HERO NUMBER */
.hero-num{{font-size:44px;font-weight:800;color:{THEME.accent_blue};line-height:1.05;margin:10px 0 6px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.hero-num span{{font-size:14px;font-weight:500;color:{THEME.ink_muted}}}

/* BIG NUMBER ROW (Carbon equivalents) */
.big-num-row{{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:center;padding:14px 0;border-bottom:1px dashed {THEME.border}}}
.big-num-row:last-child{{border-bottom:0}}
.big-num-val{{font-size:32px;font-weight:800;color:{THEME.accent_blue};font-variant-numeric:tabular-nums;line-height:1;min-width:100px}}
.big-num-row.life .big-num-val{{color:{THEME.primary_dark}}}
.big-num-label{{font-size:13px;color:{THEME.ink};font-weight:500}}
.big-num-desc{{font-size:11px;color:{THEME.ink_subtle};margin-top:2px;font-style:italic}}

.note{{font-size:12px;color:{THEME.ink_subtle};margin:12px 0 0;line-height:1.55;font-style:italic}}

.footer{{text-align:center;padding:28px 20px 20px;color:{THEME.ink_subtle};font-size:11px}}
.footer b{{color:{THEME.accent_blue}}}

@media print{{
    body{{background:white}}
    .tab-nav{{display:none !important}}
    .panel{{display:block !important;page-break-after:always;padding:14px 0 !important}}
    .wrap{{max-width:100% !important;padding:0 !important}}
    .hero{{box-shadow:none !important}}
    .card,.chart-card,.kpi{{box-shadow:none !important;page-break-inside:avoid}}
}}
</style>
</head>
<body>
<div class="wrap">
    <div class="hero">
        <h1>{self.client_name}</h1>
        <div class="sub">Posudok fotovoltickej elektrárne a batériového úložiska</div>
        <div class="meta">
            <span>{self.scenario_name}</span>
            <span>{now}</span>
            <span>OKTE 2025 · diskont {self.financial.discount_rate*100:.0f}%</span>
            <span>Horizon {self.financial.horizon_years} rokov</span>
        </div>
    </div>

    {kpi}
    {tabs}

    <div class="footer">
        <b>Energovision Analyzer v0.3.0</b> · {now} · Lokálny SK analytický nástroj
    </div>
</div>

<script>
function showTab(slug,btn){{
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
    document.getElementById('t-'+slug).classList.add('active');
    btn.classList.add('active');
    setTimeout(()=>window.dispatchEvent(new Event('resize')),50);
}}
</script>
</body>
</html>"""
