"""Energovision Analytics Engine — FVE+BESS posudky pre slovenský trh.

Sprint 1 — Data Ingestion + Validation + Tariff + Benchmark.

Hlavné komponenty:
    - core.models: Pydantic dátové modely (SiteInput, PVInput, BESSInput, ...)
    - core.time_series: TimeSeriesData s timezone-aware
    - core.manifest: RunManifest pre audit trail
    - data.readers: SSE/ZSE/VSD/OKTE/PVGIS/Excel readers
    - validation: physical limits, energy balance, anomaly detection
    - tariff: ÚRSO tarify, MRK penalty 2026, spot kontrakty
    - benchmark: NREL/IEA/EU porovnania
"""
from energovision_analytics._version import __version__
from energovision_analytics.core.models import (
    BESSInput,
    Distribuutor,
    LoadProfileInput,
    PVInput,
    Sadzba,
    ScenarioConfig,
    SiteInput,
    TariffYearInput,
    TypTarify,
)
from energovision_analytics.core.time_series import TimeSeriesData
from energovision_analytics.tariff.tariff_database import TariffEngine
from energovision_analytics.validation.validator import ValidationEngine

__all__ = [
    "__version__",
    # Models
    "SiteInput",
    "PVInput",
    "BESSInput",
    "LoadProfileInput",
    "ScenarioConfig",
    "TariffYearInput",
    "Distribuutor",
    "Sadzba",
    "TypTarify",
    # Core
    "TimeSeriesData",
    # Engines
    "TariffEngine",
    "ValidationEngine",
]
