"""
Sungrow iSolarCloud OpenAPI adapter (OAuth2 developer-portal app, EU gateway).

Auth model:
- Authorization URL (user klik): https://web3.isolarcloud.eu/#/authorized-app?cloudId=3&applicationId=4679&redirectUrl=<callback>
- Redirect nesie LEN ?code= (state sa späť nespoľahlivo neposiela) → master-mode callback.
- Token exchange: POST /openapi/apiManage/token {appkey, code, grant_type, redirect_uri}
- Refresh: POST /openapi/apiManage/refreshToken {appkey, refresh_token} — refresh token SA ROTUJE, vždy persistnúť nový.
- Každý call: headers x-access-key=<secret> + Authorization: Bearer <access_token>; body vždy appkey + lang.
- Success: result_code == "1", payload v result_data.

Control (vyžaduje schválený Configuration & Control scope):
- POST /openapi/platform/paramSetting {set_type:0, uuid, task_name, expire_second, param_list:[{param_code, set_value}]}
- uuid zariadenia z getDeviceListByPsId (ukladáme do inverter_sites.metadata.sungrow)
- param codes: 10012/10013 feed-in limitation (zero export), 10007/10008 active power limit,
  10011 power on/off. Pozn.: staršie firmvéry môžu power limit ignorovať — overiť per model.

Cloud data refresh ~5 min → nepollovať častejšie.
"""
import os
import re
import time
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List, Any

log = logging.getLogger("sungrow.oauth")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")

GATEWAY = "https://gateway.isolarcloud.eu"
DEFAULT_TTL = 3600  # fallback keď response nemá expires_in


def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


def load_credentials() -> Optional[Dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
        headers=_sb_headers(),
        params={
            "select": "id,base_url,client_id,encrypted_client_secret,current_token,refresh_token,token_expires_at,oauth_callback_url,token_source",
            "vendor": "eq.sungrow",
            "is_active": "eq.true",
            "limit": 1,
        },
        timeout=10,
    )
    rows = r.json() if r.ok else []
    return rows[0] if rows else None


