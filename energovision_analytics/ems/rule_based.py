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
        export_mode: str = "fix",
        export_spot_diskont_eur_mwh: float = 10.0,
    ) -> None:
        self.bat = battery
        self.site = site
        self.tariff = tariff
        self.retail = retail_calc
        self.config = config or EMSConfig()
        self.export_price = export_price_eur_kwh
        self.co2_factor = sk_grid_co2_kg_per_kwh
        # Výkup prebytkov (oprava 17.8.2026): doteraz sa každá exportovaná kWh oceňovala
        # rovnakou pevnou cenou — aj v poludňajšej špičke, aj v hodine so záporným spotom.
        # Model "spot_minus" oceňuje export hodinovo: max(0, spot − diskont).
        self.export_mode = export_mode
        self.export_spot_diskont = float(export_spot_diskont_eur_mwh or 0.0)

    def export_price_for(self, spot_eur_mwh: float) -> float:
        """Výkupná cena za 1 kWh pri danom hodinovom spote (€/kWh)."""
        if self.export_mode == "spot_minus":
            return max(0.0, float(spot_eur_mwh) - self.export_spot_diskont) / 1000.0
        return self.export_price

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
        # BOD 10 FIX: peak shaving znižuje IMPORTNÚ špičku → prah z RK (import),
        # nie z MRK (export). Export penalty/clip ostáva na mrk_kw.
        peak_cap_kw = self.site.rk_kw or self.site.mrk_kw
        mrk_target = peak_cap_kw * self.config.peak_shave_target_pct

        intervals: list[DispatchInterval] = []
        summary = DispatchSummary(rok=timestamps[0].year, n_intervals=n)

        # Precompute lookahead helpers — pre každý timestep top/bottom kvantily spotu
        lookahead_steps = int(self.config.lookahead_hours * 60 / timestep_min)
        # Signály lacná/drahá hodina z RETAIL ceny, ktorú klient reálne platí (nie zo spotu):
        # pri FIX tarife je retail konštantný → žiadne nabíjanie zo siete na arbitráž ani blokovanie
        # vybíjania. Predtým FIX batéria „arbitrážovala" podľa spotu a strácala na RTE
        # (audit 2026-10-24 E01/T1: FIX 300 kWp/500 kWh −595 €/r namiesto +5 719 €/r).
        _px = np.array([self.retail.retail_buy_eur_kwh(float(_s)) * 1000.0 for _s in spot_eur_mwh])

        # LP plán (E02): rozhodnutia o nabíjaní/vybíjaní z rolling-horizon LP nad retail cenou OM;
        # pravidlá nižšie ostávajú ako záloha (dispatch="rules" alebo zlyhanie LP v okne).
        _use_lp = getattr(self.config, "dispatch", "lp") == "lp" and self.bat.bess.nominal_kwh > 0
        _lp_win = max(1, int(getattr(self.config, "lp_window_h", 36) / dt_h))
        _lp_com = max(1, int(getattr(self.config, "lp_commit_h", 24) / dt_h))
        _exp_kwh = np.array([self.export_price_for(float(_s)) for _s in spot_eur_mwh])
        _plan = None          # (pv_to_bat, grid_to_bat, bat_to_load) v kWh/krok od indexu _plan_i0
        _plan_i0 = 0

        # BS-arbitraz akumulatory (spot-based, korektny model)
        _arb_charge_spot = 0.0   # nabijaci naklad z gridu @ spot
        _disch_spot = 0.0        # vybitie @ spot (pre grid-arbitraz)
        _disch_retail = 0.0      # vybitie @ retail (pre PV samospotrebu)
        _grid_charge_kwh = 0.0
        # Per-interval cost-basis oceňovanie batérie (PRESNÝ arbitrážny spread — opravuje grid_frac priemer)
        # AUDIT N7: počiatočný SoC NIE JE energia zadarmo — ide do grid-vedra s nákladovou bázou
        # retailu prvej hodiny (predtým _pv_bucket=SoC kreditoval štartovnú energiu plným retailom).
        _init_soc_kwh = max(0.0, float(self.bat.soc_kwh) - float(self.bat.soc_min_kwh))  # len využiteľná časť nad minimom
        _pv_bucket = 0.0
        _grid_bucket = 0.0
        _grid_cost = 0.0
        _bess_self_acc = 0.0
        _arbitrage_acc = 0.0
        _grid_charge_cost_total = 0.0   # celkový náklad grid nabíjania (pre LCOS)
        _pv_via_bat_acc = 0.0   # kWh PV reálne dodané z batérie do loadu (po RTE)
        _monthly = {m: {"pv": 0.0, "pv_to_load": 0.0, "export": 0.0, "import": 0.0, "load": 0.0} for m in range(1, 13)}
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
            px = float(_px[i])
            la_spots = _px[i:la_end]
            la_mean = float(la_spots.mean()) if len(la_spots) else px
            la_min = float(la_spots.min()) if len(la_spots) else px
            la_max = float(la_spots.max()) if len(la_spots) else px

            # Obchodné pásmo okna: band=0 → staré správanie (extrém ±5 €); band>0 → spodná/vrchná
            # časť rozpätia cien okna. Spread podmienka (min ekonomika po RTE) platí vždy.
            _band = max(0.0, float(getattr(self.config, "arb_band_pct", 0.0))) * (la_max - la_min)
            _cheap_lim = la_min + max(5.0, _band)
            _exp_lim = la_max - max(5.0, _band)
            # Je toto lacná hodina v lookahead? (na grid charge)
            is_cheap_now = px <= _cheap_lim and (la_max - px) >= self.config.arb_min_spread_eur_mwh
            # Je toto drahá hodina? (na discharge arbitráž)
            is_expensive_now = px >= _exp_lim and (px - la_min) >= self.config.arb_min_spread_eur_mwh

            # === STEP 2: Cycle budget check (limit max_efc_per_year) ===
            cycle_budget_left = max(0.0, max_efc - self.efc_used_this_year)
            cycle_budget_left_pct = cycle_budget_left / max_efc if max_efc > 0 else 0
            allow_extra_cycles = cycle_budget_left_pct > 0.1  # grid arbitráž len kým zostáva > 10 % budgetu
            # A17/E10 (audit 2026-10-24): TVRDÝ limit — koľko AC-in kWh ešte zmestí do zvyšku budgetu
            # (rovnaká báza ako efc_this nižšie: AC-in × eta_ch / usable). Predtým podmienka len povolila
            # akciu pred krokom a krok prestrelil (budget 0,1 EFC → 0,521 EFC).
            _eta_ch_nom = self.bat.bess.rte_ac_ac ** 0.5
            budget_ac_kwh = (cycle_budget_left * self.bat.usable_capacity_kwh / _eta_ch_nom
                             if _eta_ch_nom > 0 else 0.0)

            # === STEP 3: Dispatch logika ===
            # AUDIT N1: od P1 nižšie sa počíta VÝHRADNE v kWh za krok — pôvodný kód miešal
            # kW a kWh (remaining_load -= bat_kwh; charge(kW ako kWh)), čo bolo korektné len
            # pri dt=60 min; pri 15-min kroku vznikala/zanikala fantómová energia.
            # Priorita 1: PV → load priame (vždy)
            pv_to_load = min(pv, load)                      # kW
            remaining_pv_kwh = (pv - pv_to_load) * dt_h
            remaining_load_kwh = (load - pv_to_load) * dt_h

            # LP plán: prepočítaj na začiatku každého commit bloku zo SKUTOČNÉHO SoC
            _lp_step = None
            if _use_lp:
                if _plan is None or i - _plan_i0 >= _lp_com:
                    from energovision_analytics.ems.lp_plan import lp_okno
                    _e = min(n, i + _lp_win)
                    _eta = float(self.bat.bess.rte_ac_ac) ** 0.5
                    _soc0 = float(self.bat.soc_kwh)
                    _smin = min(float(self.bat.soc_min_kwh * self.bat.soh), _soc0)
                    _smax = max(float(self.bat.soc_max_kwh * self.bat.soh), _soc0)
                    # rozpočet cyklov: zvyšok ročného limitu, v okne najviac max_cycles_per_day denne
                    _bud = min(max(0.0, max_efc - self.efc_used_this_year),
                               float(self.config.max_cycles_per_day) * (_e - i) * dt_h / 24.0
                               ) * self.bat.usable_capacity_kwh
                    _plan = lp_okno(
                        np.asarray(load_kw[i:_e], float) * dt_h, np.asarray(pv_kw[i:_e], float) * dt_h,
                        _px[i:_e] / 1000.0, _exp_kwh[i:_e], np.asarray(spot_eur_mwh[i:_e], float),
                        soc0=_soc0, smin=_smin, smax=_smax,
                        p_kwh=float(self.bat.bess.power_kw_ac) * dt_h, eta=_eta,
                        rk_kwh=float(self.site.rk_kw or self.site.mrk_kw) * dt_h,
                        mrk_kwh=float(self.site.mrk_kw) * dt_h,
                        budget_dc_kwh=_bud,
                        grid_charge_eur_kwh=float(self.config.arb_min_spread_eur_mwh) / 1000.0,
                        curtail_neg=bool(getattr(self.config, "negative_spot_curtail", True)),
                    )
                    _plan_i0 = i
                if _plan is not None:
                    _k = i - _plan_i0
                    _lp_step = (float(_plan[0][_k]), float(_plan[1][_k]), float(_plan[2][_k]))

            if _lp_step is not None:
                # vykonaj plán cez fyzický model batérie; účtovanie nižšie je spoločné s pravidlami
                _pb, _ib, _d = _lp_step
                action = DispatchAction.NORMAL
                bat_to_load_peak = bat_to_load_arb = bat_to_load_self = 0.0
                if _d > 1e-9 and remaining_load_kwh > 0 and self.bat.soc_kwh > self.bat.soc_min_kwh * self.bat.soh:
                    _over = (max(0.0, remaining_load_kwh / dt_h - mrk_target) * dt_h
                             if self.config.peak_shave_enabled else 0.0)
                    _dk = self.bat.discharge(min(_d, remaining_load_kwh), dt_h,
                                             advance_calendar=False).actual_discharge_kwh
                    bat_to_load_peak = min(_dk, _over)
                    bat_to_load_self = _dk - bat_to_load_peak
                    remaining_load_kwh -= _dk
                    action = DispatchAction.PEAK_SHAVE if bat_to_load_peak > 0 else DispatchAction.DISCHARGE_BAT
                # LP môže dať „PV→bat + sieť→záťaž" namiesto „PV→záťaž + sieť→bat" — na meraní OM je
                # to to isté. Nabíja sa preto CELKOVÉ plánované množstvo: najprv zvyšok PV, potom sieť.
                pv_to_bat = 0.0
                _ch_plan = _pb + _ib
                if _ch_plan > 1e-9 and remaining_pv_kwh > 0 and budget_ac_kwh > 1e-9:
                    pv_to_bat = self.bat.charge(min(_ch_plan, remaining_pv_kwh, budget_ac_kwh), dt_h,
                                                advance_calendar=False).actual_charge_kwh
                    remaining_pv_kwh -= pv_to_bat
                    budget_ac_kwh = max(0.0, budget_ac_kwh - pv_to_bat)
                _ib = max(0.0, _ch_plan - pv_to_bat)
                grid_to_bat = 0.0
                if _ib > 1e-9 and budget_ac_kwh > 1e-9:
                    _free_rk = max(0.0, self.site.rk_kw - remaining_load_kwh / dt_h) * dt_h
                    _pcs_left = max(0.0, self.bat.bess.power_kw_ac * dt_h - pv_to_bat)
                    _q = min(_ib, _free_rk, _pcs_left, budget_ac_kwh)
                    if _q > 1e-9:
                        grid_to_bat = self.bat.charge(_q, dt_h, advance_calendar=False).actual_charge_kwh
                        if grid_to_bat > 0 and action == DispatchAction.NORMAL:
                            action = DispatchAction.CHARGE_GRID

            # Priorita 2a: Peak shaving (prah mrk_target je v kW → porovnávaj výkon)
            if _lp_step is None:
                action = DispatchAction.NORMAL
                bat_to_load_peak = 0.0
            if (_lp_step is None and self.config.peak_shave_enabled
                    and remaining_load_kwh / dt_h > mrk_target
                    and self.bat.soc_kwh > self.bat.soc_min_kwh * self.bat.soh):
                # Vyber dostatok BAT power na peak shave
                shave_needed_kwh = (remaining_load_kwh / dt_h - mrk_target) * dt_h
                discharge = self.bat.discharge(shave_needed_kwh, dt_h, advance_calendar=False)
                bat_to_load_peak = discharge.actual_discharge_kwh
                remaining_load_kwh -= bat_to_load_peak
                action = DispatchAction.PEAK_SHAVE

            # Priorita 2b: PV → BAT charge (ak surplus PV a BAT má miesto)
            if _lp_step is None:
                pv_to_bat = 0.0
            if _lp_step is None and remaining_pv_kwh > 0 and self.bat.soc_kwh < self.bat.soc_max_kwh * self.bat.soh:
                # cycle budget čerpá len NABÍJANIE (AUDIT N4); A17: PV prebytok sa ukladá až do
                # vyčerpania budgetu (predtým stop už pri 90 %), orezaný na jeho zvyšok
                if budget_ac_kwh > 1e-9:
                    charge_result = self.bat.charge(min(remaining_pv_kwh, budget_ac_kwh), dt_h,
                                                    advance_calendar=False)
                    pv_to_bat = charge_result.actual_charge_kwh
                    remaining_pv_kwh -= pv_to_bat
                    budget_ac_kwh = max(0.0, budget_ac_kwh - pv_to_bat)

            # Priorita 3a: BAT arbitráž discharge (drahá hodina + ešte ostal load)
            # AUDIT N4: vybíjanie NEgatovať cycle budgetom — EFC sa čerpá len nabíjaním;
            # blokovanie discharge nešetrilo cykly, len väznilo už nabitú (zaplatenú) energiu.
            if _lp_step is None:
                bat_to_load_arb = 0.0
            if (_lp_step is None and action != DispatchAction.PEAK_SHAVE
                    and is_expensive_now
                    and remaining_load_kwh > 0
                    and self.bat.soc_kwh > self.bat.soc_min_kwh * self.bat.soh):
                discharge_arb = self.bat.discharge(remaining_load_kwh, dt_h, advance_calendar=False)
                bat_to_load_arb = discharge_arb.actual_discharge_kwh
                remaining_load_kwh -= bat_to_load_arb
                action = DispatchAction.DISCHARGE_BAT

            # Priorita 3b: BESS self-consumption (ak deficit a stale je SoC > min)
            if _lp_step is None:
                bat_to_load_self = 0.0
            if (_lp_step is None and self.config.enable_bess_self_cons
                    and remaining_load_kwh > 0
                    and self.bat.soc_kwh > self.bat.soc_min_kwh * self.bat.soh
                    and not is_cheap_now  # nevybíjaj keď je lacné teraz
                    and action == DispatchAction.NORMAL):
                discharge_self = self.bat.discharge(remaining_load_kwh, dt_h, advance_calendar=False)
                bat_to_load_self = discharge_self.actual_discharge_kwh
                remaining_load_kwh -= bat_to_load_self

            # Priorita 4: Grid → BAT charge (lacná hodina, BAT má miesto, RK voľná)
            if _lp_step is None:
                grid_to_bat = 0.0
            free_rk = max(0.0, self.site.rk_kw - remaining_load_kwh / dt_h) * dt_h  # kWh voľné na grid charge
            if (_lp_step is None and is_cheap_now
                    and self.bat.soc_kwh < self.bat.soc_max_kwh * self.bat.soh
                    and free_rk > 0.1
                    and allow_extra_cycles):
                # audit: PV→BAT (P2b) a grid→BAT bežali ako dve nezávislé volania — PCS vedel
                # v jednej hodine nabíjať 2× menovitým výkonom; grid charge dostane len zvyšok výkonu
                _pcs_left_kwh = max(0.0, self.bat.bess.power_kw_ac * dt_h - pv_to_bat)
                charge_grid = self.bat.charge(min(free_rk, _pcs_left_kwh, budget_ac_kwh), dt_h,
                                              advance_calendar=False)
                grid_to_bat = charge_grid.actual_charge_kwh
                action = DispatchAction.CHARGE_GRID

            # Priorita 5: PV → grid export (zvyšok PV)
            pv_to_grid = max(0.0, remaining_pv_kwh)
            # AOM-FIX-31: Curtail pri zápornom spote — nevyplatí sa exportovať pod 0 €/MWh
            # (klient platí distribučný poplatok aj pri zápornej cene). Default ON.
            # AUDIT N2: curtailnutá energia sa eviduje (pv_curtailed_kwh), nemizne z bilancie.
            pv_curtailed_kwh = 0.0
            if getattr(self.config, "negative_spot_curtail", True) and spot < 0:
                pv_curtailed_kwh = pv_to_grid
                pv_to_grid = 0.0
            # Clip na MRK
            mrk_export_limit_kwh = self.site.mrk_kw * dt_h
            pv_to_grid_clipped = min(pv_to_grid, mrk_export_limit_kwh)
            mrk_overflow_kwh = max(0.0, pv_to_grid - pv_to_grid_clipped)

            # Toky v kWh za timestep
            pv_to_load_kwh = pv_to_load * dt_h
            bat_to_load_kwh = (bat_to_load_peak + bat_to_load_arb + bat_to_load_self)  # už kWh
            grid_to_load_kwh = remaining_load_kwh
            pv_to_bat_kwh = pv_to_bat  # už kWh
            grid_to_bat_kwh = grid_to_bat  # už kWh

            # === Per-interval cost-basis oceňovanie (presný spread per kWh, v reálnych hodinách) ===
            # Dve vedrá energie: PV-zdrojová (lacná) + grid-zdrojová (nabitá @ retail v lacnej hodine).
            # Vybitie ocenené retailom AKTUÁLNEJ hodiny; grid-zdrojová mínus jej nákladová báza
            # → arbitráž = spot_vybitia − spot_nabitia (poplatky sa vyrušia), presne kde sa to využíva.
            if i == 0 and _init_soc_kwh > 0:
                _grid_bucket += _init_soc_kwh
                _grid_cost += _init_soc_kwh * tarif_buy
            if pv_to_bat_kwh > 0:
                _pv_bucket += pv_to_bat_kwh
            if grid_to_bat_kwh > 0:
                _grid_bucket += grid_to_bat_kwh
                _grid_cost += grid_to_bat_kwh * tarif_buy
                _grid_charge_cost_total += grid_to_bat_kwh * tarif_buy
            _self_acc_before = _bess_self_acc
            _arb_acc_before = _arbitrage_acc
            if bat_to_load_kwh > 0:
                _tot_b = _pv_bucket + _grid_bucket
                if _tot_b > 1e-9:
                    # BOD 8 FIX: vedrá sú v AC-IN báze (nabíjanie). Na dodanie bat_to_load (AC-OUT)
                    # sa z nich spotrebuje bat_to_load/rte AC-IN — tým sa RTE strata na nabíjaní
                    # reálne oceníní (predtým zostala "visieť" vo vedre → arbitráž nadhodnotená).
                    _rte = float(getattr(self.bat.bess, "rte_ac_ac", 0.88)) or 0.88
                    _need_in = bat_to_load_kwh / _rte
                    _pv_share = _pv_bucket / _tot_b
                    _in_pv = _need_in * _pv_share
                    _in_grid = _need_in - _in_pv
                    _out_pv = bat_to_load_kwh * _pv_share
                    _out_grid = bat_to_load_kwh - _out_pv
                    _gcpk = (_grid_cost / _grid_bucket) if _grid_bucket > 1e-9 else 0.0
                    # A15/E08: energia ≥ 0, ale nákladový register nesie ZNAMIENKO — nabitie pri
                    # zápornej retail cene je príjem; max(0,…) ho zahadzoval (7,4992 vs 9,0393 €).
                    # Z registra sa odpisuje len energia, ktorá v ňom je (prečerpanie nad vedro, napr.
                    # pokles podlahy soc_min×SoH, nemá nákladovú bázu) → Σ nabitie = Σ odpis + zostatok.
                    _in_grid_cost = min(_in_grid, _grid_bucket)
                    _bess_self_acc += _out_pv * tarif_buy
                    _arbitrage_acc += _out_grid * tarif_buy - _in_grid_cost * _gcpk
                    _pv_via_bat_acc += _out_pv
                    _pv_bucket = max(0.0, _pv_bucket - _in_pv)
                    _grid_bucket = max(0.0, _grid_bucket - _in_grid)
                    _grid_cost = (_grid_cost - _in_grid_cost * _gcpk) if _grid_bucket > 1e-9 else 0.0
                else:
                    _bess_self_acc += bat_to_load_kwh * tarif_buy

            # SAVING decomposition (solar/export/mrk zložky per-interval)
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
            # AUDIT N3: bess_self/arbitráž per-interval = delty cost-basis akumulátorov,
            # aby Σ intervalov sedela so summary (starý per-interval vzorec dával iné čísla
            # než summary — grafy z intervalov klamali). Náklad grid nabíjania sa účtuje
            # pri VYBITÍ (cost basis), nie v hodine nabitia.
            sav["sav_bess_self_cons_eur"] = _bess_self_acc - _self_acc_before
            sav["sav_arbitrage_eur"] = _arbitrage_acc - _arb_acc_before

            # A16/E09: kalendárne starnutie presne raz za krok (aj pri nečinnosti); charge/discharge
            # vyššie počítajú len cyklové (predtým: nečinnosť nestarla, 2 akcie v kroku = 2× kalendár)
            self.bat.hold(dt_h)

            # SoC po dispatchu (battery už updated cez .charge/.discharge calls)
            soc_after = self.bat.soc_kwh

            # EFC tracking
            # BOD 9 FIX: 1 plné nabitie usable = 1 EFC. R2 #3 FIX: pv_to_bat/grid_to_bat sú AC-in,
            # usable_capacity je DC → AC-in × eta_charge = DC uložené (konzistentná DC báza, nemiešať).
            _eta_ch = self.bat.bess.rte_ac_ac ** 0.5
            efc_this = (pv_to_bat_kwh + grid_to_bat_kwh) * _eta_ch / self.bat.usable_capacity_kwh \
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
                pv_clipped_kwh=pv_curtailed_kwh + mrk_overflow_kwh,  # curtail @ záporný spot + MRK clip (AUDIT N2)
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
            summary.pv_curtailed_kwh = getattr(summary, "pv_curtailed_kwh", 0.0) + pv_curtailed_kwh + mrk_overflow_kwh
            summary.bat_charge_total_kwh += pv_to_bat_kwh + grid_to_bat_kwh
            summary.bat_discharge_total_kwh += bat_to_load_kwh
            summary.grid_import_kwh += grid_import_kwh
            summary.grid_export_kwh += grid_export_kwh
            _arb_charge_spot += grid_to_bat_kwh * spot / 1000.0
            _disch_spot += bat_to_load_kwh * spot / 1000.0
            _disch_retail += bat_to_load_kwh * tarif_buy
            _grid_charge_kwh += grid_to_bat_kwh
            _mon = ts.month if hasattr(ts,'month') else 1
            _mm = _monthly[_mon]
            _mm["pv"] += pv * dt_h; _mm["pv_to_load"] += pv_to_load_kwh
            _mm["export"] += pv_to_grid_clipped; _mm["import"] += grid_import_kwh; _mm["load"] += load * dt_h
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
        summary.monthly_flows = _monthly   # reálne mesačné toky (MWh po /1000) pre posudok
        summary.sav_bess_self_cons_eur = _bess_self_acc
        summary.sav_arbitrage_eur = _arbitrage_acc
        # AUDIT N4(econ): zostatková energia vo vedre má zaplatený náklad, ale je to aktívum
        # (vybije sa ďalší rok) — neodpisujeme, len reportujeme pre diagnostiku/identitu účtov.
        summary.grid_bucket_leftover_cost_eur = _grid_cost   # A15: so znamienkom (záporné = nabité so ziskom)

        # === Peak shaving — REALNA redukcia mesacneho maxima x MRK kapacitny poplatok ===
        # MRK sa fakturuje z mesacneho 15-min maxima; baterka ho znizuje. Nie 200h pausal.
        _mrk_eur_kw_mes = float(self.tariff.mrk_kapacita_eur_mw_mes) / 1000.0
        _peak_sav = 0.0
        # audit V: kapacitný poplatok sa platí zo ZAZMLUVNENEJ MRK — zníženie maxima, ktoré aj tak
        # ostáva pod MRK, faktúru nezníži. Kredituj len redukciu NAD zazmluvnenú MRK (prekročenia),
        # zvyšok prínosu vyžaduje zníženie zazmluvnenej MRK (obchodné rozhodnutie, nie automatická úspora).
        _mrk_contract = float(getattr(self.site, "rk_kw", 0) or 0)  # rk_kw = zazmluvnený IMPORT (mrk_kw je v modeli export limit)
        for _m in _mon_max_load:
            _ml = _mon_max_load[_m]
            _mn = _mon_max_net.get(_m, _ml)
            if _mrk_contract > 0:
                _red = max(0.0, _ml - max(_mn, _mrk_contract))
            else:
                _red = max(0.0, _ml - _mn)
            _peak_sav += _red * _mrk_eur_kw_mes
        summary.sav_peak_shaving_eur = _peak_sav

        # Finálne KPI
        summary.sav_total_eur = (
            summary.sav_solar_self_cons_eur + summary.sav_solar_export_eur
            + summary.sav_bess_self_cons_eur + summary.sav_arbitrage_eur
            + summary.sav_peak_shaving_eur + summary.sav_mrk_penalty_avoided_eur
        )
        summary.grid_charge_cost_eur = _grid_charge_cost_total
        summary.bat_efc = self.efc_used_this_year
        summary.bat_soh_end = self.bat.soh
        summary.n_replacements = self.bat.degradation.n_replacements

        summary.pv_to_load_via_bat_kwh = _pv_via_bat_acc
        if summary.pv_total_kwh > 0:
            # samospotreba = PV reálne spotrebovaná (priamo + cez batériu po RTE), NIE AC vstup do batérie
            summary.samospotreba_pct = (summary.pv_to_load_kwh + _pv_via_bat_acc) / summary.pv_total_kwh * 100
        if summary.load_total_kwh > 0:
            # nezávislosť = krytie spotreby z VLASTNEJ FVE (priamo + PV cez batériu), bez grid-nabitej arbitráže
            summary.samostatnost_pct = (summary.pv_to_load_kwh + _pv_via_bat_acc) / summary.load_total_kwh * 100

        # CO2 (SK grid mix)
        # AUDIT N9: CO2 len z PV energie (priama + PV cez batériu po RTE) — grid→bat→load emisie nešetrí
        summary.co2_avoided_t = (
            summary.pv_to_load_kwh + _pv_via_bat_acc + summary.pv_to_grid_kwh
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

        # 2) PV export (dostaneš výkupnú cenu — pri modeli spot_minus podľa hodinového spotu)
        sav_export = pv_to_grid_kwh * self.export_price_for(spot_eur_mwh)

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

        # 6) MRK export penalty avoidance — audit V: clipnutá energia sa NIKDY neexportovala,
        # takže žiadna pokuta nehrozila a "vyhnutá pokuta" je fantómový kredit; energia je len
        # stratená (eviduje sa v mrk_overflow/curtailed bilancii). Žiadna úspora.
        sav_mrk_avoid = 0.0

        return {
            "sav_solar_self_cons_eur": sav_solar,
            "sav_solar_export_eur": sav_export,
            "sav_bess_self_cons_eur": bess_self_net,
            "sav_arbitrage_eur": arb_net,
            "sav_peak_shaving_eur": sav_peak,
            "sav_mrk_penalty_avoided_eur": sav_mrk_avoid,
        }
