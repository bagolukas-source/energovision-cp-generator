"""
Solinteg Cloud Open API v2.0 adapter.
Docs: https://apidocs-en.solinteg-cloud.com

Flow:
1. POST /openapi/login with {email, password, clientId} → token (TTL 60 min)
2. Subsequent requests: header "Authorization: <token>"
3. Device-centric (NIE plant-centric ako Huawei) — virtuálne pluginy
4. MQTT pre real-time (topic /cBFQ7PMTpG)
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

# Candidate base URLs — production môže byť na inom hostname než docs
# Skúsime po jednom kým niektorý odpovie
BASE_URL_CANDIDATES = [
    "https://openapi.solinteg-cloud.com",
    "https://api.solinteg-cloud.com",
    "https://openapi-en.solinteg-cloud.com",
    "https://eu-openapi.solinteg-cloud.com",
]


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def load_credentials() -> Optional[Dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
        headers=_sb_headers(),
        params={
            "select": "id,base_url,username,encrypted_password,client_id,current_token,token_expires_at,oauth_scope",
            "vendor": "eq.solinteg",
            "is_active": "eq.true",
        },
        timeout=10,
    )
    if not r.ok:
        return None
    rows = r.json()
    return rows[0] if rows else None


def save_token(cred_id: str, token: str, expires_in_sec: int = TOKEN_TTL_SECONDS) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_sec - 60)
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
        headers=_sb_headers(),
        params={"id": f"eq.{cred_id}"},
        json={
            "current_token": token,
            "token_expires_at": expires_at.isoformat(),
            "last_token_refresh_at": datetime.now(timezone.utc).isoformat(),
        },
        timeout=10,
    )


def try_login(cred: Dict) -> Tuple[bool, Optional[Dict]]:
    """Skúsi login proti viacerým base URL kým niečo odpovie."""
    email = cred.get("username", "")
    password = cred.get("encrypted_password", "")
    client_id = cred.get("client_id", "")

    if not email or not password or not client_id:
        return False, {"error": "missing email/password/client_id"}

    primary_base = cred.get("base_url", "").rstrip("/")
    candidates = [primary_base] + [u for u in BASE_URL_CANDIDATES if u != primary_base]
    candidates = [c for c in candidates if c]  # remove empty

    attempts = []
    # Známe path varianty pre login
    paths = [
        "/openapi/login",
        "/openapi/v2/login",
        "/openapi/account/login",
    ]
    # Známe body varianty
    bodies = [
        {"email": email, "password": password, "clientId": client_id},
    ]

    for base in candidates:
        for path in paths:
            for body in bodies:
                url = f"{base}{path}"
                try:
                    r = requests.post(
                        url,
                        json=body,
                        headers={"Content-Type": "application/json"},
                        timeout=5,
                    )
                    snippet = r.text[:300]
                    attempt = {"url": url, "body_keys": list(body.keys()), "status": r.status_code, "snippet": snippet[:200]}
                    if r.ok:
                        try:
                            j = r.json()
                            # Solinteg pravdepodobne vracia {code, msg, data: {token}}
                            token = None
                            if isinstance(j, dict):
                                d = j.get("data")
                                if isinstance(d, dict):
                                    token = d.get("token") or d.get("accessToken")
                                if not token:
                                    token = j.get("token") or j.get("accessToken")
                            if token:
                                attempt["token_preview"] = token[:30] + "..."
                                attempts.append(attempt)
                                return True, {
                                    "token": token,
                                    "base_url_used": base,
                                    "path_used": path,
                                    "body_used": body,
                                    "raw": j,
                                    "attempts": attempts,
                                }
                            attempt["fail_reason"] = j.get("msg") or j.get("message") or "no token in response"
                            attempt["code"] = j.get("code")
                        except Exception as e:
                            attempt["fail_reason"] = f"json parse: {e}"
                    attempts.append(attempt)
                except Exception as ex:
                    attempts.append({"url": url, "error": str(ex)[:200]})

    return False, {"error": "all candidates failed", "attempts": attempts}


def get_valid_token(force: bool = False) -> Optional[str]:
    cred = load_credentials()
    if not cred:
        return None

    if not force:
        expires_str = cred.get("token_expires_at")
        current = cred.get("current_token")
        if expires_str and current:
            try:
                expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                if expires_at > datetime.now(timezone.utc):
                    return current
            except Exception:
                pass

    ok, result = try_login(cred)
    if not ok:
        log.warning("Solinteg login failed: %s", result)
        return None

    token = result["token"]
    save_token(cred["id"], token)

    # Update base_url s tým ktorý fungoval
    base_used = result.get("base_url_used")
    if base_used and base_used != cred.get("base_url"):
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
            headers=_sb_headers(),
            params={"id": f"eq.{cred['id']}"},
            json={"base_url": base_used},
            timeout=10,
        )

    return token


def list_devices() -> Tuple[bool, Optional[Dict]]:
    """Zoznam zariadení (Solinteg je device-centric)."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}

    base = cred.get("base_url", "").rstrip("/")
    paths = [
        "/openapi/device/list",
        "/openapi/v2/device/list",
        "/openapi/devices",
        "/device/list",
    ]
    attempts = []
    for path in paths:
        try:
            r = requests.get(
                f"{base}{path}",
                headers={"Authorization": token, "Content-Type": "application/json"},
                timeout=15,
            )
            attempt = {"url": f"{base}{path}", "status": r.status_code, "snippet": r.text[:300]}
            if r.ok:
                try:
                    j = r.json()
                    if isinstance(j, dict) and (j.get("code") == 0 or j.get("success") or "data" in j):
                        return True, {"data": j, "endpoint": path, "attempts": attempts + [attempt]}
                except Exception:
                    pass
            attempts.append(attempt)
        except Exception as ex:
            attempts.append({"url": f"{base}{path}", "error": str(ex)[:200]})

    return False, {"error": "no working endpoint", "attempts": attempts}


