"""Lightweight in-memory rate limiter pre Flask endpoints.

Token bucket per (IP, endpoint). Bez Redis/external deps.
Pre Render multi-worker: každý worker má svoj bucket, ale to je OK pri starter plan (1-2 workers).

Použitie:
    from rate_limiter import rate_limit

    @app.route("/webhook/foo")
    @rate_limit(max_calls=10, window_seconds=60)
    def foo(): ...
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict, deque
from functools import wraps
from flask import request, jsonify

# Per-process state — (key) → deque of timestamps
_buckets: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def _client_key(endpoint: str) -> str:
    """Identifikuj klienta cez IP + endpoint. Z X-Forwarded-For ak je."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    return f"{ip}::{endpoint}"


def rate_limit(max_calls: int = 30, window_seconds: int = 60, bypass_secret: bool = True):
    """Decorator. max_calls v okne window_seconds.

    bypass_secret=True: ak request má valid X-Webhook-Secret, rate limit sa preskočí
    (cron jobs, internal scripts).
    """
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            # Bypass pre internal cron/webhook calls
            if bypass_secret:
                import os
                secret = request.headers.get("X-Webhook-Secret") or request.args.get("secret")
                expected = os.environ.get("WEBHOOK_SECRET")
                if expected and secret == expected:
                    return fn(*args, **kwargs)

            key = _client_key(request.endpoint or request.path)
            now = time.time()
            window_start = now - window_seconds

            with _lock:
                bucket = _buckets[key]
                # Drop expired entries
                while bucket and bucket[0] < window_start:
                    bucket.popleft()
                if len(bucket) >= max_calls:
                    retry_after = int(bucket[0] + window_seconds - now) + 1
                    response = jsonify({
                        "ok": False,
                        "error": "rate_limit_exceeded",
                        "max_calls": max_calls,
                        "window_seconds": window_seconds,
                        "retry_after_sec": retry_after,
                    })
                    response.status_code = 429
                    response.headers["Retry-After"] = str(retry_after)
                    response.headers["X-RateLimit-Limit"] = str(max_calls)
                    response.headers["X-RateLimit-Remaining"] = "0"
                    response.headers["X-RateLimit-Reset"] = str(int(bucket[0] + window_seconds))
                    return response
                bucket.append(now)
                remaining = max_calls - len(bucket)

            # Pridaj rate limit headers do success response
            response = fn(*args, **kwargs)
            try:
                if hasattr(response, "headers"):
                    response.headers["X-RateLimit-Limit"] = str(max_calls)
                    response.headers["X-RateLimit-Remaining"] = str(remaining)
            except Exception:
                pass
            return response
        return wrapped
    return decorator


def cleanup_old_buckets():
    """Periodic cleanup — odstráni unused keys (older than 1h). Volaj raz za hodinu."""
    cutoff = time.time() - 3600
    with _lock:
        to_remove = [k for k, b in _buckets.items() if not b or b[-1] < cutoff]
        for k in to_remove:
            del _buckets[k]
