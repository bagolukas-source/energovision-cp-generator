#!/usr/bin/env python3
"""
Parser dodávateľských exportov spotreby — CSV (SSE-D, ZSDIS) a XLS (15-min × 96 stĺpcov).

Výstup: hodinový profil (8 760 h alebo menej) ako CSV s indexom timestamp.

Príklad použitia:
    python extract_consumption.py --csv "Spotreba_X_Y_3781_Tech.csv" \
                                   --csv "Spotreba_X_Y_3781_Vykurovaci.csv" \
                                   --output profil_h.csv
"""
import argparse
import sys
from pathlib import Path
import pandas as pd


def parse_sse_csv(fn: Path) -> pd.Series:
    """SSE-D / ZSDIS CSV format: skiprows=5, sep=';', kWh v MWh za 15 min."""
    try:
        df = pd.read_csv(fn, sep=';', encoding='utf-8-sig', skiprows=5,
                         header=None, names=['ts', 'kWh', 'unused1', 'unused2'])
    except Exception:
        df = pd.read_csv(fn, sep=';', encoding='utf-8-sig', skiprows=5,
                         header=None, names=['ts', 'kWh'])
    df['ts'] = pd.to_datetime(df['ts'], format='%d.%m.%Y %H:%M', errors='coerce')
    df['kWh'] = df['kWh'].astype(str).str.replace(',', '.').str.strip()
    df['kWh'] = pd.to_numeric(df['kWh'], errors='coerce')
    df = df.dropna(subset=['ts', 'kWh']).set_index('ts').sort_index()
    return df['kWh']


def parse_xls_96cols(fn: Path) -> pd.Series:
    """XLS s 96 stĺpcami (15-min intervaly), prvý stĺpec dátum."""
    df = pd.read_excel(fn, sheet_name=0, header=None)
    rows = []
    for r in range(2, len(df)):
        date = df.iloc[r, 0]
        if pd.isna(date):
            continue
        d = pd.to_datetime(date, errors='coerce')
        if pd.isna(d):
            continue
        for c in range(1, 97):
            val = df.iloc[r, c]
            if pd.notna(val) and isinstance(val, (int, float)):
                ts = d + pd.Timedelta(minutes=15 * (c - 1))
                rows.append((ts, val))
    s = pd.Series(dict(rows)).sort_index()
    s.index.name = 'ts'
    return s


def parse_zdis_xls(fn: Path) -> pd.Series:
    """ZSDIS export — hárok 'Nameraná hodinová práca'."""
    df = pd.read_excel(fn, sheet_name='Nameraná hodinová práca',
                       header=None, skiprows=9)
    df.columns = ['x', 'd', 't', 'kWh']
    df['d'] = pd.to_datetime(df['d'], format='%d.%m.%Y', errors='coerce')
    df['kWh'] = pd.to_numeric(df['kWh'], errors='coerce')

    def to_td(t):
        if pd.isna(t):
            return None
        if isinstance(t, str):
            try: return pd.to_timedelta(t)
            except: return None
        if hasattr(t, 'hour'):
            return pd.to_timedelta(f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}")
        return None

    df['t'] = df['t'].apply(to_td)
    df['ts'] = df['d'] + df['t']
    df = df.dropna(subset=['ts', 'kWh']).set_index('ts').sort_index()
    return df['kWh']


