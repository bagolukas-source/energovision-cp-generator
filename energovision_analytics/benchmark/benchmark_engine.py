"""BenchmarkEngine — porovnanie projektu s databázami.

Zdroje:
    - NREL ATB 2024 (Commercial Battery Storage, PV)
    - NREL ESHB 2025 (Energy Storage Handbook)
    - IEA PVPS Task 32 (yield benchmarks per krajinu)
    - Lazard LCOE+ June 2025
    - SK interné: 14 existujúcich Energovision posudkov

Hardcoded benchmarky (statické tabuľky) — aktualizovať raz ročne.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ============================================================================
# BENCHMARK DATA — aktualizovať raz ročne podľa najnovších zdrojov
# ============================================================================
PV_YIELD_BENCHMARKS_SK_KWH_PER_KWP: dict[str, tuple[float, float, float]] = {
    # Lokalita → (low, median, high)
    "Bratislava":  (980, 1080, 1180),
    "Nitra":       (990, 1090, 1190),
    "Levice":      (1000, 1100, 1200),  # juh SK
    "Žilina":      (920, 1020, 1120),
    "Banská Bystrica": (950, 1050, 1150),
    "Košice":      (960, 1060, 1160),
    "Prešov":      (950, 1050, 1150),
    "default":     (950, 1070, 1180),
}

# CAPEX BESS turn-key 2025–2026 (EUR/kWh), per system size
BESS_CAPEX_EUR_PER_KWH: dict[str, tuple[float, float, float]] = {
    # Range → (low, median, high)
    "<100kWh":      (550, 700, 900),
    "100-500kWh":   (450, 580, 750),
    "500-1000kWh":  (400, 520, 680),
    ">1000kWh":     (380, 480, 620),
}

# CAPEX FVE turn-key 2025–2026 (EUR/kWp)
PV_CAPEX_EUR_PER_KWP: dict[str, tuple[float, float, float]] = {
    "<100kWp":      (900, 1050, 1250),
    "100-500kWp":   (750, 880, 1050),
    "500-1000kWp":  (650, 780, 920),
    ">1000kWp":     (550, 680, 820),
}

# RTE benchmarky (AC-AC reálne)
BESS_RTE_BENCHMARKS: dict[str, tuple[float, float, float]] = {
    "Huawei":      (0.88, 0.91, 0.93),
    "Solinteg":    (0.85, 0.88, 0.90),
    "Sungrow":     (0.87, 0.90, 0.92),
    "BYD":         (0.86, 0.89, 0.91),
    "Solax":       (0.84, 0.87, 0.90),
    "default":     (0.85, 0.88, 0.91),
}

# LCOS — Levelized Cost of Storage (EUR/MWh discharged), 2025
LCOS_BENCHMARKS_EUR_PER_MWH: dict[str, tuple[float, float, float]] = {
    "2h_CI":   (250, 320, 400),   # 2-hour C&I systems
    "4h_CI":   (180, 240, 310),
    "4h_utility": (150, 200, 260),
}

# LCOE — PV (EUR/MWh), 2025 EU
LCOE_PV_EUR_PER_MWH: dict[str, tuple[float, float, float]] = {
    "utility":   (35, 45, 55),
    "C&I":       (60, 75, 90),
    "residential": (100, 120, 140),
}

# IRR expectations (%, EU C&I)
IRR_BENCHMARKS_PCT: dict[str, tuple[float, float, float]] = {
    "FVE_solo":         (10, 14, 18),
    "FVE_BESS_hybrid":  (8, 11, 14),
    "BESS_solo":        (6, 9, 12),
    "FVE_BESS_dotacia": (15, 22, 30),
}

# Payback expectations (rokov)
PAYBACK_BENCHMARKS_Y: dict[str, tuple[float, float, float]] = {
    "FVE_solo":         (5, 7, 9),
    "FVE_BESS_hybrid":  (6, 8, 11),
    "BESS_solo":        (7, 10, 14),
    "FVE_BESS_dotacia": (3, 5, 7),
}


@dataclass
class BenchmarkComparison:
    """Výsledok porovnania jednej hodnoty s benchmark range."""
    metric: str
    project_value: float
    benchmark_low: float
    benchmark_median: float
    benchmark_high: float
    unit: str
    percentile: float  # 0–100, kde sa projekt umiestňuje
    verdict: str  # 'BELOW_RANGE', 'IN_RANGE', 'ABOVE_RANGE'
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "project_value": self.project_value,
            "benchmark_low": self.benchmark_low,
            "benchmark_median": self.benchmark_median,
            "benchmark_high": self.benchmark_high,
            "unit": self.unit,
            "percentile": self.percentile,
            "verdict": self.verdict,
            "note": self.note,
        }


class BenchmarkEngine:
    """Hlavný benchmark engine."""

    @staticmethod
    def _classify(value: float, low: float, median: float, high: float) -> tuple[float, str]:
        """Vráti (percentile, verdict)."""
        if value < low:
            return (max(0, (value / low) * 25), "BELOW_RANGE")
        if value > high:
            return (min(100, 75 + (value - high) / high * 25), "ABOVE_RANGE")
        # Linear interpolation v rámci range
        if value <= median:
            pct = 25 + (value - low) / (median - low) * 25
        else:
            pct = 50 + (value - median) / (high - median) * 25
        return (pct, "IN_RANGE")

    @classmethod
    def pv_yield(
        cls,
        annual_kwh: float,
        installed_kwp: float,
        lokalita: str = "default",
    ) -> BenchmarkComparison:
        """Porovnaj špecifický výnos FVE s benchmarkmi."""
        yield_kwh_per_kwp = annual_kwh / installed_kwp
        low, median, high = PV_YIELD_BENCHMARKS_SK_KWH_PER_KWP.get(
            lokalita, PV_YIELD_BENCHMARKS_SK_KWH_PER_KWP["default"]
        )
        pct, verdict = cls._classify(yield_kwh_per_kwp, low, median, high)
        note = None
        if verdict == "BELOW_RANGE":
            note = "Nízky výnos — možno tienenie, suboptimálny sklon/azimut, alebo soiling"
        elif verdict == "ABOVE_RANGE":
            note = "Vysoký výnos — overiť, či simulácia nepreceňuje (možné chyba modelu)"
        return BenchmarkComparison(
            metric=f"PV yield ({lokalita})",
            project_value=yield_kwh_per_kwp,
            benchmark_low=low, benchmark_median=median, benchmark_high=high,
            unit="kWh/kWp/rok",
            percentile=pct, verdict=verdict, note=note,
        )

    @classmethod
    def bess_capex(
        cls,
        capex_eur: float,
        bess_kwh: float,
    ) -> BenchmarkComparison:
        """Porovnaj BESS CAPEX €/kWh s trhom."""
        capex_per_kwh = capex_eur / bess_kwh
        if bess_kwh < 100:
            key = "<100kWh"
        elif bess_kwh < 500:
            key = "100-500kWh"
        elif bess_kwh < 1000:
            key = "500-1000kWh"
        else:
            key = ">1000kWh"

        low, median, high = BESS_CAPEX_EUR_PER_KWH[key]
        pct, verdict = cls._classify(capex_per_kwh, low, median, high)
        return BenchmarkComparison(
            metric=f"BESS CAPEX ({key})",
            project_value=capex_per_kwh,
            benchmark_low=low, benchmark_median=median, benchmark_high=high,
            unit="EUR/kWh",
            percentile=pct, verdict=verdict,
        )

    @classmethod
    def bess_rte(cls, rte: float, vyrobca: str = "default") -> BenchmarkComparison:
        """Porovnaj RTE s datasheet/realistic benchmarkmi."""
        low, median, high = BESS_RTE_BENCHMARKS.get(vyrobca, BESS_RTE_BENCHMARKS["default"])
        pct, verdict = cls._classify(rte, low, median, high)
        note = None
        if verdict == "ABOVE_RANGE":
            note = (
                "Hodnota nad reálnymi limitmi — pravdepodobne datasheet (laboratory) "
                "namiesto field-measured. Použiť 0.88 pre dispatch simulácie."
            )
        return BenchmarkComparison(
            metric=f"BESS RTE ({vyrobca})",
            project_value=rte,
            benchmark_low=low, benchmark_median=median, benchmark_high=high,
            unit="AC-AC ratio",
            percentile=pct, verdict=verdict, note=note,
        )

    @classmethod
    def lcos(cls, lcos_eur_mwh: float, profile: str = "2h_CI") -> BenchmarkComparison:
        """Porovnaj LCOS s NREL benchmarkmi."""
        low, median, high = LCOS_BENCHMARKS_EUR_PER_MWH.get(profile, LCOS_BENCHMARKS_EUR_PER_MWH["2h_CI"])
        pct, verdict = cls._classify(lcos_eur_mwh, low, median, high)
        return BenchmarkComparison(
            metric=f"LCOS ({profile})",
            project_value=lcos_eur_mwh,
            benchmark_low=low, benchmark_median=median, benchmark_high=high,
            unit="EUR/MWh",
            percentile=pct, verdict=verdict,
        )

    @classmethod
    def irr(cls, irr_pct: float, project_type: str) -> BenchmarkComparison:
        """Porovnaj IRR s typickými EU C&I projektami."""
        low, median, high = IRR_BENCHMARKS_PCT.get(project_type, IRR_BENCHMARKS_PCT["FVE_BESS_hybrid"])
        pct, verdict = cls._classify(irr_pct, low, median, high)
        note = None
        if verdict == "ABOVE_RANGE":
            note = "Veľmi vysoký IRR — overiť optimistické assumptions"
        elif verdict == "BELOW_RANGE":
            note = "Nízky IRR — zvážiť dotácie, dimenzovanie BESS, EMS optimalizáciu"
        return BenchmarkComparison(
            metric=f"IRR ({project_type})",
            project_value=irr_pct,
            benchmark_low=low, benchmark_median=median, benchmark_high=high,
            unit="%",
            percentile=pct, verdict=verdict, note=note,
        )

    @classmethod
    def payback(cls, payback_y: float, project_type: str) -> BenchmarkComparison:
        low, median, high = PAYBACK_BENCHMARKS_Y.get(project_type, PAYBACK_BENCHMARKS_Y["FVE_BESS_hybrid"])
        # Pre payback je nižšie = lepšie, takže obrátime semantiku
        pct, verdict = cls._classify(payback_y, low, median, high)
        return BenchmarkComparison(
            metric=f"Payback ({project_type})",
            project_value=payback_y,
            benchmark_low=low, benchmark_median=median, benchmark_high=high,
            unit="rokov",
            percentile=pct, verdict=verdict,
        )

    @classmethod
    def compare_project(
        cls,
        annual_pv_kwh: Optional[float] = None,
        installed_kwp: Optional[float] = None,
        lokalita: str = "default",
        bess_capex_eur: Optional[float] = None,
        bess_kwh: Optional[float] = None,
        rte: Optional[float] = None,
        vyrobca: str = "default",
        lcos: Optional[float] = None,
        irr_pct: Optional[float] = None,
        payback_y: Optional[float] = None,
        project_type: str = "FVE_BESS_hybrid",
    ) -> dict[str, BenchmarkComparison]:
        """Spusti všetky benchmarky pre projekt."""
        results: dict[str, BenchmarkComparison] = {}

        if annual_pv_kwh and installed_kwp:
            results["pv_yield"] = cls.pv_yield(annual_pv_kwh, installed_kwp, lokalita)
        if bess_capex_eur and bess_kwh:
            results["bess_capex"] = cls.bess_capex(bess_capex_eur, bess_kwh)
        if rte is not None:
            results["bess_rte"] = cls.bess_rte(rte, vyrobca)
        if lcos is not None:
            results["lcos"] = cls.lcos(lcos)
        if irr_pct is not None:
            results["irr"] = cls.irr(irr_pct, project_type)
        if payback_y is not None:
            results["payback"] = cls.payback(payback_y, project_type)

        return results

    @classmethod
    def summary_table(cls, results: dict[str, BenchmarkComparison]) -> str:
        """Vyformátuj výsledky ako čitateľnú tabuľku."""
        lines = [
            f"{'Metric':<35} {'Value':>12} {'Low':>10} {'Median':>10} {'High':>10}  {'Verdict':<15}",
            "-" * 105,
        ]
        for name, comp in results.items():
            lines.append(
                f"{comp.metric:<35} {comp.project_value:>12.2f} "
                f"{comp.benchmark_low:>10.2f} {comp.benchmark_median:>10.2f} "
                f"{comp.benchmark_high:>10.2f}  {comp.verdict:<15}"
            )
            if comp.note:
                lines.append(f"    ⚠ {comp.note}")
        return "\n".join(lines)
