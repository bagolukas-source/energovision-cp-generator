"""SK posudok sa jazykovými mutáciami nesmie zmeniť (spec 2026-10-25, „Ochrana SK verzie").
Pri páde: python tests/golden/_capture.py --diff — opraviť kód, nie zlatý súbor."""
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("ENERGO_SPOT_CSV", str(ROOT / "aom_data" / "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", str(ROOT / "aom_data" / "tariffs" / "2026.yaml"))
try:
    import weasyprint  # noqa: F401
except Exception:
    _w = types.ModuleType("weasyprint"); _w.HTML = _w.CSS = object; sys.modules["weasyprint"] = _w

from aom_i18n_fixtures import render_outputs  # noqa: E402

G = ROOT / "tests" / "golden" / "sk"


def test_posudok_sk_golden():
    outs = render_outputs()
    bad = [n for n, got in outs.items() if (G / n).read_text(encoding="utf-8") != got]
    assert not bad, f"SK regresia: {bad} — python tests/golden/_capture.py --diff"
