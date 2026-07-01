"""Inšpektor spotreby — vezme ľubovoľný 15-min/hodinový profil, spoľahlivo ho
prevedie na kanonickú 15-min MWh sériu, spraví diagnostiku (červené vlajky),
grafy (SVG) a export do SSE/ZSDIS CSV, ktorý AOM analyzátor vždy prečíta.

Human-in-the-loop: UI ukáže čísla + grafy, používateľ chybu (napr. zlú jednotku)
odhalí okom ešte pred spustením analýzy.
"""
from __future__ import annotations
import io, base64, warnings
import pandas as pd
import numpy as np
from ingestion import normalizer as _norm

FULL_YEAR_15MIN = 35040


# ─────────────────────────── generický parser ───────────────────────────
def _to_num(sr: pd.Series) -> pd.Series:
    """Text → číslo, znesie desatinnú čiarku aj medzery/tisícové oddeľovače."""
    s = sr.astype(str).str.strip().str.replace(" ", "", regex=False).str.replace(" ", "", regex=False)
    # ak je čiarka aj bodka → bodka je tisíc, čiarka desatinná
    both = s.str.contains(",", na=False) & s.str.contains(r"\.", na=False)
    s = s.mask(both, s.str.replace(".", "", regex=False))
    s = s.str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


def _find_datetime_col(df: pd.DataFrame):
    best, best_ok = None, 0.0
    warnings.simplefilter("ignore")
    for c in df.columns:
        col = df[c].astype(str).str.strip()
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%d.%m.%Y", None):
            dt = pd.to_datetime(col, format=fmt, errors="coerce", dayfirst=True) if fmt \
                 else pd.to_datetime(col, errors="coerce", dayfirst=True)
            ok = dt.notna().mean()
            if ok > best_ok:
                best_ok, best, best_dt = ok, c, dt
    return (best, best_dt) if best_ok >= 0.5 else (None, None)


def _guess_unit(values: pd.Series, gran: int, header_text: str) -> tuple[str, str]:
    """Vráti (unit, confidence). unit ∈ {kw, kwh, mwh} = hodnota za interval."""
    h = (header_text or "").lower()
    if "mwh" in h:
        return "mwh", "hlavička"
    if "kwh" in h:
        return "kwh", "hlavička"
    if "kw" in h or "výkon" in h or "vykon" in h:
        return "kw", "hlavička"
    med = float(values.dropna().abs().median() or 0)
    # peak/annual pre 3 interpretácie; vyber tú s vierohodným peakom (1..100 MW)
    for unit, factor_kw in (("kwh", 60.0 / gran), ("kw", 1.0), ("mwh", 60.0 / gran * 1000.0)):
        peak_kw = float(values.abs().max() or 0) * factor_kw
        if 1.0 <= peak_kw <= 100000.0 and med > 0:
            return unit, "odhad z veľkosti"
    return "kwh", "predvolené"


def _generic_parse(raw: bytes, filename: str, unit_override: str | None):
    """Ľubovoľný tabuľkový súbor → (kw_series, meta). None ak sa nedá."""
    df = None
    for hdr in (0, None):
        try:
            d = _norm._read_table(raw, filename, header=hdr)
            if d is not None and d.shape[1] >= 2 and len(d) >= 10:
                df = d
                break
        except Exception:
            continue
    if df is None:
        return None
    df.columns = [str(c) for c in df.columns]
    ts_col, dt = _find_datetime_col(df)
    if ts_col is None:
        return None
    # value col = prvý iný stĺpec s najviac číslami
    best_col, best_ok = None, 0.0
    for c in df.columns:
        if c == ts_col:
            continue
        ok = _to_num(df[c]).notna().mean()
        if ok > best_ok:
            best_ok, best_col = ok, c
    if best_col is None or best_ok < 0.5:
        return None
    vals = _to_num(df[best_col])
    s = pd.Series(vals.values, index=dt).dropna()
    s = s[~s.index.duplicated(keep="first")].sort_index()
    if len(s) < 10:
        return None
    gran = _norm._infer_gran(s.index)
    unit = (unit_override or "").lower() if unit_override else None
    conf = "ručne zvolené"
    if unit not in ("kw", "kwh", "mwh"):
        unit, conf = _guess_unit(s, gran, f"{ts_col} {best_col}")
    factor = {"kw": 1.0, "kwh": 60.0 / gran, "mwh": 60.0 / gran * 1000.0}[unit]
    kw = s * factor
    kw.attrs["granularity_min"] = gran
    return kw, {"ts_col": ts_col, "value_col": best_col, "unit": unit,
                "unit_confidence": conf, "granularity_min": gran, "source": "generic_ts_value"}


