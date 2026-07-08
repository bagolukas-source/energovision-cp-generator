"""BatteryPack — kompletný stateful model BESS pre dispatch simuláciu.

Combinuje:
    - SoC tracking s RTE (per state, nie konštanta)
    - Power/SoC/cycle limits enforcement
    - Degradation model (Naumann-Schimpe)
    - Per-timestep audit log (pre debug a Intervals tab)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from energovision_analytics.battery.degradation import (
    BatteryDegradationModel,
    NaumannSchimpeParams,
)
from energovision_analytics.battery.efficiency import rte_curve, split_rte
from energovision_analytics.core.models import BESSInput


@dataclass
class BatteryState:
    """Stav batérie v konkrétnom timestepe."""
    soc_kwh: float
    soc_pct: float
    soh: float
    usable_kwh: float
    temp_c: float = 25.0
    total_efc: float = 0.0
    n_replacements: int = 0


@dataclass
class DispatchResult:
    """Výsledok dispatch akcie v jednom timestepe."""
    requested_charge_kwh: float = 0.0
    requested_discharge_kwh: float = 0.0
    actual_charge_kwh: float = 0.0       # po enforcement limits
    actual_discharge_kwh: float = 0.0
    energy_stored_kwh: float = 0.0       # = actual_charge × eta_ch (čistá energia do bunky)
    energy_drawn_kwh: float = 0.0        # = actual_discharge / eta_dis (čistá energia z bunky)
    soc_kwh_before: float = 0.0
    soc_kwh_after: float = 0.0
    rte_used: float = 0.88
    rejected_reason: Optional[str] = None
    degradation_delta: float = 0.0


class BatteryPack:
    """Stateful BESS s degradáciou — primárny objekt pre EMS dispatch."""

    def __init__(
        self,
        bess: BESSInput,
        initial_soc_pct: Optional[float] = None,
        degradation_params: Optional[NaumannSchimpeParams] = None,
        use_dynamic_rte: bool = True,
    ) -> None:
        self.bess = bess
        self.use_dynamic_rte = use_dynamic_rte

        # SoC initial (default = soc_min, čo simuluje "začneme nabíjať od zdrojla")
        init_pct = initial_soc_pct if initial_soc_pct is not None else bess.soc_min_pct
        self.soc_kwh = bess.nominal_kwh * init_pct

        # Limity kapacity
        self.soc_min_kwh = bess.nominal_kwh * bess.soc_min_pct
        self.soc_max_kwh = bess.nominal_kwh * bess.soc_max_pct
        self.usable_capacity_kwh = self.soc_max_kwh - self.soc_min_kwh

        # Degradation model
        params = degradation_params or NaumannSchimpeParams(
            initial_soh=bess.initial_soh,
            eol_soh=bess.warranty_eol_soh,
        )
        self.degradation = BatteryDegradationModel(
            params=params,
            nominal_kwh=bess.nominal_kwh,
            soh=bess.initial_soh,
        )

        # Throughput tracking
        self.lifetime_charge_kwh = 0.0
        self.lifetime_discharge_kwh = 0.0

    # ------------------------------------------------------------------ Properties
    @property
    def soh(self) -> float:
        return self.degradation.soh

    @property
    def effective_capacity_kwh(self) -> float:
        """Aktuálna použiteľná kapacita po degradácii."""
        return self.usable_capacity_kwh * self.soh

    @property
    def soc_pct(self) -> float:
        return self.soc_kwh / self.bess.nominal_kwh if self.bess.nominal_kwh > 0 else 0.0

    @property
    def state(self) -> BatteryState:
        return BatteryState(
            soc_kwh=self.soc_kwh,
            soc_pct=self.soc_pct,
            soh=self.soh,
            usable_kwh=self.effective_capacity_kwh,
            temp_c=self.bess.avg_ambient_temp_c,
            total_efc=self.degradation.total_efc,
            n_replacements=self.degradation.n_replacements,
        )

    # ------------------------------------------------------------------ Dispatch
    def can_charge_kwh(self, dt_hours: float, eta_charge: float | None = None) -> float:
        """Max AC kWh na nabitie v kroku (AC báza). R2 #2 FIX: AC výkonový limit (menič) vs
        DC headroom prepočítaný na AC-in cez eta_charge (predtým sa miešalo AC s DC)."""
        if eta_charge is None:
            eta_charge = self.bess.rte_ac_ac ** 0.5
        max_by_power = self.bess.power_kw_ac * dt_hours                                   # AC
        max_by_headroom_ac = (self.soc_max_kwh * self.soh - self.soc_kwh) / max(eta_charge, 1e-6)  # DC→AC-in
        return max(0.0, min(max_by_power, max_by_headroom_ac))

    def can_discharge_kwh(self, dt_hours: float, eta_discharge: float | None = None) -> float:
        """Max AC kWh na vybitie v kroku (AC báza). R2 #2 FIX: AC výkonový limit vs DC dostupné
        prepočítané na AC-out cez eta_discharge (predtým power-limit podhodnotený o eta)."""
        if eta_discharge is None:
            eta_discharge = self.bess.rte_ac_ac ** 0.5
        max_by_power = self.bess.power_kw_ac * dt_hours                                   # AC
        max_by_capacity_ac = (self.soc_kwh - self.soc_min_kwh * self.soh) * eta_discharge  # DC→AC-out
        return max(0.0, min(max_by_power, max_by_capacity_ac))

    def charge(self, requested_kwh: float, dt_hours: float, temp_c: Optional[float] = None) -> DispatchResult:
        """Nabite batériu (požadovaná energia z AC strany).

        Args:
            requested_kwh: kWh AC z grid/PV
            dt_hours: Trvanie (15-min = 0.25)
            temp_c: Aktuálna teplota (default = bess.avg_ambient_temp_c)
        """
        result = DispatchResult(requested_charge_kwh=requested_kwh, soc_kwh_before=self.soc_kwh)
        if requested_kwh <= 0:
            result.soc_kwh_after = self.soc_kwh
            return result

        t = temp_c if temp_c is not None else self.bess.avg_ambient_temp_c
        # audit: c_rate z POŽADOVANÉHO množstva skresľoval RTE krivku a nafukoval cyklovú
        # degradáciu (EMS bežne žiada viac, než PCS pustí) — počítaj z výkonovo orezaného
        c_rate = min(requested_kwh, self.bess.power_kw_ac * dt_hours) / dt_hours / self.bess.nominal_kwh

        # Dynamic RTE
        rte = (
            rte_curve(self.soc_pct, c_rate, t, self.bess.rte_ac_ac)
            if self.use_dynamic_rte else self.bess.rte_ac_ac
        )
        result.rte_used = rte
        eta_ch, _eta_dis = split_rte(rte)

        # Enforcement: capacity + power limit
        max_allowed = self.can_charge_kwh(dt_hours, eta_ch)   # R2 #2: AC báza (headroom/eta)
        actual_ac = min(requested_kwh, max_allowed)
        if actual_ac < requested_kwh:
            result.rejected_reason = (
                f"limit: requested {requested_kwh:.2f} > available {max_allowed:.2f} kWh"
            )

        energy_stored = actual_ac * eta_ch
        self.soc_kwh += energy_stored
        self.lifetime_charge_kwh += actual_ac

        # Degradation update
        deg = self.degradation.update(
            dt_hours=dt_hours,
            energy_throughput_kwh=actual_ac,
            avg_soc=self.soc_pct,
            temp_c=t,
            c_rate=c_rate,
            dod_this_cycle=0.8,
        )
        if deg["replaced"]:
            # Po výmene reset SoC na min
            self.soc_kwh = self.soc_min_kwh
        result.degradation_delta = deg["soh_delta"]

        result.actual_charge_kwh = actual_ac
        result.energy_stored_kwh = energy_stored
        result.soc_kwh_after = self.soc_kwh
        return result

    def discharge(self, requested_kwh: float, dt_hours: float, temp_c: Optional[float] = None) -> DispatchResult:
        """Vybite batériu (požadovaná energia AC do load/grid)."""
        result = DispatchResult(requested_discharge_kwh=requested_kwh, soc_kwh_before=self.soc_kwh)
        if requested_kwh <= 0:
            result.soc_kwh_after = self.soc_kwh
            return result

        t = temp_c if temp_c is not None else self.bess.avg_ambient_temp_c
        # audit: rovnaké orezanie c_rate ako pri charge (RTE + degradácia z reálneho výkonu)
        c_rate = min(requested_kwh, self.bess.power_kw_ac * dt_hours) / dt_hours / self.bess.nominal_kwh
        rte = (
            rte_curve(self.soc_pct, c_rate, t, self.bess.rte_ac_ac)
            if self.use_dynamic_rte else self.bess.rte_ac_ac
        )
        result.rte_used = rte
        _eta_ch, eta_dis = split_rte(rte)

        # R2 #2 FIX: can_discharge_kwh už vracia AC-out (DC×eta alebo AC power limit) — nenásobiť eta znova
        max_ac_out = self.can_discharge_kwh(dt_hours, eta_dis)
        actual_ac = min(requested_kwh, max_ac_out)
        if actual_ac < requested_kwh:
            result.rejected_reason = (
                f"limit: requested {requested_kwh:.2f} > available {max_ac_out:.2f} kWh AC"
            )

        energy_from_cell = actual_ac / eta_dis  # koľko sa vybralo z bunky
        self.soc_kwh -= energy_from_cell
        self.lifetime_discharge_kwh += actual_ac

        deg = self.degradation.update(
            dt_hours=dt_hours,
            energy_throughput_kwh=actual_ac,
            avg_soc=self.soc_pct,
            temp_c=t,
            c_rate=c_rate,
            dod_this_cycle=0.8,
        )
        if deg["replaced"]:
            self.soc_kwh = self.soc_min_kwh
        result.degradation_delta = deg["soh_delta"]

        result.actual_discharge_kwh = actual_ac
        result.energy_drawn_kwh = energy_from_cell
        result.soc_kwh_after = self.soc_kwh
        return result

    def hold(self, dt_hours: float, temp_c: Optional[float] = None) -> DispatchResult:
        """Idle — žiadny dispatch, len calendar aging."""
        t = temp_c if temp_c is not None else self.bess.avg_ambient_temp_c
        result = DispatchResult(soc_kwh_before=self.soc_kwh, soc_kwh_after=self.soc_kwh)
        deg = self.degradation.update(
            dt_hours=dt_hours,
            energy_throughput_kwh=0.0,
            avg_soc=self.soc_pct,
            temp_c=t,
        )
        result.degradation_delta = deg["soh_delta"]
        return result
