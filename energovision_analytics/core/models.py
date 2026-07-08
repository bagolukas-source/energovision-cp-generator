"""Pydantic v2 data models — single source of truth for all inputs/outputs.

Naming convention:
    - *Input — vstup od užívateľa (s validáciou)
    - *Output / *Result — výstupy enginu
    - *Config — runtime konfigurácia (scenár, parameter set)

Všetky polia majú slovenské popisy v description pre auto-generovanú API doc.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================================
# ENUMS — pevné taxonómie slovenského trhu
# ============================================================================
class Distribuutor(str, Enum):
    """SK distribútor — určuje tarify a špecifické pravidlá."""
    SSE = "SSE"   # Stredoslovenská distribučná (centrum, juh)
    ZSD = "ZSD"   # Západoslovenská distribučná (BA, Nitra, Trnava)
    VSD = "VSD"   # Východoslovenská distribučná (KE, Prešov)


class Sadzba(str, Enum):
    """Napäťová úroveň pripojenia."""
    NN = "NN"     # Nízke napätie (do 1 kV) — domácnosti, malé podniky
    VN = "VN"     # Vysoké napätie (22 kV) — priemysel, väčšie OM


class TypTarify(str, Enum):
    """Typ dodávateľského kontraktu."""
    FIX = "fix"           # Fixná silová cena
    SPOT = "spot"         # Hodinová cena podľa OKTE DAM
    HYBRID = "hybrid"     # Kombinácia (fix base + spot peak/offpeak)


class EMSStrategy(str, Enum):
    """Stratégia EMS dispatch — určuje optimalizáciu BESS."""
    RULE_BASED = "rule_based"          # Master Excel replica (3h lookahead)
    MILP_PERFECT = "milp_perfect"       # Pyomo + perfect foresight (upper bound)
    MPC_ROLLING = "mpc_rolling"         # 48h rolling, ±10% forecast (realistic)
    ALL_THREE = "all_three"             # Vypočítaj všetky a porovnaj


class ModulTyp(str, Enum):
    """Typ FV modulu — ovplyvňuje degradáciu a model."""
    TOPCON = "TOPCon"
    HJT = "HJT"
    PERC = "PERC"
    N_TYPE = "N-Type"
    BIFACIAL = "Bifacial"


class BESSVyrobca(str, Enum):
    """Výrobca BESS — určuje datasheet parametre."""
    HUAWEI = "Huawei"
    SOLINTEG = "Solinteg"
    SUNGROW = "Sungrow"
    BYD = "BYD"
    SOLAX = "Solax"
    INE = "Ine"


class Chemia(str, Enum):
    """Chémia článkov — určuje degradačný model."""
    LFP = "LFP"    # LiFePO4 — moderný štandard, dlhšia životnosť
    NMC = "NMC"    # Lítium-nikel-mangán-kobalt — staršie projekty


class Konfiguracia(str, Enum):
    """Mechanická konfigurácia FV inštalácie."""
    JEDNORADOVA_LANDSCAPE = "1xL"     # 1 modul, landscape
    JEDNORADOVA_PORTRAIT = "1xP"      # 1 modul, portrait
    DVOJRADOVA_PORTRAIT = "2xP"       # 2 moduly, portrait
    VYCHOD_ZAPAD = "EW"               # East-West tilted
    TRACKER = "tracker"               # Solárny tracker


# ============================================================================
# BASE MODEL — všetko dedí z neho
# ============================================================================
class EnergoBase(BaseModel):
    """Spoločná konfigurácia pre všetky modely."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
        extra="forbid",
        populate_by_name=True,
    )