def _remap_to_calendar_year(s: pd.Series, year: int) -> pd.Series:
    """Presunie profil na kalendárny rok (1.1.–31.12. cieľového roka) podľa deň/hodina/min.
    Aug2023–Aug2024 → čistý rok 2025. Feb 29 v nepriestupnom cieľovom roku sa vynechá."""
    new_ts, keep = [], []
    for ts, v in zip(s.index, s.values):
        try:
            new_ts.append(pd.Timestamp(year=year, month=ts.month, day=ts.day, hour=ts.hour, minute=ts.minute))
            keep.append(v)
        except ValueError:
            continue  # napr. 29.2. v nepriestupnom roku
    out = pd.Series(keep, index=pd.DatetimeIndex(new_ts))
    return out[~out.index.duplicated(keep="first")].sort_index()


# ─────────────────────────── kanonický CSV ───────────────────────────
def canonical_csv(series15_mwh: pd.Series) -> bytes:
    """SSE/ZSDIS formát: 5 riadkov hlavičky, potom DD.MM.YYYY HH:MM;MWh;; (desatinná čiarka)."""
    head = ["Odberné miesto;;;", "Konvertor spotreby Energovision;;;",
            "Jednotka: MWh za 15 min;;;", "Formát: SSE/ZSDIS;;;", "dátum a čas;hodnota;;"]
    lines = list(head)
    for ts, v in series15_mwh.items():
        lines.append(f"{ts.strftime('%d.%m.%Y %H:%M')};{('%.6f' % float(v)).replace('.', ',')};;")
    return ("\n".join(lines)).encode("utf-8-sig")


# ─────────────────────────── grafy (SVG) ───────────────────────────
_FONT = 'font-family="-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"'


def _lerp(a, b, t): return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


def _ramp(t: float) -> str:
    t = max(0.0, min(1.0, t))
    s0, s1, s2 = (239, 246, 255), (96, 165, 250), (30, 58, 138)
    c = _lerp(s0, s1, t / 0.5) if t <= 0.5 else _lerp(s1, s2, (t - 0.5) / 0.5)
    return "#%02x%02x%02x" % c


def _frame(W, H, title):
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}" {_FONT}>',
            f'<rect x="0.5" y="0.5" width="{W-1:.0f}" height="{H-1:.0f}" rx="10" fill="#ffffff" stroke="#e9eef5"/>',
            f'<text x="16" y="21" font-size="10.5" font-weight="700" fill="#0f172a">{title}</text>']


def _svg_heatmap(s: pd.Series) -> str:
    kw = s * 4000.0
    d = pd.DataFrame({"kw": kw.values}, index=s.index)
    d["date"] = d.index.normalize(); d["hour"] = d.index.hour
    piv = d.pivot_table(index="hour", columns="date", values="kw", aggfunc="mean").reindex(index=range(24))
    dates = list(piv.columns); ndays = len(dates)
    if ndays == 0:
        return ""
    vmax = float(np.nanpercentile(piv.values, 98)) or 1.0
    cw = max(1.6, min(3.4, 900.0 / ndays)); rh = 7.0; ox, oy = 48, 36
    W = ox + ndays * cw + 16; H = oy + 24 * rh + 30
    p = _frame(W, H, "Heatmapa deň × hodina — intenzita odberu (kW)")
    for hr in range(24):
        y = oy + hr * rh
        if hr % 3 == 0:
            p.append(f'<text x="{ox-6}" y="{y+5:.0f}" text-anchor="end" font-size="7" fill="#94a3b8">{hr:02d}h</text>')
        for j in range(ndays):
            v = piv.iat[hr, j]
            col = "#eef2f7" if (v != v) else _ramp(float(v) / vmax)
            p.append(f'<rect x="{ox+j*cw:.1f}" y="{y:.1f}" width="{cw+0.4:.1f}" height="{rh+0.4:.1f}" fill="{col}"/>')
    for j, dtc in enumerate(dates):
        if pd.Timestamp(dtc).day == 1:
            p.append(f'<text x="{ox+j*cw:.0f}" y="{H-10:.0f}" font-size="7.5" fill="#64748b">{pd.Timestamp(dtc).strftime("%b")}</text>')
    lgx = W - 150
    p.append(f'<defs><linearGradient id="hg" x1="0" x2="1"><stop offset="0" stop-color="{_ramp(0)}"/><stop offset="0.5" stop-color="{_ramp(0.5)}"/><stop offset="1" stop-color="{_ramp(1)}"/></linearGradient></defs>')
    p.append(f'<rect x="{lgx}" y="14" width="96" height="7" rx="2" fill="url(#hg)"/>')
    p.append(f'<text x="{lgx-4}" y="21" text-anchor="end" font-size="6.5" fill="#94a3b8">0</text>')
    p.append(f'<text x="{lgx+100}" y="21" font-size="6.5" fill="#94a3b8">{vmax:.0f} kW</text>')
    p.append("</svg>"); return "".join(p)


