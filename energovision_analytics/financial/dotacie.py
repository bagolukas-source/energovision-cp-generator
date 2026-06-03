"""Dotačné schémy SK 2026 — loader + apply.

Použitie:
    from energovision_analytics.financial.dotacie import (
        load_dotacie_schemes, apply_dotacia,
    )

    schemes = load_dotacie_schemes()
    result = apply_dotacia(
        scheme_id="zelena_podnikom",
        capex_eur=120000,
        samospotreba_pct=85,
    )
    # → {"amount_eur": 50000, "intensity_applied": 0.417, "eligible": True}
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class DotaciaScheme:
    scheme_id: str
    nazov: str
    vyhlasovatel: str
    status: str
    max_eur: float
    intensity_pct: float
    min_samospotreba_pct: float
    applicable_to: list[str]
    notes: str = ""
    source_url: Optional[str] = None
    last_verified: Optional[str] = None


def _resolve_default_path() -> Path:
    """Robustné hľadanie dotačného YAML — env override + viac kandidátov
    (deploy štruktúra sa líši od lokálnej)."""
    import os
    env = os.environ.get("ENERGO_DOTACIE_YAML")
    if env and Path(env).exists():
        return Path(env)
    here = Path(__file__).resolve()
    cands = [
        here.parents[3] / "data" / "dotacie" / "sk_2026.yaml",
        here.parents[2] / "aom_data" / "dotacie" / "sk_2026.yaml",   # cp-generator committed
        here.parents[2] / "data" / "dotacie" / "sk_2026.yaml",
        here.parents[1] / "data" / "dotacie" / "sk_2026.yaml",       # package-local
        Path.cwd() / "aom_data" / "dotacie" / "sk_2026.yaml",
        Path.cwd() / "data" / "dotacie" / "sk_2026.yaml",
    ]
    for c in cands:
        if c.exists():
            return c
    return cands[0]  # fallback (neexistuje -> schemes={})

_DEFAULT_PATH = _resolve_default_path()


def load_dotacie_schemes(path: Optional[Path | str] = None) -> dict[str, DotaciaScheme]:
    """Načíta všetky dostupné dotačné schémy z YAML."""
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text())
    out = {}
    for scheme_id, fields in data.items():
        out[scheme_id] = DotaciaScheme(
            scheme_id=scheme_id,
            nazov=fields.get("nazov", scheme_id),
            vyhlasovatel=fields.get("vyhlasovatel", ""),
            status=fields.get("status", "unknown"),
            max_eur=float(fields.get("max_eur", 0)),
            intensity_pct=float(fields.get("intensity_pct", 0)),
            min_samospotreba_pct=float(fields.get("min_samospotreba_pct", 0)),
            applicable_to=fields.get("applicable_to", []),
            notes=fields.get("notes", ""),
            source_url=fields.get("source_url"),
            last_verified=fields.get("last_verified"),
        )
    return out


def apply_dotacia(
    scheme_id: str,
    capex_eur: float,
    samospotreba_pct: float,
    project_type: str = "FVE+BESS",
    schemes: Optional[dict[str, DotaciaScheme]] = None,
) -> dict:
    """Vypočíta výšku dotácie pre daný scenár.

    Returns:
        {
            "scheme": DotaciaScheme,
            "eligible": bool,
            "amount_eur": float,
            "intensity_applied": float,
            "reason_if_ineligible": str | None,
        }
    """
    if schemes is None:
        schemes = load_dotacie_schemes()

    if scheme_id not in schemes:
        return {
            "scheme": None,
            "eligible": False,
            "amount_eur": 0.0,
            "intensity_applied": 0.0,
            "reason_if_ineligible": f"Schéma '{scheme_id}' nenájdená",
        }

    s = schemes[scheme_id]

    # Eligibility checks
    if s.status == "closed":
        return {
            "scheme": s, "eligible": False, "amount_eur": 0.0,
            "intensity_applied": 0.0,
            "reason_if_ineligible": f"{s.nazov} — výzva uzavretá",
        }
    if project_type not in s.applicable_to and s.applicable_to:
        return {
            "scheme": s, "eligible": False, "amount_eur": 0.0,
            "intensity_applied": 0.0,
            "reason_if_ineligible": (
                f"{s.nazov} sa nevzťahuje na typ projektu '{project_type}' "
                f"(povolené: {', '.join(s.applicable_to)})"
            ),
        }
    if samospotreba_pct < s.min_samospotreba_pct:
        return {
            "scheme": s, "eligible": False, "amount_eur": 0.0,
            "intensity_applied": 0.0,
            "reason_if_ineligible": (
                f"{s.nazov} vyžaduje min {s.min_samospotreba_pct:.0f} % samospotreby, "
                f"projekt má {samospotreba_pct:.1f} %"
            ),
        }

    # Calculate
    by_intensity = capex_eur * (s.intensity_pct / 100)
    amount = min(by_intensity, s.max_eur)
    return {
        "scheme": s,
        "eligible": True,
        "amount_eur": amount,
        "intensity_applied": amount / capex_eur if capex_eur > 0 else 0.0,
        "reason_if_ineligible": None,
    }
