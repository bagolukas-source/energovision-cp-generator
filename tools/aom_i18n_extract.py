"""Zoznam prekladových kľúčov: všetky _("…") literály v .py (AST) a {{ _("…") }} v šablónach.
  python tools/aom_i18n_extract.py [súbor ...]   # vypíše chýbajúce EN/DE preklady
"""
import ast
import pathlib
import re
import sys

_J = re.compile(r'_\(\s*"((?:[^"\\]|\\.)*)"\s*\)|_\(\s*\'((?:[^\'\\]|\\.)*)\'\s*\)')

# súbory s jazykovými mutáciami (rozširuje sa s každou oblasťou)
I18N_FILES = [
    "posudok_chocosuc/generator.py",
    "posudok_chocosuc/context.py",
    "posudok_chocosuc/charts.py",
    "posudok_chocosuc/generator_docx.py",
    "profile_classifier.py",
    "generuj_prezentaciu.py",
    "financovanie_report/generator.py",
    "financovanie_report/templates/report.html",
    "posudok_orkestra/templates/porovnanie.html",
    "posudok_orkestra/generator.py",
    "posudok_orkestra/charts.py",
    "analyza_om_v2.py",
]


def _py_keys(src):
    keys = set()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
    return keys


def _bad_calls(src):
    """_() s nekonštantným argumentom (f-string, premenná) — kľúč by sa nedal preložiť."""
    bad = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                and not (node.args and isinstance(node.args[0], ast.Constant))):
            bad.append(node.lineno)
    return bad


def extract_keys(paths):
    keys = set()
    for p in map(pathlib.Path, paths):
        src = p.read_text(encoding="utf-8")
        if p.suffix == ".py":
            keys |= _py_keys(src)
        else:
            for a, b in _J.findall(src):
                keys.add((a or b).replace('\\"', '"').replace("\\'", "'"))
    return keys


def bad_calls(paths):
    return {p: _bad_calls(pathlib.Path(p).read_text(encoding="utf-8")) for p in paths if p.endswith(".py")}


if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from aom_i18n import _catalog
    files = sys.argv[1:] or I18N_FILES
    k = extract_keys([str(root / f) for f in files])
    for lang in ("en", "de"):
        miss = sorted(k - _catalog(lang).keys())
        print(f"{lang}: {len(k)} kľúčov, {len(miss)} chýba")
        for m in miss:
            print("   ", repr(m))
