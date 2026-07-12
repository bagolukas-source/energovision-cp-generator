"""
Solinteg Cloud Open API v2.0 adapter (PREPÍSANÝ podľa oficiálnych docs).

Docs: https://apidocs-en.solinteg-cloud.com/

Kľúčové info:
- Login: POST {{EU}}/loginv2/auth body {authAccount, authPassword} → {errorCode:0, body:"<JWT>", successful:true}
- Token TTL 60 min, posiela sa ako 'token: <JWT>' header (LOWERCASE)
- Device-centric (nie plant-centric ako Huawei)
- MQTT push 1× minúta: mqtt.solinteg-cloud.com:7783 (plain), topic z user account

Endpoints:
- GET  {{EU}}/wrapper/topic/getDeviceByTopic?topic=/<topic>
- GET  {{EU}}/wrapper/device/queryDeviceRealtimeData?deviceSn=<sn>
- POST {{EU}}/wrapper/topic/addTopicMapping?deviceSn=<sn>&topic=<t>&checkCode=<c>
- GET  {{EU}}/device/checkCodeAndSn?deviceSn=<sn>&checkCode=<c>
- ... (history, alarms, commands podľa neskorších sekcií docs)

{{EU}} hostname — discovery: skúsi multiple candidates a zapamätá si winner do DB.
"""
import os
import time
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List

log = logging.getLogger("solinteg.oauth")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")

TOKEN_TTL_SECONDS = 60 * 60  # 60 min per docs

# {{EU}} candidate hostnames (DNS-resolveable kandidáti pre EU OpenAPI)
BASE_URL_CANDIDATES = [
    "https://lb.solinteg-cloud.com/openapi/v2",  # ✓ POTVRDENÝ live (login OK, devices OK, realtime OK)
    "https://lb.solinteg-cloud.com",
    "https://mqtt.solinteg-cloud.com",
    "https://openapi.solinteg-cloud.com",
]


def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


def load_credentials() -> Optional[Dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
        headers=_sb_headers(),
        params={
            "select": "id,base_url,username,encrypted_password,client_id,current_token,token_expires_at,oauth_scope",
            "vendor": "eq.solinteg",
            "is_active": "eq.true",
            "limit": 1,
        },
        timeout=10,
    )
    rows = r.json() if r.ok else []
    return rows[0] if rows else None


def save_token(cred_id: str, token: str, base_used: str, expires_in_sec: int = TOKEN_TTL_SECONDS) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in_sec)).isoformat()
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
        headers=_sb_headers(),
        params={"id": f"eq.{cred_id}"},
        json={
            "current_token": token,
            "token_expires_at": expires_at,
            "last_token_refresh_at": datetime.now(timezone.utc).isoformat(),
            "base_url": base_used,
        },
        timeout=10,
    )


def try_login(cred: Dict) -> Tuple[bool, Optional[Dict]]:
    """
    Login podľa docs: POST {{EU}}/loginv2/auth body {authAccount, authPassword}
    Response: {errorCode:0, body:"<JWT>", successful:true}
    """
    email = cred.get("username", "")
    password = cred.get("encrypted_password", "")
    if not email or not password:
        return False, {"error": "missing email/password"}

    body = {"authAccount": email, "authPassword": password}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Energovision-CRM/1.0)",
        "Accept": "application/json",
    }

    # Discovery: skúsi BASE_URL_CANDIDATES + uložený primary
    primary = cred.get("base_url", "").rstrip("/")
    candidates = [primary] + [u for u in BASE_URL_CANDIDATES if u and u != primary]
    candidates = [c for c in candidates if c]

    attempts = []
    for base in candidates:
        url = f"{base}/loginv2/auth"
        try:
            r = requests.post(url, json=body, headers=headers, timeout=8)
            try:
                j = r.json()
            except Exception:
                j = None
            attempt = {"url": url, "status": r.status_code, "json": j, "text_preview": r.text[:200]}
            if isinstance(j, dict) and (j.get("errorCode") == 0 or j.get("successful") is True):
                token = j.get("body") if isinstance(j.get("body"), str) else None
                if token and token.startswith("ey"):
                    attempt["token_preview"] = token[:40] + "..."
                    attempts.append(attempt)
                    return True, {
                        "token": token,
                        "base_url_used": base,
                        "attempts": attempts,
                    }
                attempt["fail_reason"] = "no JWT in body"
            attempts.append(attempt)
        except Exception as e:
            attempts.append({"url": url, "error": str(e)[:200]})
    return False, {"error": "no candidate succeeded", "attempts": attempts}