def _yaxis(p, ox, oy, W, plotH, vmax):
    for g in (0.25, 0.5, 0.75, 1.0):
        y = oy + plotH * (1 - g)
        p.append(f'<line x1="{ox}" y1="{y:.1f}" x2="{W-10}" y2="{y:.1f}" stroke="#eef2f7"/>')
        p.append(f'<text x="{ox-5}" y="{y+3:.1f}" text-anchor="end" font-size="6.5" fill="#cbd5e1">{vmax*g:.0f}</text>')


def _svg_monthly(s: pd.Series) -> str:
    m = s.groupby(s.index.month).sum()
    months = ["", "Jan", "Feb", "Mar", "Apr", "Máj", "Jún", "Júl", "Aug", "Sep", "Okt", "Nov", "Dec"]
    W, H, ox, oy, bottom = 540, 190, 40, 42, 30; vmax = float(m.max() or 1)
    bw = (W - ox - 16) / 12.0; plotH = H - oy - bottom
    p = _frame(W, H, "Mesačná spotreba (MWh)"); _yaxis(p, ox, oy, W, plotH, vmax)
    for i in range(1, 13):
        v = float(m.get(i, 0.0)); h = (v / vmax) * plotH; x = ox + (i - 1) * bw; y = oy + plotH - h
        p.append(f'<rect x="{x+2.5:.1f}" y="{y:.1f}" width="{bw-6:.1f}" height="{max(h,0.5):.1f}" rx="3" fill="#92D050"/>')
        p.append(f'<text x="{x+bw/2:.1f}" y="{H-bottom+16:.0f}" text-anchor="middle" font-size="7.5" fill="#64748b">{months[i]}</text>')
        if v > 0:
            p.append(f'<text x="{x+bw/2:.1f}" y="{y-3:.1f}" text-anchor="middle" font-size="7" fill="#475569">{v:.0f}</text>')
    p.append("</svg>"); return "".join(p)


def _svg_load_duration(s: pd.Series) -> str:
    kw = np.sort(s.values * 4000.0)[::-1]; n = len(kw)
    if n == 0:
        return ""
    idx = np.linspace(0, n - 1, min(n, 260)).astype(int); ys = kw[idx]
    W, H, ox, oy, bottom = 540, 190, 44, 42, 28; vmax = float(ys.max() or 1); plotH = H - oy - bottom
    def X(k): return ox + k / (len(ys) - 1) * (W - ox - 14)
    def Y(v): return oy + (1 - v / vmax) * plotH
    p = _frame(W, H, "Krivka trvania záťaže (kW zoradené od špičky)"); _yaxis(p, ox, oy, W, plotH, vmax)
    line = " ".join(f"{X(k):.1f},{Y(v):.1f}" for k, v in enumerate(ys))
    p.append(f'<polygon points="{ox},{oy+plotH:.1f} {line} {W-14},{oy+plotH:.1f}" fill="#0ea5e91f"/>')
    p.append(f'<polyline points="{line}" fill="none" stroke="#0ea5e9" stroke-width="2"/>')
    for frac, lab in ((0, "0 %"), (0.5, "50 %"), (1.0, "100 %")):
        x = ox + frac * (W - ox - 14)
        p.append(f'<text x="{x:.0f}" y="{H-bottom+15:.0f}" text-anchor="middle" font-size="7" fill="#94a3b8">{lab} času</text>')
    p.append("</svg>"); return "".join(p)


