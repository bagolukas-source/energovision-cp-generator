"""SpotContract — výkupné modely bilančných skupín pre prebytky FVE.

Bilančné skupiny (Energie2, Slovakia Energy, Greenlogy) majú odlišné zmluvné
modely pre výkup prebytočnej PV výroby. Najtypickejšie modely:

  1) FIX klient — pevná výkupná cena (napr. 60 €/MWh), uplatní sa iba ak
     spot > threshold (typicky 20 €/MWh), inak 0.
  2) SPOT klient — výkup podľa formuly závislej od OKTE DAM (napr. spot × 0.95 - 5).
     Negatívne hodiny: floor na 0 (klient neplatí za export).
"""
from __future__ import annotations

import re
from typing import Any, Literal


class SpotContract:
    """Výkupný model bilančnej skupiny pre prebytky FVE."""

    def __init__(
        self,
        nazov: str,
        fix_vykupna_eur_kwh: float = 0.060,
        fix_threshold_eur_mwh: float = 20.0,
        fix_negative_floor_eur_mwh: float = 0.0,
        spot_formula: str = "spot * 0.95 - 5",
        spot_negative_floor_eur_mwh: float = 0.0,
    ) -> None:
        self.nazov = nazov
        self.fix_vykupna_eur_kwh = fix_vykupna_eur_kwh
        self.fix_threshold_eur_mwh = fix_threshold_eur_mwh
        self.fix_negative_floor_eur_mwh = fix_negative_floor_eur_mwh
        self.spot_formula = spot_formula
        self.spot_negative_floor_eur_mwh = spot_negative_floor_eur_mwh

        # Pre-parse spot formula pre rýchle vyhodnocovanie
        # Formát: "spot * X - Y" (X = multiplikátor, Y = fix marža)
        m = re.match(r"\s*spot\s*\*\s*([\d.]+)\s*([+-])\s*([\d.]+)\s*", spot_formula)
        if not m:
            raise ValueError(
                f"Neplatná spot_formula: {spot_formula!r}. "
                "Očakávaný formát: 'spot * X +/- Y' (napr. 'spot * 0.95 - 5')"
            )
        self._spot_mult = float(m.group(1))
        self._spot_offset_sign = 1 if m.group(2) == "+" else -1
        self._spot_offset = float(m.group(3))

    @classmethod
    def from_yaml(cls, nazov: str, data: dict[str, dict[str, Any]]) -> "SpotContract":
        """Načítaj z YAML štruktúry (vid data/tariffs/2026.yaml)."""
        fix = data.get("fix_klient", {})
        spot = data.get("spot_klient", {})
        return cls(
            nazov=nazov,
            fix_vykupna_eur_kwh=fix.get("vykupna_eur_kwh", 0.060),
            fix_threshold_eur_mwh=fix.get("threshold_eur_mwh", 20.0),
            fix_negative_floor_eur_mwh=fix.get("negative_floor_eur_mwh", 0.0),
            spot_formula=spot.get("vykupna_formula", "spot * 0.95 - 5"),
            spot_negative_floor_eur_mwh=spot.get("negative_floor_eur_mwh", 0.0),
        )

    # ------------------------------------------------------------------ Compute
    def vykupna_cena_eur_kwh(
        self,
        spot_eur_mwh: float,
        typ_klienta: Literal["fix", "spot"] = "fix",
    ) -> float:
        """Vráti výkupnú cenu pre 1 kWh exportu pri danom spote.

        Args:
            spot_eur_mwh: OKTE DAM cena v €/MWh (môže byť negatívna)
            typ_klienta: 'fix' alebo 'spot'

        Returns:
            Výkupná cena v €/kWh (vždy ≥ 0 vďaka floor mechanizmu)
        """
        if typ_klienta == "fix":
            if spot_eur_mwh < self.fix_threshold_eur_mwh:
                # Pod thresholdom — výkup = 0 alebo floor
                return self.fix_negative_floor_eur_mwh / 1000
            return self.fix_vykupna_eur_kwh

        # SPOT klient
        cena_eur_mwh = self._spot_mult * spot_eur_mwh + self._spot_offset_sign * self._spot_offset
        # Floor na negatívne hodnoty
        cena_eur_mwh = max(cena_eur_mwh, self.spot_negative_floor_eur_mwh)
        return cena_eur_mwh / 1000

    def export_revenue_eur(
        self,
        export_kwh: float,
        spot_eur_mwh: float,
        typ_klienta: Literal["fix", "spot"] = "fix",
    ) -> float:
        """Vypočítaj príjem za export v €/hodinu."""
        return export_kwh * self.vykupna_cena_eur_kwh(spot_eur_mwh, typ_klienta)

    def __repr__(self) -> str:
        return (
            f"SpotContract({self.nazov!r}, fix_vykup={self.fix_vykupna_eur_kwh*1000:.0f} €/MWh, "
            f"spot_formula={self.spot_formula!r})"
        )