def get_valid_token(force: bool = False) -> Optional[str]:
    """Vráti token (z DB cache ak je platný, inak nový login)."""
    cred = load_credentials()
    if not cred:
        return None
    if not force:
        token = cred.get("current_token")
        exp = cred.get("token_expires_at")
        if token and exp:
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if exp_dt > datetime.now(timezone.utc) + timedelta(minutes=2):
                    return token
            except Exception:
                pass
    ok, result = try_login(cred)
    if ok and result:
        save_token(cred["id"], result["token"], result["base_url_used"])
        return result["token"]
    return None


def _headers(token: str) -> Dict[str, str]:
    """Solinteg používa 'token' header (LOWERCASE per docs!)."""
    return {
        "token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Energovision-CRM/1.0)",
    }


def _base() -> str:
    cred = load_credentials()
    return (cred.get("base_url") if cred else "") or "https://lb.solinteg-cloud.com/openapi/v2"


def _ok(j) -> bool:
    return isinstance(j, dict) and (j.get("errorCode") == 0 or j.get("successful") is True)


# ============================================================================
# Device manage
# ============================================================================

def list_devices_by_topic(topic: Optional[str] = None) -> Tuple[bool, Optional[Dict]]:
    """GET /wrapper/topic/getDeviceByTopic?topic=<topic>"""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    if not topic:
        topic = cred.get("oauth_scope") or "/cBFQ7PMTpG"
    url = f"{_base()}/wrapper/topic/getDeviceByTopic"
    try:
        r = requests.get(url, headers=_headers(token), params={"topic": topic}, timeout=15)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"body": j.get("body"), "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300], "json": j}
    except Exception as e:
        return False, {"error": str(e)[:300]}


# Alias pre backward compat (Render endpointy)
list_devices = list_devices_by_topic