def save_tokens(cred_id: str, access_token: str, refresh_token: Optional[str], expires_in_sec: int) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, expires_in_sec - 60))
    payload = {
        "current_token": access_token,
        "token_expires_at": expires_at.isoformat(),
        "last_token_refresh_at": datetime.now(timezone.utc).isoformat(),
        "token_source": "owner_authorization",
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


def _api_headers(cred: Dict, token: Optional[str] = None) -> Dict[str, str]:
    h = {
        "x-access-key": cred.get("encrypted_client_secret") or "",
        "Content-Type": "application/json;charset=UTF-8",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def exchange_code_for_tokens(code: str) -> Tuple[bool, Dict]:
    """Výmena authorization code za tokeny. Vracia (ok, tokens|error)."""
    cred = load_credentials()
    if not cred:
        return False, {"error": "no sungrow credentials in DB"}
    if not cred.get("encrypted_client_secret"):
        return False, {"error": "chýba secret key (x-access-key) v inverter_vendor_credentials"}

    body = {
        "appkey": cred.get("client_id"),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": cred.get("oauth_callback_url"),
    }
    try:
        r = requests.post(f"{GATEWAY}/openapi/apiManage/token", headers=_api_headers(cred), json=body, timeout=30)
        j = r.json() if r.text else {}
        # tokeny sú na top-level; niektoré verzie ich balia do result_data
        data = j if "access_token" in j else (j.get("result_data") or {})
        if not r.ok or "access_token" not in data:
            return False, {"error": f"HTTP {r.status_code}", "body": str(j)[:400]}
        save_tokens(
            cred_id=cred["id"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in_sec=int(data.get("expires_in") or DEFAULT_TTL),
        )
        return True, {
            "auth_ps_list": data.get("auth_ps_list"),
            "auth_user": data.get("auth_user"),
            "expires_in": data.get("expires_in"),
        }
    except Exception as e:
        log.exception("sungrow code exchange failed")
        return False, {"error": str(e)[:300]}


def get_valid_token(force: bool = False) -> Optional[str]:
    """Platný access_token z cache alebo refresh (s DB mutexom proti súbežným workerom)."""
    cred = load_credentials()
    if not cred:
        return None

    if not force:
        token = cred.get("current_token")
        exp = cred.get("token_expires_at")
        if token and exp:
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if exp_dt > datetime.now(timezone.utc):
                    return token
            except Exception:
                pass

    refresh_token = cred.get("refresh_token")
    if not refresh_token:
        log.warning("sungrow: žiadny refresh_token — treba OAuth autorizáciu")
        return None

    # Mutex: refresh token sa ROTUJE — súbežný refresh z 2 workerov by druhému zneplatnil grant.
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    try:
        claim = requests.patch(
            f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
            headers={**_sb_headers(), "Prefer": "return=representation"},
            params={
                "id": f"eq.{cred['id']}",
                "or": f"(last_token_refresh_at.is.null,last_token_refresh_at.lt.{cutoff_iso})",
            },
            json={"last_token_refresh_at": now_iso},
            timeout=10,
        )
        if claim.ok and not claim.json():
            time.sleep(2.5)
            cred2 = load_credentials()
            return cred2.get("current_token") if cred2 else None
    except Exception as e:
        log.warning("sungrow refresh mutex fail (pokračujem): %s", e)

    body = {"appkey": cred.get("client_id"), "refresh_token": refresh_token}
    try:
        r = requests.post(f"{GATEWAY}/openapi/apiManage/refreshToken", headers=_api_headers(cred), json=body, timeout=30)
        j = r.json() if r.text else {}
        data = j if "access_token" in j else (j.get("result_data") or {})
        if "access_token" in data:
            save_tokens(
                cred_id=cred["id"],
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token") or refresh_token,
                expires_in_sec=int(data.get("expires_in") or DEFAULT_TTL),
            )
            return data["access_token"]
        log.error("sungrow refresh failed: %s", str(j)[:300])
        _slack(f":rotating_light: Sungrow OAuth refresh zlyhal — treba re-autorizovať v iSolarCloud. {str(j)[:150]}")
        return None
    except Exception as e:
        log.exception("sungrow refresh exception")
        return None


def _slack(text: str) -> None:
    hook = os.environ.get("SLACK_WEBHOOK_OPS", "")
    if not hook:
        return
    try:
        requests.post(hook, json={"text": text}, timeout=10)
    except Exception:
        pass


def _call(path: str, body: Dict, retry_on_auth: bool = True) -> Tuple[bool, Any]:
    """Generický OpenAPI call: appkey+lang v body, Bearer header, result_code=='1' = success."""
    cred = load_credentials()
    if not cred:
        return False, {"error": "no credentials"}
    token = get_valid_token()
    if not token:
        return False, {"error": "no token — chýba OAuth autorizácia (klikni Authorization URL v iSolarCloud)"}

    full_body = {"appkey": cred.get("client_id"), "lang": "_en_US", **body}
    try:
        r = requests.post(f"{GATEWAY}{path}", headers=_api_headers(cred, token), json=full_body, timeout=30)
        j = r.json() if r.text else {}
        if isinstance(j, dict) and str(j.get("result_code")) == "1":
            return True, j.get("result_data")
        # auth chyba → jeden force-refresh retry (token mohol byť rotovaný iným workerom)
        if retry_on_auth and isinstance(j, dict) and str(j.get("result_code")) in ("E00003", "401", "010", "1002"):
            token2 = get_valid_token(force=True)
            if token2:
                r2 = requests.post(f"{GATEWAY}{path}", headers=_api_headers(cred, token2), json=full_body, timeout=30)
                j2 = r2.json() if r2.text else {}
                if isinstance(j2, dict) and str(j2.get("result_code")) == "1":
                    return True, j2.get("result_data")
                return False, {"result_code": j2.get("result_code"), "msg": j2.get("result_msg"), "retried": True}
        return False, {"http": r.status_code, "result_code": j.get("result_code") if isinstance(j, dict) else None,
                       "msg": j.get("result_msg") if isinstance(j, dict) else str(j)[:200]}
    except Exception as e:
        return False, {"error": str(e)[:300]}


# ============================================================================
# Stations & devices
# ============================================================================

def list_plants() -> Tuple[bool, Any]:
    plants: List[Dict] = []
    page = 1
    while page <= 20:
        ok, data = _call("/openapi/platform/queryPowerStationList", {"page": page, "size": 100})
        if not ok:
            return (True, plants) if plants else (False, data)
        rows = (data or {}).get("pageList") or []
        plants.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    return True, plants


CAP_KEYS = ("ps_capacity", "total_capcity", "total_capacity", "design_capacity", "install_capacity")


def _cap_to_kwp(v) -> float:
    """Kapacita môže prísť ako číslo, string alebo dict {value, unit} (kW/kWp/MWp/W)."""
    unit = ""
    if isinstance(v, dict):
        unit = str(v.get("unit") or "")
        v = v.get("value")
    try:
        f = float(v)
    except Exception:
        return 0.0
    u = unit.lower()
    if "mw" in u:
        return f * 1000
    if u in ("w", "wp"):
        return f / 1000
    return f


def _extract_capacity(d: Dict) -> float:
    for k in CAP_KEYS:
        if d.get(k) is not None:
            kwp = _cap_to_kwp(d[k])
            if kwp > 0:
                return kwp
    return 0.0


def _model_ac_kw(model: str) -> float:
    """AC rating z názvu modelu: SG33CX-P2 → 33, SG110CX → 110, SH10RT → 10."""
    m = re.search(r"S[GH](\d+(?:\.\d+)?)", str(model or "").upper())
    return float(m.group(1)) if m else 0.0


def fetch_station_details(ps_ids: List[str]) -> Dict[str, Dict]:
    """getPowerStationDetail v dávkach po 50 — kapacita často chýba v pageListe."""
    out: Dict[str, Dict] = {}
    for i in range(0, len(ps_ids), 50):
        chunk = ps_ids[i:i + 50]
        ok, data = _call("/openapi/platform/getPowerStationDetail", {"ps_ids": ",".join(chunk)})
        if ok:
            for row in (data or {}).get("data_list") or []:
                out[str(row.get("ps_id"))] = row
    return out


def list_devices(ps_id: str) -> Tuple[bool, Any]:
    ok, data = _call("/openapi/platform/getDeviceListByPsId", {"ps_id": str(ps_id), "page": 1, "size": 100})
    if not ok:
        return False, data
    return True, (data or {}).get("pageList") or []


def sync_stations() -> Dict[str, Any]:
    """Pull plant list z iSolarCloud, upsert do inverter_sites (vendor='sungrow').
    Do metadata.sungrow uloží uuid/ps_key prvého meniča (potrebné pre control)."""
    ok, plants = list_plants()
    if not ok:
        return {"ok": False, "error": "plant list failed", "detail": plants,
                "hint": "Skontroluj secret key a či prebehla OAuth autorizácia (auth_ps_list)."}

    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_sites",
        headers=_sb_headers(),
        params={"select": "id,vendor_station_id,metadata", "vendor": "eq.sungrow"},
        timeout=15,
    ).json()
    existing_map = {str(s.get("vendor_station_id")): s for s in existing if s.get("vendor_station_id")}

    # Kapacita v pageListe často chýba → dotiahni detaily v dávkach
    detail_map = fetch_station_details([str(p.get("ps_id")) for p in plants if p.get("ps_id")])

    added, updated, skipped = 0, 0, 0
    details = []
    for p in plants:
        ps_id = str(p.get("ps_id") or "")
        if not ps_id:
            skipped += 1
            continue
        name = p.get("ps_name") or ps_id
        dc_kwp = _extract_capacity(p) or _extract_capacity(detail_map.get(ps_id, {}))

        # uuid inverteru pre control API + AC výkon z modelov meničov
        sungrow_meta = {}
        ac_kw = 0.0
        dev_ok, devices = list_devices(ps_id)
        if dev_ok:
            inverters = [d for d in devices if str(d.get("device_type")) == "1"]
            if inverters:
                sungrow_meta = {
                    "uuid": inverters[0].get("uuid"),
                    "ps_key": inverters[0].get("ps_key"),
                    "device_sn": inverters[0].get("device_sn"),
                    "device_model": inverters[0].get("device_model_code") or inverters[0].get("type_name"),
                    "inverter_count": len(inverters),
                    "all_uuids": [d.get("uuid") for d in inverters if d.get("uuid")],
                }
                ac_kw = sum(_model_ac_kw(d.get("device_model_code") or d.get("type_name")) for d in inverters)

        row = {
            "vendor": "sungrow",
            "vendor_station_id": ps_id,
            "site_name": name,
            "dc_kwp": dc_kwp,
            "monitoring_enabled": True,
        }
        if ac_kw > 0:
            row["ac_kw"] = ac_kw
        if p.get("latitude") is not None:
            row["latitude"] = p.get("latitude")
        if p.get("longitude") is not None:
            row["longitude"] = p.get("longitude")

        if ps_id in existing_map:
            site = existing_map[ps_id]
            merged_meta = {**(site.get("metadata") or {})}
            if sungrow_meta:
                merged_meta["sungrow"] = sungrow_meta
            patch = {"site_name": name, "metadata": merged_meta,
                     "last_sync_at": datetime.now(timezone.utc).isoformat()}
            if dc_kwp > 0:
                patch["dc_kwp"] = dc_kwp
            if ac_kw > 0:
                patch["ac_kw"] = ac_kw
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/inverter_sites",
                headers=_sb_headers(),
                params={"id": f"eq.{site['id']}"},
                json=patch, timeout=15,
            )
            updated += 1
            details.append({"action": "updated", "ps_id": ps_id, "name": name})
        else:
            if sungrow_meta:
                row["metadata"] = {"sungrow": sungrow_meta}
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/inverter_sites",
                headers={**_sb_headers(), "Prefer": "return=minimal"},
                json=[row], timeout=15,
            )
            if r.status_code in (200, 201):
                added += 1
                details.append({"action": "added", "ps_id": ps_id, "name": name, "dc_kwp": dc_kwp})
            else:
                skipped += 1
                details.append({"action": "skipped", "ps_id": ps_id, "error": r.text[:150]})

    return {"ok": True, "total_sungrow": len(plants), "added": added, "updated": updated,
            "skipped": skipped, "details": details[:30]}


