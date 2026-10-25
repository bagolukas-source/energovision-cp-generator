"""Zachytí zlaté SK výstupy posudku (tests/golden/sk/). Spúšťať LEN na kóde, ktorého SK výstup je referencia.
  python tests/golden/_capture.py          # prepíše zlaté súbory
  python tests/golden/_capture.py --diff   # ukáže rozdiel aktuálneho SK výstupu voči zlatým súborom
"""
import difflib, os, pathlib, sys, types
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("ENERGO_SPOT_CSV", str(ROOT / "aom_data" / "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", str(ROOT / "aom_data" / "tariffs" / "2026.yaml"))
try:
    import weasyprint  # noqa: F401
except Exception:
    _w = types.ModuleType("weasyprint"); _w.HTML = _w.CSS = object; sys.modules["weasyprint"] = _w
from aom_i18n_fixtures import render_outputs  # noqa: E402

G = pathlib.Path(__file__).parent / "sk"
if __name__ == "__main__":
    outs = render_outputs()
    if "--diff" in sys.argv:
        for name, got in outs.items():
            exp = (G / name).read_text(encoding="utf-8")
            if exp != got:
                sys.stdout.writelines(list(difflib.unified_diff(exp.splitlines(True), got.splitlines(True), name, "aktuálne"))[:200])
    else:
        G.mkdir(exist_ok=True)
        for name, got in outs.items():
            (G / name).write_text(got, encoding="utf-8")
        print("zapísané:", ", ".join(sorted(outs)))
