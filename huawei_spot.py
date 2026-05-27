"""
huawei_spot.py — OKTE DAM ingest + SPOT 3-state reactor + Huawei FusionSolar command bridge.
"""
from __future__ import annotations

import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Dict, Any, List, Tuple

import requests
import json as _pj

log = logging.getLogger(__name__)

# ---------------- Config ----------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
OKTE_BASE = os.environ.get("OKTE_BASE_URL", "https://isot.okte.sk")
HUAWEI_BASE = os.environ.get("HUAWEI_BASE_URL", "https://eu5.fusionsolar.huawei.com/thirdData")
HUAWEI_USER = os.environ.get("HUAWEI_USER", "energovision_api")
HUAWEI_PASS = os.environ.get("HUAWEI_PASS", "")
SLACK_WEBHOOK_OPS = os.environ.get("SLACK_WEBHOOK_OPS", "")

_huawei_session: Dict[str, Any] = {"token": None, "expires_at": 0}

# Per-endpoint rate limit tracking (NBI doc: Plant List = 5 calls/10min per account)
# Cache výsledok na 5 min aby sa nevolal každý klik
_stations_cache: Dict[str, Any] = {"data": None, "cached_at": 0, "next_allowed_at": 0, "backoff_until": 0}


def _sb_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def sb_get(path: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    r = requests.get(url, headers=_sb_headers(), params=params or {}, timeout=30)
    if r.status_code >= 400:
        log.error("[sb_get] %s %s -> %s %s", path, params, r.status_code, r.text[:300])
        return []
    return r.json()


def sb_post(path: str, rows: List[Dict[str, Any]], on_conflict: Optional[str] = None) -> Tuple[bool, str]:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(url, headers=headers, json=rows, timeout=60)
    if r.status_code in (200, 201, 204):
        return True, ""
    return False, f"{r.status_code}: {r.text[:300]}"


def sb_patch(path: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {**_sb_headers(), "Prefer": "return=minimal"}
    r = requests.patch(url, headers=headers, json=payload, timeout=30)
    if r.status_code in (200, 204):
        return True, ""
    return False, f"{r.status_code}: {r.text[:300]}"


# ---------------- OKTE DAM ----------------
def okte_fetch_day(target_day: date) -> List[Dict[str, Any]]:
    day_str = target_day.isoformat()
    url = f"{OKTE_BASE}/api/v1/dam/results"
    params = {"deliveryDayFrom": day_str, "deliveryDayTo": day_str}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data if isinstance(data, list) else []


def okte_normalize(record: Dict[str, Any], target_day: date) -> Optional[Dict[str, Any]]:
    try:
        # Use explicit None checks — `or` operator broken for price=0 (legit zero clearing price)
        period = record.get("period")
        if period is None:
            period = record.get("Period")
        if period is None:
            period = record.get("hour")
        if period is None:
            return None
        if isinstance(period, str):
            period = int(period)
        delivery_start = record.get("deliveryStart") or record.get("DeliveryStart") or record.get("deliveryStartUtc")
        delivery_end = record.get("deliveryEnd") or record.get("DeliveryEnd") or record.get("deliveryEndUtc")
        # price môže byť 0 alebo záporné — NESMIE byť skrátené cez `or`
        price = record.get("price")
        if price is None:
            price = record.get("Price")
        if price is None:
            price = record.get("priceEurMwh")
        if delivery_start is None or price is None:
            return None
        if not delivery_end:
            try:
                dt = datetime.fromisoformat(delivery_start.replace("Z", "+00:00"))
                delivery_end = (dt + timedelta(minutes=15)).isoformat()
            except Exception:
                return None
        return {
            "market": "okte_dam_sk",
            "delivery_day": target_day.isoformat(),
            "period": int(period),
            "delivery_start": delivery_start,
            "delivery_end": delivery_end,
            "price_eur_mwh": float(price),
            "publication_status": record.get("publicationStatus") or record.get("status"),
            "raw_json": record,
        }
    except Exception as e:
        log.warning("[okte_normalize] zlyhalo: %s", e)
        return None


def okte_ingest(target_day: Optional[date] = None, backfill_days: int = 0) -> Dict[str, Any]:
    if target_day is None:
        days = [date.today() + timedelta(days=1), date.today()]
    else:
        days = [target_day]
    if backfill_days > 0:
        base = days[-1]
        for i in range(1, backfill_days + 1):
            days.append(base - timedelta(days=i))

    results = []
    for d in days:
        try:
            raw = okte_fetch_day(d)
            normalized = [okte_normalize(r, d) for r in raw]
            normalized = [n for n in normalized if n]
            if not normalized:
                results.append({"day": d.isoformat(), "ok": False, "error": "no data"})
                continue
            ok, err = sb_post("spot_prices", normalized, on_conflict="market,delivery_day,period")
            results.append({
                "day": d.isoformat(),
                "ok": ok,
                "count": len(normalized),
                "min_price": min(n["price_eur_mwh"] for n in normalized),
                "max_price": max(n["price_eur_mwh"] for n in normalized),
                "negative_count": sum(1 for n in normalized if n["price_eur_mwh"] < 0),
                "error": err or None,
            })
        except Exception as e:
            log.exception("[okte_ingest] day=%s failed", d)
            results.append({"day": d.isoformat(), "ok": False, "error": str(e)})

    return {"ok": True, "results": results, "fetched_at": datetime.now(timezone.utc).isoformat()}


# ---------------- Huawei FusionSolar API ----------------
def _load_huawei_credentials_from_db() -> Dict[str, Any]:
    """Load Huawei credentials + cached token + backoff state from inverter_vendor_credentials."""
    try:
        rows = sb_get("inverter_vendor_credentials", {
            "select": "id,base_url,username,encrypted_password,current_token,token_expires_at,last_token_refresh_at,notes,is_active",
            "vendor": "eq.huawei",
            "is_active": "eq.true",
            "limit": "1",
        })
        if rows:
            return rows[0]
    except Exception as e:
        log.warning("[_load_huawei_credentials_from_db] %s", e)
    return {}


def _save_huawei_token_to_db(cred_id: str, token: str, ttl_seconds: int = 25 * 60) -> None:
    """Persist token to DB so worker restarts dont trigger fresh login (avoid rate limit 407)."""
    try:
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        sb_patch(f"inverter_vendor_credentials?id=eq.{cred_id}", {
            "current_token": token,
            "token_expires_at": expires.isoformat(),
            "last_token_refresh_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.warning("[_save_huawei_token_to_db] %s", e)


def _save_huawei_login_failure(cred_id: str, fail_code: Optional[int], backoff_min: int = 30) -> None:
    """Persist 407 / login failure backoff so we dont retry too soon and worsen lockout."""
    try:
        until = datetime.now(timezone.utc) + timedelta(minutes=backoff_min)
        sb_patch(f"inverter_vendor_credentials?id=eq.{cred_id}", {
            "current_token": None,
            "token_expires_at": None,
            "notes": _pj.dumps({
                "last_login_fail_code": fail_code,
                "last_login_fail_at": datetime.now(timezone.utc).isoformat(),
                "next_login_allowed_at": until.isoformat(),
                "backoff_min": backoff_min,
            }),
        })
    except Exception as e:
        log.warning("[_save_huawei_login_failure] %s", e)


def _huawei_login_backoff_active(cred_row: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Vracia (is_active, next_allowed_iso). Ak je active, NEVOLAJ login (lockout 407)."""
    notes = cred_row.get("notes")
    if not notes:
        return (False, None)
    try:
        n = _pj.loads(notes) if isinstance(notes, str) else notes
        next_iso = n.get("next_login_allowed_at")
        if not next_iso:
            return (False, None)
        next_dt = datetime.fromisoformat(next_iso.replace("Z","+00:00"))
        if datetime.now(timezone.utc) < next_dt:
            return (True, next_iso)
    except Exception:
        pass
    return (False, None)


def get_huawei_backoff_status() -> Dict[str, Any]:
    """Public helper - vráti aktuálny backoff status pre UI (bez logovania).

    Returns:
      {is_blocked: bool, next_login_allowed_at: iso|null, fail_code: int|null, minutes_remaining: int|null}
    """
    cred = _load_huawei_credentials_from_db()
    is_blocked, next_iso = _huawei_login_backoff_active(cred)
    fail_code = None
    if cred.get("notes"):
        try:
            n = _pj.loads(cred["notes"]) if isinstance(cred["notes"], str) else cred["notes"]
            fail_code = n.get("last_login_fail_code")
        except Exception:
            pass
    minutes_remaining = None
    if is_blocked and next_iso:
        try:
            next_dt = datetime.fromisoformat(next_iso.replace("Z","+00:00"))
            delta = (next_dt - datetime.now(timezone.utc)).total_seconds()
            minutes_remaining = max(0, int(delta / 60))
        except Exception:
            pass
    return {
        "is_blocked": is_blocked,
        "next_login_allowed_at": next_iso,
        "fail_code": fail_code,
        "minutes_remaining": minutes_remaining,
        "has_db_token": bool(cred.get("current_token")),
        "token_expires_at": cred.get("token_expires_at"),
    }


def huawei_login(force: bool = False) -> Optional[str]:
    """
    Login do Huawei NBI.
    Token cache stratégia (vyhne sa rate limit 407 - max 5 loginov/10 min):
      1. RAM cache (process-local, ~25 min)
      2. DB cache v inverter_vendor_credentials.current_token (cross-worker)
      3. Backoff check - ak bol nedávno 407, neskúšaj
      4. Fresh login + persist token do DB
    """
    global _huawei_session
    now = time.time()
    # Step 1: RAM cache
    if not force and _huawei_session["token"] and _huawei_session["expires_at"] > now + 60:
        return _huawei_session["token"]

    # Load credentials + DB cache
    cred = _load_huawei_credentials_from_db()
    base = HUAWEI_BASE
    user = HUAWEI_USER
    pwd = HUAWEI_PASS
    cred_id = cred.get("id")
    if not pwd:
        base = cred.get("base_url") or base
        user = cred.get("username") or user
        pwd = cred.get("encrypted_password") or pwd

    # Step 2: DB cache (cross-worker token reuse)
    if not force and cred.get("current_token") and cred.get("token_expires_at"):
        try:
            exp = datetime.fromisoformat(cred["token_expires_at"].replace("Z","+00:00"))
            if datetime.now(timezone.utc) < exp - timedelta(seconds=60):
                tok = cred["current_token"]
                _huawei_session["token"] = tok
                _huawei_session["expires_at"] = exp.timestamp()
                _huawei_session["base"] = base
                log.info("[huawei_login] reusing DB-cached token (expires %s)", cred["token_expires_at"])
                return tok
        except Exception as e:
            log.warning("[huawei_login] DB token parse fail: %s", e)

    # Step 3: backoff check (avoid hammering after 407)
    # CRITICAL: aj pri force=True musí platiť backoff. Huawei vráti 407 bez ohľadu na našu intenciu.
    # force=True znamená "ak je token v DB cache, preskoč ho a urob fresh" - NIE "ignoruj rate limit".
    is_blocked, next_iso = _huawei_login_backoff_active(cred)
    if is_blocked:
        log.warning("[huawei_login] BACKOFF active (force=%s) - next attempt allowed at %s", force, next_iso)
        return None

    if not pwd:
        log.warning("[huawei_login] no password - env HUAWEI_PASS empty and no row in inverter_vendor_credentials")
        return None

    # Step 4: fresh login
    url = f"{base}/login"
    payload = {"userName": user, "systemCode": pwd}
    try:
        r = requests.post(url, json=payload, timeout=30)
        body_json = {}
        try:
            body_json = r.json() or {}
        except Exception:
            pass
        fail_code = body_json.get("failCode")

        # failCode 407 = ACCESS_FREQUENCY_IS_TOO_HIGH (rate limit 5/10min, lockout 30 min)
        if fail_code == 407:
            log.error("[huawei_login] failCode 407 ACCESS_FREQUENCY_IS_TOO_HIGH - backoff 30 min")
            if cred_id:
                _save_huawei_login_failure(cred_id, 407, backoff_min=30)
            return None
        # failCode 401 = account locked (5 wrong pwd within 10 min, lockout 30 min)
        if fail_code == 401:
            log.error("[huawei_login] failCode 401 account locked - backoff 30 min")
            if cred_id:
                _save_huawei_login_failure(cred_id, 401, backoff_min=30)
            return None
        # 20400 / 305 = wrong password
        if fail_code in (20400, 305):
            log.error("[huawei_login] wrong credentials (failCode=%s) - backoff 10 min", fail_code)
            if cred_id:
                _save_huawei_login_failure(cred_id, fail_code, backoff_min=10)
            return None

        if r.status_code != 200:
            log.error("[huawei_login] HTTP %s body=%s", r.status_code, r.text[:300])
            if cred_id:
                _save_huawei_login_failure(cred_id, fail_code, backoff_min=5)
            return None

        token = r.headers.get("XSRF-TOKEN") or r.headers.get("xsrf-token")
        if not token:
            log.error("[huawei_login] no XSRF-TOKEN in response (failCode=%s body=%s)", fail_code, r.text[:300])
            if cred_id:
                _save_huawei_login_failure(cred_id, fail_code or -1, backoff_min=5)
            return None

        # Success - cache to RAM + DB
        _huawei_session["token"] = token
        _huawei_session["expires_at"] = now + 25 * 60
        _huawei_session["base"] = base
        if cred_id:
            _save_huawei_token_to_db(cred_id, token, ttl_seconds=25 * 60)
            # clear backoff notes on success
            try:
                sb_patch(f"inverter_vendor_credentials?id=eq.{cred_id}", {"notes": None})
            except Exception:
                pass
        log.info("[huawei_login] OK new token cached for 25 min")
        return token
    except Exception as e:
        log.exception("[huawei_login] failed: %s", e)
        if cred_id:
            _save_huawei_login_failure(cred_id, None, backoff_min=5)
        return None


def huawei_send_command(station_code: str, command_type: str, params: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    token = huawei_login()
    if not token:
        return False, {"error": "no XSRF token"}
    base = _huawei_session.get("base") or HUAWEI_BASE

    huawei_command_codes = {
        "enable_zero_export":      {"code": "PCS_REVERSE_POWER_FLOW", "value": "1"},
        "disable_zero_export":     {"code": "PCS_REVERSE_POWER_FLOW", "value": "0"},
        "set_active_power_limit":  {"code": "ACTIVE_POWER_CTRL", "value": str(params.get("limit_pct", 100))},
        "set_battery_mode_grid_charge": {"code": "BATTERY_WORKING_MODE", "value": "FORCE_CHARGE"},
        "set_battery_mode_self":   {"code": "BATTERY_WORKING_MODE", "value": "SELF_USE"},
    }
    cmd = huawei_command_codes.get(command_type)
    if not cmd:
        return False, {"error": f"unknown command_type: {command_type}"}

    url = f"{base}/sendCommand"
    headers = {"XSRF-TOKEN": token, "Content-Type": "application/json"}
    payload = {
        "stationCode": station_code,
        "commandCode": cmd["code"],
        "commandValue": cmd["value"],
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        result = r.json() if r.status_code == 200 else {"http_status": r.status_code, "body": r.text[:500]}
        success = r.status_code == 200 and result.get("success", False)
        return success, result
    except Exception as e:
        log.exception("[huawei_send_command] failed")
        return False, {"error": str(e)}


# ---------------- SPOT 3-state Reactor ----------------
def get_current_spot() -> Optional[float]:
    rows = sb_get("spot_current_period", {"select": "price_eur_mwh"})
    if rows and isinstance(rows, list) and len(rows) > 0:
        return float(rows[0]["price_eur_mwh"])
    return None


def determine_target_state(spot: float, threshold_ze: float, threshold_shut: float) -> str:
    if spot < threshold_shut:
        return "FULL_SHUTDOWN"
    elif spot < threshold_ze:
        return "ZERO_EXPORT_ONLY"
    return "NORMAL"


def should_transition(current: str, target: str, spot: float, threshold_ze: float, threshold_shut: float, hys: float) -> bool:
    if current == target:
        return False
    if current == "NORMAL" and target == "ZERO_EXPORT_ONLY":
        return spot < threshold_ze
    if current == "ZERO_EXPORT_ONLY" and target == "NORMAL":
        return spot >= (threshold_ze + hys)
    if current == "ZERO_EXPORT_ONLY" and target == "FULL_SHUTDOWN":
        return spot < threshold_shut
    if current == "FULL_SHUTDOWN" and target == "ZERO_EXPORT_ONLY":
        return spot >= (threshold_shut + hys)
    if current == "FULL_SHUTDOWN" and target == "NORMAL":
        return spot >= (threshold_ze + hys)
    if current == "NORMAL" and target == "FULL_SHUTDOWN":
        return spot < threshold_shut
    return True


def slack_notify(text: str) -> None:
    if not SLACK_WEBHOOK_OPS:
        return
    try:
        requests.post(SLACK_WEBHOOK_OPS, json={"text": text}, timeout=10)
    except Exception:
        pass


def estimate_savings_eur(site: Dict[str, Any], to_state: str, spot_eur_mwh: float) -> float:
    """
    Odhad € ušetrených touto transition. Placeholder do času keď bude k dispozícii realtime production.
    Vzorec: ratio_blocked × 0.25h × ac_kw × predpoklad_30pct_solar × |spot|/1000
    Aplikujeme len ak SPOT < 0.
    """
    if spot_eur_mwh >= 0:
        return 0.0
    ac_kw = float(site.get("ac_kw") or 0)
    if ac_kw <= 0:
        return 0.0
    # ZE only blokuje len export; FULL_SHUTDOWN blokuje aj self-consumption
    # Predpokladáme že záporné ceny sú zvyčajne v slnečnom čase (12-16h) — produkcia ~30% nominálu
    ratio_blocked = 1.0 if to_state == "FULL_SHUTDOWN" else 0.7
    period_kwh = 0.25 * ac_kw * 0.30 * ratio_blocked  # 15-min perióda
    savings = (period_kwh / 1000.0) * abs(spot_eur_mwh)
    return round(savings, 2)


def log_state_transition(site: Dict[str, Any], from_state: str, to_state: str, spot: float,
                          reason: str, dry_run: bool, command_id: Optional[str]) -> None:
    savings = estimate_savings_eur(site, to_state, spot)
    payload = [{
        "site_id": site["id"],
        "from_state": from_state,
        "to_state": to_state,
        "spot_price_eur_mwh": spot,
        "threshold_ze": float(site.get("spot_threshold_zero_export", -50)),
        "threshold_shutdown": float(site.get("spot_threshold_full_shutdown", -60)),
        "hysteresis_eur": float(site.get("spot_hysteresis_eur", 5)),
        "reason": reason,
        "dry_run": dry_run,
        "command_id": command_id,
        "customer_savings_eur": savings,
    }]
    sb_post("spot_state_transitions", payload)


def execute_transition(site: Dict[str, Any], from_state: str, to_state: str, spot: float, dry_run: bool) -> Dict[str, Any]:
    site_id = site["id"]
    station_code = site.get("vendor_station_id")
    has_bess = float(site.get("bess_kwh") or 0) > 0
    grid_charge = bool(site.get("spot_bess_grid_charge_enabled")) and has_bess

    commands = []
    if to_state == "NORMAL":
        commands.append({"type": "disable_zero_export", "params": {}})
        commands.append({"type": "set_active_power_limit", "params": {"limit_pct": 100}})
        if has_bess:
            commands.append({"type": "set_battery_mode_self", "params": {}})
    elif to_state == "ZERO_EXPORT_ONLY":
        commands.append({"type": "enable_zero_export", "params": {}})
        commands.append({"type": "set_active_power_limit", "params": {"limit_pct": 100}})
    elif to_state == "FULL_SHUTDOWN":
        commands.append({"type": "set_active_power_limit", "params": {"limit_pct": 0}})
        if grid_charge:
            commands.append({"type": "set_battery_mode_grid_charge", "params": {}})

    results = []
    for cmd in commands:
        cmd_row = {
            "site_id": site_id,
            "command_type": cmd["type"],
            "command_payload": cmd["params"],
            "issued_by": "spot_reactor",
            "reason": f"SPOT={spot:.2f}/MWh, {from_state}->{to_state}",
            "status": "queued" if not dry_run else "dry_run",
            "dry_run": dry_run,
            "source": "spot_reactor",
        }
        url = f"{SUPABASE_URL}/rest/v1/inverter_commands"
        headers = {**_sb_headers(), "Prefer": "return=representation"}
        r = requests.post(url, headers=headers, json=[cmd_row], timeout=30)
        if r.status_code in (200, 201) and r.json():
            cmd_id = r.json()[0].get("id")
            results.append({"type": cmd["type"], "command_id": cmd_id, "dry_run": dry_run})

            if not dry_run and station_code:
                ok, resp = huawei_send_command(station_code, cmd["type"], cmd["params"])
                status = "success" if ok else "failed"
                sb_patch(f"inverter_commands?id=eq.{cmd_id}", {
                    "status": status,
                    "vendor_response": resp,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                results[-1]["vendor_ok"] = ok
        else:
            log.error("[execute_transition] insert command failed: %s %s", r.status_code, r.text[:200])

    return {"commands_issued": len(results), "details": results}


def spot_reactor(dry_run_override: Optional[bool] = None) -> Dict[str, Any]:
    current_spot = get_current_spot()
    if current_spot is None:
        return {"ok": False, "error": "No current SPOT price (run okte_ingest first)"}

    sites = sb_get("inverter_sites", {
        "select": "id,site_name,vendor_station_id,bess_kwh,spot_control_enabled,spot_distribution_fee_eur_mwh,spot_threshold_zero_export,spot_threshold_full_shutdown,spot_hysteresis_eur,spot_current_state,spot_bess_grid_charge_enabled,spot_dry_run,spot_customer_revenue_share_pct",
        "spot_control_enabled": "eq.true",
    })

    transitions = []
    for site in sites:
        try:
            current = site.get("spot_current_state") or "NORMAL"
            t_ze = float(site.get("spot_threshold_zero_export") or -50)
            t_shut = float(site.get("spot_threshold_full_shutdown") or -60)
            hys = float(site.get("spot_hysteresis_eur") or 5)

            target = determine_target_state(current_spot, t_ze, t_shut)
            if not should_transition(current, target, current_spot, t_ze, t_shut, hys):
                continue

            site_dry_run = bool(site.get("spot_dry_run"))
            if dry_run_override is True:
                site_dry_run = True

            reason = f"SPOT={current_spot:.2f}/MWh"
            result = execute_transition(site, current, target, current_spot, site_dry_run)
            cmd_id = (result["details"][0].get("command_id") if result["details"] else None)
            log_state_transition(site, current, target, current_spot, reason, site_dry_run, cmd_id)

            sb_patch(f"inverter_sites?id=eq.{site['id']}", {
                "spot_current_state": target,
                "spot_last_transition_at": datetime.now(timezone.utc).isoformat(),
                "spot_last_transition_reason": reason,
            })

            transitions.append({
                "site_id": site["id"],
                "site_name": site.get("site_name"),
                "from": current,
                "to": target,
                "spot": current_spot,
                "dry_run": site_dry_run,
                "commands": result["commands_issued"],
            })

            emoji = {"NORMAL": ":white_check_mark:", "ZERO_EXPORT_ONLY": ":no_entry:", "FULL_SHUTDOWN": ":octagonal_sign:"}
            mode = "DRY-RUN" if site_dry_run else "LIVE"
            slack_notify(
                f"{emoji.get(target,':bell:')} SPOT reactor [{mode}] `{site.get('site_name')}` "
                f"{current} -> {target} (SPOT={current_spot:.2f}/MWh)"
            )

        except Exception as e:
            log.exception("[spot_reactor] site=%s failed", site.get("id"))
            transitions.append({"site_id": site.get("id"), "error": str(e)})

    return {
        "ok": True,
        "current_spot_eur_mwh": current_spot,
        "sites_evaluated": len(sites),
        "transitions": transitions,
        "transition_count": sum(1 for t in transitions if not t.get("error")),
    }


def global_pause(reason: str = "Slack command") -> Dict[str, Any]:
    ok, err = sb_patch("inverter_sites?spot_control_enabled=eq.true", {"spot_dry_run": True})
    slack_notify(f":pause_button: SPOT reactor globally paused. Reason: {reason}")
    return {"ok": ok, "error": err or None}


# ---------------- Manual command (admin override) ----------------
def manual_force_state(site_id: str, target_state: str, issued_by: str = "manual",
                        reason: str = "Manuálny override z dashboardu") -> Dict[str, Any]:
    """
    Admin tlačidlo: vynúti konkrétny stav na konkrétnu stanicu bez ohľadu na SPOT.
    Rešpektuje per-site dry_run flag.
    """
    if target_state not in ("NORMAL", "ZERO_EXPORT_ONLY", "FULL_SHUTDOWN"):
        return {"ok": False, "error": f"invalid target_state: {target_state}"}

    sites = sb_get("inverter_sites", {
        "select": "id,site_name,vendor_station_id,bess_kwh,spot_current_state,spot_dry_run,spot_bess_grid_charge_enabled",
        "id": f"eq.{site_id}",
    })
    if not sites:
        return {"ok": False, "error": "site not found"}
    site = sites[0]

    current_spot = get_current_spot() or 0.0
    current_state = site.get("spot_current_state") or "NORMAL"
    dry_run = bool(site.get("spot_dry_run"))

    full_reason = f"{reason} (by {issued_by})"
    result = execute_transition(site, current_state, target_state, current_spot, dry_run)
    cmd_id = (result["details"][0].get("command_id") if result["details"] else None)
    log_state_transition(site, current_state, target_state, current_spot, full_reason, dry_run, cmd_id)

    sb_patch(f"inverter_sites?id=eq.{site_id}", {
        "spot_current_state": target_state,
        "spot_last_transition_at": datetime.now(timezone.utc).isoformat(),
        "spot_last_transition_reason": full_reason,
    })

    slack_notify(
        f":wrench: SPOT *manual override* [{'DRY' if dry_run else 'LIVE'}] `{site.get('site_name')}` "
        f"{current_state} → *{target_state}* (by {issued_by})"
    )

    return {
        "ok": True,
        "site_id": site_id,
        "site_name": site.get("site_name"),
        "from": current_state,
        "to": target_state,
        "dry_run": dry_run,
        "commands_issued": result["commands_issued"],
        "command_id": cmd_id,
    }


# ---------------- Sync stations from Huawei ----------------
def _load_api_state(endpoint_key: str) -> Dict[str, Any]:
    """Load cross-worker shared state z huawei_api_state DB tabuľky."""
    try:
        rows = sb_get("huawei_api_state", {"endpoint_key": f"eq.{endpoint_key}", "select": "*", "limit": "1"})
        if rows:
            return rows[0]
    except Exception as e:
        log.warning("[_load_api_state] %s", e)
    return {}


def _save_api_state(endpoint_key: str, payload: Dict[str, Any]) -> None:
    """Persist API state do DB pre cross-worker zdieľanie."""
    try:
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        sb_patch(f"huawei_api_state?endpoint_key=eq.{endpoint_key}", payload)
    except Exception as e:
        log.warning("[_save_api_state] %s", e)


def huawei_get_station_list(debug: bool = False, force_refresh: bool = False):
    """
    Fetch ALL stations from Huawei NBI Plant List API (/thirdData/stations).
    Uses DB-backed cache (5 min TTL) + cross-worker backoff guard (10 min after 407).
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.timestamp()
    debug_info = {"login": None, "page_responses": [], "cache_hit": False, "backoff_active": False}

    state = _load_api_state("plant_list")
    CACHE_TTL = 5 * 60
    MIN_INTERVAL = 120

    # 1) Cache check
    if not force_refresh and state.get("last_response") and state.get("last_response_at"):
        try:
            cached_at = datetime.fromisoformat(state["last_response_at"].replace("Z","+00:00"))
            age = (now_dt - cached_at).total_seconds()
            if age < CACHE_TTL:
                debug_info["cache_hit"] = True
                debug_info["cache_age_sec"] = int(age)
                cached = state["last_response"] if isinstance(state["last_response"], list) else []
                return (cached, debug_info) if debug else cached
        except Exception:
            pass

    # 2) Backoff check (cross-worker, perzistuje cez Render restart)
    if state.get("backoff_until"):
        try:
            backoff_dt = datetime.fromisoformat(state["backoff_until"].replace("Z","+00:00"))
            if now_dt < backoff_dt:
                remaining = int((backoff_dt - now_dt).total_seconds())
                debug_info["backoff_active"] = True
                debug_info["backoff_remaining_sec"] = remaining
                debug_info["error"] = f"Plant List backoff aktivny - cakaj {remaining // 60}min {remaining % 60}s (fail_code={state.get('last_fail_code')})"
                log.warning("[huawei_get_station_list] DB backoff active, %ss remaining", remaining)
                cached = state.get("last_response") if isinstance(state.get("last_response"), list) else []
                return (cached or [], debug_info) if debug else (cached or [])
        except Exception:
            pass

    # 3) Min interval
    if state.get("last_call_at"):
        try:
            last_call = datetime.fromisoformat(state["last_call_at"].replace("Z","+00:00"))
            elapsed = (now_dt - last_call).total_seconds()
            if elapsed < MIN_INTERVAL:
                wait = int(MIN_INTERVAL - elapsed)
                debug_info["error"] = f"Plant List min interval - cakaj {wait}s"
                cached = state.get("last_response") if isinstance(state.get("last_response"), list) else []
                return (cached or [], debug_info) if debug else (cached or [])
        except Exception:
            pass

    # Update last_call_at PRED API call (worker B vidí že worker A ide na API)
    _save_api_state("plant_list", {"last_call_at": now_dt.isoformat()})

    token = huawei_login()
    if not token:
        debug_info["login"] = "failed (None token)"
        return ([], debug_info) if debug else []
    debug_info["login"] = f"OK (token len={len(token)})"

    base = _huawei_session.get("base") or HUAWEI_BASE
    url = f"{base}/stations"
    headers = {"XSRF-TOKEN": token, "Content-Type": "application/json"}
    debug_info["url"] = url

    all_stations: List[Dict[str, Any]] = []
    page_no = 1
    max_pages = 50

    while page_no <= max_pages:
        body = {"pageNo": page_no}
        try:
            r = requests.post(url, headers=headers, json=body, timeout=60)
            page_debug = {"page": page_no, "http_status": r.status_code}
            body_text = (r.text or "")[:600]
            page_debug["body_preview"] = body_text
            if r.status_code != 200:
                log.error("[huawei_get_station_list] page=%s HTTP %s %s", page_no, r.status_code, body_text[:300])
                debug_info["page_responses"].append(page_debug)
                break
            data = r.json() or {}
            fail_code = data.get("failCode")
            success_flag = data.get("success")
            msg = data.get("message", "")
            page_debug["fail_code"] = fail_code
            page_debug["success"] = success_flag
            page_debug["message"] = msg
            if fail_code and fail_code != 0:
                log.error("[huawei_get_station_list] page=%s failCode=%s msg=%s", page_no, fail_code, msg)
                debug_info["page_responses"].append(page_debug)
                break

            payload = data.get("data") or {}
            if isinstance(payload, dict):
                page_list = payload.get("list", []) or []
                page_count = int(payload.get("pageCount") or 1)
                page_debug["total"] = payload.get("total")
                page_debug["pageCount"] = page_count
            else:
                page_list = data.get("list", []) or []
                page_count = 1
            page_debug["stations_in_page"] = len(page_list)
            debug_info["page_responses"].append(page_debug)

            all_stations.extend(page_list)
            log.info("[huawei_get_station_list] page %s/%s got %s plants", page_no, page_count, len(page_list))

            if page_no >= page_count or len(page_list) == 0:
                break
            page_no += 1
        except Exception as e:
            log.exception("[huawei_get_station_list] page=%s failed: %s", page_no, e)
            debug_info["page_responses"].append({"page": page_no, "error": f"{type(e).__name__}: {e}"})
            break

    debug_info["total_stations"] = len(all_stations)

    # Save state to DB cross-worker
    last_resp = debug_info["page_responses"][-1] if debug_info["page_responses"] else {}
    end_dt = datetime.now(timezone.utc)
    if last_resp.get("fail_code") == 407:
        # 10 min backoff per Huawei doc
        backoff_until = end_dt + timedelta(minutes=10)
        _save_api_state("plant_list", {
            "last_fail_code": 407,
            "last_fail_at": end_dt.isoformat(),
            "backoff_until": backoff_until.isoformat(),
        })
        log.warning("[huawei_get_station_list] failCode 407 → DB backoff %s", backoff_until.isoformat())
    elif all_stations:
        _save_api_state("plant_list", {
            "last_response": all_stations,
            "last_response_at": end_dt.isoformat(),
            "backoff_until": None,    # clear backoff on success
        })

    return (all_stations, debug_info) if debug else all_stations


def sync_huawei_stations() -> Dict[str, Any]:
    """
    Pull station list from Huawei NBI, upsert do inverter_sites.
    Returns {ok, total_huawei, added, updated, skipped, details[], diagnostic} pri fail.
    """
    stations, debug_info = huawei_get_station_list(debug=True)
    if not stations:
        return {
            "ok": False,
            "error": "no stations returned",
            "diagnostic": debug_info,
            "hint": "Skontroluj failCode v page_responses. failCode=20009 znamená 'no permission for Plant List API'. failCode=407 znamená rate limit.",
        }

    existing = sb_get("inverter_sites", {
        "select": "id,vendor_station_id",
        "vendor": "eq.huawei",
    })
    existing_map = {s["vendor_station_id"]: s["id"] for s in existing if s.get("vendor_station_id")}

    added, updated, skipped = 0, 0, 0
    details = []

    for st in stations:
        code = st.get("plantCode") or st.get("stationCode")
        if not code:
            skipped += 1
            continue
        name = st.get("plantName") or st.get("stationName") or code
        dc_kwp = float(st.get("capacity") or 0)  # NBI capacity = kWp DC strana
        addr = st.get("plantAddress") or st.get("stationAddr") or ""
        lng = st.get("longitude")
        lat = st.get("latitude")
        contact_p = st.get("contactPerson") or ""
        contact_m = st.get("contactMethod") or ""
        grid_date = st.get("gridConnectionDate") or ""

        # Bazálne polia, ktoré určite existujú v inverter_sites
        base_row = {
            "vendor": "huawei",
            "vendor_station_id": code,
            "site_name": name,
            "dc_kwp": dc_kwp,           # ✅ kWp DC (oprava: predtým sme to dávali do ac_kw)
            "address": addr,
            "monitoring_enabled": True,
        }
        # Voliteľné polia (GPS, kontakt) - len ak hodnoty existujú, aby sa neprepisovali neprázdne hodnoty v DB
        if lat is not None:
            base_row["latitude"] = lat
        if lng is not None:
            base_row["longitude"] = lng

        if code in existing_map:
            patch_row = {
                "site_name": name,
                "dc_kwp": dc_kwp,
                "address": addr,
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
            }
            if lat is not None:
                patch_row["latitude"] = lat
            if lng is not None:
                patch_row["longitude"] = lng
            sb_patch(f"inverter_sites?id=eq.{existing_map[code]}", patch_row)
            updated += 1
            details.append({"action": "updated", "code": code, "name": name, "dc_kwp": dc_kwp})
        else:
            ok, err = sb_post("inverter_sites", [base_row])
            if ok:
                added += 1
                details.append({"action": "added", "code": code, "name": name, "dc_kwp": dc_kwp, "lat": lat, "lng": lng})
            else:
                skipped += 1
                details.append({"action": "skipped", "code": code, "name": name, "error": err})

    return {
        "ok": True,
        "total_huawei": len(stations),
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "details": details[:30],
    }
