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
    OAuth2 Client Credentials flow (Service Provider).
    Overene 2026-05-29: POST oauth2.fusionsolar.huawei.com/rest/dp/uidm/oauth2/v1/token
    Form body, scope=pvms.openapi.basic pvms.openapi.control, TTL 3600s.
    """
    client_id = cred.get("client_id", "")
    secret = _decrypt_secret(cred.get("encrypted_client_secret", ""))
    scope = cred.get("oauth_scope") or "pvms.openapi.basic pvms.openapi.control"

    if not client_id or not secret:
        return False, {"error": "missing client_id or client_secret"}

    url = "https://oauth2.fusionsolar.huawei.com/rest/dp/uidm/oauth2/v1/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": secret,
        "scope": scope,
    }

    try:
        r = requests.post(url, data=payload, timeout=30)
        if not r.ok:
            log.error("Token request failed: %s %s", r.status_code, r.text[:300])
            return False, {"error": f"HTTP {r.status_code}", "body": r.text[:300]}

        data = r.json()
        # OAuth2 RFC 6749 format: {access_token, token_type, expires_in, scope}
        if "access_token" in data:
            return True, {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "expires_in_sec": data.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS),
            }
        # Fallback — legacy NBI format (shouldn't reach this branch)
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

    # Cache expired → najprv skús refresh_token (owner-authorized) ak je v DB
    refresh_token = cred.get("refresh_token")
    if refresh_token:
        log.info("Refreshing OAuth token via refresh_token for %s", vendor)
        secret = _decrypt_secret(cred.get("encrypted_client_secret", ""))
        try:
            r = requests.post(
                "https://oauth2.fusionsolar.huawei.com/rest/dp/uidm/oauth2/v1/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": cred["client_id"],
                    "client_secret": secret,
                },
                timeout=30,
            )
            if r.ok:
                j = r.json()
                if "access_token" in j:
                    save_tokens(
                        cred_id=cred["id"],
                        access_token=j["access_token"],
                        refresh_token=j.get("refresh_token", refresh_token),
                        expires_in_sec=j.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS),
                    )
                    return j["access_token"]
            log.warning("refresh_token failed: %s %s", r.status_code, r.text[:200])
        except Exception as e:
            log.exception("refresh_token exception: %s", e)
    
    # Fallback: client_credentials (NIE owner-authorized, posledná možnosť)
    log.info("Falling back to client_credentials for %s", vendor)
    ok, result = request_new_token(cred)
    if not ok:
        log.error("Token request failed: %s", result)
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


# ============================================================
# AUTHORIZATION CODE FLOW (3-legged OAuth — per-customer)
# ============================================================

# Huawei OAuth endpoints (EU region):
HUAWEI_OAUTH_AUTHORIZE_URL = "https://oauth2.fusionsolar.huawei.com/rest/dp/uidm/oauth2/v1/authorize"
HUAWEI_OAUTH_TOKEN_URL = "https://oauth2.fusionsolar.huawei.com/rest/dp/uidm/oauth2/v1/token"


def build_authorize_url(state: str, redirect_uri: str, scope: str = "pvms.openapi.basic pvms.openapi.control") -> str:
    """
    Vyrobí URL na ktorý sa user (klient) zredirectuje aby autorizoval Energovision-EMS.
    
    Args:
        state: CSRF token (uložiť do huawei_customer_authorizations.state)
        redirect_uri: callback URL po authorizácii
        scope: comma-separated permission scopes
    """
    cred = load_oauth_credentials()
    if not cred:
        return ""
    
    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "client_id": cred["client_id"],
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    }
    return f"{HUAWEI_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str, redirect_uri: str) -> Tuple[bool, Optional[Dict]]:
    """
    Po callback: vymení authorization code za access_token + refresh_token.
    """
    cred = load_oauth_credentials()
    if not cred:
        return False, {"error": "no_credentials"}
    
    secret = _decrypt_secret(cred.get("encrypted_client_secret", ""))
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cred["client_id"],
        "client_secret": secret,
        "redirect_uri": redirect_uri,
    }
    
    try:
        r = requests.post(HUAWEI_OAUTH_TOKEN_URL, data=payload, timeout=30)
        if not r.ok:
            log.error("Code exchange failed: %s %s", r.status_code, r.text[:300])
            return False, {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
        data = r.json()
        return True, {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_in_sec": data.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS),
            "scope": data.get("scope"),
            "huawei_user_id": data.get("openid") or data.get("uid"),
        }
    except Exception as e:
        log.exception("Code exchange exception")
        return False, {"error": str(e)}


def refresh_customer_token(customer_id: str) -> Tuple[bool, Optional[str]]:
    """
    Refresh access_token pre konkrétneho klienta cez jeho refresh_token.
    """
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/huawei_customer_authorizations",
        headers=_sb_headers(),
        params={
            "select": "id,refresh_token",
            "customer_id": f"eq.{customer_id}",
            "revoked_at": "is.null",
        },
        timeout=10,
    )
    if not r.ok or not r.json():
        return False, None
    
    auth_row = r.json()[0]
    refresh_token = auth_row.get("refresh_token")
    if not refresh_token:
        return False, None
    
    cred = load_oauth_credentials()
    if not cred:
        return False, None
    secret = _decrypt_secret(cred.get("encrypted_client_secret", ""))
    
    try:
        resp = requests.post(
            HUAWEI_OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": cred["client_id"],
                "client_secret": secret,
            },
            timeout=30,
        )
        if not resp.ok:
            log.error("Refresh failed: %s", resp.text[:300])
            return False, None
        data = resp.json()
        
        # Persist new tokens
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token", refresh_token)  # keep old ak nový nie je daný
        expires_in = data.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/huawei_customer_authorizations",
            headers=_sb_headers(),
            params={"id": f"eq.{auth_row['id']}"},
            json={
                "access_token": new_access,
                "refresh_token": new_refresh,
                "token_expires_at": expires_at.isoformat(),
                "last_refresh_at": datetime.now(timezone.utc).isoformat(),
            },
            timeout=10,
        )
        return True, new_access
    except Exception as e:
        log.exception("Refresh exception")
        return False, None


def get_customer_access_token(customer_id: str) -> Optional[str]:
    """Vráti platný access_token pre konkrétneho klienta. Auto-refresh ak vypršal."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/huawei_customer_authorizations",
        headers=_sb_headers(),
        params={
            "select": "access_token,token_expires_at",
            "customer_id": f"eq.{customer_id}",
            "revoked_at": "is.null",
        },
        timeout=10,
    )
    if not r.ok or not r.json():
        return None
    
    auth_row = r.json()[0]
    token = auth_row.get("access_token")
    expires_str = auth_row.get("token_expires_at")
    
    if token and expires_str:
        try:
            expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            if expires_at > datetime.now(timezone.utc):
                return token
        except Exception:
            pass
    
    # Expired → refresh
    ok, new_token = refresh_customer_token(customer_id)
    return new_token if ok else None


