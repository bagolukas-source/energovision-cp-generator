"""Huawei FusionSolar / SmartPVMS Cloud API adapter.

Endpointy overené z existujúceho huawei_spot.py na Renderi:
- /thirdData/login                         POST  → XSRF-TOKEN
- /thirdData/stations                      POST  → zoznam staníc
- /thirdData/getStationRealKpi             POST  → realtime batch
- /thirdData/getDevList                    POST  → zoznam meničov per station
- /thirdData/getDevRealKpi                 POST  → per-inverter realtime
- /thirdData/getDevHistoryKpi              POST  → historical
- /thirdData/getAlarmList                  POST  → alarmy
- /thirdData/openapi/pn-coffee/v1/sendCommand POST → příkazy (vyžaduje SP scope!)

Huawei region instances: intl.fusionsolar (default), eu5.fusionsolar (EU),
neeu5.fusionsolar (severná Európa). Pre Energovision typicky `intl` alebo `eu5`.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from .base import VendorAdapter, AuthError, NotAuthorizedError, VendorError
from .canonical import (
    PlantInfo, TelemetrySnapshot, DailySummary, VendorAlarm,
    map_severity, classify_alarm_category,
)


log = logging.getLogger(__name__)


class HuaweiAdapter(VendorAdapter):
    vendor = "huawei"
    api_base = os.environ.get("HUAWEI_API_BASE", "https://intl.fusionsolar.huawei.com")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._xsrf_token: Optional[str] = None

    # -------------------------------------------------------------------------

    def login(self) -> None:
        url = f"{self.api_base}/thirdData/login"
        r = self.session.post(
            url,
            json={"userName": self.username, "systemCode": self.password},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise AuthError(f"Huawei login HTTP {r.status_code}: {r.text[:200]}")
        token = r.headers.get("xsrf-token")
        if not token:
            data = r.json()
            raise AuthError(f"Huawei login no token: failCode={data.get('failCode')} msg={data.get('message')}")
        self._xsrf_token = token
        self._token = token
        # Huawei tokeny zvyčajne 30 min — refresh aspoň každých 25 min
        self._token_expires_at = (datetime.utcnow() + timedelta(minutes=25)).timestamp()

    def _huawei_post(self, path: str, body: dict) -> dict:
        """Huawei používa XSRF-TOKEN header namiesto Authorization."""
        self._ensure_token()
        url = f"{self.api_base}{path}"
        r = self.session.post(
            url,
            json=body,
            headers={"XSRF-TOKEN": self._xsrf_token, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise VendorError(f"Huawei {path} HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        # Huawei vracia úspech ako success=true
        if not data.get("success", False):
            fail_code = data.get("failCode")
            msg = data.get("message", "")
            if fail_code in (305, 401, 403):
                raise AuthError(f"Huawei {path} auth: failCode={fail_code} {msg}")
            if "APIG.0101" in str(msg) or "API not published" in str(msg):
                raise NotAuthorizedError(f"Huawei {path}: Service Provider scope missing")
            raise VendorError(f"Huawei {path} failCode={fail_code}: {msg}")
        return data.get("data") or data

    # -------------------------------------------------------------------------

    def fetch_plant_list(self) -> list[PlantInfo]:
        # Endpoint vracia stránkovane — pre 400 staníc treba prejsť pageNo
        all_plants: list[PlantInfo] = []
        page = 1
        while True:
            data = self._huawei_post("/thirdData/stations", {"pageNo": page, "pageSize": 100})
            items = data.get("list") if isinstance(data, dict) else data
            if not items:
                break
            for it in items:
                all_plants.append(PlantInfo(
                    vendor="huawei",
                    vendor_plant_id=str(it.get("plantCode") or it.get("stationCode")),
                    site_name=it.get("plantName") or it.get("stationName") or "",
                    kw_dc_nominal=float(it["capacity"]) if it.get("capacity") else None,
                    address=it.get("plantAddress") or it.get("stationAddr"),
                    lat=float(it["latitude"]) if it.get("latitude") else None,
                    lon=float(it["longitude"]) if it.get("longitude") else None,
                    timezone="Europe/Bratislava",
                    raw=it,
                ))
            if len(items) < 100:
                break
            page += 1
        return all_plants

    def fetch_realtime_batch(self, plant_ids: list[str]) -> list[TelemetrySnapshot]:
        """Huawei API podporuje max 100 staníc na request."""
        snapshots: list[TelemetrySnapshot] = []
        if not plant_ids:
            return snapshots
        for chunk_start in range(0, len(plant_ids), 100):
            chunk = plant_ids[chunk_start:chunk_start + 100]
            data = self._huawei_post(
                "/thirdData/getStationRealKpi",
                {"stationCodes": ",".join(chunk)},
            )
            for row in (data if isinstance(data, list) else data.get("list", [])):
                kpi = row.get("dataItemMap") or row
                snapshots.append(TelemetrySnapshot(
                    vendor_plant_id=str(row.get("stationCode")),
                    ts=datetime.now(timezone.utc),  # Huawei realKpi nemá explicit timestamp
                    ac_power_kw=_to_float(kpi.get("real_health_state")) or _to_float(kpi.get("day_power")),
                    ac_energy_today_kwh=_to_float(kpi.get("day_power")),
                    ac_energy_total_kwh=_to_float(kpi.get("total_power")),
                    raw_payload=row,
                ))
        return snapshots

    def fetch_daily_summary(self, plant_id: str, day: date) -> Optional[DailySummary]:
        # collectTime musí byť UTC ms timestamp začiatku dňa
        collect_ms = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
        data = self._huawei_post(
            "/thirdData/getKpiStationDay",
            {"stationCodes": plant_id, "collectTime": collect_ms},
        )
        if not data:
            return None
        row = data[0] if isinstance(data, list) else data
        kpi = row.get("dataItemMap") or row
        return DailySummary(
            vendor_plant_id=plant_id,
            day=day,
            energy_kwh=_to_float(kpi.get("inverter_power")),
            grid_export_kwh=_to_float(kpi.get("ongrid_power")),
            self_consumption_kwh=_to_float(kpi.get("self_use_power")),
            co2_avoided_kg=_to_float(kpi.get("reduction_total_co2")),
            raw=row,
        )

    def fetch_alarms(self, since: Optional[datetime] = None) -> list[VendorAlarm]:
        body = {"pageNo": 1, "pageSize": 100}
        if since:
            body["beginTime"] = int(since.timestamp() * 1000)
            body["endTime"] = int(datetime.now(timezone.utc).timestamp() * 1000)
        data = self._huawei_post("/thirdData/getAlarmList", body)
        out: list[VendorAlarm] = []
        for it in data.get("list", []) if isinstance(data, dict) else []:
            title = it.get("alarmName") or it.get("causeId") or "Huawei alarm"
            out.append(VendorAlarm(
                vendor="huawei",
                vendor_plant_id=str(it.get("stationCode")),
                vendor_alarm_id=str(it.get("alarmId") or it.get("alarmSn")),
                severity=map_severity("huawei", str(it.get("level", "3"))),
                category=classify_alarm_category(title, it.get("repairSuggestion", "")),
                title=title,
                description=it.get("repairSuggestion"),
                detected_at=_huawei_ts(it.get("raiseTime")),
                resolved_at=_huawei_ts(it.get("recoverTime")),
                raw=it,
            ))
        return out

    def send_command(self, plant_id: str, command: str, params: dict) -> dict:
        """Pošle príkaz cez Huawei /sendCommand endpoint.

        UPOZORNENIE: vyžaduje Service Provider scope. Bez neho vráti
        APIG.0101 → NotAuthorizedError.
        """
        body = {
            "stationCode": plant_id,
            "command": command,
            **params,
        }
        return self._huawei_post("/thirdData/openapi/pn-coffee/v1/sendCommand", body)


# =============================================================================
# Helpers
# =============================================================================

def _to_float(v) -> Optional[float]:
    if v is None or v == "" or v == "N/A":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _huawei_ts(ms) -> Optional[datetime]:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except Exception:
        return None
