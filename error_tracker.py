"""Lightweight error tracker — Sentry envelope + Slack webhook.

Zero new deps (cez requests/urllib).
Beží asynchrónne (background thread), neblokuje main request.

Použitie:
    from error_tracker import track_error, track_warning

    try: ...
    except Exception as e:
        track_error(e, context={"endpoint": "/webhook/foo", "user": user_id})
        raise
"""
from __future__ import annotations

import os
import json
import re
import traceback
import threading
import logging
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)

SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
SLACK_ALERTS_WEBHOOK = os.environ.get("SLACK_ALERTS_WEBHOOK", "")
APP_ENV = os.environ.get("RENDER_ENV", "production") if os.environ.get("RENDER") else "development"
APP_NAME = "energovision-cp-generator"
APP_VERSION = os.environ.get("RENDER_GIT_COMMIT", "unknown")[:7]


def _parse_dsn(dsn: str) -> Optional[Dict[str, str]]:
    m = re.match(r"^https://([^@]+)@([^/]+)/(\d+)$", dsn)
    if not m:
        return None
    pub, host, proj = m.groups()
    return {
        "url": f"https://{host}/api/{proj}/envelope/",
        "auth": f"Sentry sentry_version=7,sentry_client=energo-tracker-py/1.0,sentry_key={pub}",
    }


def _send_sentry(severity: str, message: str, ctx: Dict[str, Any], exc: Optional[BaseException]) -> None:
    parsed = _parse_dsn(SENTRY_DSN)
    if not parsed:
        return
    event_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    envelope_header = json.dumps({"event_id": event_id, "sent_at": now, "dsn": SENTRY_DSN})
    item_header = json.dumps({"type": "event"})
    event = {
        "event_id": event_id,
        "timestamp": now,
        "level": severity,
        "platform": "python",
        "server_name": APP_NAME,
        "release": APP_VERSION,
        "environment": APP_ENV,
        "message": {"formatted": message[:1000]},
        "extra": {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v)[:500]) for k, v in (ctx or {}).items()},
    }
    if exc:
        tb_lines = traceback.format_tb(exc.__traceback__)[:20]
        event["exception"] = {
            "values": [{
                "type": type(exc).__name__,
                "value": str(exc)[:1000],
                "stacktrace": {
                    "frames": [{"function": l.strip().split("\n")[0][:200]} for l in tb_lines],
                },
            }],
        }
    payload = f"{envelope_header}\n{item_header}\n{json.dumps(event)}"
    try:
        requests.post(parsed["url"], data=payload,
                      headers={"Content-Type": "application/x-sentry-envelope", "X-Sentry-Auth": parsed["auth"]},
                      timeout=5)
    except Exception:
        pass


def _send_slack(severity: str, message: str, ctx: Dict[str, Any], exc: Optional[BaseException]) -> None:
    if not SLACK_ALERTS_WEBHOOK:
        return
    emoji = {"fatal": ":rotating_light:", "error": ":x:", "warning": ":warning:", "info": ":information_source:"}.get(severity, ":grey_question:")
    color = "#dc2626" if severity in ("fatal", "error") else "#d97706" if severity == "warning" else "#16a34a"
    ctx_lines = [f"*{k}*: `{str(v)[:200]}`" for k, v in list((ctx or {}).items())[:8]]
    stack_preview = None
    if exc:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        stack_preview = "".join(tb[-5:])[:1500]
    fields = [
        {"title": "Env", "value": APP_ENV, "short": True},
        {"title": "App", "value": f"{APP_NAME}@{APP_VERSION}", "short": True},
    ]
    if ctx_lines:
        fields.append({"title": "Context", "value": "\n".join(ctx_lines), "short": False})
    if stack_preview:
        fields.append({"title": "Stack", "value": f"```{stack_preview}```", "short": False})
    payload = {
        "text": f"{emoji} {severity.upper()}: {message[:200]}",
        "attachments": [{
            "color": color,
            "fields": fields,
            "footer": f"Energovision · {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        }],
    }
    try:
        requests.post(SLACK_ALERTS_WEBHOOK, json=payload, timeout=5)
    except Exception:
        pass


def _async_dispatch(severity: str, message: str, ctx: Dict[str, Any], exc: Optional[BaseException]) -> None:
    """Pošli na Sentry + Slack v background thread aby sa neblokoval request."""
    def _run():
        try:
            _send_sentry(severity, message, ctx, exc)
        except Exception:
            pass
        try:
            _send_slack(severity, message, ctx, exc)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def track_error(error: BaseException, context: Optional[Dict[str, Any]] = None) -> None:
    msg = str(error) or type(error).__name__
    log.error("[track-error] %s · ctx=%s", msg, context, exc_info=error)
    _async_dispatch("error", msg, context or {}, error)


def track_warning(message: str, context: Optional[Dict[str, Any]] = None) -> None:
    log.warning("[track-warning] %s · ctx=%s", message, context)
    _async_dispatch("warning", message, context or {}, None)


def track_info(message: str, context: Optional[Dict[str, Any]] = None) -> None:
    _async_dispatch("info", message, context or {}, None)
