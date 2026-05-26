"""Canonical data model — výstupný formát zo všetkých vendor adaptérov.

Účel: každý vendor vracia rozdielne JSON štruktúry. Adaptér ich namapuje na
tieto kanonické dataclassy, ktoré sa potom ukladajú do Supabase.

Mapovanie napríklad pre Huawei:
  realTimeKpi -> mountToCanonical:
    realTimePower → ac_power_kw
    real_time_p   → ac_power_kw (iný API verzia)
    day_power     → ac_energy_today_kwh
    total_power   → ac_energy_total_kwh
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, Any


@dataclass
class PlantInfo:
    """Master data jednej inštalácie z vendor cloud-u.

    Mapuje sa do tabuľky inverter_sites pri synchronizácii.
    """
    vendor: str                                  # 'huawei','solinteg',...
    vendor_plant_id: str                         # vendor-specific PK (NE u Huawei, sid u Solinteg)
    site_name: str
    kw_dc_nominal: Optional[float] = None
    kw_ac_nominal: Optional[float] = None
    battery_kwh_nominal: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    timezone: Optional[str] = None               # 'Europe/Bratislava'
    commissioning_date: Optional[date] = None
    customer_email: Optional[str] = None         # ak vendor expose-uje
    customer_name: Optional[str] = None
    address: Optional[str] = None
    raw: dict = field(default_factory=dict)      # plný payload pre debug


@dataclass
class TelemetrySnapshot:
    """Real-time snapshot — vstup do tabuľky telemetry_5min."""
    vendor_plant_id: str
    ts: datetime                                 # vendor timestamp
    # AC
    ac_power_kw: Optional[float] = None
    ac_energy_today_kwh: Optional[float] = None
    ac_energy_total_kwh: Optional[float] = None
    voltage_l1_v: Optional[float] = None
    voltage_l2_v: Optional[float] = None
    voltage_l3_v: Optional[float] = None
    frequency_hz: Optional[float] = None
    # DC strings
    dc_strings: Optional[list[dict]] = None      # [{mppt:1,voltage_v:680,current_a:9.3,power_w:6324}]
    # Batéria
    battery_soc_pct: Optional[float] = None
    battery_soh_pct: Optional[float] = None
    battery_power_kw: Optional[float] = None     # + nabíja, − vybíja
    battery_temp_c: Optional[float] = None
    # Grid
    grid_export_kw: Optional[float] = None
    grid_import_kw: Optional[float] = None
    grid_export_kwh_today: Optional[float] = None
    grid_import_kwh_today: Optional[float] = None
    self_consumption_kw: Optional[float] = None
    # Inverter
    inverter_temp_c: Optional[float] = None
    inverter_status: Optional[str] = None        # 'running','standby','fault','comm_lost'
    # Raw
    raw_payload: dict = field(default_factory=dict)

    def to_db_row(self, site_id: str) -> dict:
        """Pripraví dict ready na INSERT do telemetry_5min."""
        d = asdict(self)
        d.pop("vendor_plant_id", None)
        d["site_id"] = site_id
        return d


@dataclass
class DailySummary:
    """Denný súhrn — vstup do telemetry_daily (alebo audit log)."""
    vendor_plant_id: str
    day: date
    energy_kwh: Optional[float] = None
    peak_power_kw: Optional[float] = None
    grid_export_kwh: Optional[float] = None
    grid_import_kwh: Optional[float] = None
    self_consumption_kwh: Optional[float] = None
    co2_avoided_kg: Optional[float] = None
    raw: dict = field(default_factory=dict)


@dataclass
class VendorAlarm:
    """Alarm z vendor cloud-u — vstup do tabuľky alarms."""
    vendor: str
    vendor_plant_id: str
    vendor_alarm_id: str
    severity: str                                # 'info','warn','minor','major','critical'
    category: str                                # canonical kategória po klasifikácii
    title: str
    description: Optional[str] = None
    detected_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    raw: dict = field(default_factory=dict)


# Mapovanie vendor severity → canonical severity
SEVERITY_MAP: dict[str, dict[str, str]] = {
    "huawei":   {"1": "critical", "2": "major", "3": "minor", "4": "warn"},
    "solinteg": {"1": "info", "2": "warn", "3": "minor", "4": "major", "5": "critical"},
    "goodwe":   {"low": "warn", "medium": "minor", "high": "major", "critical": "critical"},
    "fronius":  {"1": "info", "2": "warn", "3": "major", "4": "critical"},
    "sungrow":  {"1": "info", "2": "warn", "3": "major", "4": "critical"},
}


def map_severity(vendor: str, raw_severity: str) -> str:
    """Mapuje vendor-specific severity kód na canonical."""
    return SEVERITY_MAP.get(vendor, {}).get(str(raw_severity).lower(), "warn")


def classify_alarm_category(title: str, description: str = "") -> str:
    """Hrubá klasifikácia alarm kategórie podľa textu.

    Neskôr nahradiť LLM klasifikátorom alebo trénovaným modelom.
    """
    blob = (title + " " + (description or "")).lower()
    if any(kw in blob for kw in ["communication", "offline", "comm lost", "konektivita"]):
        return "comm_lost"
    if any(kw in blob for kw in ["string", "pv input", "mppt"]):
        return "string_fault"
    if any(kw in blob for kw in ["temperature", "overheat", "teplot"]):
        return "overtemp"
    if any(kw in blob for kw in ["grid", "voltage", "frequency", "siet"]):
        return "grid_anomaly"
    if any(kw in blob for kw in ["battery", "bms", "soc", "batéri"]):
        return "battery_fault"
    if any(kw in blob for kw in ["zero", "export", "limit"]):
        return "zero_export_breach"
    return "unknown"
