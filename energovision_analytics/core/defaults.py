"""Centrálne defaulty — single source of truth z economic_defaults.yaml.

Použitie:
    from energovision_analytics.core.defaults import ECON

    builder = CashflowBuilder(
        dppo_pct=ECON.dppo.default_pct,
        depr_years=ECON.depreciation.pv_years,
        discount_rate=ECON.financial.discount_rate_default,
        ...
    )

Pred 2026-05-24 boli tieto hodnoty hardkódované v 5 rôznych moduloch.
Teraz jeden YAML, jedna zmena → všade aplikované.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class DppoConfig:
    small_co_pct: float = 0.10
    standard_pct: float = 0.21  # zjednotené s tax_shield/cashflow (do 5 mil EUR)
    large_co_pct: float = 0.24
    default_pct: float = 0.22


@dataclass(frozen=True)
class DepreciationConfig:
    pv_years: int = 6
    bess_years: int = 6
    monitoring_years: int = 4


@dataclass(frozen=True)
class FinancialConfig:
    discount_rate_default: float = 0.06
    horizon_years_default: int = 20
    inflation_pct_default: float = 0.025


@dataclass(frozen=True)
class OpexConfig:
    pv_pct_per_year: float = 0.015
    bess_pct_per_year: float = 0.020
    insurance_pct_per_year: float = 0.003
    monitoring_eur_per_year: float = 300


@dataclass(frozen=True)
class BessLifecycleConfig:
    inverter_replacement_year: int = 12
    inverter_replacement_pct: float = 0.10
    cells_replacement_year: Optional[int] = None
    c_rate_default: float = 0.5


@dataclass(frozen=True)
class DegradationConfig:
    pv_pct_per_year: float = 0.005
    bess_pct_per_year: float = 0.020


@dataclass(frozen=True)
class ContractConfig:
    spot_markup_eur_mwh: float = 8
    export_buyback_eur_mwh: float = 60
    fix_baseline_eur_mwh: float = 145


@dataclass(frozen=True)
class EconomicDefaults:
    dppo: DppoConfig = field(default_factory=DppoConfig)
    depreciation: DepreciationConfig = field(default_factory=DepreciationConfig)
    financial: FinancialConfig = field(default_factory=FinancialConfig)
    opex: OpexConfig = field(default_factory=OpexConfig)
    bess_lifecycle: BessLifecycleConfig = field(default_factory=BessLifecycleConfig)
    degradation: DegradationConfig = field(default_factory=DegradationConfig)
    contract: ContractConfig = field(default_factory=ContractConfig)
    _source_path: str = ""

    @classmethod
    def from_yaml(cls, path: Path | str) -> "EconomicDefaults":
        path = Path(path)
        if not path.exists():
            # Fallback na zabudované defaulty
            return cls()
        data = yaml.safe_load(path.read_text())
        return cls(
            dppo=DppoConfig(**data.get("dppo", {})),
            depreciation=DepreciationConfig(**data.get("depreciation", {})),
            financial=FinancialConfig(**data.get("financial", {})),
            opex=OpexConfig(**data.get("opex", {})),
            bess_lifecycle=BessLifecycleConfig(**data.get("bess_lifecycle", {})),
            degradation=DegradationConfig(**data.get("degradation", {})),
            contract=ContractConfig(**data.get("contract", {})),
            _source_path=str(path),
        )


# === SINGLETON pre lazy import ===
_ECON_CACHE: Optional[EconomicDefaults] = None
_DEFAULT_YAML = Path(__file__).resolve().parents[3] / "data" / "config" / "economic_defaults.yaml"


def get_econ_defaults(path: Optional[Path | str] = None) -> EconomicDefaults:
    """Vráti singleton — lazy načítanie pri prvom volaní."""
    global _ECON_CACHE
    if _ECON_CACHE is None or path is not None:
        _ECON_CACHE = EconomicDefaults.from_yaml(path or _DEFAULT_YAML)
    return _ECON_CACHE


# Convenience export — používaj `from ...core.defaults import ECON`
ECON = get_econ_defaults()
