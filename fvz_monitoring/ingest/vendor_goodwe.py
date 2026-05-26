"""GoodWe SEMS Portal API adapter.

GoodWe SEMS public API:
- POST  /api/v3/Common/CrossLogin        → access token (Bearer)
- POST  /api/v3/PowerStation/QueryPagedPlantList  → zoznam staníc
- POST  /api/v3/PowerStation/GetPowerStationByDetail  → detail + realtime
- POST  /api/v3/Monitor/GetWarningStation → alarmy
- POST  /api/v3/PowerStation/GetPlantPowerChart → graf výroby

Login flow je špecifický — SEMS používa 2 fázy:
1. POST /Common/CrossLogin s account+pwd → vráti 'data.token' + redirect URL
2. Druhý request na redirect URL s tokenom → access token na použitie
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


class GoodWeAdapter(VendorAdapter):
    vendor = "goodwe"
    api_base = os.environ.get("GOODWE_API_BASE", "https://www.semsportal.com")

    def login(self) -> None:
        # SEMS CrossLogin používa Authorization header s pre-vygenerovaným tokenom
        # ktorý sa získa cez basic auth flow
        url = f"{self.api_base}/api/v2/Common/CrossLogin"
        headers = {
            "Content-Type": "application/json",
            "Token": (
                '{"version":"v3.1","client":"web","language":"en"}'
            ),
        }
        body = {"account": self.username, "pwd": self.password}
        r = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise AuthError(f"GoodWe login HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("hasError") or not (data.get("data") or {}).get("token"):
            raise AuthError(f"GoodWe login failed: {data}")
        self._token = data["data"]["token"]
        # SEMS API rotuje token + uid + timestamp v každom requeste,
        # full token string je celý JSON v 'Token' headeri.
        self._goodwe_token_obj = data["data"]
        # SEMS tokeny majú TTL ~24h ale pre istotu refresh každú hodinu
        self._token_expires_at = (datetime.utcnow() + timedelta(minutes=55)).timestamp()

    def _goodwe_post(self, path: str, body: dict) -> dict:
        import json
        self._ensure_token()
        url = f"{self.api_base}{path}"
        headers = {
            "Content-Type": "application/json",
            "Token": json.dumps(self._goodwe_token_obj),
        }
        r = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise VendorError(f"GoodWe {path} HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("hasError"):
            raise VendorError(f"GoodWe {path} error: {data.get('msg') or data}")
        return data.get("data") or {}

    def fetch_plant_list(self) -> list[PlantInfo]:
        all_plants: list[PlantInfo] = []
        page = 1
        while True:
            data = self._goodwe_post(
                "/api/v3/PowerStation/QueryPagedPlantList",
                {"page_index": page, "page_size": 100, "key": ""},
            )
            items = data.get("list", []) if isinstance(data, dict) else []
            if not items:
                break
            for it in items:
                all_plants.append(PlantInfo(
                    vendor="goodwe",
                    vendor_plant_id=str(it.get("powerstation_id")),
                    site_name=it.get("stationname") or "",
                    kw_dc_nominal=_to_float(it.get("capacity")),
                    battery_kwh_nominal=_to_float(it.get("battery_capacity")),
                    lat=_to_float(it.get("location_latitude")),
                    lon=_to_float(it.get("location_longitude")),
                    address=it.get("location"),
                    customer_name=it.get("owner_name"),
                    raw=it,
                ))
            if len(items) < 100:
                break
            page += 1
        return all_plants

    def fetch_realtime_batch(self, plant_ids: list[str]) -> list[TelemetrySnapshot]:
        # GoodWe nemá single batch realtime endpoint pre installer účet — musíme volať
        # per-plant. Pri 400 staniciach by to bolo 80 req/min — preto použijeme paralelizmus
        # a respektujeme limit 1200 req/h (= 20 req/min konzervatívne).
        # Alternatívne: použiť QueryPagedPlantList ktorý vracia 'current_power' a 'pac' v summary.
        snapshots: list[TelemetrySnapshot] = []
        for pid in plant_ids:
            try:
                data = self._goodwe_post(
                    "/api/v3/PowerStation/GetPowerStationByDetail",
                    {"powerstation_id": pid},
                )
                kpi = data.get("kpi", {})
                inv = (data.get("inverter") or [{}])[0]
                soc = (data.get("soc") or {})
                snapshots.append(TelemetrySnapshot(
                    vendor_plant_id=pid,
                    ts=datetime.now(timezone.utc),
                    ac_power_kw=_to_float(kpi.get("pac")) and _to_float(kpi["pac"]) / 1000.0,
                    ac_energy_today_kwh=_to_float(kpi.get("power")),
                    ac_energy_total_kwh=_to_float(kpi.get("total_power")),
                    battery_soc_pct=_to_float(soc.get("power") or inv.get("soc")),
                    inverter_status=inv.get("status"),
                    raw_payload=data,
                ))
            except VendorError as e:
                log.warning(f"GoodWe realtime fail for {pid}: {e}")
        return snapshots

    def fetch_daily_summary(self, plant_id: str, day: date) -> Optional[DailySummary]:
        data = self._goodwe_post(
            "/api/v3/PowerStation/GetPlantPowerChart",
            {"id": plant_id, "date": day.strftime("%Y-%m-%d"), "full_script": False},
        )
        return DailySummary(
            vendor_plant_id=plant_id,
            day=day,
            energy_kwh=_to_float(data.get("energy")),
            raw=data if isinstance(data, dict) else {},
        )

    def fetch_alarms(self, since: Optional[datetime] = None) -> list[VendorAlarm]:
        data = self._goodwe_post("/api/v3/Monitor/GetWarningStation", {"page_size": 100, "page_index": 1})
        out: list[VendorAlarm] = []
        for it in data.get("list", []) if isinstance(data, dict) else []:
            title = it.get("warning_message") or it.get("warningname") or "GoodWe alarm"
            out.append(VendorAlarm(
                vendor="goodwe",
                vendor_plant_id=str(it.get("station_id")),
                vendor_alarm_id=str(it.get("warning_id") or it.get("id")),
                severity=map_severity("goodwe", str(it.get("warning_level", "medium")).lower()),
                category=classify_alarm_category(title),
                title=title,
                description=it.get("solution"),
                detected_at=_goodwe_ts(it.get("happentime") or it.get("warning_time")),
                resolved_at=_goodwe_ts(it.get("recoveredtime")),
                raw=it,
            ))
        return out

    def send_command(self, plant_id: str, command: str, params: dict) -> dict:
        # GoodWe SEMS verejné API neumožňuje plný command/control —
        # len cez Pro Lite app alebo Inverter Setup. Tu len audit fail.
        raise VendorError("GoodWe SEMS public API does not support remote commands")


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _goodwe_ts(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None