# ============================================================================
# SITE — odberné miesto
# ============================================================================
class SiteInput(EnergoBase):
    """Odberné miesto (OM) — fyzická lokalita s pripojením k DS.

    Príklad EIC kódu pre SK: '24X-AGROSTAVL01-K' (16 znakov, distribútor + 12 znakov + checksum).
    """
    nazov: str = Field(..., min_length=2, max_length=200,
                       description="Obchodný názov OM (napr. 'AGROSTAV GROUP Levice')")
    eic_kod: Optional[str] = Field(
        None,
        pattern=r"^24[XYZ]-[A-Z0-9-]{12,16}$",
        description="EIC kód odberného miesta podľa SEPS registrácie",
    )
    distribuutor: Distribuutor = Field(..., description="Distribútor podľa územia")
    sadzba: Sadzba = Field(..., description="Napäťová úroveň pripojenia")
    rk_kw: float = Field(..., gt=0, le=80000,
                          description="Rezervovaná kapacita (max free import zo siete) v kW")
    mrk_kw: float = Field(..., ge=0, le=80000,
                           description="Maximálna rezervovaná kapacita (max free export do siete) v kW; 0 = export nepovolený")
    typ_tarify: TypTarify = Field(..., description="Typ dodávateľského kontraktu")
    bilancna_skupina: str = Field("Energie2", max_length=100,
                                   description="Bilančná skupina pre prebytky FVE")
    fakturacny_psc: str = Field(..., pattern=r"^\d{3} ?\d{2}$",
                                 description="PSČ fakturačnej adresy")
    gps_lat: float = Field(..., ge=47.5, le=49.7,
                            description="Zemepisná šírka (Slovensko 47.5–49.7°N)")
    gps_lon: float = Field(..., ge=16.8, le=22.6,
                            description="Zemepisná dĺžka (Slovensko 16.8–22.6°E)")
    nadm_vyska_m: float = Field(200.0, ge=80, le=2200,
                                 description="Nadmorská výška (Slovensko 80–2200 m)")
    rocna_spotreba_kwh: float = Field(..., gt=0,
                                       description="Ročná spotreba podľa faktúry (validation reference)")

    @model_validator(mode="after")
    def mrk_geq_rk(self) -> "SiteInput":
        # audit: export limit < RK (aj 0) je legitímny zmluvný stav — tvrdý raise nútil builder
        # posielať mrk = max(import, export) a export sa clipoval na zlej úrovni. Soft warning
        # rieši validation/validator.py.
        return self


# ============================================================================
# PV — fotovoltická elektráreň
# ============================================================================
class PVInput(EnergoBase):
    """FVE inštalácia — geometria + technické parametre pre pvlib simuláciu."""

    instalovany_kwp: float = Field(..., gt=0, le=10000,
                                    description="Inštalovaný výkon FVE v kWp (DC)")
    modul_typ: ModulTyp = Field(..., description="Typ FV modulu (ovplyvňuje degradáciu)")
    modul_vyrobca: str = Field("Trina", max_length=100, description="Výrobca modulu")
    modul_model: str = Field("Vertex S+ 700W", max_length=100)
    modul_wp: int = Field(..., ge=300, le=800, description="Nominálny výkon modulu vo Wp (STC)")
    modul_bifaciality: float = Field(0.0, ge=0, le=0.85,
                                      description="Bifacial factor (0 = mono-facial, 0.7 = TOPCon, 0.8 = HJT)")
    pocet_modulov: int = Field(..., gt=0)
    inverter_vyrobca: str = Field("Huawei", max_length=100)
    inverter_model: str = Field("SUN2000-100KTL-M2", max_length=100)
    inverter_kw_ac: float = Field(..., gt=0, le=10000,
                                   description="AC výkon invertora")
    inverter_eff_nom: float = Field(0.985, ge=0.90, le=0.995,
                                     description="Nominálna účinnosť invertora (EU eff)")
    sklon_stupne: float = Field(..., ge=0, le=90,
                                 description="Sklon panela od horizontu (10–35° optimálne pre SK)")
    azimut_stupne: float = Field(180, ge=0, le=360,
                                  description="Azimut (0=N, 90=E, 180=S, 270=W)")
    konfiguracia: Konfiguracia = Field(..., description="Mechanická konfigurácia")
    rocna_degradacia_pct: float = Field(0.4, ge=0.1, le=1.5,
                                         description="Ročná degradácia modulov v % (TOPCon 0.4, HJT 0.25)")
    soiling_pct_rok: float = Field(2.0, ge=0, le=10,
                                    description="Ročná strata znečistením (SK 1.5–2.5%)")
    snow_pct_rok: float = Field(1.5, ge=0, le=10,
                                 description="Ročná strata snehom (SK 1–3% podľa nadm. výšky)")
    mismatch_pct: float = Field(1.0, ge=0, le=5,
                                 description="Strata mismatch reťazcov")
    wiring_pct: float = Field(1.5, ge=0, le=5,
                               description="DC + AC wiring losses")

    @model_validator(mode="after")
    def kwp_consistency(self) -> "PVInput":
        expected_kwp = self.modul_wp * self.pocet_modulov / 1000
        if abs(expected_kwp - self.instalovany_kwp) / self.instalovany_kwp > 0.02:
            raise ValueError(
                f"instalovany_kwp ({self.instalovany_kwp:.1f}) nesedí s modul_wp × počet "
                f"({expected_kwp:.1f}) — viac než ±2 % odchýlka"
            )
        return self

    @model_validator(mode="after")
    def inverter_oversizing_check(self) -> "PVInput":
        """DC/AC ratio typicky 1.0–1.4. Mimo => warning v validation engine."""
        ratio = self.instalovany_kwp / self.inverter_kw_ac
        if ratio > 1.6:
            raise ValueError(f"DC/AC ratio {ratio:.2f} > 1.6 — extrémne oversizing, skontroluj parametre")
        return self


