"""Centrálne logging — namiesto print().

Použitie:
    from energovision_analytics.core.logging import get_logger
    log = get_logger(__name__)
    log.info("Variant generated: %s", variant_id)
    log.warning("BESS warranty cycles exceeded")
    log.debug("Detailed dispatch values: ...")  # zobrazí len pri --verbose

CLI/Streamlit ovláda úroveň cez env var:
    ENERGO_LOG_LEVEL=DEBUG streamlit run streamlit_app/app.py
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("ENERGO_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)5s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger("energovision_analytics")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Vráti logger s prefixom 'energovision_analytics.<name>'."""
    _configure()
    if name is None:
        return logging.getLogger("energovision_analytics")
    short = name.replace("energovision_analytics.", "").replace("energovision_analytics", "root")
    return logging.getLogger(f"energovision_analytics.{short}")
