"""Jedinečné ID variantu (audit 2026-10-24 A33).

`f"PV{kwp:.0f}_BESS{kwh:.0f}_..."` zaokrúhli desatinné veľkosti → 10.1 a 10.2 kWp
dostanú rovnaké ID a výber/štítky/párovanie ich zamieňajú. Celočíselné veľkosti
musia ostať v pôvodnom tvare („PV300_BESS0_rule_based") — ID sa páruje s uloženými
posudkami, retrofitom a top_picks.
"""
from __future__ import annotations


def _size(x: float) -> str:
    x = float(x)
    if x.is_integer():
        return f"{x:.0f}"  # identické so starým formátom
    return f"{x:.6f}".rstrip("0").rstrip(".")


def make_variant_id(pv_kwp: float, bess_kwh: float, ems_strategy: str) -> str:
    return f"PV{_size(pv_kwp)}_BESS{_size(bess_kwh)}_{ems_strategy}"
