"""CLI — `enva` command-line interface.

Sprint 1 príkazy:
    enva validate    --site --load --pv-profile --year --tariff
    enva tariff      --distribuutor --sadzba --year --typ-tarify --spot
    enva okte-fetch  --year --output
    enva okte-stats  --csv
    enva benchmark   --pv-yield --kwp --capex-bess --bess-kwh --irr --payback
    enva info        — verzia + stav modulov
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from energovision_analytics import __version__
from energovision_analytics.benchmark import BenchmarkEngine
from energovision_analytics.data.readers.okte_client import OKTEClient
from energovision_analytics.tariff import RetailCalculator, TariffEngine


def _cmd_info(_args: argparse.Namespace) -> int:
    print(f"energovision-analytics {__version__}")
    print("Sprint 1: data + validation + tariff + benchmark")
    print("Pripravené moduly:")
    print("  ✓ core/models     — Pydantic dátové modely")
    print("  ✓ core/time_series — TimeSeriesData wrapper")
    print("  ✓ tariff          — TariffEngine, RetailCalculator, MRK penalty")
    print("  ✓ data/readers    — OKTEClient, ExcelReader, eDistribúcia CSV")
    print("  ✓ validation      — ValidationEngine (8 kategórií)")
    print("  ✓ benchmark       — NREL/IEA/SK porovnania")
    return 0


def _cmd_tariff(args: argparse.Namespace) -> int:
    """Vypočítaj retail cenu pre konkrétnu kombináciu."""
    tariff_path = Path(args.tariff_yaml or f"data/tariffs/{args.year}.yaml")
    if not tariff_path.exists():
        # Skús resolve relative voči balíku
        from energovision_analytics import __file__ as pkg_init
        alt = Path(pkg_init).parent.parent.parent / "data" / "tariffs" / f"{args.year}.yaml"
        if alt.exists():
            tariff_path = alt

    engine = TariffEngine.from_yaml(tariff_path)
    tariff = engine.get(args.distribuutor, args.sadzba)

    calc = RetailCalculator(tariff, typ_tarify=args.typ_tarify)
    spot = args.spot_eur_mwh
    breakdown = calc.retail_buy_breakdown(spot)

    print(f"\n=== Retail kalkulácia ({args.distribuutor} {args.sadzba} {args.year}, {args.typ_tarify}) ===")
    if args.typ_tarify == "spot":
        print(f"Spot:               {spot:>10.2f} €/MWh")
    print(f"Silová zložka:      {breakdown['silova_eur_kwh']*1000:>10.2f} €/MWh  ({breakdown['silova_eur_kwh']*1000/1000:.4f} €/kWh)")
    print(f"Obchodník:          {breakdown['obchodnik_eur_kwh']*1000:>10.2f} €/MWh  ({breakdown['obchodnik_eur_kwh']:.4f} €/kWh)")
    print(f"  TPS:              {breakdown['tps_eur_kwh']*1000:>10.2f} €/MWh")
    print(f"  Distribúcia:      {breakdown['distrib_eur_kwh']*1000:>10.2f} €/MWh")
    print(f"  Straty:           {breakdown['straty_eur_kwh']*1000:>10.2f} €/MWh")
    print(f"  NJF:              {breakdown['njf_eur_kwh']*1000:>10.2f} €/MWh")
    print(f"  Spotrebná daň:    {breakdown['spotrebna_dan_eur_kwh']*1000:>10.2f} €/MWh")
    print(f"  TSS:              {breakdown['tss_eur_kwh']*1000:>10.2f} €/MWh")
    print(f"Regulované spolu:   {breakdown['regulovane_eur_kwh']*1000:>10.2f} €/MWh  ({breakdown['regulovane_eur_kwh']:.4f} €/kWh)")
    print("-" * 60)
    print(f"RETAIL BUY:         {breakdown['total_eur_kwh']*1000:>10.2f} €/MWh  ({breakdown['total_eur_kwh']:.4f} €/kWh)")
    print()
    if tariff.mrk_export_penalty_eur_kwh > 0:
        print(f"⚠ POZOR: MRK export penalty {tariff.mrk_export_penalty_eur_kwh*1000:.2f} €/MWh "
              f"aplikuje sa pri exporte nad MRK (od 1.1.2026)")
    return 0


def _cmd_okte_stats(args: argparse.Namespace) -> int:
    """Vypočítaj štatistiky z OKTE CSV súboru."""
    client = OKTEClient()
    df = client.load_from_csv(args.csv)
    stats = OKTEClient.annual_statistics(df)

    print(f"\n=== OKTE DAM štatistiky ({args.csv}) ===")
    for k, v in stats.items():
        if isinstance(v, float):
            print(f"  {k:30s} = {v:>12,.2f}")
        else:
            print(f"  {k:30s} = {v}")
    return 0


def _cmd_okte_fetch(args: argparse.Namespace) -> int:
    """Stiahni OKTE ceny z API pre celý rok."""
    print(f"Sťahujem OKTE DAM pre rok {args.year}...")
    client = OKTEClient(cache_dir=args.output or "data/okte_cache")
    df = client.fetch_year(args.year, force_refresh=args.force)
    stats = OKTEClient.annual_statistics(df)
    print(f"Stiahnutých {stats['n_hours']} hodín.")
    print(f"Priemer: {stats['mean_eur_mwh']:.1f} €/MWh, "
          f"medián: {stats['median_eur_mwh']:.1f}, "
          f"záporných hodín: {stats['negative_hours']} ({stats['negative_hours_pct']:.1f}%)")
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Porovnaj projekt s benchmarkmi."""
    kwargs: dict = {}
    if args.pv_yield_kwh and args.kwp:
        kwargs["annual_pv_kwh"] = args.pv_yield_kwh
        kwargs["installed_kwp"] = args.kwp
        kwargs["lokalita"] = args.lokalita or "default"
    if args.capex_bess_eur and args.bess_kwh:
        kwargs["bess_capex_eur"] = args.capex_bess_eur
        kwargs["bess_kwh"] = args.bess_kwh
    if args.rte:
        kwargs["rte"] = args.rte
        kwargs["vyrobca"] = args.vyrobca or "default"
    if args.lcos:
        kwargs["lcos"] = args.lcos
    if args.irr_pct:
        kwargs["irr_pct"] = args.irr_pct
    if args.payback_y:
        kwargs["payback_y"] = args.payback_y
    if args.project_type:
        kwargs["project_type"] = args.project_type

    results = BenchmarkEngine.compare_project(**kwargs)
    print("\n" + BenchmarkEngine.summary_table(results))
    print()
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validuj vstupné dáta projektu."""
    from energovision_analytics.core.models import SiteInput
    from energovision_analytics.core.time_series import TimeSeriesData
    from energovision_analytics.validation import ValidationEngine

    site_data = json.loads(Path(args.site).read_text(encoding="utf-8"))
    site = SiteInput(**site_data)

    engine = ValidationEngine()
    engine.validate_site(site)

    if args.load:
        ts = TimeSeriesData.from_csv(
            args.load,
            timestamp_col=args.timestamp_col or "datetime",
            value_col=args.value_col or "load_kw",
            granularity_min=args.granularity,
        )
        engine.validate_load_profile(ts, site)

    report = engine.report
    print("\n" + report.summary())
    print(f"\nÚplný report ({len(report.issues)} issues):")
    for issue in report.issues:
        print(f"  [{issue.severity.value:>8}] {issue.category}/{issue.rule}: {issue.message}")
        if issue.suggestion:
            print(f"           → {issue.suggestion}")

    if args.output:
        Path(args.output).write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
                                       encoding="utf-8")
        print(f"\nReport uložený: {args.output}")

    return 0 if report.passed else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="enva",
        description="Energovision Analytics Engine CLI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # info
    p_info = sub.add_parser("info", help="Verzia + stav modulov")
    p_info.set_defaults(func=_cmd_info)

    # tariff
    p_tariff = sub.add_parser("tariff", help="Vypočítať retail cenu")
    p_tariff.add_argument("--distribuutor", required=True, choices=["SSE", "ZSD", "VSD"])
    p_tariff.add_argument("--sadzba", required=True, choices=["NN", "VN"])
    p_tariff.add_argument("--year", type=int, required=True)
    p_tariff.add_argument("--typ-tarify", required=True, choices=["fix", "spot", "hybrid"])
    p_tariff.add_argument("--spot-eur-mwh", type=float, default=0.0)
    p_tariff.add_argument("--tariff-yaml", help="Cesta k YAML (default: data/tariffs/{year}.yaml)")
    p_tariff.set_defaults(func=_cmd_tariff)

    # okte stats
    p_okte_stats = sub.add_parser("okte-stats", help="Štatistiky z OKTE CSV")
    p_okte_stats.add_argument("--csv", required=True)
    p_okte_stats.set_defaults(func=_cmd_okte_stats)

    # okte fetch
    p_okte_fetch = sub.add_parser("okte-fetch", help="Stiahni OKTE DAM ceny")
    p_okte_fetch.add_argument("--year", type=int, required=True)
    p_okte_fetch.add_argument("--output", help="Cache adresár")
    p_okte_fetch.add_argument("--force", action="store_true")
    p_okte_fetch.set_defaults(func=_cmd_okte_fetch)

    # benchmark
    p_bench = sub.add_parser("benchmark", help="Porovnaj projekt s benchmarkmi")
    p_bench.add_argument("--pv-yield-kwh", type=float)
    p_bench.add_argument("--kwp", type=float)
    p_bench.add_argument("--lokalita", default="default")
    p_bench.add_argument("--capex-bess-eur", type=float)
    p_bench.add_argument("--bess-kwh", type=float)
    p_bench.add_argument("--rte", type=float)
    p_bench.add_argument("--vyrobca", default="default")
    p_bench.add_argument("--lcos", type=float)
    p_bench.add_argument("--irr-pct", type=float)
    p_bench.add_argument("--payback-y", type=float)
    p_bench.add_argument("--project-type", default="FVE_BESS_hybrid")
    p_bench.set_defaults(func=_cmd_benchmark)

    # validate
    p_val = sub.add_parser("validate", help="Validuj vstupné dáta projektu")
    p_val.add_argument("--site", required=True, help="Cesta k SiteInput JSON")
    p_val.add_argument("--load", help="Cesta k load CSV")
    p_val.add_argument("--timestamp-col", default="datetime")
    p_val.add_argument("--value-col", default="load_kw")
    p_val.add_argument("--granularity", type=int, default=15, choices=[15, 60])
    p_val.add_argument("--output", help="JSON report výstup")
    p_val.set_defaults(func=_cmd_validate)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
