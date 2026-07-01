"""Inšpektor spotreby — vezme ľubovoľný 15-min/hodinový profil, spoľahlivo ho
prevedie na kanonickú 15-min MWh sériu, spraví diagnostiku (červené vlajky),
grafy (SVG) a export do SSE/ZSDIS CSV, ktorý AOM analyzátor vždy prečíta.

Human-in-the-loop: UI ukáže čísla + grafy, používateľ chybu (napr. zlú jednotku)
odhalí okom ešte pred spustením analýzy.
"""
from __future__ import annotations
import io, base64
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


# ─────────────────────────── kanonický CSV ───────────────────────────
def canonical_csv(series15_mwh: pd.Series) -> bytes:
    """SSE/ZSDIS formát: 5 riadkov hlavičky, potom DD.MM.YYYY HH:MM;MWh;; (desatinná čiarka)."""
    head = ["Odberné miesto;;;", "Konvertor spotreby Energovision;;;",
            "Jednotka: MWh za 15 min;;;", "Formát: SSE/ZSDIS;;;", "dátum a čas;hodnota;;"]
    lines = list(head)
    for ts, v in series15_mwh.items():
        lines.append(f"{ts.strftime('%d.%m.%Y %H:%M')};{('%.6f' % float(v)).replace('.', ',')};;")
    return ("\n".join(lines)).encode("utf-8-sig")


# ─────────────────────────── grafy (SVG, robustné) ───────────────────────────
def _ramp(t: float) -> str:
    t = max(0.0, min(1.0, t))
    a = (234, 242, 251); b = (30, 64, 175)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _svg_heatmap(s: pd.Series) -> str:
    kw = s * 4000.0
    d = pd.DataFrame({"kw": kw.values}, index=s.index)
    d["date"] = d.index.normalize(); d["hour"] = d.index.hour
    piv = d.pivot_table(index="hour", columns="date", values="kw", aggfunc="mean")
    piv = piv.reindex(index=range(24))
    dates = list(piv.columns); ndays = len(dates)
    if ndays == 0:
        return ""
    vmax = float(np.nanpercentile(piv.values, 98)) or 1.0
    cw = max(1.4, min(3.2, 720.0 / ndays)); rh = 6.0
    W = 46 + ndays * cw + 8; H = 22 + 24 * rh + 26
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}">',
         '<rect width="100%" height="100%" fill="white"/>']
    for hr in range(24):
        y = 22 + hr * rh
        if hr % 6 == 0:
            p.append(f'<text x="42" y="{y+5:.0f}" text-anchor="end" font-size="7" fill="#94a3b8">{hr:02d}:00</text>')
        for j, dtc in enumerate(dates):
            v = piv.iat[hr, j] if hr < piv.shape[0] else np.nan
            x = 46 + j * cw
            col = "#fde2e2" if (v != v) else _ramp(float(v) / vmax)
            p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cw+0.3:.1f}" height="{rh+0.3:.1f}" fill="{col}"/>')
    # mesačné popisy na osi X
    for j, dtc in enumerate(dates):
        if pd.Timestamp(dtc).day == 1:
            x = 46 + j * cw
            p.append(f'<text x="{x:.0f}" y="{H-8:.0f}" font-size="7" fill="#94a3b8">{pd.Timestamp(dtc).strftime("%b")}</text>')
    p.append(f'<text x="46" y="12" font-size="8" font-weight="700" fill="#334155">Heatmapa deň × hodina (kW) — biele/červené = chýbajúce dáta</text>')
    p.append("</svg>"); return "".join(p)


