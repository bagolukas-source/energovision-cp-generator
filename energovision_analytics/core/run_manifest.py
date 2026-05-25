"""Run manifest — embed-uje verziu enginu/tarify/spot do každého výstupu.

Použitie:
    manifest = build_run_manifest(
        site=site,
        tariff_yaml=Path("data/tariffs/2026.yaml"),
        spot_csv=Path("../sk_spot_2025_hourly.csv"),
    )
    print(manifest.footer_html())  # → "Engine 0.8.0 · tariff 2026 · spot 2025-12-31"
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from energovision_analytics._version import __version__


def _file_hash(path: Path) -> str:
    """SHA256 short hash (8 znakov)."""
    if not path.exists():
        return "missing"
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    return h


def _last_date_of_spot_csv(path: Path) -> Optional[str]:
    """Vyparsuje posledný timestamp z OKTE CSV."""
    if not path.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_csv(path)
        # Heuristika — nájdi timestamp stĺpec alebo predpokladá hodinový krok od 2025-01-01
        ts_col = next((c for c in df.columns if "time" in c.lower() or "datum" in c.lower()), None)
        if ts_col:
            return str(pd.to_datetime(df[ts_col]).max().date())
        # Fallback — počet riadkov × 1h
        n = len(df)
        if n == 8760:
            return "2025-12-31"
        return f"{n}h_data"
    except Exception:
        return "unparseable"


@dataclass
class RunManifest:
    """Snapshot prostredia v ktorom bol výstup vyrobený."""
    engine_version: str
    generated_at: str
    tariff_yaml_path: str
    tariff_hash: str
    tariff_year: int
    spot_csv_path: str
    spot_hash: str
    spot_last_date: Optional[str]
    economic_defaults_path: str
    economic_defaults_hash: str
    extra: dict = field(default_factory=dict)

    def footer_html(self) -> str:
        """1-riadkový footer pre HTML reporty."""
        return (
            f'<div style="font-size:10px;color:#888;text-align:right;padding:8px 16px;">'
            f"Engine v{self.engine_version} · "
            f"tariff {self.tariff_year}.yaml @ {self.tariff_hash} · "
            f"spot {self.spot_last_date or 'n/a'} @ {self.spot_hash} · "
            f"econ@{self.economic_defaults_hash} · "
            f"{self.generated_at}"
            f"</div>"
        )

    def footer_plain(self) -> str:
        """Plain text pre logging / CLI."""
        return (
            f"Engine v{self.engine_version} | "
            f"tariff {self.tariff_year}@{self.tariff_hash} | "
            f"spot {self.spot_last_date}@{self.spot_hash} | "
            f"econ@{self.economic_defaults_hash} | "
            f"generated {self.generated_at}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


def build_run_manifest(
    tariff_yaml: Path | str,
    spot_csv: Path | str,
    economic_defaults_yaml: Optional[Path | str] = None,
    tariff_year: int = 2026,
    extra: Optional[dict] = None,
) -> RunManifest:
    """Postaví manifest pre aktuálny run."""
    tariff_path = Path(tariff_yaml)
    spot_path = Path(spot_csv)
    econ_path = Path(economic_defaults_yaml) if economic_defaults_yaml else (
        Path(__file__).resolve().parents[3] / "data" / "config" / "economic_defaults.yaml"
    )

    return RunManifest(
        engine_version=__version__,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tariff_yaml_path=str(tariff_path),
        tariff_hash=_file_hash(tariff_path),
        tariff_year=tariff_year,
        spot_csv_path=str(spot_path),
        spot_hash=_file_hash(spot_path),
        spot_last_date=_last_date_of_spot_csv(spot_path),
        economic_defaults_path=str(econ_path),
        economic_defaults_hash=_file_hash(econ_path),
        extra=extra or {},
    )
