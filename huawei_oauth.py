"""
Huawei FusionSolar — OAuth Client Credentials flow (Service Provider scope).

Workflow:
1. POST /thirdData/token  s {client_id, client_secret, grant_type=client_credentials}
2. Vráti access_token (TTL ~25 min) — uložíme do inverter_vendor_credentials
3. Pri každom API call použijeme Bearer Authorization header
4. Keď access_token expires, refresh — alebo client_credentials znovu

Reference: Huawei NBI Service Provider Manual 25.4.0
"""
import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List

log = logging.getLogger("huawei.oauth")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")

DEFAULT_TOKEN_TTL_SECONDS = 25 * 60  # Huawei štandard


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def load_oauth_credentials(vendor: str = "huawei") -> Optional[Dict]:
    """Načíta OAuth credentials z DB pre konkrétneho vendor-a."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
        headers=_sb_headers(),
        params={
            "select": "id,base_url,client_id,encrypted_client_secret,current_token,refresh_token,token_expires_at,oauth_scope,auth_type",
            "vendor": f"eq.{vendor}",
            "auth_type": "eq.oauth",
            "is_active": "eq.true",
        },
        timeout=10,
    )
    if not r.ok:
        log.error("Nepodarilo sa načítať credentials: %s", r.text[:200])
        return None
    rows = r.json()
    if not rows:
        log.warning("Žiadne OAuth credentials pre vendor=%s", vendor)
        return None
    return rows[0]


def _decrypt_secret(encrypted: str) -> str:
    """
    Decrypt client_secret.
    POZNÁMKA: aktuálne nemáme encryption — secrety sú uložené v plain.
    V budúcnosti môžeme pridať pgcrypto alebo Vault.
    """
    return encrypted or ""


def request_new_token(cred: Dict) -> Tuple[bool, Optional[Dict]]:
    """
    Client credentials flow:
    POST {base_url}/thirdData/token
    Body: {client_id, client_secret, grant_type: 'client_credentials'}
    """
    base_url = cred.get("base_url", "").rstrip("/")
    if not base_url:
        return False, {"error": "missing base_url in DB"}

    client_id = cred.get("client_id", "")
    secret = _decrypt_secret(cred.get("encrypted_client_secret", ""))

    if not client_id or not secret:
        return False, {"error": "missing client_id or client_secret"}

    url = f"{base_url}/thirdData/token"
    payload = {
        "client_id": client_id,
        "client_secret": secret,
        "grant_type": "client_credentials",
    }

    try:
        r = requests.post(url, json=payload, timeout=30)
        if not r.ok:
            log.error("Token request failed: %s %s", r.status_code, r.text[:300])
            return False, {"error": f"HTTP {r.status_code}", "body": r.text[:300]}

        data = r.json()
        # Huawei response format: {success, failCode, data: {accessToken, expiresIn, refreshToken?}}
        if not data.get("success"):
            return False, {"error": f"Huawei failCode {data.get('failCode')}", "msg": data.get("message")}

        token_data = data.get("data") or {}
        return True, {
            "access_token": token_data.get("accessToken"),
            "refresh_token": token_data.get("refreshToken"),
            "expires_in_sec": token_data.get("expiresIn", DEFAULT_TOKEN_TTL_SECONDS),
        }
    except Exception as e:
        log.exception("Token request exception")
        return False, {"error": str(e)}


def save_tokens(cred_id: str, access_token: str, refresh_token: Optional[str], expires_in_sec: int) -> None:
    """Persist nový access_token + refresh_token + expiry do DB."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_sec - 60)  # -60s safety buffer
    payload = {
        "current_token": access_token,
        "token_expires_at": expires_at.isoformat(),
        "last_token_refresh_at": datetime.now(timezone.utc).isoformat(),
    }
    if refresh_token:
        payload["refresh_token"] = refresh_token

    requests.patch(
        f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
        headers=_sb_headers(),
        params={"id": f"eq.{cred_id}"},
        json=payload,
        timeout=10,
    )