def _svg_typical(s: pd.Series) -> str:
    kw = s * 4000.0; d = pd.DataFrame({"kw": kw.values}, index=s.index)
    d["hour"] = d.index.hour; d["wd"] = d.index.weekday
    wk = d[d.wd < 5].groupby("hour")["kw"].mean(); we = d[d.wd >= 5].groupby("hour")["kw"].mean()
    W, H, ox, oy, bottom = 540, 190, 40, 42, 28
    vmax = float(max(wk.max() if len(wk) else 0, we.max() if len(we) else 0) or 1); plotH = H - oy - bottom
    def X(h): return ox + h / 23.0 * (W - ox - 14)
    def Y(v): return oy + (1 - v / vmax) * plotH
    p = _frame(W, H, "Typický deň — priemerný odber (kW)"); _yaxis(p, ox, oy, W, plotH, vmax)
    def pts_of(sr): return " ".join(f"{X(h):.1f},{Y(float(sr.get(h,0))):.1f}" for h in range(24))
    def area(pts, fill): return f'<polygon points="{ox},{oy+plotH:.1f} {pts} {X(23):.1f},{oy+plotH:.1f}" fill="{fill}"/>'
    def poly(pts, col): return f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2.4"/>'
    pw, pe = pts_of(wk), pts_of(we)
    # najprv obe plochy (svetlé), potom obe čiary navrch → obe vždy viditeľné
    p.append(area(pe, "#f59e0b18")); p.append(area(pw, "#3b82f618"))
    p.append(poly(pe, "#f59e0b")); p.append(poly(pw, "#3b82f6"))
    p.append(f'<rect x="{W-152}" y="13" width="8" height="8" rx="2" fill="#3b82f6"/><text x="{W-140}" y="20" font-size="7.5" fill="#475569">pracovný</text>')
    p.append(f'<rect x="{W-82}" y="13" width="8" height="8" rx="2" fill="#f59e0b"/><text x="{W-70}" y="20" font-size="7.5" fill="#475569">víkend</text>')
    for h in (0, 6, 12, 18, 23):
        p.append(f'<text x="{X(h):.0f}" y="{H-bottom+15:.0f}" text-anchor="middle" font-size="7" fill="#94a3b8">{h:02d}:00</text>')
    p.append("</svg>"); return "".join(p)


# ─────────────────────────── diagnostika ───────────────────────────
def _quality(final: pd.Series, mrk_kw, invoice_kwh, n_dupes, generic_units):
    n = len(final); flags = []
    sum_mwh = float(final.sum()); peak_kw = float(final.max()) * 4000.0
    months = sorted(set(int(m) for m in pd.Series(final.index).dt.month.unique()))
    span_days = (final.index.max().normalize() - final.index.min().normalize()).days + 1
    coverage = n / FULL_YEAR_15MIN * 100.0
    gaps = max(0, span_days * 96 - n)
    zeros = int((final == 0).sum()); neg = int((final < 0).sum())
    med = float(final.median() or 0)
    spikes = int((final > med * 8).sum()) if med > 0 else 0
    full_year = len(months) >= 12

    def add(level, text): flags.append({"level": level, "text": text})
    if sum_mwh <= 0.5:
        add("error", "Spotreba vyšla ~0 MWh — analyzátor by to odmietol. Skontroluj jednotku alebo stĺpec s hodnotou.")
    if not full_year:
        add("warn", f"Nie je celý rok — prítomných {len(months)}/12 mesiacov, pokrytie {coverage:.0f} %.")
    if gaps > 96:
        add("warn", f"Chýbajúce intervaly v rámci obdobia: {gaps} (~{gaps // 96} dní).")
    if n and zeros > n * 0.05:
        add("warn", f"Nulové intervaly: {zeros} ({zeros / n * 100:.0f} %).")
    if neg > 0:
        add("warn", f"Záporné hodnoty: {neg} — spotreba by nemala byť záporná.")
    if n_dupes > 0:
        add("info", f"Duplicitné časy (DST/opakované): {n_dupes} — zlúčené na unikátne.")
    if spikes > 0:
        add("info", f"Možné extrémne špičky: {spikes} (nad 8× medián) — over, či nie sú chyba merania.")
    if mrk_kw:
        if peak_kw > mrk_kw * 1.05:
            add("warn", f"Peak {peak_kw:.0f} kW presahuje MRK {mrk_kw:.0f} kW.")
        elif peak_kw < mrk_kw * 0.30:
            add("warn", f"Peak {peak_kw:.0f} kW je výrazne pod MRK {mrk_kw:.0f} kW — možno zlá jednotka?")
    if invoice_kwh and full_year:
        dev = sum_mwh * 1000.0 / invoice_kwh - 1
        if abs(dev) > 0.15:
            add("warn", f"Ročná {sum_mwh:.0f} MWh vs faktúra {invoice_kwh / 1000:.0f} MWh ({dev * 100:+.0f} %).")
    if any(u not in ("hlavička", "ručne zvolené") for u in generic_units):
        add("info", "Jednotka bola odhadnutá — over čísla v náhľade (kW/kWh/MWh vieš prepnúť).")
    if not any(f["level"] in ("error", "warn") for f in flags):
        add("ok", "Dáta vyzerajú v poriadku — pripravené pre Analýzu OM.")
    verdict = "error" if any(f["level"] == "error" for f in flags) else \
              ("warn" if any(f["level"] == "warn" for f in flags) else "ok")
    return flags, verdict, {
        "annual_mwh": round(sum_mwh, 1), "peak_kw": round(peak_kw, 0),
        "avg_kw": round(float(final.mean()) * 4000.0, 0), "coverage_pct": round(coverage, 1),
        "n_intervals": n, "months_present": len(months), "full_year": full_year,
        "gaps": gaps, "zeros": zeros, "negatives": neg,
        "period_from": final.index.min().strftime("%d.%m.%Y"),
        "period_to": final.index.max().strftime("%d.%m.%Y"),
    }