def _svg_monthly(s: pd.Series) -> str:
    m = (s.groupby(s.index.month).sum())
    months = ["", "Jan", "Feb", "Mar", "Apr", "Máj", "Jún", "Júl", "Aug", "Sep", "Okt", "Nov", "Dec"]
    W, H, PL, PB = 560, 170, 40, 26; vmax = float(m.max() or 1)
    bw = (W - PL - 10) / 12.0
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
         '<rect width="100%" height="100%" fill="white"/>',
         '<text x="8" y="12" font-size="8" font-weight="700" fill="#334155">Mesačná spotreba (MWh)</text>']
    for i in range(1, 13):
        v = float(m.get(i, 0.0)); h = (v / vmax) * (H - PB - 22)
        x = PL + (i - 1) * bw; y = H - PB - h
        p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw-4:.1f}" height="{h:.1f}" rx="2" fill="#3b82f6"/>')
        p.append(f'<text x="{x+bw/2-2:.1f}" y="{H-PB+10:.0f}" text-anchor="middle" font-size="7" fill="#94a3b8">{months[i]}</text>')
        if v > 0:
            p.append(f'<text x="{x+bw/2-2:.1f}" y="{y-2:.1f}" text-anchor="middle" font-size="6.5" fill="#475569">{v:.0f}</text>')
    p.append("</svg>"); return "".join(p)


def _svg_load_duration(s: pd.Series) -> str:
    kw = np.sort((s.values * 4000.0))[::-1]
    n = len(kw)
    if n == 0:
        return ""
    idx = np.linspace(0, n - 1, min(n, 240)).astype(int); ys = kw[idx]
    W, H, PL, PB = 560, 170, 46, 24; vmax = float(ys.max() or 1)
    def X(i): return PL + i / (len(ys) - 1) * (W - PL - 10)
    def Y(v): return 20 + (1 - v / vmax) * (H - PB - 20)
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(ys))
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
         '<rect width="100%" height="100%" fill="white"/>',
         '<text x="8" y="12" font-size="8" font-weight="700" fill="#334155">Load duration curve (kW zoradené)</text>',
         f'<polyline points="{pts}" fill="none" stroke="#0ea5e9" stroke-width="2"/>',
         f'<text x="{PL-4}" y="24" text-anchor="end" font-size="7" fill="#94a3b8">{vmax:.0f}</text>',
         f'<text x="{PL-4}" y="{H-PB:.0f}" text-anchor="end" font-size="7" fill="#94a3b8">0</text>']
    p.append("</svg>"); return "".join(p)


def _svg_typical(s: pd.Series) -> str:
    kw = s * 4000.0; d = pd.DataFrame({"kw": kw.values}, index=s.index)
    d["hour"] = d.index.hour; d["wd"] = d.index.weekday
    wk = d[d.wd < 5].groupby("hour")["kw"].mean(); we = d[d.wd >= 5].groupby("hour")["kw"].mean()
    W, H, PL, PB = 560, 170, 46, 24
    vmax = float(max(wk.max() if len(wk) else 0, we.max() if len(we) else 0) or 1)
    def X(h): return PL + h / 23.0 * (W - PL - 10)
    def Y(v): return 20 + (1 - v / vmax) * (H - PB - 20)
    def line(sr, col):
        pts = " ".join(f"{X(h):.1f},{Y(float(sr.get(h,0))):.1f}" for h in range(24))
        return f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2"/>'
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
         '<rect width="100%" height="100%" fill="white"/>',
         '<text x="8" y="12" font-size="8" font-weight="700" fill="#334155">Typický deň (kW): pracovný vs víkend</text>',
         line(wk, "#3b82f6"), line(we, "#f59e0b"),
         '<text x="470" y="12" font-size="7" fill="#3b82f6">■ pracovný</text>',
         '<text x="470" y="21" font-size="7" fill="#f59e0b">■ víkend</text>']
    for h in (0, 6, 12, 18, 23):
        p.append(f'<text x="{X(h):.0f}" y="{H-8:.0f}" text-anchor="middle" font-size="7" fill="#94a3b8">{h:02d}</text>')
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
        return {"ok": False, "error": "Žiadny súbor sa nepodarilo prečítať. Podporované: ZSD/SSE 15-min diagram, alebo tabuľka so stĺpcom času a hodnoty.",
                "per_file": per_file}

    combined = pd.concat(series_list).sort_index()
    n_dupes = int(combined.index.duplicated().sum())
    final = combined[~combined.index.duplicated(keep="first")].sort_index()

    flags, verdict, stats = _quality(final, mrk_kw, invoice_annual_kwh, n_dupes, generic_units)

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