def parse_sse_obis_xls(fn: Path) -> pd.Series:
    """SSE-D / ZSDIS XLS s OBIS kódmi (napr. Gonvauto formát).
    Štruktúra:
      Row 0: title, Koeficient U=X, Koeficient I=Y, ...
      Row 1: hlavička: dátum, štart, koniec, 1-1:1.5 kW (P+ kW), 1-1:2.5 kW (P-), ...kvar..., 1-1:1.8 kWh (cum.)
      Row 2+: dátové riadky (35040 = 365×96 pre 15-min ročný profil)

    Vracia: pd.Series — MWh per 15-min interval (kompatibilné s aggregate_to_hourly).
    """
    # Skús oba enginy (xlrd pre .xls, openpyxl pre .xlsx)
    suffix = fn.suffix.lower()
    if suffix == ".xls":
        df = pd.read_excel(fn, sheet_name=0, header=None, engine="xlrd")
    else:
        df = pd.read_excel(fn, sheet_name=0, header=None, engine="openpyxl")

    # 1) Detekuj koeficienty U a I z prvých 3 riadkov
    koef_u = 1.0
    koef_i = 1.0
    import re
    for r in range(min(3, len(df))):
        for c in range(min(20, df.shape[1])):
            cell = str(df.iat[r, c]) if not pd.isna(df.iat[r, c]) else ""
            mu = re.search(r"Koeficient\s*U\s*=\s*([0-9.,]+)", cell, re.IGNORECASE)
            mi = re.search(r"Koeficient\s*I\s*=\s*([0-9.,]+)", cell, re.IGNORECASE)
            if mu:
                try: koef_u = float(mu.group(1).replace(",", "."))
                except: pass
            if mi:
                try: koef_i = float(mi.group(1).replace(",", "."))
                except: pass

    multiplier = koef_u * koef_i

    # 2) Nájdi header row (obsahuje "dátum" a "štart")
    header_row = None
    for r in range(min(10, len(df))):
        row_vals = [str(df.iat[r, c]).strip().lower() if not pd.isna(df.iat[r, c]) else "" for c in range(min(20, df.shape[1]))]
        if any("dátum" in v or "datum" in v for v in row_vals) and any("štart" in v or "start" in v for v in row_vals):
            header_row = r
            break
    if header_row is None:
        raise ValueError("SSE OBIS: nenašiel som header s 'dátum' a 'štart'")

    # 3) Nájdi stĺpce: dátum, štart, P+ (kW pre odber činnej)
    headers = [str(df.iat[header_row, c]).strip() if not pd.isna(df.iat[header_row, c]) else "" for c in range(df.shape[1])]
    col_date = None
    col_start = None
    col_p_plus = None
    for i, h in enumerate(headers):
        hl = h.lower()
        if col_date is None and ("dátum" in hl or "datum" in hl):
            col_date = i
        elif col_start is None and ("štart" in hl or "start" in hl):
            col_start = i
        # P+ má OBIS kód 1-1:1.5 (priemer 15-min činný odber)
        if col_p_plus is None and ("1.5" in h or "1-1:1.5" in h) and "kw" in hl and "kvar" not in hl and "kwh" not in hl:
            col_p_plus = i

    if col_p_plus is None:
        # Fallback: prvý stĺpec po 'koniec' ktorý má 'kW' (nie kvar, nie kWh)
        for i, h in enumerate(headers):
            hl = h.lower()
            if "kw" in hl and "kvar" not in hl and "kwh" not in hl:
                col_p_plus = i
                break
    if col_date is None or col_start is None or col_p_plus is None:
        raise ValueError(f"SSE OBIS: nenašiel stĺpce dátum={col_date} štart={col_start} P+={col_p_plus}")

    # 4) Načítaj dátové riadky
    rows = []
    for r in range(header_row + 1, len(df)):
        d_val = df.iat[r, col_date]
        t_val = df.iat[r, col_start]
        v_val = df.iat[r, col_p_plus]
        if pd.isna(d_val) or pd.isna(v_val):
            continue
        # Dátum: "1. 1. 2025" alebo "01.01.2025" alebo datetime
        if isinstance(d_val, str):
            d_str = d_val.strip().replace(" ", "")
            d = pd.to_datetime(d_str, format="%d.%m.%Y", errors="coerce")
            if pd.isna(d):
                d = pd.to_datetime(d_val, errors="coerce", dayfirst=True)
        else:
            d = pd.to_datetime(d_val, errors="coerce")
        if pd.isna(d):
            continue
        # Čas: "0:00", "0:15", ... alebo time object
        if isinstance(t_val, str):
            try:
                hh, mm = t_val.split(":")[:2]
                ts = d + pd.Timedelta(hours=int(hh), minutes=int(mm))
            except Exception:
                continue
        elif hasattr(t_val, "hour"):
            ts = d + pd.Timedelta(hours=t_val.hour, minutes=t_val.minute)
        else:
            continue
        try:
            kw = float(str(v_val).replace(",", "."))
        except Exception:
            continue
        # kW (priemer 15 min) × multiplier (CT/PT) × 0.25 h = kWh per 15-min interval
        # aggregate_to_hourly očakáva MWh per 15-min ak interval < 3000s → / 1000
        kwh_per_15min = kw * multiplier * 0.25
        mwh_per_15min = kwh_per_15min / 1000.0
        rows.append((ts, mwh_per_15min))

    if not rows:
        raise ValueError("SSE OBIS: žiadne dátové riadky")
    s = pd.Series(dict(rows)).sort_index()
    s.index.name = "ts"
    s.attrs["koef_u"] = koef_u
    s.attrs["koef_i"] = koef_i
    s.attrs["multiplier"] = multiplier
    return s


