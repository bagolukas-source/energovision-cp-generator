"""Sungrow iSolarCloud API adapter.

Sungrow OpenAPI v1:
- POST  /openapi/login                          → token (Bearer, TTL ~7200s)
- POST  /openapi/getPowerStationList            → zoznam staníc (installer účet)
- POST  /openapi/getPowerStationDetail          → detail jednej stanice
- POST  /openapi/getStationRealKpi              → realtime batch
- POST  /openapi/getDevicePoint                 → per-device data
- POST  /openapi/queryDeviceList                → meniče
- POST  /openapi/queryAlarmInfoList             → alarmy
- POST  /openapi/setDeviceParam                 → command (vyžaduje SP scope)

Sungrow EU region: https://gateway.isolarcloud.eu
Globálny: https://gateway.isolarcloud.com
"""

from __future__ import annotations

import os
import hashlib
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from .base import VendorAdapter, AuthError, VendorError
from .canonical import (
    PlantInfo, TelemetrySnapshot, DailySummary, VendorAlarm,
    map_severity, classify_alarm_category,
)


log = logging.getLogger(__name__)


class SungrowAdapter(VendorAdapter):
    vendor = "sungrow"
    api_base = os.environ.get("SUNGROW_API_BASE", "https://gateway.isolarcloud.eu")

    def login(self) -> None:
        # Sungrow vyžaduje appkey + password + username (heslo je MD5 hash)
        url = f"{self.api_base}/openapi/login"
        pwd_md5 = hashlib.md5(self.password.encode("utf-8")).hexdigest() if self.password else ""
        body = {
            "appkey": self.api_key,
            "user_account": self.username,
            "user_password": pwd_md5,
            "lang": "_en_US",
        }
        r = self.session.post(url, json=body, headers={"x-access-key": self.api_key, "Content-Type": "application/json"}, timeout=self.timeout)
        if r.status_code != 200:
            raise AuthError(f"Sungrow login HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("result_code") != "1":
            raise AuthError(f"Sungrow login failed: {data.get('result_msg')}")
        self._token = (data.get("result_data") or {}).get("token")
        if not self._token:
            raise AuthError(f"Sungrow login no token: {data}")
        # TTL 2 hodiny zvyčajne
        self._token_expires_at = (datetime.utcnow() + timedelta(seconds=7100)).timestamp()

    def _sungrow_post(self, path: str, body: dict) -> dict:
        self._ensure_token()
        url = f"{self.api_base}{path}"
        full_body = {"appkey": self.api_key, "token": self._token, **body}
        headers = {"x-access-key": self.api_key, "Content-Type": "application/json"}
        r = self.session.post(url, json=full_body, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise VendorError(f"Sungrow {path} HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("result_code") != "1":
            code = data.get("result_code")
            msg = data.get("result_msg", "")
            if code in ("1003", "1004"):  # token expired
                self._token = None
                self._ensure_token()
                return self._sungrow_post(path, body)
            raise VendorError(f"Sungrow {path} code={code}: {msg}")
        return data.get("result_data") or {}

    def fetch_plant_list(self) -> list[PlantInfo]:
        all_plants: list[PlantInfo] = []
        page = 1
        while True:
            data = self._sungrow_post(
                "/openapi/getPowerStationList",
                {"curPage": page, "size": 100},
            )
            items = data.get("pageList", []) if isinstance(data, dict) else []
            if not items:
                break
            for it in items:
                all_plants.append(PlantInfo(
                    vendor="sungrow",
                    vendor_plant_code=str(it.get("ps_id")),
                    site_name=it.get("ps_name") or "",
                    kw_dc_nominal=_to_float(it.get("design_capacity")),
                    lat=_to_float(it.get("latitude")),
                    lon=_to_float(it.get("longitude")),
                    address=it.get("ps_location"),
                    timezone="Europe/Bratislava",
                    raw=it,
                ))
            if len(items) < 100:
                break
            page += 1
        return all_plants

    def fetch_realtime_batch(self, plant_ids: list[str]) -> list[TelemetrySnapshot]:
        snapshots: list[TelemetrySnapshot] = []
        if not plant_ids:
            return snapshots
        # Sungrow batch endpoint vie 50 staníc naraz
        for chunk_start in range(0, len(plant_ids), 50):
            chunk = plant_ids[chunk_start:chunk_start + 50]
            data = self._sungrow_post(
                "/openapi/getStationRealKpi",
                {"ps_id_list": chunk},
            )
            for row in data.get("pageList", []) if isinstance(data, dict) else []:
                snapshots.append(TelemetrySnapshot(
                    vendor_plant_code=str(row.get("ps_id")),
                    ts=datetime.now(timezone.utc),
                    ac_power_kw=_to_float(row.get("p83022")) or _to_float(row.get("real_power")),  # p83022 = active power
                    ac_energy_today_kwh=_to_float(row.get("p83025")) or _to_float(row.get("today_energy")),
                    ac_energy_total_kwh=_to_float(row.get("p83102")) or _to_float(row.get("total_energy")),
                    raw_payload=row,
                ))
        return snapshots

    def fetch_daily_summary(self, plant_id: str, day: date) -> Optional[DailySummary]:
        data = self._sungrow_post(
            "/openapi/getPowerStationDay",
            {"ps_id": plant_id, "date_id": day.strftime("%Y%m%d")},
        )
        return DailySummary(
            vendor_plant_code=plant_id,
            day=day,
            energy_kwh=_to_float((data or {}).get("today_energy")),
            raw=data if isinstance(data, dict) else {},
        )

    def fetch_alarms(self, since: Optional[datetime] = None) -> list[VendorAlarm]:
        body = {"curPage": 1, "size": 100, "is_clear": 0}
        if since:
            body["start_time"] = since.strftime("%Y%m%d%H%M%S")
        data = self._sungrow_post("/openapi/queryAlarmInfoList", body)
        out: list[VendorAlarm] = []
        for it in data.get("pageList", []) if isinstance(data, dict) else []:
            title = it.get("alarm_name") or it.get("fault_name") or "Sungrow alarm"
            out.append(VendorAlarm(
                vendor="sungrow",
                vendor_plant_code=str(it.get("ps_id")),
                vendor_alarm_id=str(it.get("alarm_id") or it.get("uuid")),
                severity=map_severity("sungrow", str(it.get("alarm_level", "2"))),
                category=classify_alarm_category(title, it.get("alarm_remarks", "")),
                title=title,
                description=it.get("alarm_remarks"),
                detected_at=_sungrow_ts(it.get("alarm_begin_time")),
                resolved_at=_sungrow_ts(it.get("alarm_end_time")),
                raw=it,
            ))
        return out

    def send_command(self, plant_id: str, command: str, params: dict) -> dict:
        # Sungrow setDeviceParam vyžaduje device_id (uuid meniča) — najprv treba queryDeviceList
        return self._sungrow_post(
            "/openapi/setDeviceParam",
            {"ps_id": plant_id, "param": command, **params},
        )


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sungrow_ts(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None