def save_customer_authorization(customer_id: str, state: str, tokens: Dict, initiated_by: Optional[str] = None) -> str:
    """Uloží nového customer-a po úspešnej OAuth authorizácii."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in_sec", DEFAULT_TOKEN_TTL_SECONDS) - 60)
    
    # Najprv check či customer už má aktívnu authorization (nie revoked) — update miesto insert
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/huawei_customer_authorizations",
        headers=_sb_headers(),
        params={
            "select": "id",
            "customer_id": f"eq.{customer_id}",
            "revoked_at": "is.null",
        },
        timeout=10,
    )
    existing = r.json() if r.ok else []
    
    payload = {
        "customer_id": customer_id,
        "huawei_user_id": tokens.get("huawei_user_id"),
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_expires_at": expires_at.isoformat(),
        "scope_granted": tokens.get("scope"),
        "state": state,
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "initiated_by": initiated_by,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    
    if existing:
        # Update
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/huawei_customer_authorizations",
            headers={**_sb_headers(), "Prefer": "return=representation"},
            params={"id": f"eq.{existing[0]['id']}"},
            json=payload,
            timeout=10,
        )
        return existing[0]["id"]
    else:
        # Insert
        r2 = requests.post(
            f"{SUPABASE_URL}/rest/v1/huawei_customer_authorizations",
            headers={**_sb_headers(), "Prefer": "return=representation"},
            json=payload,
            timeout=10,
        )
        if r2.ok:
            return r2.json()[0]["id"]
    return None


def revoke_customer_authorization(customer_id: str) -> bool:
    """Klient revokoval prístup. Označiť ako revoked."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/huawei_customer_authorizations",
        headers=_sb_headers(),
        params={"customer_id": f"eq.{customer_id}", "revoked_at": "is.null"},
        json={"revoked_at": datetime.now(timezone.utc).isoformat()},
        timeout=10,
    )
    return r.ok


