"""PV degradácia per technológia."""
from __future__ import annotations


# Ročná degradácia v % per technológia (warranty: 0.4 %/rok lineárne pre TOPCon)
PV_DEGRADATION_PCT_PER_YEAR: dict[str, float] = {
    "TOPCon": 0.4,    # n-type, najmodernejší (2024-2026)
    "HJT": 0.25,      # heterojunction, najlepší
    "N-Type": 0.3,
    "PERC": 0.45,    # mainstream do 2024
    "Bifacial": 0.45,
    "default": 0.5,
}


def pv_capacity_factor(year: int, modul_typ: str = "TOPCon", first_year_drop_pct: float = 2.0) -> float:
    """Vráti capacity factor pre rok N (1-indexed).

    Year 1 má "first year drop" (LID, PID — typicky 2-2.5 % pri kremíku).
    Year 2+ má lineárnu degradáciu per rok podľa typu modulu.
    """
    if year < 1:
        return 1.0

    annual_deg = PV_DEGRADATION_PCT_PER_YEAR.get(modul_typ, PV_DEGRADATION_PCT_PER_YEAR["default"]) / 100

    if year == 1:
        return 1.0 - first_year_drop_pct / 100
    return (1.0 - first_year_drop_pct / 100) * (1.0 - annual_deg) ** (year - 1)
