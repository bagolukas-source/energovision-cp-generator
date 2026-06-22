"""PVSystemSim — wrapper class spájajúci všetky PV moduly."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from energovision_analytics.core.models import PVInput, SiteInput
from energovision_analytics.pv.analytical import synthesize_hourly_profile
from energovision_analytics.pv.degradation import pv_capacity_factor
from energovision_analytics.pv.losses import total_loss_factor


class PVSystemSim:
    """Plnohodnotný PV simulator — od PV+Site inputov po hodinový profil."""

    def __init__(self, pv: PVInput, site: SiteInput) -> None:
        self.pv = pv
        self.site = site

        # Predpočítaj loss factor (konštanta pre rok 1)
        self.loss_factor = total_loss_factor(
            soiling_pct=pv.soiling_pct_rok,
            snow_pct=pv.snow_pct_rok,
            mismatch_pct=pv.mismatch_pct,
            wiring_pct=pv.wiring_pct,
            inverter_eff_pct=(1 - pv.inverter_eff_nom) * 100,
            availability_pct=1.0,
            other_pct=0.5,
        )

    def simulate_year(self, year: int, timestep_min: int = 60) -> pd.DataFrame:
        """Spustí PV simuláciu na 1 rok."""
        df = synthesize_hourly_profile(
            year=year,
            lat=self.site.gps_lat,
            lon=self.site.gps_lon,
            installed_kwp=self.pv.instalovany_kwp,
            sklon=self.pv.sklon_stupne,
            azimut=self.pv.azimut_stupne,
            timestep_min=timestep_min,
            # BOD 6 FIX: PVGIS yieldy sú net (~14%). Komponentový model strát aplikujeme LEN ak
            # je projekt HORŠÍ než PVGIS baseline (0.86); nikdy nenafukujeme nad PVGIS net.
            losses_factor=min(1.0, self.loss_factor / 0.86),
            konfig=getattr(self.pv.konfiguracia, "value", str(self.pv.konfiguracia)),
        )

        # Aplikuj inverter clipping (DC > AC limit)
        # Pre MVP zjednodušíme — analytický model už berie do úvahy inverter_eff_pct v losses
        # Clipping per timestep:
        clipped_kw = (df["pv_kw"] - self.pv.inverter_kw_ac).clip(lower=0)
        df["pv_clipped_kw"] = clipped_kw
        df["pv_kw"] = df["pv_kw"].clip(upper=self.pv.inverter_kw_ac)

        return df

    def simulate_horizon(
        self,
        start_year: int,
        horizon_years: int,
        timestep_min: int = 60,
    ) -> pd.DataFrame:
        """Spustí simuláciu pre celý horizont (s degradáciou).

        Returns:
            DataFrame s timestamp index, stĺpcami pv_kw, pv_clipped_kw + project_year.
        """
        all_years = []
        for y in range(horizon_years):
            year = start_year + y
            df = self.simulate_year(year, timestep_min)
            # Aplikuj degradáciu
            cf = pv_capacity_factor(y + 1, self.pv.modul_typ.value)
            df["pv_kw"] = df["pv_kw"] * cf
            df["pv_clipped_kw"] = df["pv_clipped_kw"] * cf
            df["project_year"] = y + 1
            all_years.append(df)
        return pd.concat(all_years)

    def annual_yield_kwh(self, year: int = 1, timestep_min: int = 60) -> float:
        """Vráti ročnú výrobu (kWh) pre konkrétny rok."""
        df = self.simulate_year(2025, timestep_min)
        dt_hours = timestep_min / 60
        # Aplikuj degradáciu
        cf = pv_capacity_factor(year, self.pv.modul_typ.value)
        return float(df["pv_kw"].sum() * dt_hours * cf)

    def specific_yield_kwh_per_kwp(self, year: int = 1) -> float:
        """Vráti špecifický výnos (kWh/kWp/rok)."""
        return self.annual_yield_kwh(year) / self.pv.instalovany_kwp