def verify_sn(device_sn: str, check_code: str) -> Tuple[bool, Optional[Dict]]:
    """GET /device/checkCodeAndSn?deviceSn=<sn>&checkCode=<code>"""
    token = get_valid_token()
    if not token:
        return False, {"error": "no token"}
    url = f"{_base()}/device/checkCodeAndSn"
    try:
        r = requests.get(url, headers=_headers(token),
                         params={"deviceSn": device_sn, "checkCode": check_code}, timeout=15)
        j = r.json() if r.text else None
        if _ok(j) and j.get("body") is True:
            return True, {"body": True, "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300], "json": j}
    except Exception as e:
        return False, {"error": str(e)[:300]}


def bind_device(device_sn: str, check_code: str, topic: Optional[str] = None) -> Tuple[bool, Optional[Dict]]:
    """POST /wrapper/topic/addTopicMapping?deviceSn=...&topic=...&checkCode=..."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    if not topic:
        topic = cred.get("oauth_scope") or "/cBFQ7PMTpG"
    url = f"{_base()}/wrapper/topic/addTopicMapping"
    try:
        r = requests.post(url, headers=_headers(token),
                          params={"deviceSn": device_sn, "topic": topic, "checkCode": check_code}, timeout=15)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"body": j.get("body"), "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300], "json": j}
    except Exception as e:
        return False, {"error": str(e)[:300]}


# ============================================================================
# Device data query
# ============================================================================

def get_realtime(device_sn: str) -> Tuple[bool, Optional[Dict]]:
    """GET /wrapper/device/queryDeviceRealtimeData?deviceSn=<sn>"""
    token = get_valid_token()
    if not token:
        return False, {"error": "no token"}
    url = f"{_base()}/wrapper/device/queryDeviceRealtimeData"
    try:
        r = requests.get(url, headers=_headers(token), params={"deviceSn": device_sn}, timeout=15)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"body": j.get("body"), "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300], "json": j}
    except Exception as e:
        return False, {"error": str(e)[:300]}


def get_device_status(device_sn: str) -> Tuple[bool, Optional[Dict]]:
    """GET /wrapper/device/queryDeviceStatus?deviceSn=<sn>"""
    token = get_valid_token()
    if not token:
        return False, {"error": "no token"}
    url = f"{_base()}/wrapper/device/queryDeviceStatus"
    try:
        r = requests.get(url, headers=_headers(token), params={"deviceSn": device_sn}, timeout=15)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"body": j.get("body"), "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300], "json": j}
    except Exception as e:
        return False, {"error": str(e)[:300]}


def get_history(device_sn: str, start_ms: int, end_ms: int) -> Tuple[bool, Optional[Dict]]:
    """GET /wrapper/device/queryDeviceHistoryData?deviceSn=...&startTime=...&endTime=..."""
    token = get_valid_token()
    if not token:
        return False, {"error": "no token"}
    url = f"{_base()}/wrapper/device/queryDeviceHistoryData"
    try:
        r = requests.get(url, headers=_headers(token),
                         params={"deviceSn": device_sn, "startTime": start_ms, "endTime": end_ms}, timeout=45)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"body": j.get("body"), "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300], "json": j}
    except Exception as e:
        return False, {"error": str(e)[:300]}


def get_alarms(device_sn: str, start_ms: int, end_ms: int) -> Tuple[bool, Optional[Dict]]:
    """GET /wrapper/alarm/queryAlarmList?deviceSn=...&startTime=...&endTime=..."""
    token = get_valid_token()
    if not token:
        return False, {"error": "no token"}
    url = f"{_base()}/wrapper/alarm/queryAlarmList"
    try:
        r = requests.get(url, headers=_headers(token),
                         params={"deviceSn": device_sn, "startTime": start_ms, "endTime": end_ms}, timeout=30)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"body": j.get("body"), "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300], "json": j}
    except Exception as e:
        return False, {"error": str(e)[:300]}


# ============================================================================
# Device control (write commands)
# ============================================================================

def send_command(device_sn: str, command_type: str, params: Optional[Dict] = None) -> Tuple[bool, Optional[Dict]]:
    """Command router (vendor-agnostic analóg pre huawei_send_command)."""
    token = get_valid_token()
    if not token:
        return False, {"error": "no token"}
    params = dict(params or {})
    base = _base()

    # TVRDÝ INVARIANT: zmluvný grid limit sa číta VŽDY z DB — params od volajúceho
    # ho nemôžu prebiť. Pri zlyhaní lookupu export príkaz odmietame (fail-closed).
    if command_type in ("disable_zero_export", "set_active_power_limit"):
        try:
            rows = []
            for col in ("vendor_station_id", "vendor_plant_code"):
                r = requests.get(f"{SUPABASE_URL}/rest/v1/inverter_sites", headers=_sb_headers(),
                                 params={"select": "grid_export_limit_kw,ac_kw",
                                         col: f"eq.{device_sn}", "limit": "1"}, timeout=10)
                if not r.ok:
                    return False, {"error": f"grid limit lookup zlyhal (HTTP {r.status_code}) — export príkaz odmietnutý (fail-closed)"}
                rows = r.json()
                if rows:
                    break
            if rows and rows[0].get("grid_export_limit_kw") is not None:
                params["grid_export_limit_kw"] = float(rows[0]["grid_export_limit_kw"])
                if rows[0].get("ac_kw"):
                    params["ac_kw"] = float(rows[0]["ac_kw"])
            else:
                params.pop("grid_export_limit_kw", None)
        except Exception as e:
            return False, {"error": f"grid limit lookup zlyhal — export príkaz odmietnutý (fail-closed): {str(e)[:200]}"}

    # Direct cmd endpoints (start/stop/restart) per docs §deviceControl/StartAndTimeCalibration
    direct = {
        "full_shutdown": "stop", "stop": "stop",
        "start": "start", "full_restore": "start",
        "restart": "restart",
    }
    if command_type in direct:
        action = direct[command_type]
        url = f"{base}/wrapper/cmd/{action}"
        try:
            r = requests.get(url, headers=_headers(token), params={"deviceSn": device_sn}, timeout=30)
            j = r.json() if r.text else None
            if _ok(j):
                return True, {"action": action, "raw": j}
            return False, {"status": r.status_code, "snippet": r.text[:300]}
        except Exception as e:
            return False, {"error": str(e)[:300]}

    # Setting commands (POST /wrapper/cmd/set body sendSettingItemList)
    SETTINGS_MAP = {
        "disable_zero_export": [
            {"settingCode": "antiCounterCurrentStartStop", "value": "0"},
            {"settingCode": "antiReverseCurrentPowerSetting", "value": "100"},
        ],
        "enable_zero_export": [
            {"settingCode": "antiCounterCurrentStartStop", "value": "1"},
            {"settingCode": "antiReverseCurrentPowerSetting", "value": "0"},
        ],
        "set_battery_mode_self": [{"settingCode": "hybridWorkMode", "value": "1#1"}],
        "set_battery_mode_economic": [{"settingCode": "hybridWorkMode", "value": "1#2"}],
        "set_battery_mode_grid_charge": [{"settingCode": "hybridWorkMode", "value": "3#3"}],
    }
    items = SETTINGS_MAP.get(command_type)
    # Zmluvný grid limit: NORMAL nesmie vypnúť anti-backflow — namiesto toho limit v % z ac_kw.
    if command_type == "disable_zero_export" and params.get("grid_export_limit_kw") is not None:
        limit_kw = float(params["grid_export_limit_kw"])
        ac_kw = float(params.get("ac_kw") or 0)
        pct = min(100.0, max(0.0, (limit_kw / ac_kw) * 100.0)) if ac_kw > 0 else 0.0
        items = [
            {"settingCode": "antiCounterCurrentStartStop", "value": "1"},
            {"settingCode": "antiReverseCurrentPowerSetting", "value": str(int(round(pct)))},
        ]
    if items is None and command_type == "set_active_power_limit":
        pct = float(params.get("limit_pct") or params.get("pct") or 100)
        if params.get("grid_export_limit_kw") is not None:
            ac = float(params.get("ac_kw") or 0)
            cap_pct = (float(params["grid_export_limit_kw"]) / ac * 100.0) if ac > 0 else 0.0
            pct = min(pct, max(0.0, cap_pct))
        items = [{"settingCode": "antiReverseCurrentPowerSetting", "value": str(pct)}]
    if items is None:
        return False, {"error": f"unknown command_type: {command_type}"}

    url = f"{base}/wrapper/cmd/set"
    try:
        r = requests.post(url, headers=_headers(token),
                          json={"deviceSn": device_sn, "sendSettingItemList": items}, timeout=30)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"record_id": j.get("body") if isinstance(j.get("body"), str) else None, "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300]}
    except Exception as e:
        return False, {"error": str(e)[:300]}


def check_control_result(record_id: str) -> Tuple[bool, Optional[Dict]]:
    """GET /cmd/checkControlResult?recordId=<id> (TTL 1 min)"""
    token = get_valid_token()
    if not token:
        return False, {"error": "no token"}
    url = f"{_base()}/cmd/checkControlResult"
    try:
        r = requests.get(url, headers=_headers(token), params={"recordId": record_id}, timeout=15)
        j = r.json() if r.text else None
        if _ok(j):
            return True, {"body": j.get("body"), "raw": j}
        return False, {"status": r.status_code, "snippet": r.text[:300]}
    except Exception as e:
        return False, {"error": str(e)[:300]}


# ============================================================================
# Realtime → inverter_measurements mapping
# ============================================================================

def map_realtime_to_measurement(realtime_body, site_id: str) -> Dict:
    """Map Solinteg realtime fields → inverter_measurements row."""
    if isinstance(realtime_body, list) and realtime_body:
        d = realtime_body[0]
    elif isinstance(realtime_body, dict):
        d = realtime_body
    else:
        return {}

    def f(*keys):
        """Get first non-None value (Solinteg občas má camelCase variant napr. pmeterTotal vs pMeterTotal)."""
        for k in keys:
            v = d.get(k)
            if v is not None and v != "--":
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    work_status = d.get("workStatus")
    try:
        work_status = int(work_status) if work_status not in (None, "--") else None
    except (TypeError, ValueError):
        work_status = None
    state_map = {0: "Standby", 1: "Standby", 4: "Standby", 2: "Grid-connected", 3: "Fault", 5: "Off-Grid"}
    state_label = state_map.get(work_status, str(work_status) if work_status is not None else None)

    pac = f("pac")
    total_pv = f("ppvInput", "totalPvPower")
    battery_p = f("batteryP")

    return {
        "site_id": site_id,
        "measured_at": d.get("rtcTime") if d.get("rtcTime") not in (None, "--") else (
            d.get("creationDate") or datetime.now(timezone.utc).isoformat()
        ),
        "active_power_kw": pac,
        "mppt_total_power_kw": total_pv,
        "pv_yield_kw": total_pv,
        "consumption_kw": f("pload"),
        "grid_power_kw": f("pmeterTotal", "pMeterTotal"),
        "battery_soc_pct": f("soc"),
        # Solinteg batteryP >0 vybíja, <0 nabíja — invertujeme aby bolo konzistentné s Huawei
        "battery_power_kw": -battery_p if battery_p is not None else None,
        "daily_energy_kwh": f("epvDay", "eday"),
        "total_energy_kwh": f("etotalPv", "etotal"),
        "ac_voltage": f("vgridPhaseA", "vGridPhaseA"),
        "phase_a_voltage_v": f("vgridPhaseA", "vGridPhaseA"),
        "phase_b_voltage_v": f("vgridPhaseB", "vGridPhaseB"),
        "phase_c_voltage_v": f("vgridPhaseC", "vGridPhaseC"),
        "phase_a_current_a": f("igridPhaseA", "iGridPhaseA"),
        "phase_b_current_a": f("igridPhaseB", "iGridPhaseB"),
        "grid_frequency_hz": f("fgrid", "fGrid"),
        "temperature_c": f("temperature1"),
        "power_factor": f("pf"),
        "inverter_state_code": work_status,
        "inverter_state_label": state_label,
        "inverter_state_category": "running" if work_status == 2 else ("fault" if work_status == 3 else "standby"),
        "raw_json": d,
    }


# ============================================================================
# Backfill + alarms helpers (rovnaký podpis ako predtým, len volajú nový API)
# ============================================================================

def backfill_history(device_sn: str, days: int = 30) -> Tuple[bool, Dict]:
    """Pull historical telemetria za N dní → inverter_measurements."""
    sb_headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    sr = requests.get(f"{SUPABASE_URL}/rest/v1/inverter_sites", headers=sb_headers,
                      params={"select": "id", "vendor": "eq.solinteg",
                              "vendor_plant_code": f"eq.{device_sn}", "limit": 1}, timeout=15)
    sites = sr.json() if sr.ok else []
    if not sites:
        return False, {"error": f"site not found for {device_sn}"}
    site_id = sites[0]["id"]

    total = 0
    failed = []
    now_ms = int(time.time() * 1000)
    for d_off in range(days):
        end_ms = now_ms - d_off * 86400 * 1000
        start_ms = end_ms - 86400 * 1000
        ok, result = get_history(device_sn, start_ms, end_ms)
        if not ok:
            failed.append({"day_offset": d_off, "error": str(result)[:200]})
            continue
        body = (result or {}).get("body")
        rows = []
        items = body if isinstance(body, list) else (body.get("list") if isinstance(body, dict) else [])
        for p in items:
            m = map_realtime_to_measurement(p, site_id)
            if m and m.get("measured_at"):
                rows.append(m)
        if rows:
            requests.post(f"{SUPABASE_URL}/rest/v1/inverter_measurements",
                          headers={**sb_headers, "Prefer": "resolution=ignore-duplicates"},
                          json=rows, timeout=30)
            total += len(rows)
    return True, {"site_id": site_id, "days_attempted": days, "rows_inserted": total, "failed_days": failed}


def sync_alarms(device_sn: str, days: int = 7) -> Tuple[bool, Dict]:
    sb_headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    sr = requests.get(f"{SUPABASE_URL}/rest/v1/inverter_sites", headers=sb_headers,
                      params={"select": "id", "vendor": "eq.solinteg",
                              "vendor_plant_code": f"eq.{device_sn}", "limit": 1}, timeout=15)
    sites = sr.json() if sr.ok else []
    if not sites:
        return False, {"error": f"site not found"}
    site_id = sites[0]["id"]

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    ok, result = get_alarms(device_sn, start_ms, end_ms)
    if not ok:
        return False, {"error": "alarms fetch failed", **(result or {})}
    body = (result or {}).get("body") or []
    if isinstance(body, dict):
        body = body.get("list") or body.get("data") or []

    rows = []
    for a in body if isinstance(body, list) else []:
        if not isinstance(a, dict):
            continue
        rows.append({
            "site_id": site_id,
            "alarm_code": str(a.get("alarmCode") or a.get("code") or "unknown"),
            "alarm_name": str(a.get("alarmName") or a.get("name") or "—")[:200],
            "severity": str(a.get("severity") or "info"),
            "raised_at": a.get("startTime") or a.get("alarmTime"),
            "resolved_at": a.get("endTime"),
            "status": "resolved" if a.get("endTime") else "active",
            "raw_description": str(a)[:1000],
        })
    if rows:
        requests.post(f"{SUPABASE_URL}/rest/v1/inverter_alarms",
                      headers={**sb_headers, "Prefer": "resolution=ignore-duplicates"},
                      json=rows, timeout=30)
    return True, {"site_id": site_id, "alarms_count": len(rows)}


# ============================================================================
# Diagnostic
# ============================================================================

def diagnose_login() -> Dict:
    """Diagnostic: try_login s plným attempts logom."""
    cred = load_credentials()
    if not cred:
        return {"error": "no credentials in DB"}
    ok, result = try_login(cred)
    return {
        "success": ok,
        "result": result,
        "body_sent": {"authAccount": cred.get("username"), "authPassword": "***"},
    }
