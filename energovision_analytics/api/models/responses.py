"""API response schémy."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    engine_version: str
    api_version: str = "v1"
    server_time: str
    uptime_seconds: float


class AutoFillSiteResponse(BaseModel):
    nazov: str
    psc: str
    distribuutor: str  # SSE | ZSD | VSD
    sadzba: str        # NN | VN
    rk_kw: float
    mrk_kw: float
    gps_lat: float
    gps_lon: float
    lokalita: str
    typ_tarify: str
    bilancna_skupina: str
    rocna_spotreba_kwh: float


class VariantSummary(BaseModel):
    """Jeden variant — kompaktná forma pre UI."""
    variant_id: str
    pv_kwp: float
    bess_kwh: float
    bess_kw: float
    ems_strategy: str

    # CAPEX
    capex_pv_eur: float
    capex_bess_eur: float
    capex_total_eur: float
    dotacia_eur: float
    net_capex_eur: float

    # Energy KPI
    samospotreba_pct: float
    samostatnost_pct: float
    pv_total_kwh: float
    grid_import_kwh: float

    # Financial
    saving_y1_eur: float
    npv_eur: float
    irr_pct: Optional[float]
    payback_simple_y: float
    lcoe_eur_mwh: Optional[float] = None
    lcos_eur_mwh: Optional[float] = None

    label: str
    rank_labels: list[str] = Field(default_factory=list, description="Top-N kritériá ktoré tento variant vyhráva")


class TopVariantsBlock(BaseModel):
    label: str  # "Najvyššie NPV", "Najvyššie IRR", ...
    variant_id: str
    npv_eur: float


class RunManifestResponse(BaseModel):
    engine_version: str
    generated_at: str
    tariff_year: int
    tariff_hash: str
    spot_last_date: Optional[str]
    economic_defaults_hash: str


class RunVariantsResponse(BaseModel):
    """Výstup pre POST /run-variants (synchronný)."""
    success: bool = True
    job_id: Optional[str] = None
    variants: list[VariantSummary]
    top_picks: list[TopVariantsBlock]
    manifest: RunManifestResponse
    n_variants_run: int
    elapsed_ms: float


class JobStatusResponse(BaseModel):
    """Pre async režim — GET /jobs/{job_id}."""
    job_id: str
    status: Literal["queued", "running", "done", "error"]
    created_at: str
    progress_pct: float = 0
    result: Optional[RunVariantsResponse] = None
    error_message: Optional[str] = None


class DotaciaSchemaResponse(BaseModel):
    scheme_id: str
    nazov: str
    vyhlasovatel: str
    status: str
    max_eur: float
    intensity_pct: float
    min_samospotreba_pct: float
    applicable_to: list[str]
    notes: str = ""


class TariffResponse(BaseModel):
    distribuutor: str
    sadzba: str
    year: int
    distribucna_eur_kwh: float
    silova_eur_kwh: float
    tps_eur_kwh: float
    nafta_eur_kwh: Optional[float] = None
    extra: dict = Field(default_factory=dict)
