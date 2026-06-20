"""CashflowBuilder — generátor mesačného/ročného cashflow s SK špecifikami."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from energovision_analytics.financial.metrics import (
    compute_irr_robust,
    compute_lcoe,
    compute_lcos,
    compute_npv,
    compute_payback,
)
from energovision_analytics.financial.tax_shield import sk_tax_shield_schedule


@dataclass
class CashflowYear:
    """Cashflow pre 1 rok."""
    year: int

    # Revenue streams (SK)
    rev_solar_self_cons: float = 0.0
    rev_solar_export: float = 0.0
    rev_bess_self_cons: float = 0.0
    rev_arbitrage: float = 0.0
    rev_peak_shaving: float = 0.0
    rev_mrk_penalty_avoided: float = 0.0
    rev_merchant: float = 0.0          # merchant arbitráž (podpora bilančnej skupiny, grid-to-grid)

    # Costs
    cost_solar_capex: float = 0.0
    cost_bess_capex: float = 0.0
    cost_solar_opex: float = 0.0
    cost_bess_opex: float = 0.0
    cost_insurance: float = 0.0
    cost_inverter_replacement: float = 0.0
    cost_bess_cells_replacement: float = 0.0
    cost_monitoring: float = 0.0

    # SK špecifické
    dotacia_zelena: float = 0.0           # +€ v roku 0 (alebo splatka)
    tax_shield: float = 0.0                # +€ rok 1-6
    vat_refund: float = 0.0                # +€ v roku 0 ak B2B

    # Performance modifiers
    bess_soh: float = 1.0
    pv_capacity_factor: float = 1.0

    @property
    def revenue_total(self) -> float:
        return (self.rev_solar_self_cons + self.rev_solar_export
                + self.rev_bess_self_cons + self.rev_arbitrage
                + self.rev_peak_shaving + self.rev_mrk_penalty_avoided
                + self.rev_merchant)

    @property
    def opex_total(self) -> float:
        return (self.cost_solar_opex + self.cost_bess_opex
                + self.cost_insurance + self.cost_monitoring)

    @property
    def capex_total(self) -> float:
        return (self.cost_solar_capex + self.cost_bess_capex
                + self.cost_inverter_replacement + self.cost_bess_cells_replacement)

    @property
    def net_cashflow(self) -> float:
        """Net cashflow rok = revenue - opex - capex + tax_shield + dotacia + vat_refund."""
        return (self.revenue_total
                - self.opex_total
                - self.capex_total
                + self.tax_shield
                + self.dotacia_zelena
                + self.vat_refund)


@dataclass
class FinancialResult:
    """Top-level výsledok finančnej analýzy."""
    horizon_years: int
    discount_rate: float

    # CAPEX
    capex_gross_eur: float
    dotacia_eur: float
    vat_refund_eur: float
    capex_net_eur: float

    # Yearly cashflows
    yearly_cashflows: list[CashflowYear] = field(default_factory=list)

    # Metriky
    npv_eur: float = 0.0
    irr_pct: Optional[float] = None
    payback_simple_y: float = 99.0
    payback_discounted_y: float = 99.0
    lcoe_eur_mwh: Optional[float] = None
    lcos_eur_mwh: Optional[float] = None

    # Annual averages
    annual_saving_y1_eur: float = 0.0
    annual_saving_lifetime_avg_eur: float = 0.0
    total_lifetime_revenue_eur: float = 0.0


class CashflowBuilder:
    """Stavebnik cashflow z dispatch summary + SK ekonomických parametrov."""

    def __init__(
        self,
        capex_solar_eur: float,
        capex_bess_eur: float,
        opex_solar_pct: float = 0.015,
        opex_bess_pct: float = 0.020,
        insurance_pct: float = 0.003,
        monitoring_eur_per_year: float = 300,
        bess_inverter_replacement_year: Optional[int] = 12,
        bess_inverter_replacement_pct: float = 0.10,
        pv_inverter_replacement_year: Optional[int] = 13,
        pv_inverter_replacement_pct: float = 0.06,
        bess_cells_replacement_year: Optional[int] = None,
        bess_cells_replacement_interval_years: Optional[int] = None,
        dppo_pct: float = 0.21,
        depr_years: int = 6,
        discount_rate: float = 0.06,
        horizon_years: int = 25,
        is_b2b_vat_refund: bool = False,
        vat_rate: float = 0.20,
        price_escalation_pct: float = 0.0,
        savings_coefficient: float = 1.0,
        has_sufficient_profit: bool = True,
    ) -> None:
        self.capex_solar = capex_solar_eur
        self.capex_bess = capex_bess_eur
        self.opex_solar_pct = opex_solar_pct
        self.opex_bess_pct = opex_bess_pct
        self.insurance_pct = insurance_pct
        self.monitoring_eur_per_year = monitoring_eur_per_year
        self.bess_inverter_replacement_year = bess_inverter_replacement_year
        self.bess_inverter_replacement_pct = bess_inverter_replacement_pct
        self.pv_inverter_replacement_year = pv_inverter_replacement_year
        self.pv_inverter_replacement_pct = pv_inverter_replacement_pct
        self.bess_cells_replacement_year = bess_cells_replacement_year
        self.bess_cells_replacement_interval_years = bess_cells_replacement_interval_years
        self.dppo_pct = dppo_pct
        self.depr_years = depr_years
        self.discount_rate = discount_rate
        self.horizon_years = horizon_years
        self.is_b2b_vat_refund = is_b2b_vat_refund
        self.vat_rate = vat_rate
        # Manuálne páčky (AOM): ročný rast cien energií % + korekčný koeficient úspory
        self.price_escalation_pct = price_escalation_pct or 0.0
        self.savings_coefficient = savings_coefficient if (savings_coefficient and savings_coefficient > 0) else 1.0
        self.has_sufficient_profit = bool(has_sufficient_profit)

    def build(
        self,
        annual_saving_y1_eur: float,
        saving_decomp_y1: dict,
        dotacia_eur: float = 0.0,
        annual_degradation_pct: float = 0.5,  # degradácia FVE panelov %/rok
        bess_degradation_pct: float = 2.0,    # degradácia batérie %/rok (LiFePO4, reset po výmene článkov)
        annual_pv_kwh: float = 0.0,
        annual_bess_discharge_kwh: float = 0.0,
        annual_bess_charge_cost_eur: float = 0.0,
    ) -> FinancialResult:
        """Postaví ročný cashflow + spočíta NPV/IRR/Payback/LCOS/LCOE."""
        gross = self.capex_solar + self.capex_bess
        vat_refund = gross * (self.vat_rate / (1 + self.vat_rate)) if self.is_b2b_vat_refund else 0.0
        net_capex = gross - dotacia_eur - vat_refund

        result = FinancialResult(
            horizon_years=self.horizon_years,
            discount_rate=self.discount_rate,
            capex_gross_eur=gross,
            dotacia_eur=dotacia_eur,
            vat_refund_eur=vat_refund,
            capex_net_eur=net_capex,
            annual_saving_y1_eur=annual_saving_y1_eur,
        )

        tax_shield_schedule = sk_tax_shield_schedule(net_capex, self.dppo_pct, self.depr_years, self.has_sufficient_profit)

        # Year 0 — investícia
        y0 = CashflowYear(
            year=0,
            cost_solar_capex=self.capex_solar,
            cost_bess_capex=self.capex_bess,
            dotacia_zelena=dotacia_eur,
            vat_refund=vat_refund,
        )
        result.yearly_cashflows.append(y0)

        # roky obnovy kapacity batérie (výmena článkov) — po nich degradácia batérie reštartuje
        _restore_years = set()
        if self.bess_cells_replacement_year:
            _restore_years.add(int(self.bess_cells_replacement_year))
        if self.bess_cells_replacement_interval_years:
            _ry = int(self.bess_cells_replacement_interval_years)
            while _ry < self.horizon_years:
                _restore_years.add(_ry); _ry += int(self.bess_cells_replacement_interval_years)

        # Year 1..horizon
        total_revenue = 0.0
        for y in range(1, self.horizon_years + 1):
            deg_pv = (1 - annual_degradation_pct / 100) ** (y - 1)
            _last_restore = max([ry for ry in _restore_years if ry <= y], default=0)
            _bess_age = (y - _last_restore) if _last_restore > 0 else (y - 1)
            deg_bess = (1 - bess_degradation_pct / 100) ** _bess_age
            deg = deg_pv  # spätná kompatibilita
            # AOM páčky: rast cien energií rastie hodnotu ušetrenej/predanej kWh; korekčný koeficient škáluje výsledok
            esc = (1.0 + self.price_escalation_pct / 100.0) ** (y - 1)
            kf_pv = deg_pv * esc * self.savings_coefficient
            kf_bess = deg_bess * esc * self.savings_coefficient
            cy = CashflowYear(year=y, bess_soh=deg_bess, pv_capacity_factor=deg_pv)
            # Revenue — FVE streamy degradujú pomaly (panely), batériové rýchlejšie (články)
            cy.rev_solar_self_cons = saving_decomp_y1.get("sav_solar_self_cons_eur", 0) * kf_pv
            cy.rev_solar_export = saving_decomp_y1.get("sav_solar_export_eur", 0) * kf_pv
            cy.rev_bess_self_cons = saving_decomp_y1.get("sav_bess_self_cons_eur", 0) * kf_bess
            cy.rev_arbitrage = saving_decomp_y1.get("sav_arbitrage_eur", 0) * kf_bess
            cy.rev_peak_shaving = saving_decomp_y1.get("sav_peak_shaving_eur", 0) * kf_bess
            cy.rev_mrk_penalty_avoided = saving_decomp_y1.get("sav_mrk_penalty_avoided_eur", 0) * kf_bess
            cy.rev_merchant = saving_decomp_y1.get("sav_merchant_eur", 0) * kf_bess

            # OPEX
            cy.cost_solar_opex = self.capex_solar * self.opex_solar_pct
            cy.cost_bess_opex = self.capex_bess * self.opex_bess_pct
            cy.cost_insurance = (self.capex_solar + self.capex_bess) * self.insurance_pct
            cy.cost_monitoring = self.monitoring_eur_per_year

            # Replacement events
            if y == self.bess_inverter_replacement_year:
                cy.cost_inverter_replacement = self.capex_bess * self.bess_inverter_replacement_pct
            if y == self.pv_inverter_replacement_year:
                cy.cost_inverter_replacement += self.capex_solar * self.pv_inverter_replacement_pct  # výmena meniča FVE ~6 % FVE CAPEXu
            if y == self.bess_cells_replacement_year:
                cy.cost_bess_cells_replacement = self.capex_bess * 0.40
            elif (self.bess_cells_replacement_interval_years
                  and y > 0 and y < self.horizon_years
                  and y % self.bess_cells_replacement_interval_years == 0):
                # Výmena článkov po vyčerpaní warranty cyklov (40 % BESS capexu), capacita sa obnoví
                cy.cost_bess_cells_replacement = self.capex_bess * 0.40

            # Tax shield
            if y <= self.depr_years:
                cy.tax_shield = tax_shield_schedule[y - 1]

            result.yearly_cashflows.append(cy)
            total_revenue += cy.revenue_total

        # Spočítaj cashflows + NPV/IRR/Payback
        cf_array = np.array([c.net_cashflow for c in result.yearly_cashflows])
        # POZOR: net_cashflow rok 0 zahŕňa CAPEX ako náklad (z capex_total) — to je negatívne
        result.npv_eur = compute_npv(cf_array, self.discount_rate)
        result.irr_pct = compute_irr_robust(cf_array)
        if result.irr_pct is not None:
            result.irr_pct *= 100
        result.payback_simple_y = compute_payback(cf_array, discount_rate=0.0)
        result.payback_discounted_y = compute_payback(cf_array, discount_rate=self.discount_rate)
        result.annual_saving_lifetime_avg_eur = total_revenue / self.horizon_years
        result.total_lifetime_revenue_eur = total_revenue

        # LCOE / LCOS
        if annual_pv_kwh > 0:
            result.lcoe_eur_mwh = compute_lcoe(
                self.capex_solar, self.capex_solar * self.opex_solar_pct,
                annual_pv_kwh, self.horizon_years, self.discount_rate, annual_degradation_pct,
            )
        if annual_bess_discharge_kwh > 0:
            result.lcos_eur_mwh = compute_lcos(
                self.capex_bess, self.capex_bess * self.opex_bess_pct,
                annual_bess_discharge_kwh, annual_bess_charge_cost_eur,
                self.horizon_years, self.discount_rate,
                self.bess_inverter_replacement_year,
                self.bess_inverter_replacement_pct,
            )

        return result
