"""AI-first normalizátor profilov spotreby → jedna kanonická forma.

Princíp: AI z malej vzorky určí normalizačný PREDPIS (jednotka kW/kWh/MW/MWh,
granularita, stĺpce, dátum, oddeľovač). Kód ho deterministicky aplikuje na všetky
riadky. Fast-path = existujúce SK parsery (lacné, 0 € AI). Výstup je VŽDY
pd.Series MWh per 15-min interval (kompatibilné s analyza_om.engine pipeline).
"""
from __future__ import annotations
import os, io, json, logging, tempfile
from pathlib import Path
import pandas as pd
import numpy as np

log = logging.getLogger("normalizer")

# ---- AI klient (lazy) ----
_client = None
def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")


# ============================================================
# Sniff + vzorka
# ============================================================
def _sniff(raw: bytes) -> str:
    head = raw[:16]
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "xls"          # BIFF (staré .xls)
    if head[:2] == b"PK":
        return "xlsx"         # ZIP (xlsx)
    low = raw[:256].lstrip().lower()
    if low[:5] in (b"<html", b"<!doc", b"<?xml", b"<tabl") or b"<table" in raw[:2048].lower():
        return "html"
    return "csv"


def _read_table(raw: bytes, filename: str, header=None, nrows=None, skiprows=0,
                sep=None, encoding=None) -> pd.DataFrame:
    """Tolerantné načítanie xls/xlsx/csv/html do DataFrame (header=None default)."""
    kind = _sniff(raw)
    suffix = Path(filename).suffix.lower()
    if kind in ("xls", "xlsx") or suffix in (".xls", ".xlsx"):
        engine = "xlrd" if (kind == "xls" or suffix == ".xls") else "openpyxl"
        try:
            return pd.read_excel(io.BytesIO(raw), sheet_name=0, header=header,
                                 nrows=nrows, skiprows=skiprows, engine=engine)
        except Exception:
            other = "openpyxl" if engine == "xlrd" else "xlrd"
            return pd.read_excel(io.BytesIO(raw), sheet_name=0, header=header,
                                 nrows=nrows, skiprows=skiprows, engine=other)
    if kind == "html":
        tbls = pd.read_html(io.BytesIO(raw))
        if not tbls:
            raise ValueError("HTML bez tabuľky")
        return tbls[0]
    # CSV
    encs = [encoding] if encoding else ["utf-8", "cp1250", "iso-8859-2"]
    seps = [sep] if sep else [";", ",", "\t"]
    last = None
    for enc in encs:
        for s in seps:
            try:
                df = pd.read_csv(io.BytesIO(raw), sep=s, encoding=enc, header=header,
                                 nrows=nrows, skiprows=skiprows, dtype=str)
                if df.shape[1] >= 2:
                    return df
            except Exception as e:
                last = e
    raise ValueError(f"CSV sa nedalo načítať: {last}")


def _sample_text(raw: bytes, filename: str, max_rows: int = 35) -> str:
    df = _read_table(raw, filename, header=None, nrows=max_rows)
    return df.to_csv(index=False, header=False)