# ============================================================================
# Vendor-agnostic API: get_realtime / get_history / get_alarms / send_command
# Pre Energovision CRM — Solinteg parity s Huawei (read + write).
#
# Pattern: token v header "Token: <token>" (alebo "Authorization" fallback).
# Response shape: {"errorCode":0, "body": <data>, "successful":true}
# alebo {"code":0, "data": <data>, "success":true}
#
# Discovery: pre každý endpoint skúšame N candidate paths, prvý úspešný
# si zapamätáme do credentials.last_endpoints (JSON) pre next runs.
# ============================================================================

def _headers(token: str) -> Dict[str, str]:
    """Solinteg používa 'Token' header. Niektoré inštancie akceptujú aj 'Authorization'."""
    return {"Token": token, "Authorization": token, "Content-Type": "application/json"}


def _ok_response(resp_json: Dict) -> bool:
    """Solinteg má viacero response shape variantov — všetky validujem."""
    if not isinstance(resp_json, dict):
        return False
    if resp_json.get("errorCode") == 0 or resp_json.get("errorCode") == "0":
        return True
    if resp_json.get("code") == 0 or resp_json.get("code") == "0":
        return True
    if resp_json.get("successful") is True or resp_json.get("success") is True:
        return True
    return False


def _extract_body(resp_json: Dict):
    """Extrahuje payload z Solinteg response — body | data | result."""
    if not isinstance(resp_json, dict):
        return None
    return resp_json.get("body") or resp_json.get("data") or resp_json.get("result")


def _try_paths(base: str, method: str, paths: List[str], headers: Dict, json_body=None, params=None, timeout: int = 20) -> Tuple[bool, Optional[Dict]]:
    """Generická discovery — skúsi paths kým nedostaneme success response."""
    attempts = []
    for path in paths:
        url = f"{base.rstrip('/')}{path}"
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, params=params, timeout=timeout)
            else:
                r = requests.post(url, headers=headers, json=json_body, params=params, timeout=timeout)
            try:
                j = r.json()
            except Exception:
                j = None
            ok = r.ok and _ok_response(j) if j is not None else False
            attempts.append({"url": url, "status": r.status_code, "ok": ok})
            if ok:
                return True, {"path_used": path, "body": _extract_body(j), "raw": j, "attempts": attempts}
        except Exception as ex:
            attempts.append({"url": url, "error": str(ex)[:200]})
    return False, {"attempts": attempts}


