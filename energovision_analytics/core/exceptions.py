"""Vlastné výnimky enginu — jasná taxonómia chýb."""
from __future__ import annotations


class EnergovisionError(Exception):
    """Base exception pre celý balík."""


class DataIngestionError(EnergovisionError):
    """Chyba pri čítaní/parsovaní vstupných dát."""


class ValidationError(EnergovisionError):
    """Vstupné dáta neprošli validáciou."""

    def __init__(self, message: str, issues: list[dict] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


class TariffError(EnergovisionError):
    """Chyba v tariff engine (chýbajúca tabuľka, neznámy distribútor)."""


class BenchmarkError(EnergovisionError):
    """Chyba pri benchmark porovnaní."""


class SimulationError(EnergovisionError):
    """Chyba simulácie (PV, battery, EMS)."""


class OptimizationError(EnergovisionError):
    """Optimalizácia zlyhala (infeasible, timeout, solver crash)."""


class ConfigError(EnergovisionError):
    """Chyba v konfigurácii (nesúlad parametrov)."""
