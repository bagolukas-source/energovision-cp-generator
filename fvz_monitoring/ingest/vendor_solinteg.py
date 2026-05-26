"""Solinteg iSolar Cloud API adapter.

Endpointy zo Solinteg developer docs (v1 API):
- POST  /v1/oauth2/token            → access_token (Bearer)
- POST  /v1/plant/list/byInstaller  → zoznam staníc pod installer účtom
- POST  /v1/plant/realtime          → realtime dáta batch
- POST  /v1/plant/dailyKpi          → denný kPI per plant
- POST  /v1/plant/alarmList         → alarmy
- POST  /v1/inverter/sendCommand    → príkazy (DC switch, power limit, atď.)

Solinteg API token TTL je typicky 2 hodiny — refresh proaktívne.
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


class SolintegAdapter(VendorAdapter):
    vendor = "solinteg"
    api_base = os.environ.get("SOLINTEG_API_BASE", "https://api.solinteg-cloud.com")

    def login(self) -> None:
        # OAuth2 client_credentials flow
        url = f"{self.api_base}/v1/oauth2/token"
        r = self.session.post(
            url,
            data={
                "grant_type": "password",
                "client_id": self.api_key,
                "client_secret": self.api_secret,
                "username": self.username,
                "password": self.password,
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise AuthError(f"Solinteg login HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise AuthError(f"Solinteg login no token: {data}")
        self._token = token
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = (datetime.utcnow() + timedelta(seconds=expires_in - 60)).timestamp()

    def fetch_plant_list(self) -> list[PlantInfo]:
        all_plants: list[PlantInfo] = []
        page = 1
        while True:
            data = self._request(
                "POST",
                "/v1/plant/list/byInstaller",
                json={"page": page, "pageSize": 200},
            )
            items = (data.get("data") or {}).get("list", [])
            if not items:
                break
            for it in items:
                all_plants.append(PlantInfo(
                    vendor="solinteg",
                    vendor_plant_id=str(it.get("plantId") or it.get("sid")),
                    site_name=it.get("plantName") or "",
                    kw_dc_nominal=_to_float(it.get("designedCapacity")) or _to_float(it.get("installedCapacity")),
                    battery_kwh_nominal=_to_float(it.get("batteryCapacity")),
                    lat=_to_float(it.get("latitude")),
                    lon=_to_float(it.get("longitude")),
                    address=it.get("address"),
                    timezone=it.get("timezone") or "Europe/Bratislava",
                    customer_email=it.get("ownerEmail"),
                    customer_name=it.get("ownerName"),
                    raw=it,
                ))
            if len(items) < 200:
                break
            page += 1
        return all_plants

    def fetch_realtime_batch(self, plant_ids: list[str]) -> list[TelemetrySnapshot]:
        snapshots: list[TelemetrySnapshot] = []
        if not plant_ids:
            return snapshots
        # Solinteg batch endpoint
        data = self._request(
            "POST",
            "/v1/plant/realtime",
            json={"plantIds": plant_ids},
        )
        items = (data.get("data") or {}).get("list", []) if isinstance(data, dict) else []
        for row in items:
            ts = _solinteg_ts(row.get("updateTime")) or datetime.now(timezone.utc)
            snapshots.append(TelemetrySnapshot(
                vendor_plant_id=str(row.get("plantId")),
                ts=ts,
                ac_power_kw=_to_float(row.get("realPower")) and _to_float(row["realPower"]) / 1000.0,
                ac_energy_today_kwh=_to_float(row.get("dailyEnergy")),
                ac_energy_total_kwh=_to_float(row.get("totalEnergy")),
                battery_soc_pct=_to_float(row.get("batterySoc")),
                battery_power_kw=_to_float(row.get("batteryPower")) and _to_float(row["batteryPower"]) / 1000.0,
                grid_export_kw=_to_float(row.get("gridExportPower")) and _to_float(row["gridExportPower"]) / 1000.0,
                grid_import_kw=_to_float(row.get("gridImportPower")) and _to_float(row["gridImportPower"]) / 1000.0,
                inverter_status=row.get("status"),
                raw_payload=row,
            ))
        return snapshots

    def fetch_daily_summary(self, plant_id: str, day: date) -> Optional[DailySummary]:
        data = self._request(
            "POST",
            "/v1/plant/dailyKpi",
            json={"plantId": plant_id, "date": day.isoformat()},
        )
        row = (data.get("data") or {}) if isinstance(data, dict) else {}
        if not row:
            return None
        return DailySummary(
            vendor_plant_id=plant_id,
            day=day,
            energy_kwh=_to_float(row.get("dailyEnergy")),
            peak_power_kw=_to_float(row.get("peakPower")) and _to_float(row["peakPower"]) / 1000.0,
            grid_export_kwh=_to_float(row.get("exportEnergy")),
            grid_import_kwh=_to_float(row.get("importEnergy")),
            self_consumption_kwh=_to_float(row.get("selfConsumptionEnergy")),
            raw=row,
        )

    def fetch_alarms(self, since: Optional[datetime] = None) -> list[VendorAlarm]:
        body = {"page": 1, "pageSize": 100}
        if since:
            body["startTime"] = since.strftime("%Y-%m-%d %H:%M:%S")
        data = self._request("POST", "/v1/plant/alarmList", json=body)
        out: list[VendorAlarm] = []
        for it in ((data.get("data") or {}).get("list", []) if isinstance(data, dict) else []):
            title = it.get("alarmName") or it.get("alarmCode") or "Solinteg alarm"
            out.append(VendorAlarm(
                vendor="solinteg",
                vendor_plant_id=str(it.get("plantId")),
                vendor_alarm_id=str(it.get("alarmId")),
                severity=map_severity("solinteg", str(it.get("level", "2"))),
                category=classify_alarm_category(title, it.get("description", "")),
                title=title,
                description=it.get("description"),
                detected_at=_solinteg_ts(it.get("startTime")),
                resolved_at=_solinteg_ts(it.get("endTime")),
                raw=it,
            ))
        return out

    def send_command(self, plant_id: str, command: str, params: dict) -> dict:
        return self._request(
            "POST",
            "/v1/inverter/sendCommand",
            json={"plantId": plant_id, "command": command, **params},
        )


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _solinteg_ts(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Formát "YYYY-MM-DD HH:MM:SS" v plant timezone
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None