def parse_obis_datetime_xls(fn: Path) -> pd.Series:
    """OBIS .xls s JEDNÝM stĺpcom 'Dátum a čas merania' (Excel serial) + '1.5.0 - Činný odber (kW)'.
    Formát napr. Savencia/distribútor mesačné exporty. Vracia MWh per 15-min interval."""
    suffix = fn.suffix.lower()
    df = pd.read_excel(fn, sheet_name=0, header=None, engine=("xlrd" if suffix == ".xls" else "openpyxl"))
    hr = None
    for r in range(min(8, len(df))):
        vals = [str(df.iat[r, c]).lower() if not pd.isna(df.iat[r, c]) else "" for c in range(min(15, df.shape[1]))]
        if any(("dátum" in v or "datum" in v) and ("čas" in v or "cas" in v) for v in vals) and any("1.5.0" in v for v in vals):
            hr = r; break
    if hr is None:
        raise ValueError("OBIS-dt: header s 'dátum a čas' + '1.5.0' nenájdený")
    headers = [str(df.iat[hr, c]).strip() if not pd.isna(df.iat[hr, c]) else "" for c in range(df.shape[1])]
    col_dt = col_p = None
    for i, h in enumerate(headers):
        hl = h.lower()
        if col_dt is None and (("dátum" in hl or "datum" in hl) and ("čas" in hl or "cas" in hl)):
            col_dt = i
        if col_p is None and "1.5.0" in h and "kw" in hl and "odber" in hl and "kvar" not in hl and "kvalit" not in hl:
            col_p = i
    if col_p is None:
        for i, h in enumerate(headers):
            hl = h.lower()
            if "1.5.0" in h and "kw" in hl and "kvalit" not in hl and "kvar" not in hl:
                col_p = i; break
    if col_dt is None or col_p is None:
        raise ValueError(f"OBIS-dt: stĺpce dt={col_dt} p={col_p}")
    rows = []
    for r in range(hr + 1, len(df)):
        dv = df.iat[r, col_dt]; vv = df.iat[r, col_p]
        if pd.isna(dv) or pd.isna(vv):
            continue
        if isinstance(dv, (int, float)):
            ts = pd.to_datetime(dv, origin="1899-12-30", unit="D")
        else:
            ts = pd.to_datetime(dv, errors="coerce", dayfirst=True)
        if pd.isna(ts):
            continue
        try:
            kw = float(str(vv).replace(",", "."))
        except Exception:
            continue
        rows.append((ts, kw * 0.25 / 1000.0))  # kW × 0.25h = kWh/15min, /1000 = MWh/15min
    if not rows:
        raise ValueError("OBIS-dt: žiadne dátové riadky")
    s = pd.Series(dict(rows)).sort_index()
    s.index.name = "ts"
    return s


def aggregate_to_hourly(series: pd.Series) -> pd.Series:
    """Agreguje 15-min profil na hodinový (suma kWh za hodinu)."""
    if (series.index[1] - series.index[0]).total_seconds() < 3000:  # ~15min
        return series.resample('1h').sum() * 1000  # ak je v MWh, prepočítaj na kWh
    return series  # už hodinové


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', action='append', default=[],
                    help='CSV súbor — viacero pre súčet okruhov')
    ap.add_argument('--xls', action='append', default=[],
                    help='XLS súbor (96 stĺpcov)')
    ap.add_argument('--zdis-xls', action='append', default=[],
                    help='XLS export ZSDIS s hárkom Nameraná hodinová práca')
    ap.add_argument('--output', required=True, help='Výstupný CSV súbor')
    args = ap.parse_args()

    series_list = []
    for fn in args.csv:
        s = parse_sse_csv(Path(fn))
        s = aggregate_to_hourly(s)
        series_list.append(s)
        print(f"  CSV {fn}: {len(s)} h, suma {s.sum()/1000:.1f} MWh", file=sys.stderr)

    for fn in args.xls:
        s = parse_xls_96cols(Path(fn))
        s = aggregate_to_hourly(s)
        series_list.append(s)
        print(f"  XLS {fn}: {len(s)} h, suma {s.sum()/1000:.1f} MWh", file=sys.stderr)

    for fn in args.zdis_xls:
        s = parse_zdis_xls(Path(fn))
        series_list.append(s)
        print(f"  ZDIS {fn}: {len(s)} h, suma {s.sum()/1000:.1f} MWh", file=sys.stderr)

    if not series_list:
        print("Žiadne vstupy — daj --csv, --xls alebo --zdis-xls", file=sys.stderr)
        sys.exit(1)

    # Sčítaj všetky okruhy
    combined = pd.concat(series_list, axis=1).fillna(0).sum(axis=1)
    combined.name = 'kWh'
    combined.to_csv(args.output)

    print(f"\nVýsledok: {len(combined)} h, {combined.sum()/1000:.1f} MWh/rok")
    print(f"  Max 1-h: {combined.max():.0f} kW, priemer {combined.mean():.0f} kW")
    work = combined[combined.index.weekday < 5]
    weekend = combined[combined.index.weekday >= 5]
    print(f"  Pracovný deň ⌀ {work.mean():.0f} kW, víkend ⌀ {weekend.mean():.0f} kW")
    print(f"  Uložené: {args.output}")


if __name__ == '__main__':
    main()
