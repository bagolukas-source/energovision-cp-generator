"""Monte Carlo simulácia neistoty NPV/IRR."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass
class MonteCarloConfig:
    """Konfigurácia Monte Carlo neistoty."""
    n_runs: int = 5000
    # Distribučné parametre pre top 5 premenných
    spot_price_sigma: float = 0.25      # ±25 % spot ceny (lognormal sigma)
    capex_sigma: float = 0.10           # ±10 % CAPEX (normal)
    load_sigma: float = 0.05            # ±5 % load (normal)
    pv_yield_sigma: float = 0.08        # ±8 % PV výnos (normal)
    bess_degradation_sigma: float = 0.30  # ±30 % degradácia rate (normal)
    discount_rate_sigma: float = 0.01    # ±1 percentage point


def monte_carlo_npv(
    baseline_npv_fn: Callable[[dict], float],
    baseline_inputs: dict,
    config: MonteCarloConfig | None = None,
    seed: int = 42,
) -> dict:
    """Spustí MC simuláciu na NPV.

    Args:
        baseline_npv_fn: funkcia ktorá berie dict s perturbovanými parametrami
                         a vracia NPV
        baseline_inputs: dict s baseline hodnotami (spot_factor, capex_factor, ...)
        config: MC config

    Returns:
        Dict s p10/p25/p50/p75/p90 NPV + probability NPV positive
    """
    cfg = config or MonteCarloConfig()
    rng = np.random.default_rng(seed)
    npvs = []

    for _ in range(cfg.n_runs):
        perturbed = dict(baseline_inputs)
        perturbed["spot_factor"] = float(rng.lognormal(0, cfg.spot_price_sigma))
        perturbed["capex_factor"] = float(rng.normal(1.0, cfg.capex_sigma))
        perturbed["load_factor"] = float(rng.normal(1.0, cfg.load_sigma))
        perturbed["pv_yield_factor"] = float(rng.normal(1.0, cfg.pv_yield_sigma))
        perturbed["degradation_factor"] = float(rng.normal(1.0, cfg.bess_degradation_sigma))
        perturbed["discount_rate_add"] = float(rng.normal(0, cfg.discount_rate_sigma))

        try:
            npv = baseline_npv_fn(perturbed)
            npvs.append(npv)
        except Exception:
            continue

    npvs = np.array(npvs)
    return {
        "n_runs": len(npvs),
        "p10": float(np.percentile(npvs, 10)),
        "p25": float(np.percentile(npvs, 25)),
        "p50": float(np.percentile(npvs, 50)),
        "p75": float(np.percentile(npvs, 75)),
        "p90": float(np.percentile(npvs, 90)),
        "mean": float(np.mean(npvs)),
        "stdev": float(np.std(npvs)),
        "prob_positive": float((npvs > 0).mean()),
        "min": float(np.min(npvs)),
        "max": float(np.max(npvs)),
    }


def tornado_sensitivity(
    baseline_npv_fn: Callable[[dict], float],
    baseline_inputs: dict,
    variables: dict[str, tuple[float, float]] | None = None,
) -> list[dict]:
    """Deterministická sensitivity analýza (tornado chart).

    Args:
        baseline_npv_fn: funkcia podľa Monte Carlo
        baseline_inputs: baseline
        variables: dict premenných {name: (low_factor, high_factor)}

    Returns:
        List dict s name, low_npv, high_npv, range
    """
    if variables is None:
        variables = {
            "spot_factor": (0.70, 1.30),
            "capex_factor": (0.90, 1.10),
            "load_factor": (0.95, 1.05),
            "pv_yield_factor": (0.92, 1.08),
            "degradation_factor": (0.70, 1.30),
        }

    baseline_npv = baseline_npv_fn(dict(baseline_inputs))
    results = []
    for name, (lo, hi) in variables.items():
        inputs_lo = dict(baseline_inputs)
        inputs_lo[name] = lo
        npv_lo = baseline_npv_fn(inputs_lo)
        inputs_hi = dict(baseline_inputs)
        inputs_hi[name] = hi
        npv_hi = baseline_npv_fn(inputs_hi)
        results.append({
            "variable": name,
            "low_factor": lo,
            "high_factor": hi,
            "npv_low": npv_lo,
            "npv_high": npv_hi,
            "delta_low_eur": npv_lo - baseline_npv,
            "delta_high_eur": npv_hi - baseline_npv,
            "range_eur": abs(npv_hi - npv_lo),
        })

    # Sortni podľa range (najväčší vplyv top)
    results.sort(key=lambda r: -r["range_eur"])
    return results