def get_realtime(device_sn: str) -> Tuple[bool, Optional[Dict]]:
    """Aktuálne realtime hodnoty z invertora — analóg getStationRealKpi pre Huawei."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    paths = [
        "/openapi/v2/device/realtime",
        "/openapi/device/realtime",
        "/openapi/v2/realtime",
        "/wrapper/device/realtime",
        "/api/device/realtime",
    ]
    # Skús najprv POST s body
    ok, res = _try_paths(base, "POST", paths, _headers(token), json_body={"deviceSn": device_sn})
    if ok:
        return True, res
    # Fallback GET s query param
    return _try_paths(base, "GET", paths, _headers(token), params={"deviceSn": device_sn})


def get_history(device_sn: str, start_ms: int, end_ms: int) -> Tuple[bool, Optional[Dict]]:
    """Historická telemetria (5-min/15-min granularita)."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    paths = [
        "/openapi/v2/device/history",
        "/openapi/device/history",
        "/wrapper/device/history",
    ]
    body = {"deviceSn": device_sn, "startTime": start_ms, "endTime": end_ms}
    return _try_paths(base, "POST", paths, _headers(token), json_body=body, timeout=45)


def get_device_status(device_sn: str) -> Tuple[bool, Optional[Dict]]:
    """Stav zariadenia (online/offline, last seen)."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    paths = [
        "/openapi/v2/device/status",
        "/openapi/device/status",
        "/wrapper/device/status",
    ]
    return _try_paths(base, "POST", paths, _headers(token), json_body={"deviceSn": device_sn})


def get_alarms(device_sn: str, start_ms: int, end_ms: int) -> Tuple[bool, Optional[Dict]]:
    """Zoznam alarmov za obdobie."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    paths = [
        "/openapi/v2/alarm/list",
        "/openapi/alarm/list",
        "/wrapper/alarm/list",
    ]
    body = {"deviceSn": device_sn, "startTime": start_ms, "endTime": end_ms}
    return _try_paths(base, "POST", paths, _headers(token), json_body=body, timeout=30)


def verify_sn(device_sn: str, check_code: str) -> Tuple[bool, Optional[Dict]]:
    """Overiť že deviceSn + checkCode patria do existujúceho zariadenia (pred bind)."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    paths = [
        "/openapi/v2/device/verify",
        "/openapi/device/verify",
        "/wrapper/device/verify",
    ]
    body = {"deviceSn": device_sn, "checkCode": check_code}
    return _try_paths(base, "POST", paths, _headers(token), json_body=body)


def bind_device(device_sn: str, check_code: str) -> Tuple[bool, Optional[Dict]]:
    """Pridať device do nášho účtu — MQTT push začne ísť na náš topic."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    paths = [
        "/openapi/v2/device/bind",
        "/openapi/device/bind",
        "/wrapper/device/bind",
    ]
    body = {"deviceSn": device_sn, "checkCode": check_code}
    return _try_paths(base, "POST", paths, _headers(token), json_body=body)


# ============================================================================
# Device control (write commands)
# ============================================================================