def _fingerprint(raw: bytes, filename: str) -> str:
    """Podpis hlavičky (prvé 3 riadky) — na re-use AI predpisu pre rovnaké súbory."""
    try:
        df = _read_table(raw, filename, header=None, nrows=3)
        import hashlib
        sig = "|".join("~".join(str(x)[:24] for x in row) for row in df.values.tolist())
        return hashlib.md5(sig.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        return ""


# ============================================================
# AI detekcia predpisu
# ============================================================
_SPEC_PROMPT = """Si parser meraných dát o spotrebe elektriny od slovenských distribútorov (VSD, ZSDIS, SSD, SPP).
Dostal si VZORKU súboru (prvé riadky). Vráť IBA JSON predpis ako ho prečítať. Žiadny text navyše.

Schéma JSON:
{{
  "header_row": <číslo riadku s hlavičkou, 0-indexované, alebo null ak bez hlavičky>,
  "skip_rows": <koľko riadkov preskočiť pred dátami>,
  "timestamp_col": <názov ALEBO 0-index stĺpca s časom; ak sú dátum a čas zvlášť, daj [datum_col, cas_col]>,
  "value_cols": [<názvy alebo 0-indexy stĺpcov s nameranou hodnotou činného ODBERU; viac = súčet registrov>],
  "value_unit": "kW" | "kWh" | "MW" | "MWh",
  "granularity_min": 15 | 30 | 60,
  "date_format": <strftime napr "%d.%m.%Y %H:%M", alebo "iso", alebo "excel_serial", alebo "auto">,
  "decimal_sep": "," | ".",
  "value_multiplier": <násobič napr CT/PT prevod, default 1.0>,
  "confidence": <0.0-1.0>,
  "notes": "<krátko prečo>"
}}

Pravidlá:
- Zaujíma nás ČINNÝ ODBER (P+, 1.5.0, 1.8.0, "Činný odber", "odber"). NIE dodávka (P-, 2.5.0), NIE jalová (kvar), NIE kumulatív ak je aj 15-min.
- 15-min meranie býva priemerný výkon v kW → value_unit "kW".
- Excel serial = číslo ako 45658.5 (dni od 1900) → date_format "excel_serial".
- Ak je hodnota energia za interval, daj kWh/MWh; ak priemerný výkon, daj kW/MW.

VZORKA súboru "{filename}":
```
{sample}
```
Vráť IBA JSON."""


def _ai_detect_spec(raw: bytes, filename: str, context: dict | None = None) -> dict:
    sample = _sample_text(raw, filename)
    prompt = _SPEC_PROMPT.format(filename=filename, sample=sample[:6000])
    msg = _get_client().messages.create(
        model=_MODEL, max_tokens=600, temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1].lstrip("json").strip()
    spec = json.loads(txt)
    spec["_source"] = "ai"
    return spec


# ============================================================
# Aplikácia predpisu → kW power series
# ============================================================
def _excel_serial_to_dt(s):
    return pd.to_datetime(pd.to_numeric(s, errors="coerce"), unit="D", origin="1899-12-30")


def _col(df: pd.DataFrame, ref):
    """Vyber stĺpec podľa názvu alebo 0-indexu."""
    if isinstance(ref, int):
        return df.iloc[:, ref]
    if ref in df.columns:
        return df[ref]
    # fuzzy: case-insensitive contains
    for c in df.columns:
        if str(ref).strip().lower() in str(c).strip().lower():
            return df[c]
    raise KeyError(f"stĺpec '{ref}' nenájdený")


def _apply_spec_to_kw(raw: bytes, filename: str, spec: dict) -> pd.Series:
    hdr = spec.get("header_row")
    skip = int(spec.get("skip_rows") or 0)
    df = _read_table(raw, filename, header=hdr, skiprows=skip)
    df = df.dropna(how="all")

    # timestamp
    tcol = spec.get("timestamp_col")
    dfmt = (spec.get("date_format") or "auto").lower()
    if isinstance(tcol, (list, tuple)) and len(tcol) == 2:
        d = _col(df, tcol[0]).astype(str).str.strip()
        t = _col(df, tcol[1]).astype(str).str.strip()
        ts = pd.to_datetime(d + " " + t, dayfirst=True, errors="coerce")
    else:
        raw_ts = _col(df, tcol)
        if dfmt == "excel_serial":
            ts = _excel_serial_to_dt(raw_ts)
        elif dfmt in ("auto", "iso"):
            ts = pd.to_datetime(raw_ts, dayfirst=(dfmt != "iso"), errors="coerce")
        else:
            ts = pd.to_datetime(raw_ts.astype(str).str.strip(), format=spec["date_format"], errors="coerce")

    # value (súčet registrov)
    dec = spec.get("decimal_sep") or "."
    vcols = spec.get("value_cols") or []
    if not isinstance(vcols, (list, tuple)):
        vcols = [vcols]
    val = None
    for vc in vcols:
        col = _col(df, vc).astype(str).str.replace(" ", "", regex=False)
        if dec == ",":
            col = col.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
        num = pd.to_numeric(col, errors="coerce")
        val = num if val is None else val.add(num, fill_value=0)
    val = val * float(spec.get("value_multiplier") or 1.0)

    s = pd.Series(val.values, index=ts).dropna()
    s = s[~s.index.duplicated(keep="first")].sort_index()
    if len(s) == 0:
        raise ValueError("predpis nevrátil žiadne platné riadky")

    gran = int(spec.get("granularity_min") or _infer_gran(s.index))
    unit = (spec.get("value_unit") or "kW").lower()
    # → priemerný výkon kW za interval
    if unit in ("kw", "mw"):
        kw = s * (1000.0 if unit == "mw" else 1.0)
    else:  # energia za interval
        energy_kwh = s * (1000.0 if unit == "mwh" else 1.0)
        kw = energy_kwh * (60.0 / gran)
    kw.attrs["granularity_min"] = gran
    return kw


def _infer_gran(idx) -> int:
    try:
        dt = pd.Series(idx).diff().dropna().dt.total_seconds().median()
        if dt <= 0:
            return 15
        return int(round(dt / 60.0))
    except Exception:
        return 15


# ============================================================
# kW → kanonická 15-min MWh série
# ============================================================
def _kw_to_15min_mwh(kw: pd.Series) -> pd.Series:
    """Regrid power (kW) na pravidelnú 15-min mriežku → MWh per 15-min interval.
    Upsampling (hodinové→15min) drží energiu (ffill konšt. výkon). Downsampling = priemer."""
    kw = kw.sort_index()
    gran = _infer_gran(kw.index)
    grid = pd.date_range(kw.index.min().floor("15min"), kw.index.max().ceil("15min"), freq="15min")
    if gran > 15:
        p15 = kw.reindex(kw.index.union(grid)).sort_index().ffill().reindex(grid)
    elif gran < 15:
        p15 = kw.resample("15min").mean()
    else:
        p15 = kw.reindex(grid)
        if p15.isna().mean() > 0.5:          # zlé zarovnanie → tolerantne
            p15 = kw.resample("15min").mean()
    mwh15 = (p15 * 0.25 / 1000.0)
    mwh15.name = "kWh"
    return mwh15.dropna()


# ============================================================
# Fast-path (existujúce SK parsery)
# ============================================================
# Holý zoznam hodnôt bez časových pečiatok: počet hodnôt -> (granularita min, prestupný rok)
_BARE_GRID = {35040: (15, False), 35136: (15, True),
              17520: (30, False), 17568: (30, True),
              8760: (60, False), 8784: (60, True)}


def _bare_numeric_kw(raw: bytes):
    """.txt/.csv s holým zoznamom číselných hodnôt bez timestampov (Lukáš 2026-06-11):
    35040 hodnôt = 15-min ročný profil ČINNÉHO ODBERU v kW, zoradený od 1.1. 00:00
    do 31.12. 23:45. Rok = posledný ukončený rok so zhodnou dĺžkou (prestupný/nie).
    Podporené aj 30-min/hodinové rady a prestupné roky. Vráti kW series alebo None."""
    import re
    from datetime import datetime
    try:
        txt = raw.decode("utf-8", "replace")
    except Exception:
        return None
    if "<" in txt[:200]:  # HTML/XML maskované ako text
        return None
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    if len(lines) == 1:
        toks = re.split(r"[;,\t ]+", lines[0])
    else:
        # viacriadkový: vezmi prvý stĺpec (oddelený ; alebo tab), toleruj medzery v číslach
        toks = [re.split(r"[;\t]", l)[0].strip().replace("\xa0", "").replace(" ", "") for l in lines]
    vals, bad = [], 0
    for t in toks:
        if t and re.fullmatch(r"-?\d+(?:[.,]\d+)?", t):
            vals.append(float(t.replace(",", ".")))
        else:
            bad += 1
    if len(vals) < 1000 or bad > max(3, 0.05 * len(toks)):
        return None
    n = len(vals)
    if n not in _BARE_GRID:
        return None
    gran, leap = _BARE_GRID[n]
    y = datetime.now().year - 1
    _is_leap = lambda yy: yy % 4 == 0 and (yy % 100 != 0 or yy % 400 == 0)
    while _is_leap(y) != leap:
        y -= 1
    idx = pd.date_range(f"{y}-01-01 00:00", periods=n, freq=f"{gran}min")
    kw = pd.Series(vals, index=idx)
    kw.attrs["granularity_min"] = gran
    return kw


def _fastpath_kw(raw: bytes, filename: str):
    """Skús známe parsery. Vráti (kw_series, label) alebo None."""
    # 0) holý číselný rad bez timestampov (.txt s 35040 hodnotami a pod.)
    try:
        bare = _bare_numeric_kw(raw)
        if bare is not None:
            return bare, "fastpath:bare_series"
    except Exception:
        pass
    from analyza_om import extract_consumption as ec
    suffix = Path(filename).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        tf.write(raw); tmp = Path(tf.name)
    try:
        candidates = []
        if suffix == ".csv" or _sniff(raw) == "csv":
            candidates = [(ec.parse_sse_csv, "mwh", 15, "sse_csv")]
        else:
            candidates = [
                (ec.parse_obis_datetime_xls, "mwh", 15, "obis_datetime_xls"),
                (ec.parse_sse_obis_xls, "mwh", 15, "sse_obis_xls"),
                (ec.parse_xls_96cols, "mwh", 15, "xls_96cols"),
                (ec.parse_zdis_xls, "kwh", 60, "zdis_xls"),
            ]
        for fn, unit, gran_hint, label in candidates:
            try:
                s = fn(tmp)
                if s is None or len(s) < 10:
                    continue
                gran = _infer_gran(s.index)
                # konverzia na kW (jednotná medzireprezentácia)
                if unit == "mwh":
                    kw = s * (60.0 / gran) * 1000.0
                else:  # kwh
                    kw = s * (60.0 / gran)
                kw.attrs["granularity_min"] = gran
                return kw, f"fastpath:{label}"
            except Exception:
                continue
        return None
    finally:
        tmp.unlink(missing_ok=True)


# ============================================================
# Verejné API
# ============================================================
def normalize_file(raw: bytes, filename: str, context: dict | None = None,
                   spec_cache: dict | None = None, force: dict | None = None) -> dict:
    """Vráti dict: {ok, series(MWh/15min), granularity_min, source, spec, period, mwh, fingerprint, warnings}."""
    warnings = []
    fp = _fingerprint(raw, filename)
    kw = None; spec = None; source = None

    # 0) FORCE override (používateľ opravil jednotku/granularitu) — detekuj mapovanie cez AI, prepíš jednotku/gran
    if force:
        try:
            spec = _ai_detect_spec(raw, filename, context)
            for k in ("value_unit", "granularity_min", "decimal_sep", "timestamp_col", "value_cols"):
                if force.get(k):
                    spec[k] = force[k]
            kw = _apply_spec_to_kw(raw, filename, spec)
            source = "override"
        except Exception as e:
            warnings.append(f"override zlyhal: {e}")

    # 1) re-use AI predpisu pre rovnaký formát (12 mesačných súborov)
    if spec_cache is not None and fp and fp in spec_cache:
        try:
            kw = _apply_spec_to_kw(raw, filename, spec_cache[fp])
            spec = spec_cache[fp]; source = "ai_cached"
        except Exception as e:
            warnings.append(f"re-use predpisu zlyhal: {e}")

    # 2) fast-path
    if kw is None:
        try:
            fpres = _fastpath_kw(raw, filename)
            if fpres is not None:
                kw, source = fpres
                spec = {"_source": source, "granularity_min": int(kw.attrs.get("granularity_min", 15))}
        except Exception as e:
            warnings.append(f"fast-path chyba: {e}")

    # 3) AI detekcia predpisu
    if kw is None:
        try:
            spec = _ai_detect_spec(raw, filename, context)
            kw = _apply_spec_to_kw(raw, filename, spec)
            source = "ai"
            if spec_cache is not None and fp:
                spec_cache[fp] = spec
        except Exception as e:
            return {"ok": False, "error": f"normalizácia zlyhala: {str(e)[:200]}", "warnings": warnings,
                    "fingerprint": fp}

    gran = int(kw.attrs.get("granularity_min", _infer_gran(kw.index)))
    series = _kw_to_15min_mwh(kw)
    mwh = round(float(series.sum()), 2)
    try:
        period = series.index.min().strftime("%m/%Y")
    except Exception:
        period = None
    return {"ok": True, "series": series, "granularity_min": gran, "source": source,
            "spec": spec, "period": period, "mwh": mwh, "fingerprint": fp, "warnings": warnings}