# ============================================================================
# BESS — batériové úložisko
# ============================================================================
class BESSInput(EnergoBase):
    """Batériové úložisko — datasheet + použiteľné parametre."""

    vyrobca: BESSVyrobca = Field(..., description="Výrobca BESS systému")
    typ: str = Field(..., min_length=3, max_length=100,
                      description="Konkrétny model (napr. 'LUNA2000-215')")
    chemie: Chemia = Field(Chemia.LFP, description="Chémia článkov")
    nominal_kwh: float = Field(..., gt=0, le=10000,
                                description="Nominálna kapacita podľa datasheetu")
    usable_kwh: float = Field(..., gt=0,
                               description="Použiteľná kapacita (nominal × (soc_max - soc_min))")
    power_kw_ac: float = Field(..., gt=0,
                                description="AC výkon (PCS) — limit charge aj discharge")
    soc_min_pct: float = Field(0.08, ge=0.02, le=0.30,
                                description="DoD floor (LFP typicky 5-10%)")
    soc_max_pct: float = Field(0.98, ge=0.80, le=0.99,
                                description="DoD ceiling")
    rte_ac_ac: float = Field(0.88, ge=0.80, le=0.95,
                              description="Round-trip AC-AC efficiency (reálne 0.88; datasheet 0.91)")
    c_rate_max: float = Field(0.5, gt=0, le=2.0,
                               description="Max C-rate (power_kw / nominal_kwh)")
    initial_soh: float = Field(1.0, ge=0.5, le=1.0,
                                description="Initial State of Health (1.0 = nová)")
    warranty_years: int = Field(10, ge=1, le=25)
    warranty_cycles: int = Field(6000, ge=500, le=15000)
    warranty_eol_soh: float = Field(0.80, ge=0.50, le=0.95,
                                     description="End-of-life SoH (warranty čerpá pri poklese pod túto)")
    container_hvac: bool = Field(True, description="Má container HVAC (cooling)?")
    avg_ambient_temp_c: float = Field(22.0, ge=-10, le=40,
                                       description="Priemerná interná teplota baterky (s HVAC 20-28°C)")

    @model_validator(mode="after")
    def usable_consistency(self) -> "BESSInput":
        expected_usable = self.nominal_kwh * (self.soc_max_pct - self.soc_min_pct)
        if abs(expected_usable - self.usable_kwh) / self.usable_kwh > 0.05:
            raise ValueError(
                f"usable_kwh ({self.usable_kwh:.1f}) nesedí s nominal × (soc_max-soc_min) "
                f"({expected_usable:.1f}) — viac než ±5 % odchýlka"
            )
        return self

    @model_validator(mode="after")
    def c_rate_consistency(self) -> "BESSInput":
        actual = self.power_kw_ac / self.nominal_kwh
        if actual > self.c_rate_max + 0.1:
            raise ValueError(
                f"power_kw_ac / nominal_kwh = {actual:.2f} > c_rate_max ({self.c_rate_max:.2f})"
            )
        return self