# Mapping: vendor-agnostic command_type → Solinteg setting codes / direct paths
# Inšpirované huawei_spot.py.execute_transition aby UI ostalo identické.
COMMAND_MAP = {
    "disable_zero_export": [
        {"settingCode": "antiCounterCurrentStartStop", "value": "0"},
        {"settingCode": "antiReverseCurrentPowerSetting", "value": "100"},
    ],
    "enable_zero_export": [
        {"settingCode": "antiCounterCurrentStartStop", "value": "1"},
        {"settingCode": "antiReverseCurrentPowerSetting", "value": "0"},
    ],
    "set_active_power_limit": None,  # dynamic: limit_pct → antiReverseCurrentPowerSetting
    "set_battery_mode_self": [
        {"settingCode": "hybridWorkMode", "value": "1#1"},  # General Mode = self-consumption
    ],
    "set_battery_mode_economic": [
        {"settingCode": "hybridWorkMode", "value": "1#2"},  # Economic Mode (ToU arbitráž)
    ],
    "set_battery_mode_grid_charge": [
        {"settingCode": "hybridWorkMode", "value": "3#3"},  # EMS BattCtrl
    ],
}


def send_command(device_sn: str, command_type: str, params: Optional[Dict] = None) -> Tuple[bool, Optional[Dict]]:
    """
    Vendor-agnostic command rozhraním — analóg huawei_send_command.
    Vracia (ok, response_dict).
    """
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    params = params or {}

    # Direct on/off commands (wrapper/cmd/{action})
    if command_type == "full_shutdown":
        return _try_paths(base, "GET", ["/wrapper/cmd/stop"], _headers(token), params={"deviceSn": device_sn})
    if command_type == "start" or command_type == "full_restore":
        return _try_paths(base, "GET", ["/wrapper/cmd/start"], _headers(token), params={"deviceSn": device_sn})
    if command_type == "restart":
        return _try_paths(base, "GET", ["/wrapper/cmd/restart"], _headers(token), params={"deviceSn": device_sn})

    # Setting commands (wrapper/cmd/set)
    setting_items = COMMAND_MAP.get(command_type)
    if setting_items is None:
        # Dynamic — set_active_power_limit
        if command_type == "set_active_power_limit":
            pct = float(params.get("limit_pct") or params.get("pct") or 100)
            setting_items = [{"settingCode": "antiReverseCurrentPowerSetting", "value": str(pct)}]
        elif command_type == "forced_charge":
            # Solinteg nemá priamy forced_charge ako Huawei — riešime cez Economic mode
            # (Zatiaľ vrátime "not supported", treba implementovať cez ToU mode group)
            return False, {"error": "forced_charge not yet implemented for Solinteg — use Economic mode + ToU groups"}
        else:
            return False, {"error": f"unknown command_type: {command_type}"}

    paths = [
        "/wrapper/cmd/set",
        "/openapi/cmd/set",
        "/cmd/set",
    ]
    body = {"deviceSn": device_sn, "sendSettingItemList": setting_items}
    ok, res = _try_paths(base, "POST", paths, _headers(token), json_body=body, timeout=30)
    if ok and res:
        record_id = res.get("body")
        if isinstance(record_id, str):
            res["record_id"] = record_id
    return ok, res


def check_control_result(record_id: str) -> Tuple[bool, Optional[Dict]]:
    """Verify výsledok command setu — TTL 1 min, polling pattern."""
    cred = load_credentials()
    token = get_valid_token()
    if not token or not cred:
        return False, {"error": "no token"}
    base = cred.get("base_url", "https://openapi.solinteg-cloud.com")
    paths = [
        "/cmd/checkControlResult",
        "/wrapper/cmd/checkControlResult",
        "/openapi/cmd/checkControlResult",
    ]
    return _try_paths(base, "GET", paths, _headers(token), params={"recordId": record_id})


# ============================================================================
# Field mapping: Solinteg realtime → inverter_measurements columns
# ============================================================================