# ============================================================================
# Control — paramSetting (vyžaduje Configuration & Control scope na appke)
# ============================================================================

def _get_site_uuid(ps_id: str) -> Optional[str]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_sites",
        headers=_sb_headers(),
        params={"select": "metadata", "vendor": "eq.sungrow", "vendor_station_id": f"eq.{ps_id}", "limit": 1},
        timeout=10,
    )
    rows = r.json() if r.ok else []
    if rows:
        return ((rows[0].get("metadata") or {}).get("sungrow") or {}).get("uuid")
    return None


def _param_setting(uuid: str, param_list: List[Dict]) -> Tuple[bool, Dict]:
    ok, data = _call("/openapi/platform/paramSetting", {
        "set_type": 0,
        "uuid": str(uuid),
        "task_name": f"Energovision SPOT {datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "expire_second": 120,
        "param_list": param_list,
    })
    if not ok:
        return False, {"error": "paramSetting failed", "detail": data, "param_list": param_list}
    dev_results = (data or {}).get("dev_result_list") or []
    task_ok = bool(dev_results) and str(dev_results[0].get("code")) == "1"
    return task_ok, {"task_id": (dev_results[0].get("task_id") if dev_results else None),
                     "raw": data, "param_list": param_list, "auth_method": "sungrow_oauth"}


