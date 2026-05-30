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
        "/v2/login",
        "/login",
        "/api/login",
    ]
    # Známe body varianty
    bodies = [
        {"email": email, "password": password, "clientId": client_id},
        {"email": email, "password": password, "client_id": client_id},
        {"userName": email, "password": password, "clientId": client_id},
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
                        timeout=15,
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