# ============================================================================
# LOAD PROFILE — intervalové dáta spotreby
# ============================================================================
class LoadProfileInput(EnergoBase):
    """Profil spotreby — 15-min alebo 60-min intervaly."""

    timestamps: list[datetime] = Field(..., min_length=24)
    values_kw: list[float] = Field(..., min_length=24)
    granularity_min: Literal[15, 60] = Field(15)
    rocna_spotreba_kwh: float = Field(..., gt=0,
                                       description="Suma za rok (validation reference)")
    source: str = Field("manual", description="SSE | ZSD | VSD | manual | synthetic")

    @model_validator(mode="after")
    def lengths_match(self) -> "LoadProfileInput":
        if len(self.values_kw) != len(self.timestamps):
            raise ValueError(
                f"timestamps ({len(self.timestamps)}) a values_kw ({len(self.values_kw)}) "
                f"musia mať rovnakú dĺžku"
            )
        return self

    @model_validator(mode="after")
    def annual_sum_check(self) -> "LoadProfileInput":
        """Suma intervalových kW × Δt by mala sa rovnať rocna_spotreba_kwh ±5%."""
        dt_h = self.granularity_min / 60.0
        sum_kwh = sum(self.values_kw) * dt_h
        deviation = abs(sum_kwh - self.rocna_spotreba_kwh) / self.rocna_spotreba_kwh
        if deviation > 0.05:
            raise ValueError(
                f"Suma intervalových dát ({sum_kwh:.0f} kWh) nesedí s rocna_spotreba_kwh "
                f"({self.rocna_spotreba_kwh:.0f} kWh) — odchýlka {deviation*100:.1f}% > 5%"
            )
        return self


# ============================================================================
# TARIFF — ÚRSO + distribúcia + obchodník
# ============================================================================
class TariffYearInput(EnergoBase):
    """Tarif pre konkrétny rok / distribútor / sadzba.

    Hodnoty v EUR/MWh (zaužívaná konvencia ÚRSO).
    Zdroje: ÚRSO cenové rozhodnutia, distribučné cenníky SSE/ZSD/VSD.
    """
    rok: int = Field(..., ge=2020, le=2030)
    distribuutor: Distribuutor
    sadzba: Sadzba

    # Tarifné zložky (€/MWh)
    tps_eur_mwh: float = Field(..., ge=0, le=100,
                                description="Tarifa za prevádzkovanie systému")
    distrib_eur_mwh: float = Field(..., ge=0, le=200,
                                    description="Distribučný poplatok za odber")
    straty_eur_mwh: float = Field(0, ge=0, le=50,
                                   description="Straty (% silovej × spot, alebo €/MWh)")
    njf_eur_mwh: float = Field(..., ge=0, le=50,
                                description="Národný jadrový fond")
    spotrebna_dan_eur_mwh: float = Field(1.32, ge=0, le=20,
                                          description="Spotrebná daň z elektriny")
    tss_eur_mwh: float = Field(0, ge=0, le=30,
                                description="Tarifa za sieťové straty")

    # Kapacitné (€/MW/mes pre VN klientov)
    mrk_kapacita_eur_mw_mes: float = Field(0, ge=0,
                                            description="Mesačná sadzba za MRK (iba VN)")
    rk_kapacita_eur_mw_mes: float = Field(0, ge=0,
                                           description="Mesačná sadzba za RK (iba VN)")

    # NOVÉ od 1.1.2026 — penalty za prekročenie MRK pri exporte do DS
    mrk_export_penalty_eur_kwh: float = Field(
        0.0, ge=0, le=0.10,
        description="Penalty pri exporte > MRK v 15-min agregáte (SSD od 1.1.2026)",
    )

    # Obchodník
    obchodnik_aditiv_eur_mwh: float = Field(20.0, ge=0, le=100)
    obchodnik_prirazka_eur_mwh: float = Field(5.0, ge=0, le=50)

    # Fix base (pre FIX kontrakty)
    fix_silova_eur_mwh: float = Field(114.0, ge=0, le=500,
                                       description="Fixná silová cena pre fix kontrakty")

    @property
    def regulovane_zlozky_eur_mwh(self) -> float:
        """Súčet regulovaných zložiek (TPS, distribúcia, straty, NJF, spotrebná daň, TSS).

        Tieto zložky platíš VŽDY bez ohľadu na typ tarify alebo dodávateľa.
        Self-consumption ich ušetrí v plnej výške.
        """
        return (
            self.tps_eur_mwh + self.distrib_eur_mwh + self.straty_eur_mwh
            + self.njf_eur_mwh + self.spotrebna_dan_eur_mwh + self.tss_eur_mwh
        )

    @property
    def obchodnik_zlozky_eur_mwh(self) -> float:
        """Marža dodávateľa (aditív + prirážka)."""
        return self.obchodnik_aditiv_eur_mwh + self.obchodnik_prirazka_eur_mwh