# ============================================================
# CLIENT CREDENTIALS — SMART DIAGNOSTIC (try multiple endpoints)
# ============================================================
def try_client_credentials_smart() -> Dict:
    """
    Skúsi viac endpointov + auth štýlov a vráti detailný report.
    Použité diagnostické rozhranie pre app.py /api/huawei/test-token.

    Vráti: {"success": True, "endpoint_used": "...", "access_token": "...",
            "attempts": [{endpoint, method, status, snippet}, ...]}
    """
    cred = load_oauth_credentials()
    if not cred:
        return {"success": False, "error": "no credentials in DB"}

    client_id = cred.get("client_id", "")
    secret = _decrypt_secret(cred.get("encrypted_client_secret", ""))
    scope = cred.get("oauth_scope") or "pvms.openapi.basic pvms.openapi.control"

    if not client_id or not secret:
        return {"success": False, "error": "missing client_id/secret"}

    # Kandidáti — Huawei má 2 paralelné svety: NBI thirdData a OAuth2
    candidates = [
        # NBI legacy — JSON body
        {
            "name": "NBI eu5 thirdData/login (userName)",
            "url": "https://eu5.fusionsolar.huawei.com/thirdData/login",
            "method": "POST",
            "json": {"userName": client_id, "systemCode": secret},
        },
        {
            "name": "NBI intl thirdData/login (userName)",
            "url": "https://intl.fusionsolar.huawei.com/thirdData/login",
            "method": "POST",
            "json": {"userName": client_id, "systemCode": secret},
        },
        # NBI token endpoint (Service Provider)
        {
            "name": "NBI eu5 thirdData/token (client_credentials)",
            "url": "https://eu5.fusionsolar.huawei.com/thirdData/token",
            "method": "POST",
            "json": {
                "client_id": client_id,
                "client_secret": secret,
                "grant_type": "client_credentials",
            },
        },
        # OAuth2 — form body
        {
            "name": "OAuth2 token (form body)",
            "url": "https://oauth2.fusionsolar.huawei.com/rest/dp/uidm/oauth2/v1/token",
            "method": "POST",
            "data": {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
                "scope": scope,
            },
        },
        # OAuth2 — Basic auth + form body
        {
            "name": "OAuth2 token (Basic auth)",
            "url": "https://oauth2.fusionsolar.huawei.com/rest/dp/uidm/oauth2/v1/token",
            "method": "POST",
            "data": {
                "grant_type": "client_credentials",
                "scope": scope,
            },
            "auth": (client_id, secret),
        },
    ]

    attempts = []
    success_attempt = None

    for c in candidates:
        try:
            kwargs = {"timeout": 30}
            if "json" in c:
                kwargs["json"] = c["json"]
            if "data" in c:
                kwargs["data"] = c["data"]
            if "auth" in c:
                kwargs["auth"] = c["auth"]

            r = requests.request(c["method"], c["url"], **kwargs)
            body = r.text[:500]
            attempt = {
                "name": c["name"],
                "url": c["url"],
                "status": r.status_code,
                "snippet": body,
            }

            # Skús extrahovať token
            token = None
            ttl = None
            try:
                j = r.json()
                # OAuth2 format
                if "access_token" in j:
                    token = j["access_token"]
                    ttl = j.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)
                # NBI format
                elif j.get("success") and isinstance(j.get("data"), dict):
                    d = j["data"]
                    token = d.get("accessToken") or d.get("access_token")
                    ttl = d.get("expiresIn", DEFAULT_TOKEN_TTL_SECONDS)
                # NBI login cookie response
                elif j.get("success") is True:
                    # XSRF-TOKEN cookie based — use cookie value
                    xsrf = r.cookies.get("XSRF-TOKEN")
                    if xsrf:
                        token = xsrf
                        ttl = 30 * 60  # ~30 min
                attempt["fail_code"] = j.get("failCode")
                attempt["fail_msg"] = j.get("message") or j.get("data")
            except Exception:
                pass

            if token:
                attempt["token_preview"] = token[:20] + "..."
                attempt["ttl_sec"] = ttl
                attempts.append(attempt)
                success_attempt = {**attempt, "access_token": token, "ttl_sec": ttl}
                break

            attempts.append(attempt)
        except Exception as e:
            attempts.append({
                "name": c["name"],
                "url": c["url"],
                "error": str(e)[:200],
            })

    result = {
        "success": success_attempt is not None,
        "attempts": attempts,
        "credentials_used": {
            "client_id": client_id,
            "client_id_len": len(client_id),
            "secret_len": len(secret),
            "scope": scope,
            "base_url_in_db": cred.get("base_url"),
        },
    }
    if success_attempt:
        result["endpoint_used"] = success_attempt["name"]
        result["token_preview"] = success_attempt["token_preview"]
        result["ttl_sec"] = success_attempt["ttl_sec"]

        # Persist token do DB
        try:
            save_tokens(
                cred_id=cred["id"],
                access_token=success_attempt["access_token"],
                refresh_token=None,
                expires_in_sec=success_attempt.get("ttl_sec") or DEFAULT_TOKEN_TTL_SECONDS,
            )
            result["saved_to_db"] = True
        except Exception as e:
            result["saved_to_db"] = False
            result["save_error"] = str(e)[:200]

    return result
