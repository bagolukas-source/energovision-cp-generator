"""Každý _("…") v súboroch s mutáciami má EN aj DE preklad so zhodnými {zástupnými} symbolmi."""
import os
import string
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aom_i18n import _catalog  # noqa: E402
from tools.aom_i18n_extract import I18N_FILES, bad_calls, extract_keys  # noqa: E402

FILES = [os.path.join(ROOT, f) for f in I18N_FILES]


def _fields(s):
    return sorted(f for _, f, _, _ in string.Formatter().parse(s) if f is not None)


def test_all_keys_translated():
    k = extract_keys(FILES)
    for lang in ("en", "de"):
        miss = sorted(k - _catalog(lang).keys())
        assert not miss, f"{lang}: chýba {len(miss)} prekladov, napr. {miss[:5]} — python tools/aom_i18n_extract.py"


def test_placeholders_match():
    for lang in ("en", "de"):
        for sk, t in _catalog(lang).items():
            assert _fields(sk) == _fields(t), (lang, sk, t)


def test_no_dynamic_keys():
    bad = {p: l for p, l in bad_calls(FILES).items() if l}
    assert not bad, f"_() musí dostať reťazcový literál: {bad}"


def test_no_conflicting_translations():
    """Ten istý SK kľúč s rôznym prekladom vo viacerých oblastiach musí mať kanonický preklad v zz_kanon."""
    import importlib
    import pkgutil
    from aom_i18n import catalogs
    from aom_i18n.catalogs import zz_kanon
    for lang in ("EN", "DE"):
        seen = {}
        for m in pkgutil.iter_modules(catalogs.__path__):
            if m.name == "zz_kanon":
                continue
            for k, v in getattr(importlib.import_module(f"{catalogs.__name__}.{m.name}"), lang, {}).items():
                seen.setdefault(k, set()).add(v)
        bad = sorted(k for k, vals in seen.items() if len(vals) > 1 and k not in getattr(zz_kanon, lang))
        assert not bad, f"{lang}: rozdielne preklady bez kánonu v zz_kanon.py: {bad[:10]}"
