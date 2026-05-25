"""Scorer — výber top-N variantov podľa rôznych kritérií."""
from __future__ import annotations

from typing import Optional

from energovision_analytics.variants.generator import VariantResult


def pick_top_variants(
    variants: list[VariantResult],
    n: int = 6,
) -> list[tuple[str, VariantResult]]:
    """Vyber top N variantov podľa 6 kritérií (jeden variant na kritérium).

    Returns:
        List (label, VariantResult) párov:
            "Najvyššie NPV"
            "Najvyššie IRR"
            "Najrýchlejšia návratnosť"
            "Najvyššia samospotreba"
            "Najvyššia samostatnosť"
            "Najlacnejšia investícia s NPV > 0"
    """
    # Filter ven valid (positive NPV preferred)
    valid = [v for v in variants if v.npv_eur is not None]
    if not valid:
        return []

    # Sortuj podľa rôznych metrik
    by_npv = sorted(valid, key=lambda v: -v.npv_eur)
    by_irr = sorted([v for v in valid if v.irr_pct], key=lambda v: -v.irr_pct)
    by_payback = sorted([v for v in valid if 0 < v.payback_y < 99], key=lambda v: v.payback_y)
    by_samospotreba = sorted(valid, key=lambda v: -v.samospotreba_pct)
    by_samostatnost = sorted(valid, key=lambda v: -v.samostatnost_pct)

    # Najlacnejšia s positive NPV
    positive_npv = [v for v in valid if v.npv_eur > 0]
    by_cheapest_positive = sorted(positive_npv, key=lambda v: v.capex_total_eur)

    chosen: list[tuple[str, VariantResult]] = []
    seen_ids: set[str] = set()

    def add(label, candidates):
        for v in candidates:
            if v.variant_id not in seen_ids:
                chosen.append((label, v))
                seen_ids.add(v.variant_id)
                return
        # Fallback — pridaj prvý ak nič nové
        if candidates:
            chosen.append((label, candidates[0]))

    add("Najvyššie NPV", by_npv)
    add("Najvyššie IRR", by_irr)
    add("Najrýchlejšia návratnosť", by_payback)
    add("Najvyššia samospotreba", by_samospotreba)
    add("Najvyššia samostatnosť", by_samostatnost)
    add("Najlacnejšia s NPV > 0", by_cheapest_positive)

    return chosen[:n]


def variants_to_dataframe(variants: list[VariantResult]):
    """Vráti pandas DataFrame so všetkými variantami pre porovnanie."""
    import pandas as pd
    rows = []
    for v in variants:
        rows.append({
            "id": v.variant_id,
            "PV (kWp)": v.pv_kwp,
            "BESS (kWh)": v.bess_kwh,
            "BESS (kW)": v.bess_kw,
            "EMS": v.ems_strategy,
            "CAPEX (€)": v.capex_total_eur,
            "Dotácia (€)": v.dotacia_eur,
            "Net CAPEX (€)": v.capex_total_eur - v.dotacia_eur,
            "Úspora Y1 (€)": v.saving_y1_eur,
            "NPV (€)": v.npv_eur,
            "IRR (%)": v.irr_pct,
            "Payback (r)": v.payback_y,
            "Samospotreba (%)": v.samospotreba_pct,
            "Samostatnosť (%)": v.samostatnost_pct,
        })
    return pd.DataFrame(rows)
