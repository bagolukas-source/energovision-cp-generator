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


class LpPlanovac:
    """Rýchly LP plánovač: model HiGHS sa postaví RAZ pre daný počet krokov okna a pri každom
    okne sa menia len ceny a hranice stĺpcov (warm start z minulého riešenia).

    Údaje okna (FVE, záťaž, počiatočný SoC, rozpočet cyklov) vstupujú ako pevné stĺpce
    s dolnou = hornou hranicou — HiGHS nemá hromadnú zmenu pravých strán riadkov.
    Matematicky totožné s `lp_okno`; scipy.linprog pri každom okne model skladal odznova
    (~1 s/rok lokálne, ~4 s/rok na Renderi → matica s rastom spotreby > 120 s)."""

    def __init__(self, n, *, eta, p_kwh, rk_kwh, grid_charge_eur_kwh=0.0):
        import highspy
        self.n = n
        self.eta = float(eta)
        self.p_kwh = float(p_kwh)
        self.gc = float(grid_charge_eur_kwh)
        t = np.arange(n)
        # stĺpce: 9 tokov × n + PVSRC n + LOADSRC n + SOC0 1 + BUD 1
        self.IL, self.IB, self.PL, self.PB, self.PE, self.PC, self.D, self.S, self.OV = (k * n for k in range(9))
        self.PVS, self.LDS = 9 * n, 10 * n
        self.S0, self.BUD = 11 * n, 11 * n + 1
        nc = 11 * n + 2
        rows, cols, vals, lo, up = [], [], [], [], []
        r = 0

        def add(rr, cc, vv):
            rows.extend(rr); cols.extend(cc); vals.extend(vv)

        for k in (self.PL, self.PB, self.PE, self.PC):          # PV split − PVSRC = 0
            add(r + t, k + t, [1.0] * n)
        add(r + t, self.PVS + t, [-1.0] * n)
        lo += [0.0] * n; up += [0.0] * n; r += n
        for k in (self.PL, self.D, self.IL):                    # krytie záťaže − LOADSRC = 0
            add(r + t, k + t, [1.0] * n)
        add(r + t, self.LDS + t, [-1.0] * n)
        lo += [0.0] * n; up += [0.0] * n; r += n
        add(r + t, self.S + t, [1.0] * n)                       # SoC dynamika, s_{-1} = SOC0
        add(r + t[1:], self.S + t[1:] - 1, [-1.0] * (n - 1))
        add([r], [self.S0], [-1.0])
        add(r + t, self.PB + t, [-self.eta] * n)
        add(r + t, self.IB + t, [-self.eta] * n)
        add(r + t, self.D + t, [1.0 / self.eta] * n)
        lo += [0.0] * n; up += [0.0] * n; r += n
        add(r + t, self.IL + t, [1.0] * n)                      # odber ≤ RK (+ pretiahnutie)
        add(r + t, self.IB + t, [1.0] * n)
        add(r + t, self.OV + t, [-1.0] * n)
        lo += [-np.inf] * n; up += [float(rk_kwh)] * n; r += n
        add(r + t, self.PB + t, [1.0] * n)                      # PCS nabíjanie zdieľané
        add(r + t, self.IB + t, [1.0] * n)
        lo += [-np.inf] * n; up += [self.p_kwh] * n; r += n
        add([r] * n, self.PB + t, [self.eta] * n)               # rozpočet cyklov − BUD ≤ 0
        add([r] * n, self.IB + t, [self.eta] * n)
        add([r], [self.BUD], [-1.0])
        lo += [-np.inf]; up += [0.0]; r += 1

        a = sp.csc_matrix((vals, (rows, cols)), shape=(r, nc))
        lp = highspy.HighsLp()
        lp.num_col_, lp.num_row_ = nc, r
        lp.col_cost_ = np.zeros(nc)
        lp.col_lower_ = np.zeros(nc)
        lp.col_upper_ = np.full(nc, highspy.kHighsInf)
        lp.row_lower_ = np.where(np.isinf(lo), -highspy.kHighsInf, lo)
        lp.row_upper_ = np.array(up, float)
        lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
        lp.a_matrix_.start_ = a.indptr
        lp.a_matrix_.index_ = a.indices
        lp.a_matrix_.value_ = a.data
        self.h = highspy.Highs()
        self.h.setOptionValue("output_flag", False)
        self.h.passModel(lp)
        self._idx = np.arange(nc, dtype=np.int32)
        self._inf = highspy.kHighsInf
        self._ok = highspy.HighsModelStatus.kOptimal

    def plan(self, load_kwh, pv_kwh, tar_eur_kwh, exp_eur_kwh, spot_eur_mwh, *, soc0, smin, smax,
             mrk_kwh, budget_dc_kwh, curtail_neg=True):
        n, inf = self.n, self._inf
        c = np.zeros(11 * n + 2)
        c[self.IL:self.IL + n] = tar_eur_kwh
        c[self.IB:self.IB + n] = np.asarray(tar_eur_kwh) + self.gc
        c[self.PE:self.PE + n] = -np.asarray(exp_eur_kwh)
        c[self.D:self.D + n] = _TIE_EUR_KWH
        c[self.OV:self.OV + n] = _PRETAH_RK_EUR_KWH
        lo = np.zeros(11 * n + 2)
        up = np.full(11 * n + 2, inf)
        pe_up = mrk_kwh if (mrk_kwh is not None and mrk_kwh > 0) else (0.0 if mrk_kwh is not None else inf)
        up[self.PE:self.PE + n] = pe_up
        if curtail_neg:
            up[self.PE:self.PE + n] = np.where(np.asarray(spot_eur_mwh) < 0, 0.0, up[self.PE:self.PE + n])
        up[self.D:self.D + n] = self.p_kwh
        lo[self.S:self.S + n] = smin; up[self.S:self.S + n] = smax
        lo[self.PVS:self.PVS + n] = pv_kwh; up[self.PVS:self.PVS + n] = pv_kwh
        lo[self.LDS:self.LDS + n] = load_kwh; up[self.LDS:self.LDS + n] = load_kwh
        lo[self.S0] = up[self.S0] = soc0
        up[self.BUD] = inf if budget_dc_kwh is None else max(0.0, budget_dc_kwh)
        h = self.h
        h.changeColsCost(len(c), self._idx, c)
        h.changeColsBounds(len(c), self._idx, lo, up)
        h.run()
        if h.getModelStatus() != self._ok:
            return None
        x = np.asarray(h.getSolution().col_value)
        return (np.maximum(x[self.PB:self.PB + n], 0.0), np.maximum(x[self.IB:self.IB + n], 0.0),
                np.maximum(x[self.D:self.D + n], 0.0))
