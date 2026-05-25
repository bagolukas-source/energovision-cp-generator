"""Degradačný model batérie — Naumann-Schimpe semi-empirický.

Zdroj:
    Naumann et al. (2018) — Analysis and modeling of calendar aging of LFP/graphite
    Schimpe et al. (2018) — Comprehensive Modeling of Temperature-Dependent Degradation
    NREL ESHB 2025 — battery degradation reference

Model:
    SoH(t) = 1 - cal_fade(t, T, SoC_avg) - cyc_fade(N_EFC, DoD, C, T)

    cal_fade(t)  = k_cal × √(t/year) × Arrhenius(T) × SoC_stress(SoC_avg)
    cyc_fade(N) = k_cyc × N × DoD_stress(DoD) × C_stress(C) × Arrhenius_cyc(T)

Default parametre kalibrované pre LFP/graphite pri 25 °C, 50 % avg SoC, 80 % DoD, 0.5 C.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, sqrt


@dataclass
class NaumannSchimpeParams:
    """Parametre Naumann-Schimpe modelu pre LFP/graphite.

    KALIBRÁCIA 2026-05-24 (Sprint 8):
    Pôvodné hodnoty (k_cal=0.025, k_cyc=0.0003) vyrábali ~8.6 %/r SoH drop —
    nesúhlasilo s real-world datasheetmi Huawei LUNA2000, Solinteg E2BR, BYD HVS.
    Recalibrované na ~1.5-2.0 %/rok celkový SoH drop (cal+cyc) pri 25 °C,
    50 % avg SoC, 350 EFC/rok, 80 % DoD, 0.5 C — čo zodpovedá 70 % SoH po
    10 000 cykloch (typická LFP warranty).
    """

    # === Calendar aging ===
    # 0.007 → 0.7 %/rok pri T=25°C, SoC=0.5 (1 rok), 1.0 %/rok @ 35°C
    k_cal: float = 0.007  # year^-0.5 pri T_ref=25°C, SoC=0.5
    Ea_cal: float = 30_000  # aktivačná energia (J/mol)
    soc_stress_factor: float = 1.0

    # === Cycle aging ===
    # 0.000025 × 350 EFC = 0.875 %/rok pri sweet-spot
    k_cyc: float = 0.000025  # per EFC pri DoD=0.8, C=0.5, T=25°C
    Ea_cyc: float = 35_000
    dod_exponent: float = 1.5

    # Constants
    R: float = 8.314
    T_ref_K: float = 298.15

    # Operational limits
    eol_soh: float = 0.80
    initial_soh: float = 1.0

    # === Vendor presets (pre BESS pack_model selection) ===
    @classmethod
    def huawei_luna2000(cls) -> "NaumannSchimpeParams":
        """Huawei LUNA2000 — deklarované 60% SoH po 6 000 cykloch (8000 → 70%)."""
        return cls(k_cal=0.008, k_cyc=0.000030)

    @classmethod
    def solinteg_e2br(cls) -> "NaumannSchimpeParams":
        """Solinteg E2BR — LFP, podobné Huawei, mierne konzervatívnejšie."""
        return cls(k_cal=0.009, k_cyc=0.000032)

    @classmethod
    def byd_hvs(cls) -> "NaumannSchimpeParams":
        """BYD Battery-Box HVS — premium LFP, 70% SoH po 10 000 cykloch."""
        return cls(k_cal=0.006, k_cyc=0.000022)

    @classmethod
    def conservative(cls) -> "NaumannSchimpeParams":
        """Konzervatívny scenár pre worst-case underwriting (2.5 %/r)."""
        return cls(k_cal=0.012, k_cyc=0.000040)


@dataclass
class BatteryDegradationModel:
    """Stateful degradation model — accumuluje SoH cez čas.

    Volaj `update()` per timestep s aktuálnymi prevádzkovými podmienkami.
    """

    params: NaumannSchimpeParams = field(default_factory=NaumannSchimpeParams)
    nominal_kwh: float = 100.0
    soh: float = 1.0
    total_efc: float = 0.0          # Equivalent Full Cycles
    total_calendar_days: float = 0.0
    n_replacements: int = 0
    replacement_history: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.soh == 1.0 and self.params.initial_soh != 1.0:
            self.soh = self.params.initial_soh

    # ------------------------------------------------------------------ Update
    def update(
        self,
        dt_hours: float,
        energy_throughput_kwh: float,
        avg_soc: float = 0.5,
        temp_c: float = 25.0,
        c_rate: float = 0.3,
        dod_this_cycle: float = 0.8,
    ) -> dict:
        """Aktualizuj SoH na základe prevádzky za posledný timestep.

        Args:
            dt_hours: Timestep v hodinách (0.25 pre 15-min)
            energy_throughput_kwh: |charge| + |discharge| v tomto kroku (kWh AC)
            avg_soc: Priemerný SoC v tomto kroku (0–1)
            temp_c: Priemerná teplota článku
            c_rate: Aktuálny C-rate
            dod_this_cycle: DoD ekvivalent (0.8 = full discharge cycle)

        Returns:
            dict s breakdown degradácie (cal_fade, cyc_fade, soh, replaced)
        """
        # Calendar fade — square root, Arrhenius, SoC stress
        days_now = dt_hours / 24.0
        T_K = temp_c + 273.15
        arrhenius_cal = exp(-self.params.Ea_cal / self.params.R * (1/T_K - 1/self.params.T_ref_K))
        soc_stress = 1.0 + max(0, avg_soc - 0.5) * 2 * 0.3  # nad 50% SoC zrýchľuje
        cal_fade_increment = (
            self.params.k_cal *
            (sqrt((self.total_calendar_days + days_now) / 365.0) - sqrt(self.total_calendar_days / 365.0))
            * arrhenius_cal * soc_stress * self.params.soc_stress_factor
        )

        # Cycle fade — per EFC
        efc_this_step = energy_throughput_kwh / (2 * self.nominal_kwh)  # /2 lebo charge+discharge
        arrhenius_cyc = exp(-self.params.Ea_cyc / self.params.R * (1/T_K - 1/self.params.T_ref_K))
        dod_stress = (dod_this_cycle / 0.8) ** self.params.dod_exponent
        c_stress = max(1.0, c_rate / 0.5)
        cyc_fade_increment = (
            self.params.k_cyc * efc_this_step * dod_stress * c_stress * arrhenius_cyc
        )

        # Update state
        soh_before = self.soh
        self.soh = max(0.0, self.soh - cal_fade_increment - cyc_fade_increment)
        self.total_efc += efc_this_step
        self.total_calendar_days += days_now

        # Replacement check
        replaced = False
        if self.soh <= self.params.eol_soh:
            self.n_replacements += 1
            self.replacement_history.append({
                "at_calendar_days": self.total_calendar_days,
                "at_efc": self.total_efc,
                "soh_at_replacement": self.soh,
            })
            self.soh = 1.0
            self.total_efc = 0.0  # reset cycles
            replaced = True

        return {
            "soh": self.soh,
            "soh_delta": soh_before - (self.soh if not replaced else self.params.eol_soh),
            "cal_fade": cal_fade_increment,
            "cyc_fade": cyc_fade_increment,
            "total_efc": self.total_efc,
            "total_days": self.total_calendar_days,
            "replaced": replaced,
            "n_replacements": self.n_replacements,
            "usable_kwh": self.nominal_kwh * self.soh,
        }

    def reset(self) -> None:
        """Reset stavu (pre new run)."""
        self.soh = self.params.initial_soh
        self.total_efc = 0.0
        self.total_calendar_days = 0.0
        self.n_replacements = 0
        self.replacement_history.clear()


def estimate_lifetime_years(
    annual_efc: float,
    avg_soc: float = 0.5,
    temp_c: float = 25.0,
    dod: float = 0.8,
    c_rate: float = 0.5,
    params: NaumannSchimpeParams | None = None,
) -> dict:
    """Odhad životnosti pre dané prevádzkové podmienky.

    Vráti počet rokov do dosiahnutia EOL (typicky 80% SoH).
    Užitočné pre sizing decisions a pre report "battery lasts X years".
    """
    if params is None:
        params = NaumannSchimpeParams()

    model = BatteryDegradationModel(params=params, nominal_kwh=100)

    # Simuluj rok po roku
    daily_efc = annual_efc / 365.0
    daily_throughput_kwh = daily_efc * 2 * 100  # 100 kWh nominal

    years = 0.0
    while model.soh > params.eol_soh and years < 50:
        # Aktualizuj 1 deň naraz pre rýchlosť
        model.update(
            dt_hours=24,
            energy_throughput_kwh=daily_throughput_kwh,
            avg_soc=avg_soc,
            temp_c=temp_c,
            c_rate=c_rate,
            dod_this_cycle=dod,
        )
        years += 1.0 / 365.0
        if model.n_replacements > 0:
            break

    return {
        "lifetime_years": years,
        "lifetime_efc": years * annual_efc,
        "final_soh": model.soh,
        "limiting_factor": (
            "calendar_aging" if (years * annual_efc) < 0.5 * (params.eol_soh / 0.0003) else "cycle_aging"
        ),
    }
