"""TimeSeriesData — zjednotená reprezentácia časových rád.

Wrapper okolo pandas DataFrame s timezone-aware timestampami (Europe/Bratislava),
explicitnou granularitou (15-min alebo 60-min) a metadata.

Použitie:
    >>> ts = TimeSeriesData.from_csv(
    ...     "load.csv", timestamp_col="datetime", value_col="kw",
    ...     granularity_min=15, tz="Europe/Bratislava",
    ... )
    >>> ts.annual_sum_kwh()
    312456.7
    >>> ts_hourly = ts.resample(60)  # 15-min → 60-min
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional, Union

import numpy as np
import pandas as pd


class TimeSeriesData:
    """Časová rada s timezone, granularitou a validáciou."""

    def __init__(
        self,
        timestamps: list[datetime],
        values: list[float],
        granularity_min: Literal[15, 60],
        column_name: str = "value",
        unit: str = "kW",
        tz: str = "Europe/Bratislava",
        source: str = "manual",
    ) -> None:
        if len(timestamps) != len(values):
            raise ValueError(
                f"timestamps ({len(timestamps)}) a values ({len(values)}) musia mať rovnakú dĺžku"
            )
        if granularity_min not in (15, 60):
            raise ValueError(f"granularity_min musí byť 15 alebo 60, dostal: {granularity_min}")

        self.granularity_min = granularity_min
        self.column_name = column_name
        self.unit = unit
        self.tz = tz
        self.source = source

        # Build DataFrame s timezone-aware index
        df = pd.DataFrame({column_name: values}, index=pd.DatetimeIndex(timestamps))
        if df.index.tz is None:
            df.index = df.index.tz_localize(tz, ambiguous="infer", nonexistent="shift_forward")
        else:
            df.index = df.index.tz_convert(tz)
        self._df = df.sort_index()

    # ------------------------------------------------------------------ Factories
    @classmethod
    def from_csv(
        cls,
        path: Union[str, Path],
        timestamp_col: str,
        value_col: str,
        granularity_min: Literal[15, 60],
        tz: str = "Europe/Bratislava",
        unit: str = "kW",
        source: str = "csv",
        **read_csv_kwargs: object,
    ) -> "TimeSeriesData":
        """Načítaj z CSV — najjednoduchší vstup."""
        df = pd.read_csv(path, **read_csv_kwargs)  # type: ignore[arg-type]
        if timestamp_col not in df.columns:
            raise ValueError(f"Stĺpec '{timestamp_col}' neexistuje. Dostupné: {list(df.columns)}")
        if value_col not in df.columns:
            raise ValueError(f"Stĺpec '{value_col}' neexistuje. Dostupné: {list(df.columns)}")

        timestamps = pd.to_datetime(df[timestamp_col]).tolist()
        values = df[value_col].astype(float).tolist()
        return cls(
            timestamps=timestamps,
            values=values,
            granularity_min=granularity_min,
            column_name=value_col,
            unit=unit,
            tz=tz,
            source=source,
        )

    @classmethod
    def from_dataframe(
        cls,
        df: pd.DataFrame,
        value_col: str,
        granularity_min: Literal[15, 60],
        tz: str = "Europe/Bratislava",
        unit: str = "kW",
        source: str = "dataframe",
    ) -> "TimeSeriesData":
        """Načítaj z existujúceho DataFrame (predpoklad: index je timestamp)."""
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("DataFrame musí mať DatetimeIndex")
        return cls(
            timestamps=df.index.tolist(),
            values=df[value_col].astype(float).tolist(),
            granularity_min=granularity_min,
            column_name=value_col,
            unit=unit,
            tz=tz,
            source=source,
        )

    # ------------------------------------------------------------------ Aggregations
    @property
    def df(self) -> pd.DataFrame:
        """Access to underlying DataFrame (read-only intent)."""
        return self._df

    @property
    def values(self) -> np.ndarray:
        return self._df[self.column_name].to_numpy()

    @property
    def n_steps(self) -> int:
        return len(self._df)

    @property
    def dt_hours(self) -> float:
        """Časový krok v hodinách (0.25 pre 15-min, 1.0 pre 60-min)."""
        return self.granularity_min / 60.0

    def annual_sum_kwh(self) -> float:
        """Suma kW × Δt → kWh za rok."""
        if self.unit != "kW":
            raise ValueError(f"annual_sum_kwh predpokladá unit='kW', dostal: {self.unit}")
        return float(self._df[self.column_name].sum()) * self.dt_hours

    def annual_max_kw(self) -> float:
        return float(self._df[self.column_name].max())

    def annual_min_kw(self) -> float:
        return float(self._df[self.column_name].min())

    def annual_mean_kw(self) -> float:
        return float(self._df[self.column_name].mean())

    def quarterly_max_kw(self) -> pd.Series:
        """Maximum per 1/4-h — relevantné pre VN MRK fakturáciu."""
        if self.granularity_min == 15:
            return self._df[self.column_name].copy()
        # Pre hodinovú granularitu — vrátime hodinové (nie skutočné 1/4-h)
        return self._df[self.column_name].copy()

    def monthly_max_kw(self) -> pd.Series:
        """Mesačné maximum 1/4-h — základ MRK kapacitnej fakturácie pre VN."""
        return self._df[self.column_name].resample("MS").max()

    # ------------------------------------------------------------------ Transforms
    def resample(self, target_granularity_min: Literal[15, 60]) -> "TimeSeriesData":
        """Zmena granularity (15→60 mean, 60→15 forward fill)."""
        if target_granularity_min == self.granularity_min:
            return self

        rule = f"{target_granularity_min}min"
        if target_granularity_min > self.granularity_min:
            # Downsample (15→60): priemer
            new_df = self._df.resample(rule).mean()
        else:
            # Upsample (60→15): forward fill
            new_df = self._df.resample(rule).ffill()

        return TimeSeriesData.from_dataframe(
            new_df,
            value_col=self.column_name,
            granularity_min=target_granularity_min,
            tz=self.tz,
            unit=self.unit,
            source=f"{self.source}+resample",
        )

    # ------------------------------------------------------------------ Diagnostics
    def has_gaps(self, tolerance_steps: int = 0) -> bool:
        """Detekuj časové medzery v rade."""
        expected_freq = pd.Timedelta(minutes=self.granularity_min)
        diffs = self._df.index.to_series().diff().dropna()
        return bool((diffs > expected_freq * (1 + tolerance_steps)).any())

    def gap_count(self) -> int:
        expected_freq = pd.Timedelta(minutes=self.granularity_min)
        diffs = self._df.index.to_series().diff().dropna()
        return int((diffs > expected_freq).sum())

    def n_outliers_iqr(self, k: float = 3.0) -> int:
        """Počet outlierov (Tukey k×IQR)."""
        q1, q3 = self._df[self.column_name].quantile([0.25, 0.75])
        iqr = q3 - q1
        lo, hi = q1 - k * iqr, q3 + k * iqr
        return int(((self._df[self.column_name] < lo) | (self._df[self.column_name] > hi)).sum())

    def summary(self) -> dict[str, object]:
        return {
            "n_steps": self.n_steps,
            "granularity_min": self.granularity_min,
            "tz": self.tz,
            "source": self.source,
            "start": str(self._df.index[0]),
            "end": str(self._df.index[-1]),
            "annual_sum_kwh": self.annual_sum_kwh() if self.unit == "kW" else None,
            "max": self.annual_max_kw(),
            "min": self.annual_min_kw(),
            "mean": self.annual_mean_kw(),
            "has_gaps": self.has_gaps(),
            "gap_count": self.gap_count(),
            "n_outliers_iqr_3x": self.n_outliers_iqr(),
        }

    def __repr__(self) -> str:
        return (
            f"TimeSeriesData({self.column_name}, n={self.n_steps}, "
            f"granularity={self.granularity_min}min, source={self.source!r})"
        )

    def __len__(self) -> int:
        return self.n_steps
