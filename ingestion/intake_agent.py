"""AI intake agent pre Analýzu OM — „cowork v CRM".

Rozcestník: dostane súbory + kontext (faktúra/MRK/segment), rozhodne stratégiu
(measured / extrapolate / synthesize / manual), prevedie na JEDNU kanonickú formu
(15-min MWh celý rok) a vráti reasoning report + validáciu + istotu.

Princíp: 100 % isté čo dáta hovoria, transparentné čo je domyslené. Nikdy nemiešať.
"""
from __future__ import annotations
import os, json, logging
import pandas as pd
import numpy as np

from ingestion import normalizer as _norm

log = logging.getLogger("intake_agent")

# Default mesačné sezónne váhy spotreby (SK komerčný/priemyselný priemer, mierna sezónnosť).
# Slúžia LEN na extrapoláciu chýbajúcich mesiacov; reálny tvar ide z nameraných dát.
_SEASON = {1:1.03,2:0.97,3:1.00,4:0.98,5:1.00,6:1.02,7:1.00,8:1.00,9:1.01,10:1.01,11:0.98,12:0.95}

_SEGMENT_TEMPLATE = {
    "priemysel": dict(peak_hours=(6,18), peak_kw_extra=6.0, base_kw=6.0, winter_factor=1.10),
    "vyroba":    dict(peak_hours=(6,18), peak_kw_extra=6.0, base_kw=6.0, winter_factor=1.10),
    "kancelaria":dict(peak_hours=(8,17), peak_kw_extra=8.0, base_kw=3.0, winter_factor=1.20),
    "obchod":    dict(peak_hours=(9,20), peak_kw_extra=7.0, base_kw=3.5, winter_factor=1.15),
    "domacnost": dict(peak_hours=(17,22),peak_kw_extra=8.0, base_kw=2.0, winter_factor=1.30),
}


# ============================================================
# Kombinácia viacerých nameraných súborov
# ============================================================
def _combine(series_list: list[pd.Series]) -> pd.Series:
    """Mesačné rezy (malý prekryv) → zjednoť/dedupe; podružné merače (veľký prekryv) → sčítaj."""
    if not series_list:
        return pd.Series(dtype=float)
    if len(series_list) == 1:
        return series_list[0].sort_index()
    cat = pd.concat(series_list)
    overlap = int(cat.index.duplicated().sum()) / max(1, len(cat))
    if overlap < 0.30:
        return cat[~cat.index.duplicated(keep="first")].sort_index()
    combined = series_list[0].copy()
    for s in series_list[1:]:
        # Rozhodni: ten istý odber v dvoch súboroch (duplicita) vs reálne podružné merače.
        common = combined.index.intersection(s.index)
        ov = len(common) / max(1, min(len(combined), len(s)))
        is_dup = False
        if len(common) >= 50 and ov > 0.5:
            a = combined.reindex(common).astype(float); b = s.reindex(common).astype(float)
            denom = float(a.abs().sum())
            rel_diff = float((a - b).abs().sum() / denom) if denom > 0 else 1.0
            try:
                corr = float(a.corr(b))
            except Exception:
                corr = 0.0
            # ten istý odber (zhodné hodnoty ALEBO zhodný tvar pri časovom prekryve) → duplicita, NEZDVOJUJ.
            # Rôzne profily pri prekryve = reálne podružné merače → sčítaj.
            if rel_diff < 0.05 or (corr == corr and corr > 0.9):
                is_dup = True
        if is_dup:
            combined = combined.combine_first(s).sort_index()
        else:
            combined = combined.add(s, fill_value=0)
    return combined.sort_index()