def send_command(ps_id: str, command_type: str, params: Optional[Dict] = None) -> Tuple[bool, Dict]:
    """Vendor router adapter — rovnaké command_type ako Huawei (volané z huawei_spot.vendor_send_command)."""
    params = params or {}
    uuid = _get_site_uuid(str(ps_id))
    if not uuid:
        return False, {"error": f"chýba device uuid pre ps_id={ps_id} — spusti sync staníc (metadata.sungrow.uuid)"}

    # Param codes (pysolarcloud, validované na live EU API):
    # 10012 feed_in_limitation switch, 10013 feed_in_limitation_value (W),
    # 10007 limited_power_switch, 10008 active_power_limit_ratio (%), 10011 power_on
    if command_type in ("enable_zero_export", "set_active_power_limit_zero"):
        return _param_setting(uuid, [
            {"param_code": "10012", "set_value": "1"},
            {"param_code": "10013", "set_value": "0"},
        ])
    if command_type in ("disable_zero_export", "normal_export"):
        return _param_setting(uuid, [
            {"param_code": "10012", "set_value": "0"},
            {"param_code": "10007", "set_value": "0"},
        ])
    if command_type == "set_active_power_limit":
        pct = float(params.get("limit_pct", 100))
        if pct >= 100:
            return _param_setting(uuid, [{"param_code": "10007", "set_value": "0"}])
        return _param_setting(uuid, [
            {"param_code": "10007", "set_value": "1"},
            {"param_code": "10008", "set_value": str(int(pct))},
        ])
    if command_type == "full_shutdown":
        # Kurtailment na 0 % namiesto power-off (reverzibilnejšie; staršie FW môžu limit ignorovať)
        return _param_setting(uuid, [
            {"param_code": "10007", "set_value": "1"},
            {"param_code": "10008", "set_value": "0"},
        ])
    if command_type in ("set_battery_mode_self", "set_battery_mode_grid_charge", "forced_charge",
                        "forced_discharge", "stop_forced_charge_discharge"):
        return False, {"error": f"sungrow: {command_type} zatiaľ neimplementované (hybrid params 10004/10005)",
                       "non_critical": True}
    return False, {"error": f"unknown command_type: {command_type}"}


