"""LP plánovač batérie pre site režim (samospotreba + arbitráž za retail cenu OM).

Rolling horizon: v každom bode rozhodnutia sa optimalizuje `window` krokov dopredu
(day-ahead spot je známy o 12:00 na ďalší deň → 36 h) a EMS z plánu použije prvých
`commit` krokov; potom sa plán prepočíta zo SKUTOČNÉHO SoC batérie.

Nahrádza pravidlá P2b–P4 v RuleBasedEMS, ktoré dosahovali 39–66 % optima a väčšia batéria
vedela zarobiť menej (audit 2026-10-24 E02, koreň t4). Plán určuje len koľko nabiť z FVE,
koľko zo siete a koľko vybiť; fyziku (dynamická RTE, SoH) aj účtovanie robí RuleBasedEMS.
"""
from __future__ import annotations

import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog

# cena za kWh odberu nad RK — mäkké obmedzenie, aby LP bolo riešiteľné aj pri záťaži nad RK
_PRETAH_RK_EUR_KWH = 1.0
# drobný náklad na vybitie — bráni zbytočnému cyklovaniu pri rovnakej cene
_TIE_EUR_KWH = 1e-5


def lp_okno(load_kwh, pv_kwh, tar_eur_kwh, exp_eur_kwh, spot_eur_mwh, *, soc0, smin, smax,
            p_kwh, eta, rk_kwh, mrk_kwh, budget_dc_kwh, grid_charge_eur_kwh=0.0,
            curtail_neg=True):
    """Optimálny plán pre jedno okno. Všetky toky v kWh za krok. Vracia (pv_to_bat, grid_to_bat,
    bat_to_load) ako numpy polia, alebo None, ak LP zlyhá (EMS potom použije pravidlá).

    grid_charge_eur_kwh: prah arbitráže zo siete (arb_min_spread) — nabitie zo siete sa oplatí,
    len ak spread pokryje straty AJ tento prah (rovnaká sémantika ako pravidlové EMS)."""
    n = len(load_kwh)
    IL, IB, PL, PB, PE, PC, D, S, OV = range(9)
    nv = 9
    t = np.arange(n)
    ix = lambda k: k * n + t  # noqa: E731

    c = np.zeros(nv * n)
    c[ix(IL)] = tar_eur_kwh
    c[ix(IB)] = tar_eur_kwh + grid_charge_eur_kwh
    c[ix(PE)] = -exp_eur_kwh
    c[ix(D)] = _TIE_EUR_KWH
    c[ix(OV)] = _PRETAH_RK_EUR_KWH

    lb = np.zeros(nv * n)
    ub = np.full(nv * n, np.inf)
    ub[ix(S)] = smax
    lb[ix(S)] = smin
    ub[ix(D)] = p_kwh
    ub[ix(PE)] = mrk_kwh if mrk_kwh and mrk_kwh > 0 else np.inf
    if mrk_kwh is not None and mrk_kwh <= 0:
        ub[ix(PE)] = 0.0                      # export zakázaný (MRK 0)
    if curtail_neg:
        ub[ix(PE)] = np.where(np.asarray(spot_eur_mwh) < 0, 0.0, ub[ix(PE)])

    rows, cols, vals, beq = [], [], [], []
    r = 0
    for k in (PL, PB, PE, PC):                # PV: priamo, do batérie, export, orezanie
        rows += list(r + t); cols += list(ix(k)); vals += [1.0] * n
    beq += list(pv_kwh); r += n
    for k in (PL, D, IL):                     # záťaž: z PV, z batérie, zo siete
        rows += list(r + t); cols += list(ix(k)); vals += [1.0] * n
    beq += list(load_kwh); r += n
    # SoC: s_t − s_{t−1} − η(pb+ib) + d/η = 0, s_{−1} = soc0
    rows += list(r + t); cols += list(ix(S)); vals += [1.0] * n
    rows += list(r + t[1:]); cols += list(S * n + t[1:] - 1); vals += [-1.0] * (n - 1)
    rows += list(r + t); cols += list(ix(PB)); vals += [-eta] * n
    rows += list(r + t); cols += list(ix(IB)); vals += [-eta] * n
    rows += list(r + t); cols += list(ix(D)); vals += [1.0 / eta] * n
    b = np.zeros(n); b[0] = soc0
    beq += list(b); r += n
    a_eq = sp.csr_matrix((vals, (rows, cols)), shape=(r, nv * n))

    rows, cols, vals, bub = [], [], [], []
    r = 0
    for k, v in ((IL, 1.0), (IB, 1.0), (OV, -1.0)):   # odber ≤ RK (+ pretiahnutie s pokutou)
        rows += list(r + t); cols += list(ix(k)); vals += [v] * n
    bub += [rk_kwh] * n; r += n
    for k in (PB, IB):                                  # PCS: nabíjanie z PV aj siete zdieľa výkon
        rows += list(r + t); cols += list(ix(k)); vals += [1.0] * n
    bub += [p_kwh] * n; r += n
    if budget_dc_kwh is not None:                       # rozpočet cyklov (DC uložené)
        for k in (PB, IB):
            rows += [r] * n; cols += list(ix(k)); vals += [eta] * n
        bub += [max(0.0, budget_dc_kwh)]; r += 1
    a_ub = sp.csr_matrix((vals, (rows, cols)), shape=(r, nv * n))

    try:
        res = linprog(c, A_ub=a_ub, b_ub=bub, A_eq=a_eq, b_eq=beq,
                      bounds=list(zip(lb, ub)), method="highs")
    except Exception:
        return None
    if res.status != 0 or res.x is None:
        return None
    x = res.x.reshape(nv, n)
    return np.maximum(x[PB], 0.0), np.maximum(x[IB], 0.0), np.maximum(x[D], 0.0)
