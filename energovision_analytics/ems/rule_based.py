"""Rule-based EMS dispatch s multi-cycle + warranty constraint.

Priority per timestep (high → low):
    P1: Peak shaving (ak load_15min > MRK × target_pct)
    P2: PV → load priame (vždy preferované)
    P3: PV → BAT charge (ak surplus)
    P4: BAT discharge arbitráž (drahá hodina v lookahead, BAT má SoC)
    P5: BESS self-cons (ak deficit a SoC > min)
    P6: Grid charge arbitráž (lacná hodina, BAT mieta priestor)
    P7: PV → grid export (zvyšok)
    P8: Hold

Warranty constraint: sum(EFC) ≤ max_efc_per_year × cycle_budget_factor.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from energovision_analytics.battery.pack_model import BatteryPack
from energovision_analytics.core.models import SiteInput, TariffYearInput
from energovision_analytics.ems.dispatch_state import (
    DispatchAction,
    DispatchInterval,
    DispatchSummary,
    EMSConfig,
)
from energovision_analytics.tariff.retail_calculator import RetailCalculator


class RuleBasedEMS:
    """Multi-cycle rule-based EMS dispatcher."""

    def __init__(
        self,
        battery: BatteryPack,
        site: SiteInput,
        tariff: TariffYearInput,
        retail_calc: RetailCalculator,
        config: Optional[EMSConfig] = None,
        export_price_eur_kwh: float = 0.06,
        sk_grid_co2_kg_per_kwh: float = 0.25,
    ) -> None:
        self.bat = battery
        self.site = site
        self.tariff = tariff
        self.retail = retail_calc
        self.config = config or EMSConfig()
        self.export_price = export_price_eur_kwh
        self.co2_factor = sk_grid_co2_kg_per_kwh

        # Annual cycle budget tracker
        self.efc_used_this_year = 0.0
        self.cycle_budget_factor = 1.0  # 1.0 = plný target, <1.0 ak treba šetriť cykly

    # ------------------------------------------------------------------ Run year
    def run_year(
        self,
        load_kw: np.ndarray,
        pv_kw: np.ndarray,
        spot_eur_mwh: np.ndarray,
        timestamps: pd.DatetimeIndex,
        timestep_min: int = 60,
    ) -> tuple[list[DispatchInterval], DispatchSummary]:
        """Spustí dispatch na 1 rok.

        Vstupy:
            load_kw, pv_kw, spot_eur_mwh: numpy arrays rovnakej dĺžky
            timestamps: pandas DatetimeIndex
            timestep_min: 15 alebo 60

        Returns:
            (intervals, summary)
        """
        n = len(load_kw)
        assert len(pv_kw) == n
        assert len(spot_eur_mwh) == n
        dt_h = timestep_min / 60

        # Reset annual cycle budget
        self.efc_used_this_year = 0.0
        max_efc = self.config.max_efc_per_year

        # MRK target (kW)
        mrk_target = self.site.mrk_kw * self.config.peak_shave_target_pct

        intervals: list[DispatchInterval] = []
        summary = DispatchSummary(rok=timestamps[0].year, n_intervals=n)

        # Precompute lookahead helpers — pre každý timestep top/bottom kvantily spotu
        lookahead_steps = int(self.config.lookahead_hours * 60 / timestep_min)

        # BS-arbitraz akumulatory (spot-based, korektny model)
        _arb_charge_spot = 0.0   # nabijaci naklad z gridu @ spot
        _disch_spot = 0.0        # vybitie @ spot (pre grid-arbitraz)
        _disch_retail = 0.0      # vybitie @ retail (pre PV samospotrebu)
        _grid_charge_kwh = 0.0
        # Per-interval cost-basis oceňovanie batérie (PRESNÝ arbitrážny spread — opravuje grid_frac priemer)
        _pv_bucket = float(self.bat.soc_kwh)   # počiatočný SoC = PV-báza (neutrálne)
        _grid_bucket = 0.0
        _grid_cost = 0.0
        _bess_self_acc = 0.0
        _arbitrage_acc = 0.0
        _mon_max_load = {}   # mesiac -> max load kW
        _mon_max_net = {}    # mesiac -> max (load - peak_shave) kW
        for i in range(n):
            ts = timestamps[i].to_pydatetime() if hasattr(timestamps[i], "to_pydatetime") else timestamps[i]
            load = float(load_kw[i])
            pv = float(pv_kw[i])
            spot = float(spot_eur_mwh[i])
            tarif_buy = self.retail.retail_buy_eur_kwh(spot)

            # SoC before
            soc_before = self.bat.soc_kwh

            # === STEP 1: Lookahead window pre arbitráž ===
            la_end = min(i + lookahead_steps, n)
            la_spots = spot_eur_mwh[i:la_end]
            la_mean = float(la_spots.mean()) if len(la_spots) else spot
            la_min = float(la_spots.min()) if len(la_spots) else spot
            la_max = float(la_spots.max()) if len(la_spots) else spot

            # Je toto lacná hodina v lookahead? (na grid charge)
            is_cheap_now = spot <= la_min + 5 and (la_max - spot) >= self.config.arb_min_spread_eur_mwh
            # Je toto drahá hodina? (na discharge arbitráž)
            is_expensive_now = spot >= la_max - 5 and (spot - la_min) >= self.config.arb_min_spread_eur_mwh

            # === STEP 2: Cycle budget check (limit max_efc_per_year) ===
            cycle_budget_left = max_efc - self.efc_used_this_year
            cycle_budget_left_pct = cycle_budget_left / max_efc if max_efc > 0 else 0
            allow_extra_cycles = cycle_budget_left_pct > 0.1  # zostáva > 10 % budgetu

            # === STEP 3: Dispatch logika ===
            # Priorita 1: PV → load priame (vždy)
            pv_to_load = min(pv, load)
            remaining_pv = pv - pv_to_load
            remaining_load = load - pv_to_load

            # Priorita 2a: Peak shaving (ak load_15min > MRK target)
            action = DispatchAction.NORMAL
            bat_to_load_peak = 0.0
            if (self.config.peak_shave_enabled
                    and remaining_load > mrk_target
                    and self.bat.soc_kwh > self.bat.soc_min_kwh * self.bat.soh):
                # Vyber dostatok BAT power na peak shave
                shave_needed = remaining_load - mrk_target
                discharge = self.bat.discharge(shave_needed, dt_h)
                bat_to_load_peak = discharge.actual_discharge_kwh
                remaining_load -= bat_to_load_peak
                action = DispatchAction.PEAK_SHAVE

            # Priorita 2b: PV → BAT charge (ak surplus PV a BAT má miesto)
            pv_to_bat = 0.0
            if remaining_pv > 0 and self.bat.soc_kwh < self.bat.soc_max_kwh * self.bat.soh:
                if allow_extra_cycles:  # respektuj cycle budget
                    charge_result = self.bat.charge(remaining_pv, dt_h)
                    pv_to_bat = charge_result.actual_charge_kwh
                    remaining_pv -= pv_to_bat

            # Priorita 3a: BAT arbitráž discharge (drahá hodina + ešte ostal load)
            bat_to_load_arb = 0.0
            if (action != DispatchAction.PEAK_SHAVE
                    and is_expensive_now
                    and remaining_load > 0
                    and self.bat.soc_kwh > self.bat.soc_min_kwh * self.bat.soh
                    and allow_extra_cycles):
                discharge_arb = self.bat.discharge(remaining_load, dt_h)
                bat_to_load_arb = discharge_arb.actual_discharge_kwh
                remaining_load -= bat_to_load_arb
                action = DispatchAction.DISCHARGE_BAT

            # Priorita 3b: BESS self-consumption (ak deficit a stale je SoC > min)
            bat_to_load_self = 0.0
            if (self.config.enable_bess_self_cons
                    and remaining_load > 0
                    and self.bat.soc_kwh > self.bat.soc_min_kwh * self.bat.soh
                    and not is_cheap_now  # nevybíjaj keď je lacné teraz
                    and allow_extra_cycles
                    and action == DispatchAction.NORMAL):
                discharge_self = self.bat.discharge(remaining_load, dt_h)
                bat_to_load_self = discharge_self.actual_discharge_kwh
                remaining_load -= bat_to_load_self

            # Priorita 4: Grid → BAT charge (lacná hodina, BAT má miesto, RK voľná)
            grid_to_bat = 0.0
            free_rk = max(0.0, self.site.rk_kw - remaining_load) * dt_h  # kWh voľné na grid charge
            if (is_cheap_now
                    and self.bat.soc_kwh < self.bat.soc_max_kwh * self.bat.soh
                    and free_rk > 0.1
                    and allow_extra_cycles):
                charge_grid = self.bat.charge(min(free_rk, self.bat.bess.power_kw_ac * dt_h), dt_h)
                grid_to_bat = charge_grid.actual_charge_kwh
                action = DispatchAction.CHARGE_GRID

            # Priorita 5: PV → grid export (zvyšok PV)
            pv_to_grid = max(0.0, remaining_pv * dt_h)
            # AOM-FIX-31: Curtail pri zápornom spote — nevyplatí sa exportovať pod 0 €/MWh
            # (klient platí distribučný poplatok aj pri zápornej cene). Default ON.
            if getattr(self.config, "negative_spot_curtail", True) and spot < 0:
                pv_to_grid = 0.0
            # Clip na MRK
            mrk_export_limit_kwh = self.site.mrk_kw * dt_h
            pv_to_grid_clipped = min(pv_to_grid, mrk_export_limit_kwh)
            mrk_overflow_kwh = max(0.0, pv_to_grid - pv_to_grid_clipped)

            # Konverzia kW na kWh per timestep (väčšina už je v kWh)
            pv_to_load_kwh = pv_to_load * dt_h
            bat_to_load_kwh = (bat_to_load_peak + bat_to_load_arb + bat_to_load_self)  # už kWh
            grid_to_load_kwh = remaining_load * dt_h
            pv_to_bat_kwh = pv_to_bat  # už kWh
            grid_to_bat_kwh = grid_to_bat  # už kWh

            # === Per-interval cost-basis oceňovanie (presný spread per kWh, v reálnych hodinách) ===
            # Dve vedrá energie: PV-zdrojová (lacná) + grid-zdrojová (nabitá @ retail v lacnej hodine).
            # Vybitie ocenené retailom AKTUÁLNEJ hodiny; grid-zdrojová mínus jej nákladová báza
            # → arbitráž = spot_vybitia − spot_nabitia (poplatky sa vyrušia), presne kde sa to využíva.
            if pv_to_bat_kwh > 0:
                _pv_bucket += pv_to_bat_kwh
            if grid_to_bat_kwh > 0:
                _grid_bucket += grid_to_bat_kwh
                _grid_cost += grid_to_bat_kwh * tarif_buy
            if bat_to_load_kwh > 0:
                _tot_b = _pv_bucket + _grid_bucket
                if _tot_b > 1e-9:
                    _pv_share = _pv_bucket / _tot_b
                    _e_pv = bat_to_load_kwh * _pv_share
                    _e_grid = bat_to_load_kwh - _e_pv
                    _gcpk = (_grid_cost / _grid_bucket) if _grid_bucket > 1e-9 else 0.0
                    _bess_self_acc += _e_pv * tarif_buy
                    _arbitrage_acc += _e_grid * tarif_buy - _e_grid * _gcpk
                    _pv_bucket = max(0.0, _pv_bucket - _e_pv)
                    _grid_bucket = max(0.0, _grid_bucket - _e_grid)
                    _grid_cost = max(0.0, _grid_cost - _e_grid * _gcpk)
                else:
                    _bess_self_acc += bat_to_load_kwh * tarif_buy

            # SAVING decomposition
            sav = self._compute_savings(
                pv_to_load_kwh=pv_to_load_kwh,
                pv_to_grid_kwh=pv_to_grid_clipped,
                bat_to_load_peak_kwh=bat_to_load_peak,
                bat_to_load_arb_kwh=bat_to_load_arb,
                bat_to_load_self_kwh=bat_to_load_self,
                grid_to_bat_kwh=grid_to_bat_kwh,
                mrk_overflow_kwh=mrk_overflow_kwh,
                tarif_buy=tarif_buy,
                spot_eur_mwh=spot,
            )

            # SoC po dispatchu (battery už updated cez .charge/.discharge calls)
            soc_after = self.bat.soc_kwh

            # EFC tracking
            efc_this = (pv_to_bat_kwh + grid_to_bat_kwh) / (2 * self.bat.usable_capacity_kwh) \
                if self.bat.usable_capacity_kwh > 0 else 0.0
            self.efc_used_this_year += efc_this

            # Grid net
            grid_import_kwh = grid_to_load_kwh + grid_to_bat_kwh
            grid_export_kwh = pv_to_grid_clipped

            # Interval record
            iv = DispatchInterval(
                timestamp=ts,
                dt_hours=dt_h,
                load_kw=load,
                pv_kw=pv,
                spot_eur_mwh=spot,
                tarif_buy_eur_kwh=tarif_buy,
                bat_soc_kwh_start=soc_before,
                bat_soc_kwh_end=soc_after,
                bat_soh=self.bat.soh,
                pv_to_load_kwh=pv_to_load_kwh,
                pv_to_bat_kwh=pv_to_bat_kwh,
                pv_to_grid_kwh=pv_to_grid_clipped,
                pv_clipped_kwh=0.0,  # inverter clipping je už v PV simulátore
                grid_to_load_kwh=grid_to_load_kwh,
                grid_to_bat_kwh=grid_to_bat_kwh,
                bat_to_load_kwh=bat_to_load_kwh,
                bat_to_grid_kwh=0.0,
                bat_losses_kwh=0.0,
                grid_import_kwh=grid_import_kwh,
                grid_export_kwh=grid_export_kwh,
                mrk_overflow_kw=mrk_overflow_kwh / dt_h if dt_h > 0 else 0,
                action=action,
                efc_this_step=efc_this,
                **sav,
            )
            intervals.append(iv)

            # Aktualizuj summary
            summary.load_total_kwh += load * dt_h
            summary.pv_total_kwh += pv * dt_h
            summary.pv_to_load_kwh += pv_to_load_kwh
            summary.pv_to_bat_kwh += pv_to_bat_kwh
            summary.pv_to_grid_kwh += pv_to_grid_clipped
            summary.bat_charge_total_kwh += pv_to_bat_kwh + grid_to_bat_kwh
            summary.bat_discharge_total_kwh += bat_to_load_kwh
            summary.grid_import_kwh += grid_import_kwh
            summary.grid_export_kwh += grid_export_kwh
            _arb_charge_spot += grid_to_bat_kwh * spot / 1000.0
            _disch_spot += bat_to_load_kwh * spot / 1000.0
            _disch_retail += bat_to_load_kwh * tarif_buy
            _grid_charge_kwh += grid_to_bat_kwh
            _mon = ts.month if hasattr(ts,'month') else 1
            _net_kw = load - (bat_to_load_peak / dt_h if dt_h > 0 else 0)
            _mon_max_load[_mon] = max(_mon_max_load.get(_mon, 0.0), load)
            _mon_max_net[_mon] = max(_mon_max_net.get(_mon, 0.0), _net_kw)

            for k, v in sav.items():
                if k.startswith("sav_"):
                    setattr(summary, k, getattr(summary, k) + v)

            if action == DispatchAction.NORMAL:
                summary.n_state_normal += 1
            elif action == DispatchAction.CHARGE_GRID:
                summary.n_state_charge_grid += 1
            elif action == DispatchAction.DISCHARGE_BAT:
                summary.n_state_discharge += 1
            elif action == DispatchAction.PEAK_SHAVE:
                summary.n_state_peak_shave += 1

        # === BS-arbitraz (spot) + PV samospotreba (retail) — korektny rozklad ===
        # Energia v baterii sa miesa (PV vs grid). PV-zdrojove vybitie usetri retail;
        # grid-zdrojove vybitie je BS-arbitraz ocenena spot spreadom (BS vyrovnava komoditu
        # za spot, ziadna distribucia). grid_frac = podiel grid nabijania na celkovom.
        # PRESNÝ rozklad (per-interval cost-basis) — nahrádza priemerujúcu grid_frac aproximáciu.
        summary.sav_bess_self_cons_eur = _bess_self_acc
        summary.sav_arbitrage_eur = _arbitrage_acc

        # === Peak shaving — REALNA redukcia mesacneho maxima x MRK kapacitny poplatok ===
        # MRK sa fakturuje z mesacneho 15-min maxima; baterka ho znizuje. Nie 200h pausal.
        _mrk_eur_kw_mes = float(self.tariff.mrk_kapacita_eur_mw_mes) / 1000.0
        _peak_sav = 0.0
        for _m in _mon_max_load:
            _red = max(0.0, _mon_max_load[_m] - _mon_max_net.get(_m, _mon_max_load[_m]))
            _peak_sav += _red * _mrk_eur_kw_mes
        summary.sav_peak_shaving_eur = _peak_sav

        # Finálne KPI
        summary.sav_total_eur = (
            summary.sav_solar_self_cons_eur + summary.sav_solar_export_eur
            + summary.sav_bess_self_cons_eur + summary.sav_arbitrage_eur
            + summary.sav_peak_shaving_eur + summary.sav_mrk_penalty_avoided_eur
        )
        summary.bat_efc = self.efc_used_this_year
        summary.bat_soh_end = self.bat.soh
        summary.n_replacements = self.bat.degradation.n_replacements

        if summary.pv_total_kwh > 0:
            summary.samospotreba_pct = (summary.pv_to_load_kwh + summary.pv_to_bat_kwh) / summary.pv_total_kwh * 100
        if summary.load_total_kwh > 0:
            summary.samostatnost_pct = (summary.pv_to_load_kwh + summary.bat_discharge_total_kwh) / summary.load_total_kwh * 100

        # CO2 (SK grid mix)
        summary.co2_avoided_t = (
            summary.pv_to_load_kwh + summary.bat_discharge_total_kwh + summary.pv_to_grid_kwh
        ) * self.co2_factor / 1000

        return intervals, summary

    # ------------------------------------------------------------------ Savings
    def _compute_savings(
        self,
        pv_to_load_kwh: float,
        pv_to_grid_kwh: float,
        bat_to_load_peak_kwh: float,
        bat_to_load_arb_kwh: float,
        bat_to_load_self_kwh: float,
        grid_to_bat_kwh: float,
        mrk_overflow_kwh: float,
        tarif_buy: float,
        spot_eur_mwh: float,
    ) -> dict:
        """Decomposicia úspor per value stream."""
        # 1) PV self-consumption (ušetríš plnú retail cenu)
        sav_solar = pv_to_load_kwh * tarif_buy

        # 2) PV export (dostaneš výkupnú cenu)
        sav_export = pv_to_grid_kwh * self.export_price

        # 3) BAT self-cons — vybitie energie (z PV zadarmo) ušetrí PLNÚ retail cenu.
        #    Nabíjací náklad z gridu sa účtuje v arbitráži (#4), tu žiadny paušál.
        bess_self_net = bat_to_load_self_kwh * tarif_buy

        # 4) Arbitráž round-trip: vybitie v drahej hodine ušetrí retail, nabitie z gridu
        #    v lacnej hodine STÁLO REÁLNU retail cenu importu (tarif_buy, vrát. distribúcie),
        #    nie surový spot. tarif_buy je per-step → zachytí spread, distribúciu aj RTE
        #    (vybiješ menej než nabiješ). Konzervatívne a správne.
        arb_net = bat_to_load_arb_kwh * tarif_buy - grid_to_bat_kwh * tarif_buy

        # 5) Peak shaving — znižuje 15-min monthly peak → znižuje fakturovanú MRK kapacitu
        # Real fakturácia: monthly_peak_kw × mrk_kapacita_eur_mw_mes / 1000
        # Approximation: bat_to_load_peak_kwh je len v hodinách kedy load > target.
        # Predpokladajme priemerný peak shave event trvá ~1 hour, takže
        # kWh/hour ≈ kW redukcia. Pre mesačnú fakturáciu:
        #   eur_per_kwh_peak ≈ mrk_kapacita_eur_mw_mes × 12 / 1000 / peak_hours_per_year
        #   peak_hours_per_year ≈ 200 (B2B prevádzka, prevažne pracovné dni 8-16h × ~80 dní peak)
        peak_hours_per_year = 200.0
        eur_per_kwh_peak = (self.tariff.mrk_kapacita_eur_mw_mes * 12 / 1000) / peak_hours_per_year
        sav_peak = bat_to_load_peak_kwh * eur_per_kwh_peak

        # 6) MRK export penalty avoidance (nová SK 2026)
        sav_mrk_avoid = mrk_overflow_kwh * self.tariff.mrk_export_penalty_eur_kwh

        return {
            "sav_solar_self_cons_eur": sav_solar,
            "sav_solar_export_eur": sav_export,
            "sav_bess_self_cons_eur": bess_self_net,
            "sav_arbitrage_eur": arb_net,
            "sav_peak_shaving_eur": sav_peak,
            "sav_mrk_penalty_avoided_eur": sav_mrk_avoid,
        }