def get_valid_access_token(vendor: str = "huawei", force_refresh: bool = False) -> Optional[str]:
    """
    Hlavná funkcia — vráti platný access_token pre použitie v API calls.
    Ak je current_token ešte platný → použije cache.
    Ak vypršal → nový request.
    """
    cred = load_oauth_credentials(vendor)
    if not cred:
        return None

    # Skontroluj cache
    if not force_refresh:
        expires_str = cred.get("token_expires_at")
        current = cred.get("current_token")
        if expires_str and current:
            try:
                expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                if expires_at > datetime.now(timezone.utc):
                    return current
            except Exception:
                pass

    # Cache expired alebo neexistuje → nový token
    log.info("Requesting fresh OAuth token for %s", vendor)
    ok, result = request_new_token(cred)
    if not ok:
        log.error("Token refresh failed: %s", result)
        return None

    access_token = result.get("access_token")
    if not access_token:
        return None

    save_tokens(
        cred_id=cred["id"],
        access_token=access_token,
        refresh_token=result.get("refresh_token"),
        expires_in_sec=result.get("expires_in_sec", DEFAULT_TOKEN_TTL_SECONDS),
    )
    return access_token


def get_authenticated_session() -> Tuple[Optional[requests.Session], Optional[Dict]]:
    """
    Helper: vráti pripravenú requests.Session s OAuth Authorization header.
    + credentials dict (base_url, atd.)
    """
    cred = load_oauth_credentials()
    if not cred:
        return None, None

    token = get_valid_access_token()
    if not token:
        return None, None

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "XSRF-TOKEN": token,  # niektoré Huawei endpointy stále vyžadujú aj XSRF
        "Content-Type": "application/json",
    })
    return session, cred


# ============================================================
# 5 WRITE ENDPOINTS pre Service Provider scope
# ============================================================

def _api_post(endpoint: str, payload: Dict, command_type: str, site_id: Optional[str], station_code: Optional[str],
              triggered_by: Optional[str], source: str = "spot_reactor") -> Tuple[bool, Dict]:
    """Helper: POST request + audit log."""
    session, cred = get_authenticated_session()
    if not session or not cred:
        return False, {"error": "no_credentials"}

    base_url = cred["base_url"].rstrip("/")
    url = f"{base_url}{endpoint}"

    t0 = time.time()
    try:
        r = session.post(url, json=payload, timeout=30)
        dur_ms = int((time.time() - t0) * 1000)
        success = r.ok and (r.json().get("success") if r.text.startswith("{") else False)
        body = r.json() if r.text.startswith("{") else {"raw": r.text[:500]}
    except Exception as e:
        success = False
        body = {"error": str(e)}
        dur_ms = int((time.time() - t0) * 1000)

    # Audit log
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/huawei_write_log",
            headers={**_sb_headers(), "Prefer": "return=minimal"},
            json={
                "site_id": site_id,
                "station_code": station_code,
                "command_type": command_type,
                "command_payload": payload,
                "command_source": source,
                "triggered_by": triggered_by,
                "http_status": r.status_code if "r" in dir() else None,
                "response_body": body,
                "success": success,
                "error_message": body.get("error") or body.get("message"),
                "duration_ms": dur_ms,
            },
            timeout=10,
        )
    except Exception:
        log.warning("Failed to log to huawei_write_log")

    return success, body


def set_forced_charge(station_code: str, power_kw: float, duration_min: int,
                       site_id: Optional[str] = None, triggered_by: Optional[str] = None,
                       source: str = "spot_reactor") -> Tuple[bool, Dict]:
    """Forced charge — batéria sa nabíja zo siete daným power_kw počas duration_min."""
    return _api_post(
        "/thirdData/setBatteryForcedChargeDischarge",
        {
            "stationCodes": station_code,
            "chargeDischargeMode": 1,  # 1 = charge, 2 = discharge
            "power": int(power_kw * 1000),  # W
            "duration": duration_min,
        },
        command_type="forced_charge",
        site_id=site_id,
        station_code=station_code,
        triggered_by=triggered_by,
        source=source,
    )