# ─────────────────────────── hlavná funkcia ───────────────────────────
def inspect(files: list[dict], unit_override: str | None = None,
            invoice_annual_kwh: float | None = None, mrk_kw: float | None = None,
            year: int = 2025) -> dict:
    """files: [{filename, bytes}]. Vráti stats + flags + charts(SVG) + kanonický CSV(base64)."""
    series_list, per_file, generic_units, spec_cache = [], [], [], {}
    for f in files:
        fn = f.get("filename", "súbor"); raw = f["bytes"]; got = False
        try:
            r = _norm.normalize_file(raw, fn, {}, spec_cache)
            if r.get("ok"):
                series_list.append(r["series"])
                per_file.append({"filename": fn, "source": r.get("source"), "mwh": r.get("mwh"),
                                 "granularity_min": r.get("granularity_min")})
                got = True
        except Exception:
            pass
        if not got:
            try:
                g = _generic_parse(raw, fn, unit_override)
            except Exception as e:
                g = None
                per_file.append({"filename": fn, "error": f"generický parser zlyhal: {str(e)[:120]}"})
            if g:
                kw, meta = g
                series_list.append(_norm._kw_to_15min_mwh(kw))
                generic_units.append(meta.get("unit_confidence", ""))
                per_file.append({"filename": fn, "source": meta["source"], "unit": meta["unit"],
                                 "unit_confidence": meta["unit_confidence"],
                                 "value_col": meta["value_col"], "ts_col": meta["ts_col"],
                                 "granularity_min": meta["granularity_min"]})
            elif not any(p.get("filename") == fn for p in per_file):
                per_file.append({"filename": fn, "error": "neznámy formát — nenašiel sa časový a hodnotový stĺpec"})

    if not series_list:
        return {"ok": False,
                "error": ("Nenašiel sa 15-min/hodinový profil — teda stĺpec s dátumom+časom a k nemu hodnota. "
                          "Podporované: ZSD/SSE intervalový diagram (Odberový diagram), alebo tabuľka typu „čas ; hodnota“. "
                          "Ak máš len mesačný výkaz alebo faktúru (mesačné/ročné súčty), 15-min profil sa z nich spätne získať nedá — "
                          "buď si vyžiadaj od distribútora intervalový diagram, alebo v Analýze OM zadaj ročnú spotrebu z faktúry a profil sa nasyntetizuje."),
                "per_file": per_file}

    combined = pd.concat(series_list).sort_index()
    n_dupes = int(combined.index.duplicated().sum())
    final = combined[~combined.index.duplicated(keep="first")].sort_index()

    # Premapuj na kalendárny rok (1.1.–31.12. cieľového roka) — presne to, čo AOM analyzátor očakáva.
    src_from = final.index.min().strftime("%d.%m.%Y")
    src_to = final.index.max().strftime("%d.%m.%Y")
    remapped = (final.index.min().year != year) or (final.index.max().year != year)
    final = _remap_to_calendar_year(final, year)

    flags, verdict, stats = _quality(final, mrk_kw, invoice_annual_kwh, n_dupes, generic_units)
    if remapped:
        flags.insert(0, {"level": "info", "text": f"Výstup premapovaný na kalendárny rok {year} (1.1.–31.12.). Zdroj: {src_from} – {src_to}."})
    stats["source_period"] = f"{src_from} – {src_to}"

    charts = {}
    for name, fnc in (("heatmap", _svg_heatmap), ("monthly", _svg_monthly),
                      ("load_duration", _svg_load_duration), ("typical", _svg_typical)):
        try:
            charts[name] = fnc(final)
        except Exception:
            charts[name] = ""

    csv_bytes = canonical_csv(final)
    return {"ok": True, "verdict": verdict, "stats": stats, "flags": flags,
            "charts": charts, "per_file": per_file,
            "csv_base64": base64.b64encode(csv_bytes).decode("ascii"),
            "csv_filename": "spotreba_15min_ocistene.csv",
            "detected_unit": (per_file[-1].get("unit") if per_file else None)}