# ============================================================================
# SCENARIO — konfigurácia behu
# ============================================================================
class ScenarioConfig(EnergoBase):
    """Scenár pre simuláciu — určuje EMS stratégiu, ekonomiku, výstupy."""

    nazov: str = Field(..., min_length=2, max_length=100,
                        description="Názov scenára (napr. 'Báza', 'Spot s arbitrážou')")
    ems_strategy: EMSStrategy = Field(EMSStrategy.ALL_THREE)
    timestep_min: Literal[15, 60] = Field(15,
                                            description="Granularita simulácie (15 = 1/4-h, 60 = 1-h)")
    horizon_years: int = Field(20, ge=1, le=30)

    # Finančné parametre
    diskont: float = Field(0.06, ge=0.0, le=0.30,
                            description="Diskontná sadzba pre NPV")
    dppo: float = Field(0.21, ge=0.10, le=0.30,
                         description="DPPO sadzba (21% štandard, 15% malé firmy, 24% > 5 mil)")
    depr_years: int = Field(6, ge=4, le=20,
                             description="Daňový odpis (6r pre BESS+FVE v SK)")

    # CAPEX/OPEX
    capex_eur: float = Field(..., gt=0)
    dotacia_eur: float = Field(0, ge=0)
    opex_pct_capex: float = Field(0.015, ge=0, le=0.10,
                                   description="Ročný OPEX ako % CAPEX")
    insurance_pct_capex: float = Field(0.003, ge=0, le=0.02,
                                        description="Poistenie ako % CAPEX")

    # Pokročilé
    monte_carlo_runs: int = Field(0, ge=0, le=100000,
                                    description="0 = vypnúť MC; typicky 1000–10000")
    include_vat_refund: bool = Field(False,
                                      description="B2B klient — odpočet DPH 20% z CAPEX")
    load_growth_pct_y: float = Field(0.0, ge=-2, le=10,
                                      description="Ročný rast spotreby v %")

    @model_validator(mode="after")
    def dotacia_leq_capex(self) -> "ScenarioConfig":
        if self.dotacia_eur > self.capex_eur:
            raise ValueError(
                f"dotacia_eur ({self.dotacia_eur:.0f}) nemôže byť > capex_eur ({self.capex_eur:.0f})"
            )
        return self


# ============================================================================
# RESULTS — výstupy simulácie (Sprint 2/3 ich naplní)
# ============================================================================
class AnnualSummary(EnergoBase):
    """Ročný sumár — jeden riadok per rok počas horizontu."""
    rok: int
    load_kwh: float
    pv_kwh: float
    pv_to_load_kwh: float
    pv_to_bat_kwh: float
    pv_to_grid_kwh: float
    bat_charge_kwh: float
    bat_discharge_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    samospotreba_pct: float = Field(..., ge=0, le=100,
                                     description="(PV→load + PV→BAT) / PV_total × 100")
    samostatnost_pct: float = Field(..., ge=0, le=100,
                                     description="(PV→load + BAT→load) / load × 100")
    bat_cycles_efc: float = Field(..., ge=0,
                                   description="Equivalent full cycles za rok")
    bat_soh_end: float = Field(..., ge=0, le=1)
    saving_pv_self_eur: float
    saving_pv_export_eur: float
    saving_bat_self_cons_eur: float
    saving_arb_net_eur: float
    saving_peak_shave_eur: float = 0.0
    saving_total_eur: float
    mrk_export_penalty_eur: float = 0.0
    co2_avoided_t: float = Field(..., ge=0,
                                  description="Vyhnuté CO2 emisie (priemerný SK grid mix)")


class FinancialResult(EnergoBase):
    """Finančný výsledok scenára."""
    capex_gross_eur: float
    capex_net_eur: float
    capex_after_vat_refund_eur: Optional[float] = None
    annual_saving_y1_eur: float
    annual_opex_eur: float
    annual_tax_shield_eur: float
    npv_eur: float
    irr_pct: Optional[float] = None
    lcoe_eur_mwh: Optional[float] = None
    lcos_eur_mwh: Optional[float] = None
    payback_simple_y: float
    payback_discounted_y: Optional[float] = None
    cashflows: list[float]
    # Monte Carlo (ak ScenarioConfig.monte_carlo_runs > 0)
    p10_npv_eur: Optional[float] = None
    p50_npv_eur: Optional[float] = None
    p90_npv_eur: Optional[float] = None
    prob_npv_positive: Optional[float] = None
