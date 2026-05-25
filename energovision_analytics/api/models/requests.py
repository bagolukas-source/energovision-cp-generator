"""API request schémy."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AutoFillSiteRequest(BaseModel):
    """Vstup pre POST /auto-fill-site."""
    nazov: str = Field(..., description="Názov OM / klient", examples=["RATUFA Tenisový klub"])
    psc: str = Field(..., description="SK PSČ", examples=["934 01"])
    rocna_spotreba_kwh: float = Field(..., gt=0, examples=[45000])
    rk_kw: float = Field(..., gt=0, examples=[25])
    mrk_kw: Optional[float] = Field(None, description="0/null = auto 1.2× RK")
    typ_tarify: Literal["spot", "fix", "hybrid"] = "spot"
    bilancna_skupina: str = "Energie2"
    eic_kod: Optional[str] = None


class LoadProfileInput(BaseModel):
    """Profil spotreby — buď CSV bytes (base64) alebo synthetic params."""
    source: Literal["csv_base64", "synthetic"] = "synthetic"
    csv_base64: Optional[str] = Field(None, description="Base64-encoded CSV obsah")
    csv_filename: Optional[str] = None
    granularity_min: int = 60
    # Synthetic params (ak source=synthetic)
    profile_template: Literal[
        "tenisovy_klub", "kancelaria", "priemysel_24_7", "domacnost"
    ] = "kancelaria"


class VariantOptions(BaseModel):
    """Range PV × BESS variantov."""
    pv_kwp_options: list[float] = Field(..., examples=[[15, 25, 40, 60]])
    bess_kwh_options: list[float] = Field(..., examples=[[0, 50, 100]])
    ems_strategies: list[str] = Field(default=["rule_based"])


class CapexConfig(BaseModel):
    """CAPEX vstupy — buď €/kWp+€/kWh alebo plný CapexBreakdown."""
    mode: Literal["quick", "breakdown"] = "quick"
    # Quick mode
    capex_pv_eur_per_kwp: float = 800
    capex_bess_eur_per_kwh: float = 480
    # Breakdown mode — kategórie z CapexBreakdown (viď data/capex_from_pon26.py)
    breakdown: Optional[dict] = None


class FinancialConfig(BaseModel):
    """Finančné parametre."""
    dppo_pct: float = 0.22
    discount_rate: float = 0.06
    horizon_years: int = 20
    depr_years: int = 6


class DotaciaConfig(BaseModel):
    """Konfigurácia dotácie."""
    enabled: bool = True
    scheme_id: str = "zelena_podnikom"  # alebo "ziadna", "modernizacny_fond", ...


class RunVariantsRequest(BaseModel):
    """Vstup pre POST /run-variants — kompletný projekt."""
    model_config = ConfigDict(extra="forbid")

    site: AutoFillSiteRequest
    load_profile: LoadProfileInput
    variants: VariantOptions
    capex: CapexConfig = CapexConfig()
    financial: FinancialConfig = FinancialConfig()
    dotacia: DotaciaConfig = DotaciaConfig()
    async_mode: bool = Field(False, description="True = vráti job_id, False = synchrónne výsledky")


class RenderPosudokRequest(BaseModel):
    """Vstup pre POST /render-posudok — vyrobí DOCX."""
    model_config = ConfigDict(extra="forbid")

    # Buď existujúce výsledky variantu (z predchádzajúceho /run-variants)
    variant_result_json: Optional[dict] = None
    # Alebo spusti znova
    run_request: Optional[RunVariantsRequest] = None
    selected_variant_id: Optional[str] = Field(
        None, description="ID variantu z výsledkov (default = najvyššie NPV)"
    )
    template: Literal["one_pager", "full_posudok", "premium"] = "premium"
    client_name: str
    project_name: str = "Hybridné riešenie FVE + BESS"
    project_id: str = ""
    client_address: str = ""
    client_contact: str = ""
    additional_notes: str = ""
    # Premium template extras
    include_sensitivity: bool = True
    include_monte_carlo: bool = True
    posudok_date: Optional[str] = None  # default = today
    prepared_by_name: str = "Lukáš Bago"
    prepared_by_email: str = "lukas.bago@energovision.sk"
    prepared_by_phone: str = "0918 187 762"