def _typical_week_kw(series_15: pd.Series) -> pd.DataFrame:
    """Z nameraných 15-min MWh vyrob typický týždeň priemerného výkonu kW
    indexovaný (dayofweek, hour, quarter)."""
    kw = series_15 * 4 * 1000.0  # MWh/15min → kW
    idx = kw.index
    df = pd.DataFrame({
        "kw": kw.values,
        "dow": idx.dayofweek,
        "hour": idx.hour,
        "q": (idx.minute // 15),
    })
    return df.groupby(["dow", "hour", "q"])["kw"].mean()


# ============================================================
# Stratégie doplnenia na celý rok
# ============================================================
def _extrapolate_to_year(measured_15: pd.Series, target_annual_kwh: float | None, year: int):
    """Reálny tvar (typický týždeň) z nameraných dát × sezónne váhy → celý rok 15-min MWh.
    Hladina: kalibruj na faktúru ak je, inak odhad z nameraných mesiacov."""
    tw = _typical_week_kw(measured_15)
    grid = pd.date_range(f"{year}-01-01 00:00", f"{year}-12-31 23:45", freq="15min")
    keys = list(zip(grid.dayofweek, grid.hour, grid.minute // 15))
    base_kw = np.array([tw.get(k, float(tw.mean())) for k in keys])
    season = np.array([_SEASON.get(m, 1.0) for m in grid.month])
    prof_kw = base_kw * season
    mwh = pd.Series(prof_kw * 0.25 / 1000.0, index=grid)
    # kalibrácia hladiny
    measured_annual_est = float(measured_15.sum()) / max(_covered_fraction(measured_15), 1e-6)
    target = target_annual_kwh if (target_annual_kwh and target_annual_kwh > 0) else measured_annual_est * 1000.0
    cur = float(mwh.sum()) * 1000.0
    if cur > 0:
        mwh = mwh * (target / cur)
    basis = "faktúra" if (target_annual_kwh and target_annual_kwh > 0) else "odhad z nameraných mesiacov"
    return mwh, {"target_annual_kwh": round(target), "level_basis": basis,
                 "measured_months": round(_covered_fraction(measured_15) * 12, 1)}


def _covered_fraction(series_15: pd.Series) -> float:
    """Aká časť roka je reálne pokrytá (podľa rozsahu dátumov, váženo sezónou)."""
    if len(series_15) == 0:
        return 0.0
    months = sorted(set(series_15.index.month))
    w = sum(_SEASON.get(m, 1.0) for m in months)
    return min(1.0, w / sum(_SEASON.values()))


def _synthesize_from_invoice(annual_kwh: float, segment: str, year: int):
    if not annual_kwh or annual_kwh <= 0:
        raise ValueError("syntetický profil odmietnutý: annual_kwh <= 0 (chýba ročná spotreba)")
    """Iba faktúra → syntetický 15-min profil z ročnej spotreby + segmentu."""
    from energovision_analytics.data.auto_fill import synthetic_load_profile
    params = _SEGMENT_TEMPLATE.get((segment or "kancelaria").lower(), _SEGMENT_TEMPLATE["kancelaria"])
    df = synthetic_load_profile(annual_kwh=annual_kwh, year=year, granularity_min=15, **params)
    kw = df["load_kw"]
    mwh = kw * 0.25 / 1000.0
    cur = float(mwh.sum()) * 1000.0
    if cur > 0:
        mwh = mwh * (annual_kwh / cur)
    mwh.index = df.index
    return mwh


# ============================================================
# Validácia + istota
# ============================================================
def _cross_check(annual_kwh, peak_kw, avg_kw, coverage_frac, invoice_annual_kwh, mrk_kw, strategy):
    checks, warnings = [], []
    conf = 0.9
    if invoice_annual_kwh and invoice_annual_kwh > 0:
        dev = annual_kwh / invoice_annual_kwh - 1
        ok = abs(dev) <= 0.15
        checks.append({"name": "faktúra cross-check", "ok": ok,
                       "detail": f"ročná {round(annual_kwh/1000)} MWh vs faktúra {round(invoice_annual_kwh/1000)} MWh ({dev*100:+.0f} %)"})
        if not ok:
            conf -= 0.4; warnings.append("Ročná spotreba nesedí s faktúrou (možná zámena kW/kWh alebo zlá granularita).")
        else:
            conf = min(0.99, conf + 0.08)
    else:
        warnings.append("Bez faktúry — ročná spotreba neoverená proti účtu.")
        conf -= 0.1
    if mrk_kw and mrk_kw > 0:
        ratio = peak_kw / mrk_kw if mrk_kw else 0
        ok = ratio <= 1.2
        checks.append({"name": "peak vs MRK", "ok": ok, "detail": f"peak {round(peak_kw)} kW vs MRK {round(mrk_kw)} kW ({ratio*100:.0f} %)"})
        if not ok:
            conf -= 0.3; warnings.append("Peak prekračuje MRK × 1.2 — možná zámena jednotky alebo zlé MRK.")
        if avg_kw and avg_kw > mrk_kw:
            checks.append({"name": "priemer vs MRK", "ok": False,
                           "detail": f"priemer {round(avg_kw)} kW > MRK {round(mrk_kw)} kW — fyzikálne nemožné"})
            conf -= 0.5
            warnings.append("Priemerný výkon prekračuje MRK — spotreba je takmer iste nadhodnotená (zámena jednotky alebo duplicitné súbory).")
    else:
        warnings.append("Bez MRK — peak nemá voči čomu overiť.")
    pa = peak_kw / avg_kw if avg_kw else 0
    if not (1.1 <= pa <= 20):
        conf -= 0.2; warnings.append(f"Pomer peak/priemer {pa:.1f} je netypický.")
    checks.append({"name": "peak/priemer", "ok": 1.1 <= pa <= 20, "detail": f"{pa:.1f}×"})
    if coverage_frac < 0.9 and strategy == "measured":
        warnings.append(f"Pokrytie len {coverage_frac*100:.0f} % roka.")
    if strategy == "extrapolated":
        conf = min(conf, 0.7); 
    if strategy == "synthesized":
        conf = min(conf, 0.5)
    conf = max(0.1, min(0.99, conf))
    needs_review = conf < 0.7 or any(not c["ok"] for c in checks)
    return {"confidence": round(conf, 2), "checks": checks, "warnings": warnings, "needs_review": needs_review}


# ============================================================
# Hlavný agent
# ============================================================
def run_agent(files: list[dict], context: dict | None = None, year: int = 2025, force: dict | None = None) -> dict:
    """files: [{filename, bytes}]. context: {invoice_annual_kwh, mrk_kw, segment}.
    Vráti kanonickú 15-min sériu + stratégiu + validáciu + reasoning + per-file."""
    context = context or {}
    invoice_annual = float(context.get("invoice_annual_kwh") or 0) or None
    mrk_kw = float(context.get("mrk_kw") or 0) or None
    segment = context.get("segment")

    per_file, series_list, spec_cache, warnings = [], [], {}, []
    for f in files:
        try:
            res = _norm.normalize_file(f["bytes"], f["filename"], context, spec_cache, force=force)
        except Exception as e:
            res = {"ok": False, "error": str(e)[:200]}
        if res.get("ok"):
            series_list.append(res["series"])
            per_file.append({"filename": f["filename"], "mwh": res["mwh"], "period": res.get("period"),
                             "granularity_min": res.get("granularity_min"), "source": res.get("source"),
                             "unit": (res.get("spec") or {}).get("value_unit")})
        else:
            per_file.append({"filename": f["filename"], "error": res.get("error"), "needs_manual": True})
            warnings.append(f"{f['filename']}: {res.get('error')} → na ručné spracovanie (dvojča)")

    measured = _combine(series_list) if series_list else pd.Series(dtype=float)
    coverage = _covered_fraction(measured) if len(measured) else 0.0
    # úplnosť roka: všetkých 12 kalendárnych mesiacov musí byť prítomných, inak doplniť extrapoláciou
    _months_present = set(int(m) for m in pd.Series(measured.index).dt.month.unique()) if len(measured) else set()
    _full_year = len(_months_present) >= 12

    # ---- ROZCESTNÍK ----
    strat_meta = {}
    if len(measured) == 0:
        if invoice_annual:
            strategy = "synthesized"
            final = _synthesize_from_invoice(invoice_annual, segment, year)
            strat_meta = {"segment": segment or "kancelaria", "annual_kwh": round(invoice_annual)}
        else:
            return {"ok": False, "strategy": "needs_input", "reason": "Žiadne čitateľné dáta ani faktúra.",
                    "per_file": per_file, "warnings": warnings}
    elif coverage >= 0.90 and _full_year:
        strategy = "measured"
        final = measured
    else:
        strategy = "extrapolated"
        final, strat_meta = _extrapolate_to_year(measured, invoice_annual, year)

    final = final[~final.index.duplicated(keep="first")].sort_index()
    annual_kwh = float(final.sum()) * 1000.0
    peak_kw_15 = float(final.max()) * 4 * 1000.0
    hourly = final.resample("1h").sum() * 1000.0   # kWh/h
    peak_kw_h = float(hourly.max())
    avg_kw = float(hourly.mean())

    # ── TVRDÁ VALIDAČNÁ BRÁNA ──────────────────────────────────────────
    # Nikdy nevyrob posudok na nulovej/nezmyselnej alebo príliš tenkej spotrebe.
    # Prepojené s engine.parse_consumption → ok:False = status:error = žiadny posudok.
    _MIN_ANNUAL_KWH = 500.0          # < 0,5 MWh/rok = zjavne rozbitý/nečitateľný vstup pre OM
    _MIN_MEASURED_PTS = 5760         # ~60 dní 15-min; menej + bez faktúry = nespoľahlivý prepočet roka
    _measured_pts = int((measured > 0).sum()) if len(measured) else 0
    _fail = []
    if annual_kwh <= _MIN_ANNUAL_KWH or peak_kw_15 <= 0 or avg_kw <= 0:
        _fail.append(f"ročná spotreba vyšla {round(annual_kwh)} kWh (peak {round(peak_kw_15)} kW) — vstup je nulový alebo nečitateľný")
    if strategy in ("measured", "extrapolated") and not invoice_annual and _measured_pts < _MIN_MEASURED_PTS:
        _fail.append(f"len {_measured_pts} nameraných 15-min intervalov a bez faktúry — nevieme spoľahlivo dopočítať celý rok")
    if _fail:
        return {"ok": False, "strategy": "invalid_input",
                "reason": "Chyba vstupu: " + "; ".join(_fail) + ". Skontroluj formát a obsah súboru (podporované: ZSD/SSE 15-min diagram, alebo faktúra s ročnou spotrebou).",
                "annual_kwh": round(annual_kwh), "coverage_pct": round(coverage * 100, 1),
                "per_file": per_file, "warnings": warnings}

    validation = _cross_check(annual_kwh, peak_kw_15, avg_kw, coverage, invoice_annual, mrk_kw, strategy)

    return {
        "ok": True,
        "strategy": strategy,
        "strategy_meta": strat_meta,
        "series_15min": final,
        "hourly": hourly,
        "annual_kwh": round(annual_kwh),
        "annual_mwh": round(annual_kwh / 1000.0, 2),
        "peak_kw_15min": round(peak_kw_15, 1),
        "peak_kw_hourly": round(peak_kw_h, 1),
        "avg_kw": round(avg_kw, 1),
        "coverage_pct": round(coverage * 100, 1),
        "per_file": per_file,
        "validation": validation,
        "warnings": warnings + validation["warnings"],
        "reasoning": _reasoning(strategy, strat_meta, annual_kwh, peak_kw_15, coverage, validation, per_file, invoice_annual, mrk_kw),
    }


def _reasoning(strategy, smeta, annual_kwh, peak_kw, coverage, validation, per_file, invoice_annual, mrk_kw):
    """Krátky grounded report: čo namerané / čo domyslené / istota. AI s deterministickým fallbackom."""
    facts = {
        "strategia": strategy, "strategia_detail": smeta,
        "rocna_mwh": round(annual_kwh/1000, 1), "peak_kw": round(peak_kw),
        "pokrytie_pct": round(coverage*100), "confidence": validation["confidence"],
        "kontroly": validation["checks"], "varovania": validation["warnings"],
        "subory": [p for p in per_file],
        "faktura_mwh": round(invoice_annual/1000) if invoice_annual else None, "mrk_kw": mrk_kw,
    }
    strat_txt = {"measured":"reálny nameraný profil","extrapolated":"nameraný tvar extrapolovaný na celý rok",
                 "synthesized":"syntetický profil z faktúry","manual":"ručné spracovanie"}.get(strategy, strategy)
    fallback = (f"Stratégia: {strat_txt}. Ročná spotreba {round(annual_kwh/1000,1)} MWh, peak {round(peak_kw)} kW, "
                f"pokrytie {round(coverage*100)} %. Istota {int(validation['confidence']*100)} %. "
                + (" ".join(validation["warnings"]) if validation["warnings"] else "Všetky kontroly OK."))
    try:
        from anthropic import Anthropic
        cl = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
        prompt = ("Si dátový analytik Energovision. Na základe FAKTOV napíš 2-4 vety po slovensky: akú stratégiu si zvolil, "
                  "čo je NAMERANÉ a čo DOMYSLENÉ, a istotu. Len fakty, žiadna fabrikácia, žiadne čísla navyše.\n\nFAKTY:\n"
                  + json.dumps(facts, ensure_ascii=False))
        msg = cl.messages.create(model=model, max_tokens=300, temperature=0.2,
                                 messages=[{"role":"user","content":prompt}])
        txt = "".join(b.text for b in msg.content if getattr(b,"type","")=="text").strip()
        return txt or fallback
    except Exception as e:
        log.warning(f"reasoning AI zlyhal: {e}")
        return fallback