def map_realtime_to_measurement(realtime_body, site_id: str) -> Dict:
    """
    Map Solinteg realtime fields → inverter_measurements row.
    Zdroj: Inverter Realtime Data appendix.
    """
    if isinstance(realtime_body, list) and len(realtime_body) > 0:
        d = realtime_body[0]
    elif isinstance(realtime_body, dict):
        d = realtime_body
    else:
        return {}

    def f(key):
        v = d.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # workStatus: 0/1/4 Standby, 2 On-Grid, 3 Fault, 5 Off-Grid
    work_status = d.get("workStatus")
    state_map = {0: "Standby", 1: "Standby", 4: "Standby", 2: "Grid-connected", 3: "Fault", 5: "Off-Grid"}
    state_label = state_map.get(work_status, str(work_status) if work_status is not None else None)

    return {
        "site_id": site_id,
        "measured_at": d.get("rtcTime") or d.get("creationDate") or datetime.now(timezone.utc).isoformat(),
        "active_power_kw": f("pac"),
        "mppt_total_power_kw": f("totalPvPower") or f("ppvInput"),
        "pv_yield_kw": f("totalPvPower") or f("ppvInput"),
        "consumption_kw": f("pload"),
        "grid_power_kw": f("pMeterTotal"),
        "battery_soc_pct": f("soc"),
        # Pozor: Solinteg má batteryP >0 vybíja, <0 nabíja — invertujeme aby bolo konzistentné s Huawei (>0 nabíja)
        "battery_power_kw": -f("batteryP") if f("batteryP") is not None else None,
        "daily_energy_kwh": f("epvDay") or f("eDay"),
        "total_energy_kwh": f("eTotalPv") or f("eTotal"),
        "ac_voltage": f("vGridPhaseA"),
        "phase_a_voltage_v": f("vGridPhaseA"),
        "phase_b_voltage_v": f("vGridPhaseB"),
        "phase_c_voltage_v": f("vGridPhaseC"),
        "phase_a_current_a": f("iGridPhaseA"),
        "phase_b_current_a": f("iGridPhaseB"),
        "grid_frequency_hz": f("fGrid"),
        "temperature_c": f("temperature1"),
        "power_factor": f("pf"),
        "inverter_state_code": int(work_status) if work_status is not None else None,
        "inverter_state_label": state_label,
        "inverter_state_category": "running" if work_status == 2 else ("fault" if work_status == 3 else "standby"),
        "raw_json": d,
    }


# ============================================================================
# Backfill helpers — pre paralelu k huawei backfill cron
# ============================================================================

def backfill_history(device_sn: str, days: int = 30) -> Tuple[bool, Dict]:
    """
    Pull historical telemetria za N dní -> inverter_measurements.
    Volá get_history v cykle po dňoch (Solinteg môže mať limit max range).
    """
    import os
    import time as _time
    SB_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
    SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")
    sb_headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}

    # Najdi site_id
    sr = requests.get(f"{SB_URL}/rest/v1/inverter_sites", headers=sb_headers,
                      params={"select": "id,site_name", "vendor": "eq.solinteg",
                              "vendor_plant_code": f"eq.{device_sn}", "limit": 1}, timeout=15)
    sites = sr.json() if sr.ok else []
    if not sites:
        return False, {"error": f"site not found for {device_sn}"}
    site_id = sites[0]["id"]

    total_inserted = 0
    failed_days = []
    now_ms = int(_time.time() * 1000)
    for d in range(days):
        end_ms = now_ms - d * 86400 * 1000
        start_ms = end_ms - 86400 * 1000
        ok, result = get_history(device_sn, start_ms, end_ms)
        if not ok:
            failed_days.append({"day_offset": d, "error": "history fetch failed"})
            continue
        body = (result or {}).get("body")
        rows = []
        # Solinteg history typically returns list of data points
        if isinstance(body, list):
            for point in body:
                m = map_realtime_to_measurement(point, site_id)
                if m and m.get("measured_at"):
                    rows.append(m)
        elif isinstance(body, dict) and isinstance(body.get("list"), list):
            for point in body["list"]:
                m = map_realtime_to_measurement(point, site_id)
                if m and m.get("measured_at"):
                    rows.append(m)
        if rows:
            ir = requests.post(f"{SB_URL}/rest/v1/inverter_measurements",
                               headers={**sb_headers, "Prefer": "resolution=ignore-duplicates"},
                               json=rows, timeout=30)
            if ir.status_code in (200, 201):
                total_inserted += len(rows)
    return True, {"site_id": site_id, "days_attempted": days, "rows_inserted": total_inserted, "failed_days": failed_days}