def set_forced_discharge(station_code: str, power_kw: float, duration_min: int,
                          site_id: Optional[str] = None, triggered_by: Optional[str] = None,
                          source: str = "spot_reactor") -> Tuple[bool, Dict]:
    """Forced discharge — batéria sa vybíja do siete daným power_kw."""
    return _api_post(
        "/thirdData/setBatteryForcedChargeDischarge",
        {
            "stationCodes": station_code,
            "chargeDischargeMode": 2,
            "power": int(power_kw * 1000),
            "duration": duration_min,
        },
        command_type="forced_discharge",
        site_id=site_id,
        station_code=station_code,
        triggered_by=triggered_by,
        source=source,
    )


def set_working_mode(station_code: str, mode: str,
                      site_id: Optional[str] = None, triggered_by: Optional[str] = None,
                      source: str = "manual_ui") -> Tuple[bool, Dict]:
    """
    Working mode:
    - 'self_consumption' — uprednostni self-consumption
    - 'feed_in_priority' — uprednostni export
    - 'time_of_use' — TOU scheduling
    - 'backup' — backup-only mode
    """
    mode_map = {"self_consumption": 1, "feed_in_priority": 2, "time_of_use": 3, "backup": 4}
    mode_code = mode_map.get(mode, 1)
    return _api_post(
        "/thirdData/setBatteryWorkingMode",
        {"stationCodes": station_code, "workingMode": mode_code},
        command_type="working_mode",
        site_id=site_id,
        station_code=station_code,
        triggered_by=triggered_by,
        source=source,
    )


def set_battery_params(station_code: str, params: Dict,
                       site_id: Optional[str] = None, triggered_by: Optional[str] = None,
                       source: str = "manual_ui") -> Tuple[bool, Dict]:
    """
    Battery params — depth of discharge, max charge power, ...
    params: {max_charge_power_kw, max_discharge_power_kw, min_soc_pct, max_soc_pct}
    """
    return _api_post(
        "/thirdData/setBatteryParameters",
        {
            "stationCodes": station_code,
            "maxChargePower": int(params.get("max_charge_power_kw", 0) * 1000),
            "maxDischargePower": int(params.get("max_discharge_power_kw", 0) * 1000),
            "minSoc": params.get("min_soc_pct", 10),
            "maxSoc": params.get("max_soc_pct", 100),
        },
        command_type="battery_params",
        site_id=site_id,
        station_code=station_code,
        triggered_by=triggered_by,
        source=source,
    )


def set_active_power(station_code: str, power_percent: float,
                      site_id: Optional[str] = None, triggered_by: Optional[str] = None,
                      source: str = "spot_reactor") -> Tuple[bool, Dict]:
    """
    Active power curtailment — obmedz výrobu na X% nominal.
    Užitočné pri záporných cenách spot trhu.
    power_percent: 0-100 (% nominal)
    """
    return _api_post(
        "/thirdData/setActivePowerControl",
        {
            "stationCodes": station_code,
            "activePowerPercent": int(power_percent),
        },
        command_type="active_power",
        site_id=site_id,
        station_code=station_code,
        triggered_by=triggered_by,
        source=source,
    )


def dispatch_plan(station_code: str, dispatch_points: List[Dict],
                   site_id: Optional[str] = None, triggered_by: Optional[str] = None,
                   source: str = "okte_dispatch") -> Tuple[bool, Dict]:
    """
    Real-time dispatch plan — pošle 96 (15-min) bodov do batt EMS.
    dispatch_points: [{time: 'HH:MM', power_kw: ±float}, ...]
    """
    return _api_post(
        "/thirdData/setRealTimeDispatch",
        {
            "stationCodes": station_code,
            "dispatchPoints": dispatch_points,
        },
        command_type="dispatch",
        site_id=site_id,
        station_code=station_code,
        triggered_by=triggered_by,
        source=source,
    )
