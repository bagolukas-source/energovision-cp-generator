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
            # Logger (device_type 9) na multi-menič staniciach RIADI export a prepisuje
            # per-inverter nastavenia — control musí ísť cez neho (ak povolí zápis)
            loggers = [d for d in devices if str(d.get("device_type")) == "9"]
            if inverters:
                sungrow_meta = {
                    "uuid": inverters[0].get("uuid"),
                    "ps_key": inverters[0].get("ps_key"),
                    "device_sn": inverters[0].get("device_sn"),
                    "device_model": inverters[0].get("device_model_code") or inverters[0].get("type_name"),
                    "inverter_count": len(inverters),
                    "all_uuids": [d.get("uuid") for d in inverters if d.get("uuid")],
                }
                if loggers:
                    sungrow_meta["logger_uuid"] = loggers[0].get("uuid")
                    sungrow_meta["logger_model"] = loggers[0].get("device_model_code") or loggers[0].get("type_name")
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

def _get_site_control_info(ps_id: str) -> Dict:
    """Meniče + prípadný logger stanice. Ak je logger, control ide cez NEHO —
    per-inverter zápisy by prepísal svojou konfiguráciou."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_sites",
        headers=_sb_headers(),
        params={"select": "metadata", "vendor": "eq.sungrow", "vendor_station_id": f"eq.{ps_id}", "limit": 1},
        timeout=10,
    )
    rows = r.json() if r.ok else []
    if not rows:
        return {"uuids": [], "logger_uuid": None}
    sg = (rows[0].get("metadata") or {}).get("sungrow") or {}
    uuids = sg.get("all_uuids") or ([sg.get("uuid")] if sg.get("uuid") else [])
    return {"uuids": [str(u) for u in uuids if u],
            "logger_uuid": str(sg["logger_uuid"]) if sg.get("logger_uuid") else None}


def _get_site_uuids(ps_id: str) -> List[str]:
    return _get_site_control_info(ps_id)["uuids"]


def _translate_enum(logical_value: str, param_result: Dict) -> Optional[str]:
    """Preloží logickú hodnotu 1/0 (enable/disable) na device enum podľa set_val_name_val.
    Napr. SG125CX: Feed-in power limitation Enable=170, Disable=85 — hodnotu '1' menič ticho odmietne."""
    names = [n.strip().lower() for n in (param_result.get("set_val_name") or "").split("|")]
    vals = [v.strip() for v in (param_result.get("set_val_name_val") or "").split("|")]
    if len(names) != len(vals) or len(names) < 2:
        return None
    mapping = dict(zip(names, vals))
    if str(logical_value) in ("1", "true"):
        return mapping.get("enable") or mapping.get("on")
    if str(logical_value) in ("0", "false"):
        return mapping.get("disable") or mapping.get("off")
    return None


def _values_match(desired: str, readback_param: Dict) -> bool:
    """Porovná želanú hodnotu s hodnotou prečítanou z meniča (enum aj numericky)."""
    rb = str(readback_param.get("return_value") if readback_param.get("return_value") is not None else "")
    d = str(desired)
    if rb == d:
        return True
    enum_d = _translate_enum(d, readback_param)
    if enum_d and rb == enum_d:
        return True
    try:
        return abs(float(rb) - float(d)) < 0.01
    except (TypeError, ValueError):
        return False


def _readback_verify(uuid: str, param_list: List[Dict]) -> Tuple[bool, List[Dict]]:
    """Ground truth: prečíta hodnoty z meniča a porovná so želanými.
    Vracia (all_match, mismatches[s enum info pre prípadný preklad])."""
    codes = [str(p["param_code"]) for p in param_list]
    rb = read_params(uuid, codes)
    if not rb.get("ok"):
        return False, [{"param_code": c, "error": "readback failed"} for c in codes]
    # read_params vracia zjednodušené hodnoty — potrebujeme aj enum mapy, doplň z raw ak treba
    by_code: Dict[str, Dict] = {}
    for v in rb.get("values") or []:
        by_code[str(v.get("param_code"))] = {"return_value": v.get("value"),
                                             "set_val_name": v.get("label"),
                                             "set_val_name_val": v.get("enum_vals")}
    mismatches = []
    for p in param_list:
        code = str(p["param_code"])
        rp = by_code.get(code)
        if rp is None or not _values_match(str(p["set_value"]), rp):
            mismatches.append({"param_code": code, "desired": p["set_value"],
                               "actual": (rp or {}).get("return_value"),
                               "set_val_name": (rp or {}).get("set_val_name"),
                               "set_val_name_val": (rp or {}).get("set_val_name_val")})
    return not mismatches, mismatches


def _submit_and_verify(uuid: str, param_list: List[Dict], task_name: str) -> Dict:
    """Submitne write task, počká na dokončenie a OVERÍ READBACKOM, že menič je v želanom stave.
    'Operation successful' tasku ani per-param status nestačia — jediná pravda je readback
    (param s hodnotou, ktorá už platí, menič 'zamietne', hoci stav je správny)."""
    ok, data = _call("/openapi/platform/paramSetting", {
        "set_type": 0, "uuid": str(uuid), "task_name": task_name,
        "expire_second": 120, "param_list": param_list,
    })
    if not ok:
        return {"ok": False, "error": data, "param_list": param_list}
    dev_results = (data or {}).get("dev_result_list") or []
    if not dev_results or str(dev_results[0].get("code")) != "1":
        return {"ok": False, "error": data, "param_list": param_list}
    task_id = dev_results[0].get("task_id")

    # počkaj na dokončenie tasku (best effort, max ~15 s — readback je aj tak rozhodujúci)
    for _ in range(5):
        time.sleep(3)
        ok2, task = check_task(task_id, uuid)
        if ok2 and isinstance(task, dict) and str(task.get("command_status")) == "8":
            break

    verified, mismatches = _readback_verify(uuid, param_list)
    return {"ok": verified, "task_id": task_id, "param_list": param_list,
            "failed_params": mismatches}


def _apply_to_device(uuid: str, param_list: List[Dict], task_name: str) -> Dict:
    """Zápis + readback verifikácia + prípadný enum-retry pre JEDEN menič."""
    res = _submit_and_verify(uuid, param_list, task_name)

    # Retry s enum prekladom: nezhodné parametre skús poslať s enum hodnotou z readbacku meniča
    if not res.get("ok") and res.get("failed_params"):
        corrected = []
        changed = False
        orig_by_code = {str(p["param_code"]): str(p["set_value"]) for p in param_list}
        for p in param_list:
            code = str(p["param_code"])
            fail = next((f for f in res["failed_params"] if str(f.get("param_code")) == code), None)
            if fail:
                enum_val = _translate_enum(orig_by_code[code], fail)
                if enum_val and enum_val != orig_by_code[code]:
                    corrected.append({"param_code": code, "set_value": enum_val})
                    changed = True
                    continue
            corrected.append(p)
        if changed:
            res2 = _submit_and_verify(uuid, corrected, task_name + "R")
            res2["enum_retry"] = True
            res = res2

    return {"uuid": uuid, "ok": bool(res.get("ok")), "task_id": res.get("task_id"),
            "enum_retry": res.get("enum_retry", False),
            "failed_params": res.get("failed_params") or [],
            "error": res.get("error")}


def _param_setting(uuids: List[str], param_list: List[Dict]) -> Tuple[bool, Dict]:
    """Pošle paramSetting na KAŽDÝ menič PARALELNE (gunicorn timeout 120 s — sériovo by
    multi-menič stanica nestihla), overí readbackom, enum hodnoty preloží automaticky."""
    import concurrent.futures
    task_name = f"Energovision SPOT {datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(uuids)) or 1) as ex:
        per_device = list(ex.map(lambda u: _apply_to_device(u, param_list, task_name), uuids))

    failed = [d for d in per_device if not d.get("ok")]
    result = {"devices": per_device, "devices_total": len(uuids), "devices_failed": len(failed),
              "param_list": param_list, "auth_method": "sungrow_oauth"}
    if failed:
        result["error"] = f"{len(failed)}/{len(uuids)} meničov nepotvrdilo zápis parametrov (readback)"
    return len(failed) == 0, result


def send_command(ps_id: str, command_type: str, params: Optional[Dict] = None) -> Tuple[bool, Dict]:
    """Vendor router adapter — rovnaké command_type ako Huawei (volané z huawei_spot.vendor_send_command)."""
    params = params or {}
    info = _get_site_control_info(str(ps_id))
    uuids = info["uuids"]
    logger_uuid = info["logger_uuid"]
    if not uuids and not logger_uuid:
        return False, {"error": f"chýbajú device uuids pre ps_id={ps_id} — spusti sync staníc (metadata.sungrow.all_uuids)"}

    def _logger_fail_hint(ok: bool, result: Dict) -> Tuple[bool, Dict]:
        if not ok:
            result["hint"] = (
                "Stanica má Logger, ktorý odmieta vzdialený zápis (per-param status 5). "
                "Servis: na Loggeri povoliť remote parameter setting / power control, "
                "prípadne kontaktovať Sungrow support."
            )
        return ok, result

    # Param codes: menič — 10012 feed-in switch (enum napr. 170/85), 10013 limit (kW),
    # 10007 active power switch, 10008 ratio (%). Logger — 10012 (enum 1/0), 10014 ratio (%).
    if command_type in ("enable_zero_export", "set_active_power_limit_zero"):
        if logger_uuid:
            # Logger RIADI export — per-inverter zápis by prepísal; píš na logger
            return _logger_fail_hint(*_param_setting([logger_uuid], [
                {"param_code": "10012", "set_value": "1"},
                {"param_code": "10014", "set_value": "0"},
            ]))
        return _param_setting(uuids, [
            {"param_code": "10012", "set_value": "1"},
            {"param_code": "10013", "set_value": "0"},
        ])
    if command_type in ("disable_zero_export", "normal_export"):
        if logger_uuid:
            return _logger_fail_hint(*_param_setting([logger_uuid], [
                {"param_code": "10012", "set_value": "0"},
            ]))
        return _param_setting(uuids, [
            {"param_code": "10012", "set_value": "0"},
            {"param_code": "10007", "set_value": "0"},
        ])
    if command_type == "set_active_power_limit":
        pct = float(params.get("limit_pct", 100))
        if logger_uuid:
            if pct >= 100:
                return _logger_fail_hint(*_param_setting([logger_uuid], [
                    {"param_code": "10012", "set_value": "0"},
                ]))
            return _logger_fail_hint(*_param_setting([logger_uuid], [
                {"param_code": "10012", "set_value": "1"},
                {"param_code": "10014", "set_value": str(int(pct))},
            ]))
        if pct >= 100:
            return _param_setting(uuids, [{"param_code": "10007", "set_value": "0"}])
        return _param_setting(uuids, [
            {"param_code": "10007", "set_value": "1"},
            {"param_code": "10008", "set_value": str(int(pct))},
        ])
    if command_type == "full_shutdown":
        if logger_uuid:
            # Logger vie garantovane riadiť len feed-in → SHUTDOWN degraduje na zero export
            return _logger_fail_hint(*_param_setting([logger_uuid], [
                {"param_code": "10012", "set_value": "1"},
                {"param_code": "10014", "set_value": "0"},
            ]))
        # Kurtailment na 0 % namiesto power-off (reverzibilnejšie; staršie FW môžu limit ignorovať)
        return _param_setting(uuids, [
            {"param_code": "10007", "set_value": "1"},
            {"param_code": "10008", "set_value": "0"},
        ])
    if command_type in ("set_battery_mode_self", "set_battery_mode_grid_charge", "forced_charge",
                        "forced_discharge", "stop_forced_charge_discharge"):
        return False, {"error": f"sungrow: {command_type} zatiaľ neimplementované (hybrid params 10004/10005)",
                       "non_critical": True}
    return False, {"error": f"unknown command_type: {command_type}"}


def check_control_support(ps_id: str) -> Dict:
    """Neinvazívna kontrola: podporujú VŠETKY meniče stanice remote paramSetting? (nič nenastavuje)"""
    uuids = _get_site_uuids(str(ps_id))
    if not uuids:
        return {"ok": False, "error": f"chýbajú uuids pre ps_id={ps_id} — spusti sync staníc"}
    per_device = []
    for uuid in uuids:
        ok, data = _call("/openapi/platform/paramSettingCheck", {"set_type": 0, "uuid": str(uuid)})
        dev_results = ((data or {}).get("dev_result_list") or []) if ok else []
        supported = bool(dev_results) and str(dev_results[0].get("check_result")) == "1"
        per_device.append({"uuid": uuid, "control_supported": supported if ok else None,
                           "error": None if ok else data})
    all_supported = all(d.get("control_supported") for d in per_device)
    return {"ok": True, "devices": per_device, "control_supported": all_supported}


def control_audit() -> Dict:
    """
    Overovací audit celej Sungrow flotily: pre každú stanicu neinvazívne skontroluje
    (paramSettingCheck), či VŠETKY meniče prijímajú remote príkazy.
    Výsledok uloží do inverter_sites.metadata.sungrow.control_check:
      {"status": "ok"|"partial"|"failed"|"error", "checked_at": iso,
       "devices_ok": n, "devices_total": m}
    Značka sa zobrazuje v CRM na /admin/spot/stanice — servis vidí, kde ovládanie nefunguje.
    """
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/inverter_sites",
        headers=_sb_headers(),
        params={"select": "id,vendor_station_id,site_name,metadata", "vendor": "eq.sungrow",
                "archived_at": "is.null"},
        timeout=20,
    )
    sites = r.json() if r.ok else []
    summary = {"checked": 0, "ok": 0, "partial": 0, "failed": 0, "error": 0, "details": []}

    for site in sites:
        ps_id = site.get("vendor_station_id")
        if not ps_id:
            continue
        res = check_control_support(str(ps_id))
        checked_at = datetime.now(timezone.utc).isoformat()
        if not res.get("ok"):
            check = {"status": "error", "checked_at": checked_at, "error": str(res.get("error"))[:150]}
            summary["error"] += 1
        else:
            devs = res.get("devices") or []
            n_ok = sum(1 for d in devs if d.get("control_supported"))
            status = "ok" if n_ok == len(devs) and devs else ("failed" if n_ok == 0 else "partial")
            check = {"status": status, "checked_at": checked_at,
                     "devices_ok": n_ok, "devices_total": len(devs)}
            summary[status] += 1
        summary["checked"] += 1

        merged = {**(site.get("metadata") or {})}
        merged["sungrow"] = {**(merged.get("sungrow") or {}), "control_check": check}
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/inverter_sites",
            headers=_sb_headers(),
            params={"id": f"eq.{site['id']}"},
            json={"metadata": merged},
            timeout=15,
        )
        if check["status"] != "ok":
            summary["details"].append({"site": site.get("site_name"), "ps_id": ps_id, **check})

    summary["ok_flag"] = True
    return summary


def check_task(task_id: str, uuid: str) -> Tuple[bool, Any]:
    """Poll výsledku paramSetting tasku (command_status 2=beží, 8=hotovo)."""
    return _call("/openapi/platform/getParamSettingTask", {"task_id": str(task_id), "uuid": str(uuid)})


def read_params(uuid: str, codes: List[str]) -> Dict:
    """Prečíta AKTUÁLNE hodnoty parametrov z meniča (set_type=2 = read, nič nemení).
    Submitne read task a polluje výsledok."""
    ok, data = _call("/openapi/platform/paramSetting", {
        "set_type": 2,
        "uuid": str(uuid),
        "task_name": f"Energovision read {datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "expire_second": 120,
        "param_list": [{"param_code": str(c), "set_value": ""} for c in codes],
    })
    if not ok:
        return {"ok": False, "step": "submit", "error": data}
    dev_results = (data or {}).get("dev_result_list") or []
    if not dev_results or str(dev_results[0].get("code")) != "1":
        return {"ok": False, "step": "submit", "error": data}
    task_id = dev_results[0].get("task_id")

    for attempt in range(6):
        time.sleep(3)
        ok2, task = check_task(task_id, uuid)
        if not ok2:
            continue
        status = str((task or {}).get("command_status"))
        if status == "8":  # done
            params = (task or {}).get("param_list") or []
            return {"ok": True, "task_id": task_id, "command_status": status,
                    "values": [{"param_code": p.get("param_code"),
                                "value": p.get("return_value") or p.get("set_value"),
                                "name": p.get("point_name"), "unit": p.get("unit"),
                                "label": p.get("set_val_name"),
                                "enum_vals": p.get("set_val_name_val")} for p in params]}
        if status not in ("2", "None"):  # iný stav než beží — vráť raw
            return {"ok": False, "task_id": task_id, "command_status": status, "raw": task}
    return {"ok": False, "task_id": task_id, "error": "timeout — task stále beží", "last": task if 'task' in dir() else None}


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
