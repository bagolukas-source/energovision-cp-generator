"""Normalizer — zjednoteni vstupov na TimeSeriesData / LoadProfileInput.

Konverzie:
    - kWh → kW (delenie intervalom)
    - 60-min → 15-min (upsample forward fill)
    - DST handling (ambiguous, nonexistent timestamps)
    - Gap filling (linear interpolation pre < 1 % gapy)
"""
from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from energovision_analytics.core.exceptions import ValidationError
from energovision_analytics.core.models import LoadProfileInput
from energovision_analytics.core.time_series import TimeSeriesData


def normalize_load_profile(
    ts: TimeSeriesData,
    fill_gaps: bool = True,
    max_gap_pct: float = 1.0,
    target_granularity_min: Literal[15, 60] = 15,
) -> LoadProfileInput:
    """Konvertuj TimeSeriesData → validovaný LoadProfileInput.

    Args:
        ts: Vstupná časová rada
        fill_gaps: Doplniť malé medzery linearne (max_gap_pct)
        max_gap_pct: Maximálne % medzier — nad tým error
        target_granularity_min: Cieľová granularita

    Returns:
        LoadProfileInput pripravený pre simuláciu

    Raises:
        ValidationError: ak je príliš veľa gapov, alebo negatívne hodnoty
    """
    df = ts.df.copy()

    # 1) Skontroluj gaps
    expected_freq = pd.Timedelta(minutes=ts.granularity_min)
    full_range = pd.date_range(df.index[0], df.index[-1], freq=expected_freq, tz=ts.tz)
    n_expected = len(full_range)
    n_actual = len(df)
    gap_pct = (n_expected - n_actual) / n_expected * 100

    issues: list[dict] = []

    if gap_pct > max_gap_pct:
        raise ValidationError(
            f"Príliš veľa medzier v dátach: {gap_pct:.2f}% > {max_gap_pct}%. "
            f"Očakávaných {n_expected}, načítaných {n_actual}.",
            issues=[{
                "severity": "ERROR",
                "category": "data_completeness",
                "message": f"gap_pct={gap_pct:.2f}%",
            }],
        )

    if fill_gaps and gap_pct > 0:
        df = df.reindex(full_range)
        df[ts.column_name] = df[ts.column_name].interpolate(method="linear", limit_direction="both")
        issues.append({
            "severity": "WARNING",
            "category": "data_completeness",
            "message": f"Doplnené {n_expected - n_actual} medzier linearnou interpoláciou",
        })

    # 2) Skontroluj negatívne hodnoty (load nemôže byť záporný — okrem ak je tam aj PV export)
    if (df[ts.column_name] < 0).any():
        n_neg = int((df[ts.column_name] < 0).sum())
        issues.append({
            "severity": "WARNING",
            "category": "physical_limit",
            "message": f"{n_neg} záporných hodnôt v load — možno PV export v netto profile?",
        })

    # 3) Resample na cieľovú granularitu
    if ts.granularity_min != target_granularity_min:
        ts_resampled = TimeSeriesData.from_dataframe(
            df, value_col=ts.column_name, granularity_min=ts.granularity_min,
            tz=ts.tz, source=ts.source,
        ).resample(target_granularity_min)
        df = ts_resampled.df

    # 4) Vypočítaj ročnú spotrebu
    dt_h = target_granularity_min / 60.0
    annual_kwh = float(df[ts.column_name].sum()) * dt_h

    return LoadProfileInput(
        timestamps=df.index.to_pydatetime().tolist(),
        values_kw=df[ts.column_name].astype(float).tolist(),
        granularity_min=target_granularity_min,
        rocna_spotreba_kwh=annual_kwh,
        source=ts.source,
    )


def align_load_and_pv(
    load_ts: TimeSeriesData,
    pv_ts: TimeSeriesData,
    spot_ts: TimeSeriesData | None = None,
) -> pd.DataFrame:
    """Zarovnaj load, PV a (voliteľne) spot ceny do jedného DataFrame.

    Vykoná resample na spoločnú granularitu (najmenšia z troch),
    inner join na časový rozsah.
    """
    target_gran = min(load_ts.granularity_min, pv_ts.granularity_min)
    if spot_ts is not None:
        target_gran = min(target_gran, spot_ts.granularity_min)

    load = load_ts.resample(target_gran).df.rename(columns={load_ts.column_name: "load_kw"})
    pv = pv_ts.resample(target_gran).df.rename(columns={pv_ts.column_name: "pv_kw"})

    merged = load.join(pv, how="inner")
    if spot_ts is not None:
        spot = spot_ts.resample(target_gran).df.rename(columns={spot_ts.column_name: "spot_eur_mwh"})
        merged = merged.join(spot, how="inner")

    return merged
