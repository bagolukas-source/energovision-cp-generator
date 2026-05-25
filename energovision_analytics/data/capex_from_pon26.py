"""CAPEX z PON-26-XXX cenovej ponuky — scaffold pre budúci parser.

STATUS: SCAFFOLD (2026-05-24). Po dohodnutí formátu PON-26 sa doplní:
    - PDF parser (pdfplumber / camelot)
    - Excel parser (XLSX export z eponuka.sk / interný formát)
    - Štruktúrované sčítanie kategórií (panely / menice / konstrukcia / BESS / práce)

Použitie (budúce):
    from energovision_analytics.data.capex_from_pon26 import parse_pon26
    capex = parse_pon26("ponuky/PON-26-1234.pdf")
    # → CapexBreakdown(total=87650, panely=42000, menice=8000, ...)

V medziobdobí používaj `CapexBreakdown.manual(...)` pre ručné zadanie.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CapexBreakdown:
    """Štruktúrovaný CAPEX rozpis — kompatibilný s VariantGenerator."""
    # Hlavné kategórie (EUR bez DPH)
    panely: float = 0.0
    menice: float = 0.0
    konstrukcia: float = 0.0
    bess_pack: float = 0.0
    bess_pcs: float = 0.0
    kabelaz_AC: float = 0.0
    kabelaz_DC: float = 0.0
    rozvadzace: float = 0.0
    pripojenie_NN: float = 0.0
    pripojenie_VN: float = 0.0
    stavebne_prace: float = 0.0
    montaz: float = 0.0
    revizia_uvedenie: float = 0.0
    projektova_dokumentacia: float = 0.0
    monitoring_HW: float = 0.0
    ostatne: float = 0.0

    # Meta
    source: str = "manual"            # "manual" | "pon26_pdf" | "pon26_xlsx"
    pon_id: Optional[str] = None      # napr. "PON-26-1234"
    dodavatel: Optional[str] = None
    datum: Optional[str] = None
    poznamka: str = ""

    @property
    def capex_fve_total(self) -> float:
        """CAPEX FVE časť (panely + menice + konstrukcia + DC + montaz_FVE share)."""
        return (
            self.panely + self.menice + self.konstrukcia + self.kabelaz_DC
            + 0.5 * self.montaz + 0.4 * self.rozvadzace
        )

    @property
    def capex_bess_total(self) -> float:
        """CAPEX BESS časť (pack + PCS + share inštalácie)."""
        return (
            self.bess_pack + self.bess_pcs
            + 0.3 * self.montaz + 0.3 * self.rozvadzace
        )

    @property
    def capex_spolocne(self) -> float:
        """Spoločné položky (kábloáž AC, pripojenie, stavebné, revízia, PD)."""
        return (
            self.kabelaz_AC + self.pripojenie_NN + self.pripojenie_VN
            + self.stavebne_prace + self.revizia_uvedenie
            + self.projektova_dokumentacia + self.monitoring_HW + self.ostatne
            + 0.2 * self.montaz + 0.3 * self.rozvadzace
        )

    @property
    def total(self) -> float:
        return self.capex_fve_total + self.capex_bess_total + self.capex_spolocne

    @classmethod
    def manual(cls, **kwargs) -> "CapexBreakdown":
        """Ručné zadanie — všetky kategórie ako kwargs."""
        b = cls(**kwargs)
        b.source = "manual"
        return b

    def to_per_kwp_kwh(self, pv_kwp: float, bess_kwh: float) -> dict:
        """Konvertuje na €/kWp + €/kWh — kompatibilita s VariantGenerator."""
        return {
            "capex_pv_eur_per_kwp": (
                (self.capex_fve_total + self.capex_spolocne * 0.6) / pv_kwp
                if pv_kwp > 0 else 0
            ),
            "capex_bess_eur_per_kwh": (
                (self.capex_bess_total + self.capex_spolocne * 0.4) / bess_kwh
                if bess_kwh > 0 else 0
            ),
        }


def parse_pon26(path: Path | str) -> CapexBreakdown:
    """Parser PON-26-XXX ponuky.

    TODO: implementovať po dohodnutí formátu (PDF / XLSX / iný).
    Plán:
        1. PDF: pdfplumber + regex na riadky tabuľky
        2. XLSX: openpyxl, mapping podľa stĺpcov
        3. Heuristika kategorizácie podľa kľúčových slov v popise položky
    """
    raise NotImplementedError(
        "PON-26 parser zatial nie je implementovaný. "
        "Použi CapexBreakdown.manual(...) alebo zdieľaj formát ponuky."
    )
