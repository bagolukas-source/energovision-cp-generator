"""OKTE klient — REST API pre Slovak Day-Ahead Market ceny.

API base: https://isot.okte.sk/api/v1/
Bez autentifikácie, public, free.

Kľúčové endpointy:
    GET /dam/results?deliveryDay=YYYY-MM-DD   → hodinové ceny + objemy
    GET /dam/indices?deliveryDay=YYYY-MM-DD   → denné indexy (base, peak, off-peak)

Implementácia s lokálnym parquet cache (data/okte_cache/year=YYYY/...).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from energovision_analytics.core.exceptions import DataIngestionError


class OKTEClient:
    """Klient pre OKTE Day-Ahead Market dáta s parquet cache."""

    BASE_URL = "https://isot.okte.sk/api/v1"
    TZ = "Europe/Bratislava"
    REQUEST_TIMEOUT_S = 15.0

    def __init__(
        self,
        cache_dir: str | Path = "data/okte_cache",
        session: Optional[requests.Session] = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "energovision-analytics/0.1"})

    # ------------------------------------------------------------------ Cache
    def _cache_path(self, year: int) -> Path:
        return self.cache_dir / f"okte_dam_{year}.parquet"

    def _load_cached(self, year: int) -> Optional[pd.DataFrame]:
        path = self._cache_path(year)
        if path.exists():
            return pd.read_parquet(path)
        return None

    def _save_cache(self, year: int, df: pd.DataFrame) -> None:
        path = self._cache_path(year)
        df.to_parquet(path, compression="snappy")

    # ------------------------------------------------------------------ API
    def _fetch_day(self, delivery_day: date) -> pd.DataFrame:
        """Stiahni hodinové ceny pre jeden deň."""
        url = f"{self.BASE_URL}/dam/results"
        params = {"deliveryDay": delivery_day.isoformat()}
        try:
            r = self.session.get(url, params=params, timeout=self.REQUEST_TIMEOUT_S)
            r.raise_for_status()
        except requests.RequestException as e:
            raise DataIngestionError(f"OKTE API request failed for {delivery_day}: {e}") from e

        data = r.json()
        if "results" not in data and "hours" not in data and not isinstance(data, list):
            raise DataIngestionError(
                f"Neočakávaný OKTE response format pre {delivery_day}. Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
            )

        # API formát sa môže meniť — robíme tolerantne
        rows = data.get("results") or data.get("hours") or (data if isinstance(data, list) else [])
        records = []
        for h_idx, row in enumerate(rows):
            # Tolerant key matching
            price = row.get("price") or row.get("priceEur") or row.get("priceEurMwh")
            hour = row.get("hour") or row.get("deliveryHour") or h_idx + 1
            ts = datetime.combine(delivery_day, datetime.min.time()) + timedelta(hours=int(hour) - 1)
            records.append({
                "timestamp_local": ts,
                "delivery_day": delivery_day,
                "hour": int(hour),
                "price_eur_mwh": float(price),
            })

        df = pd.DataFrame(records)
        return df

    # ------------------------------------------------------------------ Public
    def fetch_year(self, year: int, force_refresh: bool = False) -> pd.DataFrame:
        """Stiahni / načítaj z cache celý rok.

        Returns:
            DataFrame s columnami: timestamp_local (tz-aware), delivery_day,
            hour, price_eur_mwh.
        """
        if not force_refresh:
            cached = self._load_cached(year)
            if cached is not None:
                return cached

        start = date(year, 1, 1)
        end = date(year, 12, 31)
        all_dfs = []
        current = start
        while current <= end:
            try:
                day_df = self._fetch_day(current)
                all_dfs.append(day_df)
            except DataIngestionError as e:
                # Skip missing days but warn
                print(f"⚠ OKTE: skip {current} ({e})")
            current += timedelta(days=1)

        if not all_dfs:
            raise DataIngestionError(f"OKTE: žiadne dáta pre rok {year}")

        df = pd.concat(all_dfs, ignore_index=True)
        df["timestamp_local"] = pd.to_datetime(df["timestamp_local"]).dt.tz_localize(
            self.TZ, ambiguous="infer", nonexistent="shift_forward"
        )
        df = df.sort_values("timestamp_local").reset_index(drop=True)
        self._save_cache(year, df)
        return df

    def load_from_csv(self, path: str | Path,
                       timestamp_col: str = "datetime_local_cet",
                       price_col: str = "price_eur_per_mwh") -> pd.DataFrame:
        """Načítaj OKTE ceny z existujúceho CSV (napr. sk_spot_2025_hourly.csv).

        Fallback pre prípad, že nemáme prístup k API alebo chceme použiť
        už pripravený dataset.
        """
        path = Path(path)
        if not path.exists():
            raise DataIngestionError(f"CSV neexistuje: {path}")

        df = pd.read_csv(path)
        if timestamp_col not in df.columns:
            raise DataIngestionError(f"Stĺpec {timestamp_col!r} neexistuje. Dostupné: {list(df.columns)}")
        if price_col not in df.columns:
            raise DataIngestionError(f"Stĺpec {price_col!r} neexistuje. Dostupné: {list(df.columns)}")

        df = df.rename(columns={timestamp_col: "timestamp_local", price_col: "price_eur_mwh"})
        df["timestamp_local"] = pd.to_datetime(df["timestamp_local"])
        if df["timestamp_local"].dt.tz is None:
            # OKTE DAM CSV typicky obsahuje 8760 hodín v fixnom UTC+1 (CET, žiadny DST).
            # Ak je počet hodín = 8760, ide o fixed offset; inak DST-aware.
            n_hours = len(df)
            if n_hours == 8760:
                # Fixed CET (UTC+1) — žiadny DST
                df["timestamp_local"] = df["timestamp_local"].dt.tz_localize("Etc/GMT-1")
            else:
                df["timestamp_local"] = df["timestamp_local"].dt.tz_localize(
                    self.TZ, ambiguous="NaT", nonexistent="shift_forward"
                )
                # Drop riadky bez timezone (DST ambiguous)
                df = df.dropna(subset=["timestamp_local"]).reset_index(drop=True)
        df["delivery_day"] = df["timestamp_local"].dt.date
        df["hour"] = df["timestamp_local"].dt.hour + 1
        return df[["timestamp_local", "delivery_day", "hour", "price_eur_mwh"]].sort_values("timestamp_local").reset_index(drop=True)

    @staticmethod
    def annual_statistics(df: pd.DataFrame) -> dict[str, float]:
        """Vráti štatistiky pre rok — užitočné pre report."""
        prices = df["price_eur_mwh"]
        return {
            "n_hours": int(len(prices)),
            "mean_eur_mwh": float(prices.mean()),
            "median_eur_mwh": float(prices.median()),
            "stdev_eur_mwh": float(prices.std()),
            "min_eur_mwh": float(prices.min()),
            "max_eur_mwh": float(prices.max()),
            "p10_eur_mwh": float(prices.quantile(0.10)),
            "p90_eur_mwh": float(prices.quantile(0.90)),
            "spread_p90_p10_eur_mwh": float(prices.quantile(0.90) - prices.quantile(0.10)),
            "negative_hours": int((prices < 0).sum()),
            "negative_hours_pct": float((prices < 0).mean() * 100),
            "very_high_hours_over_300": int((prices > 300).sum()),
        }
