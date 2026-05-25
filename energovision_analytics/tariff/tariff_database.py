"""TariffEngine — YAML-backed tarif databáza s validáciou.

Načítava `data/tariffs/{rok}.yaml` a poskytuje typovaný prístup k tarifným sadzbám
pre konkrétny rok / distribútor / sadzbu.

Príklad:
    >>> engine = TariffEngine.from_yaml("data/tariffs/2026.yaml")
    >>> tariff = engine.get("SSE", "VN")
    >>> print(f"TPS: {tariff.tps_eur_mwh} €/MWh")
    TPS: 8.5 €/MWh
    >>> print(f"MRK export penalty: {tariff.mrk_export_penalty_eur_kwh*1000:.1f} €/MWh")
    MRK export penalty: 12.5 €/MWh
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from energovision_analytics.core.exceptions import TariffError
from energovision_analytics.core.models import Distribuutor, Sadzba, TariffYearInput
from energovision_analytics.tariff.spot_contract import SpotContract


class TariffEngine:
    """Manažér tarif — načíta YAML, vracia typované TariffYearInput objekty."""

    def __init__(
        self,
        rok: int,
        distribuutori_data: dict[str, dict[str, dict[str, Any]]],
        bilancne_skupiny_data: dict[str, dict[str, dict[str, Any]]],
        zdroj: str = "manual",
    ) -> None:
        self.rok = rok
        self.zdroj = zdroj
        self._tariffs: dict[tuple[str, str], TariffYearInput] = {}
        self._bilancne: dict[str, SpotContract] = {}

        # Parse distribútorov
        for dist_kod, dist_data in distribuutori_data.items():
            for sadzba_kod in ("NN", "VN"):
                if sadzba_kod in dist_data:
                    raw = dist_data[sadzba_kod]
                    tariff = TariffYearInput(
                        rok=rok,
                        distribuutor=Distribuutor(dist_kod),
                        sadzba=Sadzba(sadzba_kod),
                        **{k: v for k, v in raw.items() if k in TariffYearInput.model_fields},
                    )
                    self._tariffs[(dist_kod, sadzba_kod)] = tariff

        # Parse bilančných skupín
        for skupina_nazov, modes in bilancne_skupiny_data.items():
            self._bilancne[skupina_nazov] = SpotContract.from_yaml(skupina_nazov, modes)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "TariffEngine":
        """Načítaj z YAML súboru."""
        path = Path(path)
        if not path.exists():
            raise TariffError(f"Tariff YAML neexistuje: {path}")

        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if "rok" not in data:
            raise TariffError("YAML nemá field 'rok'")

        return cls(
            rok=data["rok"],
            distribuutori_data=data.get("distribuutori", {}),
            bilancne_skupiny_data=data.get("bilancne_skupiny", {}),
            zdroj=data.get("zdroj", str(path)),
        )

    # ------------------------------------------------------------------ Access
    def get(self, distribuutor: str | Distribuutor, sadzba: str | Sadzba) -> TariffYearInput:
        """Vráti TariffYearInput pre danú kombináciu."""
        d = distribuutor.value if isinstance(distribuutor, Distribuutor) else distribuutor
        s = sadzba.value if isinstance(sadzba, Sadzba) else sadzba
        key = (d, s)
        if key not in self._tariffs:
            available = list(self._tariffs.keys())
            raise TariffError(
                f"Tarify pre ({d}, {s}) v roku {self.rok} neexistujú. "
                f"Dostupné: {available}"
            )
        return self._tariffs[key]

    def get_bilancna_skupina(self, nazov: str) -> SpotContract:
        if nazov not in self._bilancne:
            raise TariffError(
                f"Bilančná skupina '{nazov}' v roku {self.rok} neexistuje. "
                f"Dostupné: {list(self._bilancne.keys())}"
            )
        return self._bilancne[nazov]

    def list_distribuutori(self) -> list[str]:
        return sorted({d for d, _ in self._tariffs})

    def list_bilancne_skupiny(self) -> list[str]:
        return sorted(self._bilancne)

    # ------------------------------------------------------------------ Validation
    def validate_consistency(self) -> list[str]:
        """Sanity check naprieč distribútormi — vráti list warningov."""
        warnings: list[str] = []

        # Skontroluj, či TPS je rovnaké naprieč distribútormi (regulačné)
        tps_vn = {(d, t.tps_eur_mwh) for (d, s), t in self._tariffs.items() if s == "VN"}
        if len({v for _, v in tps_vn}) > 1:
            warnings.append(f"VN TPS sa líši medzi distribútormi: {tps_vn} — má byť rovnaké")

        njf_all = {(d, s, t.njf_eur_mwh) for (d, s), t in self._tariffs.items()}
        if len({v for _, _, v in njf_all}) > 1:
            warnings.append(f"NJF sa líši: {njf_all} — má byť rovnaké celoSK")

        return warnings

    def __repr__(self) -> str:
        return (
            f"TariffEngine(rok={self.rok}, "
            f"distrib={self.list_distribuutori()}, "
            f"bs={self.list_bilancne_skupiny()})"
        )
