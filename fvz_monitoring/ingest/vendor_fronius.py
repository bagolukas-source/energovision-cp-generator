"""Fronius Solar.web API v2 adapter.

Fronius používa **OAuth2 + AccessKey** kombináciu:
- Header `Authorization: Bearer <oauth_token>` + `AccessKeyId` + `AccessKeyValue`
- POST  /oauth2/token                       → access token
- GET   /swqapi/pvsystems-list              → zoznam PV systémov pod kontom
- GET   /swqapi/pvsystems/{id}/aggdata      → realtime/aggregated dáta
- GET   /swqapi/pvsystems/{id}/devices      → meniče + dataloggery
- GET   /swqapi/pvsystems/{id}/messages     → alarmy + udalosti

Fronius Service Provider účet vidí všetkých svojich klientov.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from .base import VendorAdapter, AuthError, VendorError
from .canonical import (
    PlantInfo, TelemetrySnapshot, DailySummary, VendorAlarm,
    map_severity, classify_alarm_category,
)


log = logging.getLogger(__name__)


class FroniusAdapter(VendorAdapter):
    vendor = "fronius"
    api_base = os.environ.get("FRONIUS_API_BASE", "https://api.solarweb.com")

    def __init__(self, **kwargs):
        self.access_key_id = kwargs.pop("access_key_id", os.environ.get("FRONIUS_ACCESS_KEY_ID"))
        self.access_key_value = kwargs.pop("access_key_value", os.environ.get("FRONIUS_ACCESS_KEY_VALUE"))
        super().__init__(**kwargs)

    def login(self) -> None:
        url = f"{self.api_base}/oauth2/token"
        r = self.session.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.api_secret,
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise AuthError(f"Fronius login HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        self._token = data.get("access_token")
        if not self._token:
            raise AuthError(f"Fronius login no token: {data}")
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires_at = (datetime.utcnow() + timedelta(seconds=expires_in - 60)).timestamp()

    def _fronius_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "AccessKeyId": self.access_key_id,
            "AccessKeyValue": self.access_key_value,
            "Accept": "application/json",
        }

    def _fronius_get(self, path: str, params: dict = None) -> dict:
        self._ensure_token()
        url = f"{self.api_base}{path}"
        r = self.session.get(url, params=params or {}, headers=self._fronius_headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise VendorError(f"Fronius {path} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def fetch_plant_list(self) -> list[PlantInfo]:
        all_plants: list[PlantInfo] = []
        # Fronius používa offset / limit pagination
        offset = 0
        limit = 200
        while True:
            data = self._fronius_get("/swqapi/pvsystems-list", {"offset": offset, "limit": limit})
            items = data.get("pvSystems", [])
            if not items:
                break
            for it in items:
                addr = it.get("address", {}) or {}
                all_plants.append(PlantInfo(
                    vendor="fronius",
                    vendor_plant_code=str(it.get("pvSystemId")),
                    site_name=it.get("name") or "",
                    kw_dc_nominal=_to_float(it.get("peakPower")) and _to_float(it["peakPower"]) / 1000.0,  # Wp → kWp
                    lat=_to_float((it.get("location") or {}).get("latitude")),
                    lon=_to_float((it.get("location") or {}).get("longitude")),
                    address=", ".join(filter(None, [addr.get("street"), addr.get("city"), addr.get("country")])),
                    timezone=it.get("timeZone") or "Europe/Bratislava",
                    raw=it,
                ))
            if len(items) < limit:
                break
            offset += limit
        return all_plants

    def fetch_realtime_batch(self, plant_ids: list[str]) -> list[TelemetrySnapshot]:
        snapshots: list[TelemetrySnapshot] = []
        # Fronius nemá batch endpoint pre realtime — per pvSystem
        for pid in plant_ids:
            try:
                data = self._fronius_get(
                    f"/swqapi/pvsystems/{pid}/aggdata",
                    {"channel": "PowerProduction,EnergyProductionDay,EnergyProductionTotal,SOC"},
                )
                channels = {ch["channelName"]: ch.get("value") for ch in data.get("data", [])}
                snapshots.append(TelemetrySnapshot(
                    vendor_plant_code=pid,
                    ts=datetime.now(timezone.utc),
                    ac_power_kw=_to_float(channels.get("PowerProduction")) and _to_float(channels["PowerProduction"]) / 1000.0,
                    ac_energy_today_kwh=_to_float(channels.get("EnergyProductionDay")) and _to_float(channels["EnergyProductionDay"]) / 1000.0,
                    ac_energy_total_kwh=_to_float(channels.get("EnergyProductionTotal")) and _to_float(channels["EnergyProductionTotal"]) / 1000.0,
                    battery_soc_pct=_to_float(channels.get("SOC")),
                    raw_payload=data,
                ))
            except VendorError as e:
                log.warning(f"Fronius realtime fail for {pid}: {e}")
        return snapshots

    def fetch_daily_summary(self, plant_id: str, day: date) -> Optional[DailySummary]:
        data = self._fronius_get(
            f"/swqapi/pvsystems/{plant_id}/aggdata",
            {
                "from": day.isoformat(),
                "to": day.isoformat(),
                "duration": "day",
                "channel": "EnergyProductionTotal,EnergyExported,EnergyImported",
            },
        )
        rows = data.get("data", [])
        if not rows:
            return None
        sums = {r["channelName"]: r.get("value") for r in rows}
        return DailySummary(
            vendor_plant_code=plant_id,
            day=day,
            energy_kwh=_to_float(sums.get("EnergyProductionTotal")) and _to_float(sums["EnergyProductionTotal"]) / 1000.0,
            grid_export_kwh=_to_float(sums.get("EnergyExported")) and _to_float(sums["EnergyExported"]) / 1000.0,
            grid_import_kwh=_to_float(sums.get("EnergyImported")) and _to_float(sums["EnergyImported"]) / 1000.0,
            raw=data,
        )

    def fetch_alarms(self, since: Optional[datetime] = None) -> list[VendorAlarm]:
        # Fronius messages endpoint vracia eventy a alarmy
        out: list[VendorAlarm] = []
        # Iterate all plants — neoptimálne ale Fronius nemá tenant-wide alarm endpoint
        # Pre prvú verziu túto časť zavoláme z orchestrátora per plant only on schedule
        # alebo cez Fronius webhook (preferovaný spôsob).
        # TODO: prepojiť na Fronius webhook → /webhook/fronius-alarms
        return out

    def send_command(self, plant_id: str, command: str, params: dict) -> dict:
        raise VendorError("Fronius Solar.web API does not support remote commands (only via local Modbus/Solar API)")


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
