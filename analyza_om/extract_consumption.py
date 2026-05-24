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