def check_task(task_id: str, uuid: str) -> Tuple[bool, Any]:
    """Poll výsledku paramSetting tasku (command_status 2=beží, 8=hotovo)."""
    return _call("/openapi/platform/getParamSettingTask", {"task_id": str(task_id), "uuid": str(uuid)})


def diagnose() -> Dict:
    """Diagnostika pre /api/sungrow/test: credentials → token → plant list."""
    cred = load_credentials()
    out: Dict[str, Any] = {
        "has_credentials": bool(cred),
        "has_secret": bool(cred and cred.get("encrypted_client_secret")),
        "has_refresh_token": bool(cred and cred.get("refresh_token")),
        "appkey": (cred or {}).get("client_id"),
        "callback": (cred or {}).get("oauth_callback_url"),
    }
    if not cred or not cred.get("encrypted_client_secret"):
        out["next_step"] = "Ulož secret key do inverter_vendor_credentials.encrypted_client_secret"
        return out
    if not cred.get("refresh_token"):
        out["next_step"] = "Klikni Authorization URL v iSolarCloud a dokonči autorizáciu plantov"
        return out
    token = get_valid_token()
    out["token_ok"] = bool(token)
    if token:
        ok, plants = list_plants()
        out["plant_list_ok"] = ok
        out["plant_count"] = len(plants) if ok else None
        if not ok:
            out["plant_list_error"] = plants
    return out
