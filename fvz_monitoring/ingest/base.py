"""VendorAdapter abstrakcia.

Každý vendor (Huawei, Solinteg, GoodWe, Fronius, Sungrow) implementuje túto
triedu. Orchestrátor potom rovnakým spôsobom volá `fetch_plant_list`,
`fetch_realtime_batch`, `fetch_daily_summary`, `fetch_alarms`, `send_command`.

Adaptér je zodpovedný za:
- login + refresh tokenu
- rate limiting (tenacity decorator)
- mapping vendor schémy → canonical dataclasses
- error handling (raise VendorError, RateLimitError, AuthError)
"""

from __future__ import annotations

import os
import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, date
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .canonical import PlantInfo, TelemetrySnapshot, DailySummary, VendorAlarm


log = logging.getLogger(__name__)


# =============================================================================
# Exceptions
# =============================================================================

class VendorError(Exception):
    """Generická chyba zo strany vendor API."""


class AuthError(VendorError):
    """Login/token zlyhal alebo expiroval."""


class RateLimitError(VendorError):
    """Vendor vrátil 429 alebo ekvivalent."""


class NotAuthorizedError(VendorError):
    """Vendor odmietol scope (napr. Huawei Service Provider scope chýba)."""


# =============================================================================
# Base adapter
# =============================================================================

class VendorAdapter(ABC):
    """Abstraktný interface pre všetkých vendorov."""

    vendor: str = "abstract"        # override v subclass
    api_base: str = ""              # override v subclass

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_base: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
        credentials_loader: Optional[callable] = None,
    ):
        self.username = username
        self.password = password
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_base = api_base or self.api_base
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expires_at: Optional[float] = None

        # Lazy-load credentials zo Supabase tabuľky inverter_vendor_credentials
        # ak nie sú dodané explicitne.
        if credentials_loader and not (username or api_key):
            creds = credentials_loader(self.vendor)
            self.username = creds.get("username") or self.username
            self.password = creds.get("password") or self.password
            self.api_key = creds.get("api_key") or self.api_key
            self.api_secret = creds.get("api_secret") or self.api_secret

    # -------------------------------------------------------------------------
    # Abstract interface — povinné v subclass
    # -------------------------------------------------------------------------

    @abstractmethod
    def login(self) -> None:
        """Získa bearer token / session cookie. Cache do self._token."""

    @abstractmethod
    def fetch_plant_list(self) -> list[PlantInfo]:
        """Vráti všetky stanice viditeľné pod installer účtom."""

    @abstractmethod
    def fetch_realtime_batch(self, plant_ids: list[str]) -> list[TelemetrySnapshot]:
        """Vráti aktuálny realtime snapshot pre dané stanice (alebo všetky ak prázdny zoznam).

        Implementácia musí preferovať batch endpoint pred per-plant volaním.
        Pri 400 staniciach a 5min poolingu je per-plant volanie cesta do rate limit pekla.
        """

    @abstractmethod
    def fetch_daily_summary(self, plant_id: str, day: date) -> Optional[DailySummary]:
        """Denný súhrn pre jednu stanicu (energy, peak, export, atď.)."""

    @abstractmethod
    def fetch_alarms(self, since: Optional[datetime] = None) -> list[VendorAlarm]:
        """Aktívne / nové alarmy zo všetkých staníc od daného času."""

    @abstractmethod
    def send_command(self, plant_id: str, command: str, params: dict) -> dict:
        """Pošle príkaz meniču (set_active_power_limit, restart, ...).

        Vracia vendor response. Throws NotAuthorizedError ak SP scope chýba.
        Implementácia musí zapísať audit záznam do inverter_commands tabuľky.
        """

    # -------------------------------------------------------------------------
    # Spoločné helpers
    # -------------------------------------------------------------------------

    def _ensure_token(self) -> None:
        """Zabezpečí že máme platný token; login + refresh ako treba."""
        if self._token and (self._token_expires_at is None or self._token_expires_at > time.time() + 30):
            return
        self.login()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, RateLimitError)),
        reraise=True,
    )
    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Sieťové volanie s retry pri sieťových chybách a 429."""
        self._ensure_token()
        url = f"{self.api_base}{path}"
        headers = kwargs.pop("headers", {})
        if self._token:
            headers.setdefault("Authorization", f"Bearer {self._token}")
        kwargs.setdefault("timeout", self.timeout)
        r = self.session.request(method, url, headers=headers, **kwargs)
        if r.status_code == 429:
            log.warning(f"[{self.vendor}] rate-limit hit on {path}")
            raise RateLimitError(f"429 from {url}")
        if r.status_code in (401, 403):
            # Token expirovaný? Re-login a try once
            log.warning(f"[{self.vendor}] auth error on {path}, attempting re-login")
            self._token = None
            self._ensure_token()
            r = self.session.request(method, url, headers={**headers, "Authorization": f"Bearer {self._token}"}, **kwargs)
            if r.status_code in (401, 403):
                raise AuthError(f"{r.status_code} on {url}: {r.text[:200]}")
        if r.status_code >= 400:
            raise VendorError(f"{r.status_code} from {url}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    # -------------------------------------------------------------------------
    # Wrappers s persistovaním do Supabase (volá orchestrátor)
    # -------------------------------------------------------------------------

    def health_check(self) -> dict:
        """Quick health check — login + plant_list count. Pre Render readiness probe."""
        try:
            self.login()
            plants = self.fetch_plant_list()
            return {"vendor": self.vendor, "ok": True, "plant_count": len(plants)}
        except Exception as e:
            return {"vendor": self.vendor, "ok": False, "error": str(e)}
