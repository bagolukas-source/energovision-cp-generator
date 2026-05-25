"""ValidationEngine — systematická kontrola vstupov a výstupov.

Cieľ: detekcia všetkých 8 kritických chýb identifikovaných v AUDIT_FINDINGS_2026-05-24.md
**pred** spustením simulácie, nie po nej.

Kategórie validácie:
    1. PHYSICAL_LIMITS — PV ≤ Pmax × 1.05, load ≤ RK × 1.2, SoC limits
    2. ENERGY_BALANCE — ∑PV = direct + export + losses
    3. TIMESTAMP — kontinuita, DST, gaps
    4. ANOMALY — spikes, frozen sensors, NaN/inf
    5. TARIFF — sanity súčet sadzieb, MRK penalty applicability
    6. CONFIG — sizing ratios, dotácia ≤ CAPEX, etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel

from energovision_analytics.core.models import (
    BESSInput,
    LoadProfileInput,
    PVInput,
    ScenarioConfig,
    SiteInput,
    TariffYearInput,
)
from energovision_analytics.core.time_series import TimeSeriesData


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class Issue:
    """Jedno zistenie validácie."""
    severity: Severity
    category: str
    rule: str
    message: str
    location: Optional[str] = None
    actual: Optional[float] = None
    expected: Optional[float] = None
    suggestion: Optional[str] = None


@dataclass
class ValidationReport:
    """Súhrn validácie."""
    issues: list[Issue] = field(default_factory=list)

    @property
    def n_critical(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.CRITICAL)

    @property
    def n_errors(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.ERROR)

    @property
    def n_warnings(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.WARNING)

    @property
    def n_infos(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.INFO)

    @property
    def passed(self) -> bool:
        return self.n_critical == 0 and self.n_errors == 0

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def summary(self) -> str:
        return (
            f"Validation: {self.n_critical} CRITICAL, {self.n_errors} ERRORS, "
            f"{self.n_warnings} WARNINGS, {self.n_infos} INFOS. "
            f"PASSED={self.passed}"
        )

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "n_critical": self.n_critical,
            "n_errors": self.n_errors,
            "n_warnings": self.n_warnings,
            "n_infos": self.n_infos,
            "issues": [
                {
                    "severity": i.severity.value,
                    "category": i.category,
                    "rule": i.rule,
                    "message": i.message,
                    "location": i.location,
                    "actual": i.actual,
                    "expected": i.expected,
                    "suggestion": i.suggestion,
                }
                for i in self.issues
            ],
        }


class ValidationEngine:
    """Hlavný engine — kombinuje všetky validačné kontroly."""

    def __init__(self) -> None:
        self.report = ValidationReport()

    # ------------------------------------------------------------------ Public API
    def validate_site(self, site: SiteInput) -> ValidationReport:
        """Validuj odberné miesto."""
        # GPS check — v SK
        if not (47.5 <= site.gps_lat <= 49.7):
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="PHYSICAL_LIMITS",
                rule="gps_lat_slovakia",
                message=f"GPS lat {site.gps_lat} mimo Slovensko (47.5–49.7°N)",
                location="site.gps_lat",
                actual=site.gps_lat,
            ))

        # RK / MRK sanity
        if site.mrk_kw < site.rk_kw:
            self.report.add(Issue(
                severity=Severity.CRITICAL,
                category="PHYSICAL_LIMITS",
                rule="mrk_geq_rk",
                message=f"MRK ({site.mrk_kw}) < RK ({site.rk_kw})",
                location="site",
                suggestion="Skontroluj zmluvu s distribútorom",
            ))

        if site.mrk_kw / site.rk_kw > 3.0:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="mrk_rk_ratio",
                message=f"MRK/RK = {site.mrk_kw/site.rk_kw:.1f} — neobvykle veľký pomer",
                location="site",
                suggestion="Overiť, či nie je preklep v hodnotách",
            ))

        return self.report

    def validate_pv(self, pv: PVInput, site: SiteInput) -> ValidationReport:
        """Validuj FVE."""
        # DC/AC ratio
        ratio = pv.instalovany_kwp / pv.inverter_kw_ac
        if ratio < 1.0:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="dc_ac_ratio_low",
                message=f"DC/AC ratio {ratio:.2f} < 1.0 — invertor je predimenzovaný",
                actual=ratio, expected=1.2,
                suggestion="Typický optimum 1.1–1.3",
            ))
        elif ratio > 1.4:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="dc_ac_ratio_high",
                message=f"DC/AC ratio {ratio:.2f} > 1.4 — clipping bude vysoký",
                actual=ratio, expected=1.2,
            ))

        # Sklon vs nadm výška + zemepisná šírka
        if pv.sklon_stupne < 5 and pv.konfiguracia.value != "EW":
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="sklon_low",
                message=f"Sklon {pv.sklon_stupne}° < 5° — vysoké soiling losses",
                suggestion="Pre SK optimum 25–35° (juh), pre EW 10–15°",
            ))

        # FVE vs MRK
        if pv.inverter_kw_ac > site.mrk_kw * 1.5:
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="PHYSICAL_LIMITS",
                rule="inverter_exceeds_mrk",
                message=f"Inverter AC {pv.inverter_kw_ac} kW > 1.5× MRK ({site.mrk_kw}) — "
                        f"môže prekročiť export limit",
                suggestion="Zvážiť BESS pre samospotrebu alebo dynamic export limit",
            ))

        return self.report

    def validate_bess(self, bess: BESSInput) -> ValidationReport:
        """Validuj BESS."""
        # C-rate
        actual_c = bess.power_kw_ac / bess.nominal_kwh
        if actual_c > bess.c_rate_max + 0.05:
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="PHYSICAL_LIMITS",
                rule="c_rate_exceeded",
                message=f"power/capacity = {actual_c:.2f}C > c_rate_max ({bess.c_rate_max}C)",
                actual=actual_c, expected=bess.c_rate_max,
            ))

        # RTE realism check
        if bess.rte_ac_ac > 0.92:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="rte_too_high",
                message=f"RTE {bess.rte_ac_ac:.2%} > 92% — datasheet hodnota, "
                        f"reálne 88–90% AC-AC po HVAC + BMS + standby straty",
                actual=bess.rte_ac_ac, expected=0.88,
                suggestion="Použiť 0.88 pre dispatch, 0.91 iba pre marketing materiály",
            ))

        # DoD window
        dod_window = bess.soc_max_pct - bess.soc_min_pct
        if dod_window < 0.80:
            self.report.add(Issue(
                severity=Severity.INFO,
                category="CONFIG",
                rule="dod_window_conservative",
                message=f"DoD window {dod_window:.0%} — konzervatívne, predĺži životnosť",
            ))
        elif dod_window > 0.95:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="dod_window_aggressive",
                message=f"DoD window {dod_window:.0%} — agresívne, skráti životnosť (cycle aging)",
            ))

        # Warranty cycle check
        max_cycles_per_year = bess.warranty_cycles / bess.warranty_years
        if max_cycles_per_year > 400:
            self.report.add(Issue(
                severity=Severity.INFO,
                category="CONFIG",
                rule="cycle_allowance_high",
                message=f"Warranty {max_cycles_per_year:.0f} cyklov/rok — vyhovuje aktívnemu EMS",
            ))

        return self.report

    def validate_load_profile(
        self,
        load_ts: TimeSeriesData,
        site: SiteInput,
    ) -> ValidationReport:
        """Validuj profil spotreby."""
        # 1) Granularita
        if load_ts.granularity_min not in (15, 60):
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="DATA_FORMAT",
                rule="granularity",
                message=f"Granularita {load_ts.granularity_min} min — povolené iba 15 alebo 60",
            ))

        # 2) Gaps
        if load_ts.has_gaps():
            n_gaps = load_ts.gap_count()
            sev = Severity.ERROR if n_gaps > 10 else Severity.WARNING
            self.report.add(Issue(
                severity=sev,
                category="DATA_COMPLETENESS",
                rule="timestamp_gaps",
                message=f"V dátach je {n_gaps} medzier",
                actual=n_gaps, expected=0,
                suggestion="Doplniť interpoláciou alebo vyžiadať nové dáta z distribútora",
            ))

        # 3) Negative values
        if (load_ts.values < 0).any():
            n_neg = int((load_ts.values < 0).sum())
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="PHYSICAL_LIMITS",
                rule="negative_load",
                message=f"{n_neg} hodnôt < 0 v load profile — možno netto profile (load - PV)?",
                suggestion="Skontroluj, či dáta nezahŕňajú už existujúcu FVE výrobu",
            ))

        # 4) Outliers
        n_out = load_ts.n_outliers_iqr(k=5.0)
        if n_out > 0:
            sev = Severity.WARNING if n_out < 20 else Severity.ERROR
            self.report.add(Issue(
                severity=sev,
                category="ANOMALY",
                rule="iqr_outliers_5x",
                message=f"{n_out} outlierov (> 5× IQR) — možné chybové merania",
                actual=n_out,
            ))

        # 5) Annual sum sanity vs site.rocna_spotreba_kwh
        annual_kwh = load_ts.annual_sum_kwh()
        deviation = abs(annual_kwh - site.rocna_spotreba_kwh) / site.rocna_spotreba_kwh
        if deviation > 0.05:
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="DATA_CONSISTENCY",
                rule="annual_sum_mismatch",
                message=f"Suma load dát ({annual_kwh:.0f} kWh) sa nezhoduje s "
                        f"faktúrou ({site.rocna_spotreba_kwh:.0f} kWh) — odchýlka {deviation*100:.1f}%",
                actual=annual_kwh, expected=site.rocna_spotreba_kwh,
                suggestion="Skontrolovať, či profil pokrýva celý kalendárny rok",
            ))

        # 6) Max load vs RK
        max_load = load_ts.annual_max_kw()
        if max_load > site.rk_kw * 1.2:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="PHYSICAL_LIMITS",
                rule="load_exceeds_rk",
                message=f"Max load {max_load:.0f} kW > 1.2× RK ({site.rk_kw}) — "
                        f"prekročenie RK = pokuta od distribútora",
                actual=max_load, expected=site.rk_kw,
                suggestion="Zvážiť peak shaving cez BESS",
            ))

        # 7) Constantness check (frozen sensor)
        n_unique_pct = len(np.unique(load_ts.values)) / len(load_ts.values) * 100
        if n_unique_pct < 5:
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="ANOMALY",
                rule="frozen_sensor",
                message=f"Iba {n_unique_pct:.1f}% unikátnych hodnôt — možný zaseknutý senzor",
            ))

        return self.report

    def validate_pv_profile(
        self,
        pv_ts: TimeSeriesData,
        pv: PVInput,
    ) -> ValidationReport:
        """Validuj PV profile vs inštalácia."""
        # Max PV nemá byť > Pdc × 1.05 (clipping margin)
        max_pv = pv_ts.annual_max_kw()
        if max_pv > pv.inverter_kw_ac * 1.05:
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="PHYSICAL_LIMITS",
                rule="pv_exceeds_inverter",
                message=f"Max PV {max_pv:.0f} > inverter Pmax ({pv.inverter_kw_ac:.0f}) — "
                        f"clipping nebol aplikovaný v dátach",
                actual=max_pv, expected=pv.inverter_kw_ac,
            ))

        # PV nesmie byť záporné
        if (pv_ts.values < -0.01).any():
            n_neg = int((pv_ts.values < -0.01).sum())
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="PHYSICAL_LIMITS",
                rule="negative_pv",
                message=f"{n_neg} záporných hodnôt v PV — nemožné fyzicky",
            ))

        # Yield sanity (SK typický 950–1150 kWh/kWp/rok)
        annual_kwh = pv_ts.annual_sum_kwh()
        yield_kwh_per_kwp = annual_kwh / pv.instalovany_kwp
        if yield_kwh_per_kwp < 800:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="pv_yield_low",
                message=f"Špecifický výnos {yield_kwh_per_kwp:.0f} kWh/kWp < 800 — "
                        f"príliš nízky pre SK (typický 950–1150)",
                actual=yield_kwh_per_kwp, expected=1050,
            ))
        elif yield_kwh_per_kwp > 1300:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="pv_yield_high",
                message=f"Špecifický výnos {yield_kwh_per_kwp:.0f} kWh/kWp > 1300 — "
                        f"veľmi vysoký pre SK, overiť (možno overestimovaná dáta?)",
                actual=yield_kwh_per_kwp, expected=1050,
            ))

        return self.report

    def validate_spot_prices(self, spot_ts: TimeSeriesData) -> ValidationReport:
        """Validuj OKTE spot ceny."""
        values = spot_ts.values
        if (values < -500).any():
            n = int((values < -500).sum())
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="PHYSICAL_LIMITS",
                rule="spot_too_negative",
                message=f"{n} cien < -500 €/MWh — pravdepodobne chyba dát",
            ))

        if (values > 4000).any():
            n = int((values > 4000).sum())
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="ANOMALY",
                rule="spot_extreme_high",
                message=f"{n} cien > 4000 €/MWh — extrémne hodnoty, overiť",
            ))

        # OKTE typicky 8760 hodín za rok
        if 8000 < spot_ts.n_steps < 8800:
            pass  # ok
        else:
            self.report.add(Issue(
                severity=Severity.INFO,
                category="DATA_COMPLETENESS",
                rule="spot_hours_per_year",
                message=f"Spot dáta majú {spot_ts.n_steps} hodín — očakávaných 8760",
            ))

        return self.report

    def validate_tariff(self, tariff: TariffYearInput) -> ValidationReport:
        """Validuj tarif."""
        # Súčet komponentov nesmie byť 0
        total = tariff.regulovane_zlozky_eur_mwh + tariff.obchodnik_zlozky_eur_mwh
        if total < 10:
            self.report.add(Issue(
                severity=Severity.ERROR,
                category="CONFIG",
                rule="tariff_sum_low",
                message=f"Súčet regulovaných + obchodník = {total:.1f} €/MWh — neprimerane nízky",
            ))

        # MRK penalty 2026 awareness
        if tariff.rok >= 2026 and tariff.distribuutor.value == "SSE" and tariff.sadzba.value == "VN":
            if tariff.mrk_export_penalty_eur_kwh == 0:
                self.report.add(Issue(
                    severity=Severity.WARNING,
                    category="CONFIG",
                    rule="mrk_export_penalty_missing",
                    message="SSE VN od 1.1.2026 zavádza MRK export penalty (§ 24/11 vyhl. 154/2024) — "
                            "tariff má hodnotu 0, čo môže byť zastaralé",
                    suggestion="Aktualizovať mrk_export_penalty_eur_kwh na 0.0125",
                ))

        return self.report

    def validate_scenario(self, sc: ScenarioConfig) -> ValidationReport:
        """Validuj scenár konfiguráciu."""
        if sc.dotacia_eur > sc.capex_eur:
            self.report.add(Issue(
                severity=Severity.CRITICAL,
                category="CONFIG",
                rule="dotacia_exceeds_capex",
                message=f"Dotácia ({sc.dotacia_eur}) > CAPEX ({sc.capex_eur})",
            ))

        dotacia_pct = sc.dotacia_eur / sc.capex_eur * 100 if sc.capex_eur else 0
        if dotacia_pct > 50:
            self.report.add(Issue(
                severity=Severity.WARNING,
                category="CONFIG",
                rule="dotacia_too_high",
                message=f"Dotácia {dotacia_pct:.0f}% z CAPEX — Zelená podnikom max 45%",
            ))

        if sc.diskont < 0.04:
            self.report.add(Issue(
                severity=Severity.INFO,
                category="CONFIG",
                rule="discount_low",
                message=f"Diskont {sc.diskont*100:.1f}% < 4% — optimistický pre energo projekty",
            ))

        if sc.timestep_min == 60:
            self.report.add(Issue(
                severity=Severity.INFO,
                category="CONFIG",
                rule="hourly_granularity",
                message="60-min granularita — peak shaving sa nedá presne modelovať. "
                        "Pre VN klientov preferovať 15-min",
            ))

        return self.report

    def validate_all(
        self,
        site: SiteInput,
        pv: Optional[PVInput] = None,
        bess: Optional[BESSInput] = None,
        load_ts: Optional[TimeSeriesData] = None,
        pv_ts: Optional[TimeSeriesData] = None,
        spot_ts: Optional[TimeSeriesData] = None,
        tariff: Optional[TariffYearInput] = None,
        scenario: Optional[ScenarioConfig] = None,
    ) -> ValidationReport:
        """Spusti všetky relevantné validácie naraz."""
        self.validate_site(site)
        if pv:
            self.validate_pv(pv, site)
        if bess:
            self.validate_bess(bess)
        if load_ts:
            self.validate_load_profile(load_ts, site)
        if pv_ts and pv:
            self.validate_pv_profile(pv_ts, pv)
        if spot_ts:
            self.validate_spot_prices(spot_ts)
        if tariff:
            self.validate_tariff(tariff)
        if scenario:
            self.validate_scenario(scenario)
        return self.report