def sync_alarms(device_sn: str, days: int = 7) -> Tuple[bool, Dict]:
    """Pull alarmov za N dní -> inverter_alarms."""
    import os
    import time as _time
    SB_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
    SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")
    sb_headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}

    sr = requests.get(f"{SB_URL}/rest/v1/inverter_sites", headers=sb_headers,
                      params={"select": "id", "vendor": "eq.solinteg",
                              "vendor_plant_code": f"eq.{device_sn}", "limit": 1}, timeout=15)
    sites = sr.json() if sr.ok else []
    if not sites:
        return False, {"error": f"site not found for {device_sn}"}
    site_id = sites[0]["id"]

    end_ms = int(_time.time() * 1000)
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
        # Solinteg alarm field guesses (treba potvrdiť pri prvom real run)
        rows.append({
            "site_id": site_id,
            "alarm_code": str(a.get("alarmCode") or a.get("code") or a.get("faultCode") or "unknown"),
            "alarm_name": str(a.get("alarmName") or a.get("name") or a.get("description") or "—")[:200],
            "severity": str(a.get("severity") or a.get("level") or "info"),
            "raised_at": a.get("startTime") or a.get("alarmTime") or a.get("createTime"),
            "resolved_at": a.get("endTime") or a.get("resolveTime"),
            "status": "resolved" if (a.get("endTime") or a.get("resolved")) else "active",
            "raw_description": str(a)[:1000],
        })
    if rows:
        ir = requests.post(f"{SB_URL}/rest/v1/inverter_alarms",
                           headers={**sb_headers, "Prefer": "resolution=ignore-duplicates"},
                           json=rows, timeout=30)
        if ir.status_code not in (200, 201):
            return False, {"error": "insert failed", "status": ir.status_code, "snippet": ir.text[:300]}
    return True, {"site_id": site_id, "alarms_count": len(rows)}



def diagnose_login() -> Dict:
    """Diagnostic: POST login na viacero candidate hostnames + return raw responses."""
    import requests as _rq
    cred = load_credentials()
    if not cred:
        return {"error": "no credentials in DB"}
    body = {
        "email": cred.get("username"),
        "password": cred.get("encrypted_password"),
        "clientId": cred.get("client_id"),
    }
    # Multi-host fan-out — z DNS hľadanie ktorý hostname funguje pre OpenAPI
    candidate_urls = [
        "https://openapi.solinteg-cloud.com/openapi/login",
        "https://api.solinteg-cloud.com/openapi/login",
        "https://eu-openapi.solinteg-cloud.com/openapi/login",
        "https://www.solinteg-cloud.com/openapi/login",
        "https://solinteg-cloud.com/openapi/login",
        "https://gateway.solinteg-cloud.com/openapi/login",
        "https://openapi-eu.solinteg-cloud.com/openapi/login",
    ]
    attempts = []
    success_attempt = None
    for url in candidate_urls:
        attempt = {"url": url}
        try:
            r = _rq.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=8)
            try:
                j = r.json()
            except Exception:
                j = None
            attempt["status"] = r.status_code
            attempt["text_preview"] = r.text[:300]
            attempt["json"] = j
            # Solinteg success: {code:0, data:{token: "..."}} alebo {errorCode:0, body:{token}}
            if isinstance(j, dict):
                token = None
                d = j.get("data") or j.get("body")
                if isinstance(d, dict):
                    token = d.get("token") or d.get("accessToken")
                if not token:
                    token = j.get("token") or j.get("accessToken")
                if token:
                    attempt["token_found"] = True
                    success_attempt = attempt
                    attempts.append(attempt)
                    break
        except Exception as e:
            attempt["error"] = str(e)[:200]
        attempts.append(attempt)
    return {
        "body_sent": {k: (v if k != "password" else "***") for k, v in body.items()},
        "attempts": attempts,
        "success": success_attempt is not None,
        "winning_url": success_attempt["url"] if success_attempt else None,
    }
