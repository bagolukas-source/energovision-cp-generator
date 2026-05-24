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
        period = record.get("period") or record.get("Period") or record.get("hour")
        if period is None:
            return None
        if isinstance(period, str):
            period = int(period)
        delivery_start = record.get("deliveryStart") or record.get("DeliveryStart") or record.get("deliveryStartUtc")
        delivery_end = record.get("deliveryEnd") or record.get("DeliveryEnd") or record.get("deliveryEndUtc")
        price = record.get("price") or record.get("Price") or record.get("priceEurMwh")
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
def _load_huawei_credentials_from_db() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Load Huawei credentials from inverter_vendor_credentials table."""
    try:
        rows = sb_get("inverter_vendor_credentials", {
            "select": "base_url,username,encrypted_password,is_active",
            "vendor": "eq.huawei",
            "is_active": "eq.true",
            "limit": "1",
        })
        if rows:
            r = rows[0]
            return r.get("base_url"), r.get("username"), r.get("encrypted_password")
    except Exception as e:
        log.warning("[_load_huawei_credentials_from_db] %s", e)
    return None, None, None


def huawei_login(force: bool = False) -> Optional[str]:
    global _huawei_session
    now = time.time()
    if not force and _huawei_session["token"] and _huawei_session["expires_at"] > now + 60:
        return _huawei_session["token"]

    # Priority: env vars first, fallback to Supabase inverter_vendor_credentials
    base = HUAWEI_BASE
    user = HUAWEI_USER
    pwd = HUAWEI_PASS
    if not pwd:
        db_base, db_user, db_pwd = _load_huawei_credentials_from_db()
        base = db_base or base
        user = db_user or user
        pwd = db_pwd or pwd

    if not pwd:
        log.warning("[huawei_login] no password — env HUAWEI_PASS empty and no row in inverter_vendor_credentials")
        return None

    url = f"{base}/login"
    payload = {"userName": user, "systemCode": pwd}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            log.error("[huawei_login] %s %s", r.status_code, r.text[:300])
            return None
        token = r.headers.get("XSRF-TOKEN") or r.headers.get("xsrf-token")
        if not token:
            log.error("[huawei_login] no XSRF-TOKEN in response (body=%s)", r.text[:300])
            return None
        _huawei_session["token"] = token
        _huawei_session["expires_at"] = now + 25 * 60
        _huawei_session["base"] = base
        return token
    except Exception as e:
        log.exception("[huawei_login] failed: %s", e)
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
def huawei_get_station_list() -> List[Dict[str, Any]]:
    """Fetch list of all stations from Huawei /getStationList endpoint."""
    token = huawei_login()
    if not token:
        return []
    base = _huawei_session.get("base") or HUAWEI_BASE
    url = f"{base}/stations"
    headers = {"XSRF-TOKEN": token, "Content-Type": "application/json"}
    body = {"pageNo": 1, "pageSize": 100}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
        if r.status_code != 200:
            log.error("[huawei_get_station_list] %s %s", r.status_code, r.text[:300])
            return []
        data = r.json() or {}
        # v6 returns {"data": {"list": [...]}}, v2 returns {"list": [...]}
        if isinstance(data.get("data"), dict):
            return data["data"].get("list", []) or []
        return data.get("list", []) or []
    except Exception as e:
        log.exception("[huawei_get_station_list] failed")
        return []


def sync_huawei_stations() -> Dict[str, Any]:
    """
    Pull station list from Huawei, upsert do inverter_sites.
    Returns {added: N, updated: N, skipped: N}.
    """
    stations = huawei_get_station_list()
    if not stations:
        return {"ok": False, "error": "no stations returned (login fail alebo prázdny zoznam)"}

    existing = sb_get("inverter_sites", {
        "select": "id,vendor_station_id",
        "vendor": "eq.huawei",
    })
    existing_map = {s["vendor_station_id"]: s["id"] for s in existing if s.get("vendor_station_id")}

    added, updated, skipped = 0, 0, 0
    details = []

    for st in stations:
        code = st.get("stationCode") or st.get("plantCode")
        if not code:
            skipped += 1
            continue
        name = st.get("stationName") or st.get("plantName") or code
        ac_kw = float(st.get("capacity") or st.get("aidType") or 0)
        addr = st.get("stationAddr") or st.get("plantAddress") or ""

        row = {
            "vendor": "huawei",
            "vendor_station_id": code,
            "site_name": name,
            "ac_kw": ac_kw,
            "address": addr,
            "monitoring_enabled": True,
        }

        if code in existing_map:
            sb_patch(f"inverter_sites?id=eq.{existing_map[code]}", {
                "site_name": name,
                "ac_kw": ac_kw,
                "address": addr,
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
            })
            updated += 1
            details.append({"action": "updated", "code": code, "name": name})
        else:
            ok, err = sb_post("inverter_sites", [row])
            if ok:
                added += 1
                details.append({"action": "added", "code": code, "name": name})
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
