"""
Solinteg MQTT real-time subscriber.

Beží ako background thread v Render Flask app — subscribe na topic
'/<TOPIC>' (z inverter_vendor_credentials.oauth_scope alebo env SOLINTEG_MQTT_TOPIC)
a každý prijatý message dekóduje + zapíše do telemetry_5min + inverter_measurements.

Sub-second latencia oproti polling (3-5 min).

Dependencies: paho-mqtt (pridané do requirements.txt).
Auto-start v app.py pri boot (ak je SOLINTEG_MQTT_ENABLED=true).
"""
import os
import json
import logging
import threading
import time
from datetime import datetime, timezone
import requests

log = logging.getLogger("solinteg.mqtt")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")

# Broker config — z Solinteg dokumentácie / DB credentials
MQTT_BROKER_HOST = os.environ.get("SOLINTEG_MQTT_HOST", "openapi-mqtt.solinteg-cloud.com")
MQTT_BROKER_PORT = int(os.environ.get("SOLINTEG_MQTT_PORT", "8883"))  # 8883 TLS, 1883 plain
MQTT_USE_TLS = os.environ.get("SOLINTEG_MQTT_TLS", "true").lower() == "true"
MQTT_TOPIC = os.environ.get("SOLINTEG_MQTT_TOPIC", "/cBFQ7PMTpG")  # default z Lukášovho účtu

# Worker stav
_worker_thread = None
_worker_stop = threading.Event()
_message_count = 0
_last_message_at = None


def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


def _resolve_site_id(device_sn: str) -> str:
    """Cache site_id per deviceSn (in-memory)."""
    if not hasattr(_resolve_site_id, "_cache"):
        _resolve_site_id._cache = {}
    if device_sn in _resolve_site_id._cache:
        return _resolve_site_id._cache[device_sn]
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/inverter_sites",
                         headers=_sb_headers(),
                         params={"select": "id", "vendor": "eq.solinteg",
                                 "vendor_plant_code": f"eq.{device_sn}", "limit": 1},
                         timeout=10)
        sites = r.json() if r.ok else []
        if sites:
            site_id = sites[0]["id"]
            _resolve_site_id._cache[device_sn] = site_id
            return site_id
    except Exception:
        pass
    return None


def _on_message(client, userdata, msg):
    """Spracuje MQTT message → zapíše do inverter_measurements."""
    global _message_count, _last_message_at
    _message_count += 1
    _last_message_at = datetime.now(timezone.utc).isoformat()
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        log.warning("[solinteg.mqtt] non-JSON payload: %s", str(e)[:100])
        return

    # Payload struktura: {deviceSn, rtcTime, pac, ...} (rovnaké fields ako Realtime API)
    device_sn = payload.get("invSn") or payload.get("deviceSn") or payload.get("sn")
    if not device_sn:
        return
    site_id = _resolve_site_id(device_sn)
    if not site_id:
        log.debug("[solinteg.mqtt] device %s not in inverter_sites", device_sn)
        return

    try:
        from solinteg_oauth import map_realtime_to_measurement
        m = map_realtime_to_measurement(payload, site_id)
        if m:
            requests.post(f"{SUPABASE_URL}/rest/v1/inverter_measurements",
                          headers=_sb_headers(), json=[m], timeout=10)
    except Exception as e:
        log.warning("[solinteg.mqtt] insert failed: %s", str(e)[:200])


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("[solinteg.mqtt] connected to %s:%s", MQTT_BROKER_HOST, MQTT_BROKER_PORT)
        client.subscribe(MQTT_TOPIC, qos=1)
    else:
        log.error("[solinteg.mqtt] connection failed rc=%s", rc)


def _on_disconnect(client, userdata, rc):
    log.warning("[solinteg.mqtt] disconnected rc=%s, will auto-reconnect", rc)


def _worker_loop():
    """Persistent MQTT loop — auto-reconnect on disconnect."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.error("[solinteg.mqtt] paho-mqtt not installed, worker disabled")
        return

    # Načítaj credentials z DB
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/inverter_vendor_credentials",
                         headers=_sb_headers(),
                         params={"select": "username,encrypted_password,client_id",
                                 "vendor": "eq.solinteg", "is_active": "eq.true", "limit": 1},
                         timeout=10)
        creds = r.json() if r.ok else []
        if not creds:
            log.error("[solinteg.mqtt] no credentials in DB")
            return
        cred = creds[0]
        username = cred.get("username")
        password = cred.get("encrypted_password")
    except Exception as e:
        log.exception("[solinteg.mqtt] credentials load failed")
        return

    client = mqtt.Client(client_id=f"energovision-{int(time.time())}", clean_session=True)
    client.username_pw_set(username, password)
    if MQTT_USE_TLS:
        import ssl
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect

    while not _worker_stop.is_set():
        try:
            client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except Exception as e:
            log.warning("[solinteg.mqtt] loop error: %s — reconnect in 10s", str(e)[:200])
            time.sleep(10)


def start_worker():
    """Štart MQTT subscriber thread. Idempotentné — vráti True ak začal nový run."""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return False
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="solinteg-mqtt")
    _worker_thread.start()
    return True


def stop_worker():
    _worker_stop.set()


def get_status():
    return {
        "alive": _worker_thread.is_alive() if _worker_thread else False,
        "message_count": _message_count,
        "last_message_at": _last_message_at,
        "broker": f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}",
        "topic": MQTT_TOPIC,
        "tls": MQTT_USE_TLS,
    }
# build trigger Sun May 31 13:38:47 UTC 2026
