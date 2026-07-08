"""Spoločné dátové typy pre EMS dispatch."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ValueStream(str, Enum):
    """Kategorizácia úspor — kam šiel benefit dispatch akcie."""
    SOLAR_SELF_CONS = "solar_self_cons"           # PV → load
    SOLAR_EXPORT = "solar_export"                  # PV → grid
    BESS_SELF_CONS = "bess_self_cons"             # BAT → load (deficit)
    ARBITRAGE = "arbitrage"                        # BAT load-shifting (lacný spot → drahý)
    PEAK_SHAVING = "peak_shaving"                 # BAT zníži ¼-h špičku
    MRK_PENALTY_AVOIDED = "mrk_penalty_avoided"   # PV→BAT namiesto >MRK exportu


class DispatchAction(str, Enum):
    NORMAL = "normal"           # PV→load + prebytok export
    CHARGE_GRID = "charge_grid" # Arbitráž — lacný spot
    DISCHARGE_BAT = "discharge_bat"  # Arbitráž alebo deficit
    PEAK_SHAVE = "peak_shave"   # Núdzové discharge pre MRK
    HOLD = "hold"


@dataclass
class EMSConfig:
    """Konfigurácia EMS stratégie."""
    # Multi-cycle limit
    max_cycles_per_day: int = 2          # Realistic optimum pre LFP (warranty target)
    max_efc_per_year: float = 1000.0      # warranty 10000 cy / 10 r = 1000/y target (LFP B2B)

    # Arbitráž triggery
    lookahead_hours: int = 12             # 12h okno → zachytí intra-day sedlá/špičky (2 cykly/deň); pôvodne 24
    arb_min_spread_eur_mwh: float = 30.0  # Min ekonomický spread (po RTE straty)
    # Šírka obchodného pásma v okne: 0 = len absolútny extrém ±5 € (default — meraním
    # overené, že širšie pásmo kanibalizuje samospotrebu a CELKOVO zarába menej;
    # test 2026-07-08: band 0/0.25/0.40 → 1285/1043/866 €/r). Laditeľné cez ems_config.
    arb_band_pct: float = 0.0
    arb_threshold_charge_eur_mwh: float = 50.0   # Charge len pod touto cenou
    arb_threshold_discharge_eur_mwh: float = 120.0  # Discharge len nad touto

    # Peak shaving
    peak_shave_enabled: bool = True
    peak_shave_target_pct: float = 0.95   # Target = 95% MRK (rezerva)

    # Self-consumption
    enable_bess_self_cons: bool = True    # BESS vybíja v deficite (Stav 0)

    # Negative spot curtailment (AOM-FIX-31)
    negative_spot_curtail: bool = True    # Pri spot < 0 €/MWh neexportuj (preferuj BAT charge alebo clip)


@dataclass
class DispatchInterval:
    """Stav v 1 timestepe simulácie (15-min alebo 60-min)."""
    timestamp: datetime
    dt_hours: float

    # Vstupy
    load_kw: float
    pv_kw: float            # PV výroba (po inverter clipping)
    spot_eur_mwh: float
    tarif_buy_eur_kwh: float

    # SoC tracking
    bat_soc_kwh_start: float
    bat_soc_kwh_end: float
    bat_soh: float

    # Energy flows (kWh za timestep)
    pv_to_load_kwh: float = 0.0
    pv_to_bat_kwh: float = 0.0
    pv_to_grid_kwh: float = 0.0
    pv_clipped_kwh: float = 0.0

    grid_to_load_kwh: float = 0.0
    grid_to_bat_kwh: float = 0.0
    bat_to_load_kwh: float = 0.0
    bat_to_grid_kwh: float = 0.0
    bat_losses_kwh: float = 0.0

    # Finálne
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    mrk_overflow_kw: float = 0.0

    # Dispatch metadata
    action: DispatchAction = DispatchAction.NORMAL
    efc_this_step: float = 0.0

    # Saving decomposition per value stream
    sav_solar_self_cons_eur: float = 0.0
    sav_solar_export_eur: float = 0.0
    sav_bess_self_cons_eur: float = 0.0
    sav_arbitrage_eur: float = 0.0
    sav_peak_shaving_eur: float = 0.0
    sav_mrk_penalty_avoided_eur: float = 0.0

    @property
    def sav_total_eur(self) -> float:
        return (
            self.sav_solar_self_cons_eur
            + self.sav_solar_export_eur
            + self.sav_bess_self_cons_eur
            + self.sav_arbitrage_eur
            + self.sav_peak_shaving_eur
            + self.sav_mrk_penalty_avoided_eur
        )


@dataclass
class DispatchSummary:
    """Ročný sumár dispatch behu."""
    rok: int
    n_intervals: int

    # Energetické bilancie (kWh/rok)
    load_total_kwh: float = 0.0
    pv_total_kwh: float = 0.0
    pv_to_load_kwh: float = 0.0
    pv_to_bat_kwh: float = 0.0
    pv_to_grid_kwh: float = 0.0
    pv_clipped_kwh: float = 0.0
    bat_charge_total_kwh: float = 0.0
    bat_discharge_total_kwh: float = 0.0
    bat_losses_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0

    # KPI
    samospotreba_pct: float = 0.0  # (PV→load + PV→BAT) / PV_total
    samostatnost_pct: float = 0.0  # (PV→load + BAT→load) / load
    bat_efc: float = 0.0
    bat_soh_end: float = 1.0
    n_replacements: int = 0

    # Value streams (€/rok)
    sav_solar_self_cons_eur: float = 0.0
    sav_solar_export_eur: float = 0.0
    sav_bess_self_cons_eur: float = 0.0
    sav_arbitrage_eur: float = 0.0
    sav_peak_shaving_eur: float = 0.0
    sav_mrk_penalty_avoided_eur: float = 0.0
    sav_total_eur: float = 0.0

    # Akcie distribúcia
    n_state_normal: int = 0
    n_state_charge_grid: int = 0
    n_state_discharge: int = 0
    n_state_peak_shave: int = 0

    # CO2
    co2_avoided_t: float = 0.0
