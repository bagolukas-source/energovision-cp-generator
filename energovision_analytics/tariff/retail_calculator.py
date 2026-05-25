"""RetailCalculator — výpočet plnej retail ceny v €/kWh pre danú hodinu.

Cena pre konečného klienta sa skladá z:
    - Silovej zložky (fix alebo hodinový spot × koeficient)
    - Marže obchodníka (aditív + prirážka)
    - Regulovaných distribučných zložiek (TPS + distrib + straty + NJF + TSS + spotr. daň)

Pre self-consumption (PV → load priame) klient ušetrí CELÚ retail cenu.
Pre export (PV → grid) klient dostane iba výkupnú cenu od bilančnej skupiny.

Príklad VN klient na spote pri spot=95 €/MWh, distribútor SSE 2026:
    silová = 95 × 1.0 = 95 €/MWh         = 0.0950 €/kWh
    obchodník = 20 + 5 = 25 €/MWh         = 0.0250 €/kWh
    regulované = 8.5 + 12.3 + 5.2 + 3.27 + 1.32 + 4.55 = 35.14 €/MWh = 0.0351 €/kWh
    -------------------------------------------------------
    RETAIL BUY = 0.1551 €/kWh
"""
from __future__ import annotations

from typing import Literal, Optional

from energovision_analytics.core.models import TariffYearInput, TypTarify


class RetailCalculator:
    """Kalkulačka retail cien pre konkrétneho klienta."""

    def __init__(
        self,
        tariff: TariffYearInput,
        typ_tarify: TypTarify | Literal["fix", "spot", "hybrid"],
        spot_koeficient: float = 1.0,
        hybrid_spot_pct: float = 0.5,
    ) -> None:
        """
        Args:
            tariff: Tarif pre daný rok/distribútor/sadzbu
            typ_tarify: Kontraktový model klienta
            spot_koeficient: Multiplikátor spot ceny (typicky 1.0; niektoré
                kontrakty majú indexáciu napr. 1.05)
            hybrid_spot_pct: Pre hybrid kontrakt — váha spot zložky (0–1)
        """
        self.tariff = tariff
        self.typ_tarify = TypTarify(typ_tarify) if isinstance(typ_tarify, str) else typ_tarify
        self.spot_koeficient = spot_koeficient
        self.hybrid_spot_pct = hybrid_spot_pct

    # ------------------------------------------------------------------ Komponenty
    def silova_eur_mwh(self, spot_eur_mwh: Optional[float] = None) -> float:
        """Silová zložka v €/MWh pre danú hodinu.

        - FIX: pevná z tariff.fix_silova_eur_mwh
        - SPOT: hodinový spot × koeficient
        - HYBRID: vážený priemer (spot × pct + fix × (1-pct))
        """
        if self.typ_tarify == TypTarify.FIX:
            return self.tariff.fix_silova_eur_mwh
        if self.typ_tarify == TypTarify.SPOT:
            if spot_eur_mwh is None:
                raise ValueError("SPOT klient vyžaduje spot_eur_mwh")
            return spot_eur_mwh * self.spot_koeficient
        # HYBRID
        if spot_eur_mwh is None:
            raise ValueError("HYBRID klient vyžaduje spot_eur_mwh")
        return (
            spot_eur_mwh * self.spot_koeficient * self.hybrid_spot_pct
            + self.tariff.fix_silova_eur_mwh * (1 - self.hybrid_spot_pct)
        )

    def obchodnik_eur_mwh(self) -> float:
        """Marža obchodníka — vždy konštantná."""
        return self.tariff.obchodnik_zlozky_eur_mwh

    def regulovane_eur_mwh(self) -> float:
        """Regulované zložky — vždy konštantné (ÚRSO)."""
        return self.tariff.regulovane_zlozky_eur_mwh

    # ------------------------------------------------------------------ Public API
    def retail_buy_eur_kwh(self, spot_eur_mwh: Optional[float] = None) -> float:
        """Plná retail nákupná cena v €/kWh pre danú hodinu.

        Toto je cena, ktorú klient ušetrí za 1 kWh self-consumption (z PV alebo BAT).
        """
        total_eur_mwh = (
            self.silova_eur_mwh(spot_eur_mwh)
            + self.obchodnik_eur_mwh()
            + self.regulovane_eur_mwh()
        )
        return total_eur_mwh / 1000

    def retail_buy_breakdown(self, spot_eur_mwh: Optional[float] = None) -> dict[str, float]:
        """Vráti dekompozíciu retail ceny — užitočné pre transparentnosť posudku."""
        return {
            "silova_eur_kwh": self.silova_eur_mwh(spot_eur_mwh) / 1000,
            "obchodnik_eur_kwh": self.obchodnik_eur_mwh() / 1000,
            "regulovane_eur_kwh": self.regulovane_eur_mwh() / 1000,
            "tps_eur_kwh": self.tariff.tps_eur_mwh / 1000,
            "distrib_eur_kwh": self.tariff.distrib_eur_mwh / 1000,
            "straty_eur_kwh": self.tariff.straty_eur_mwh / 1000,
            "njf_eur_kwh": self.tariff.njf_eur_mwh / 1000,
            "spotrebna_dan_eur_kwh": self.tariff.spotrebna_dan_eur_mwh / 1000,
            "tss_eur_kwh": self.tariff.tss_eur_mwh / 1000,
            "total_eur_kwh": self.retail_buy_eur_kwh(spot_eur_mwh),
        }

    def annual_capacity_charge_eur(self, mrk_kw: float, rk_kw: float) -> float:
        """Ročná kapacitná zložka (iba VN) — €/rok."""
        mrk_mw = mrk_kw / 1000
        rk_mw = rk_kw / 1000
        return (
            mrk_mw * self.tariff.mrk_kapacita_eur_mw_mes * 12
            + rk_mw * self.tariff.rk_kapacita_eur_mw_mes * 12
        )

    def __repr__(self) -> str:
        return (
            f"RetailCalculator(rok={self.tariff.rok}, "
            f"distrib={self.tariff.distribuutor.value}, "
            f"sadzba={self.tariff.sadzba.value}, "
            f"typ={self.typ_tarify.value})"
        )
