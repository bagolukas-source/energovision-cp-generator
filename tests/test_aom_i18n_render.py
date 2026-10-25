"""Posudok: jazyk z požiadavky / analýzy / zákazníka, názov súboru, záznam dokumentu, AI prompt v jazyku."""
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

import analyza_om_v2 as aom  # noqa: E402
import posudok_chocosuc.generator as G  # noqa: E402
import posudok_chocosuc.generator_docx as GD  # noqa: E402
from aom_fakes import FakeSB  # noqa: E402
from aom_i18n import get_lang  # noqa: E402
from aom_i18n_fixtures import fixture_analyza  # noqa: E402


def _render(monkeypatch, lang=None, **a_kw):
    a = fixture_analyza()
    a.update(a_kw)
    sb = FakeSB()
    sb.db["analyza_om"] = [a]
    seen = {}

    def pdf(ctx):
        seen["html"] = G.render_chocosuc_html(ctx)
        seen["lang_pdf"] = get_lang()
        return b"%PDF"

    def ai(ctx):
        seen["lang_ai"] = get_lang()
        return {}
    monkeypatch.setattr(G, "generate_chocosuc_pdf", pdf)
    monkeypatch.setattr(GD, "generate_chocosuc_docx", lambda ctx: b"PK")
    monkeypatch.setattr(aom, "_generate_chocosuc_ai", ai)
    r = aom.render_posudok_chocosuc(sb, a["id"], lang=lang)
    return r, seen, sb


def test_lang_from_customer(monkeypatch):
    r, seen, sb = _render(monkeypatch, customers={"company_name": "Testovacia firma s.r.o.", "lang": "en"})
    assert r["lang"] == "en" and seen["lang_ai"] == "en" and seen["lang_pdf"] == "en"
    assert '<html lang="en">' in seen["html"]
    assert "/Assessment_PV_" in r["pdf_url"] and "_EN_" in r["pdf_url"]
    assert {d.get("lang") for d in sb.db["analyza_om_documents"]} == {"en"}
    assert get_lang() == "sk"  # kontext sa po renderi vráti


def test_request_lang_wins(monkeypatch):
    r, seen, _ = _render(monkeypatch, lang="de", doc_lang="en")
    assert r["lang"] == "de" and "/Gutachten_PV_" in r["pdf_url"] and "_DE_" in r["pdf_url"]


def test_sk_default_unchanged(monkeypatch):
    r, seen, sb = _render(monkeypatch)
    assert r["lang"] == "sk" and "/Posudok_FVE_" in r["pdf_url"] and "_SK" not in r["pdf_url"]
    assert all("lang" not in d for d in sb.db["analyza_om_documents"])  # sk = DB default, payload bez zmeny


def test_glossary_in_ai_prompt_only_for_en_de(monkeypatch):
    calls = []

    class _Msg:
        def __init__(self):
            self.content = [types.SimpleNamespace(text="{}")]

    class _Anth:
        def __init__(self, *a, **k):
            self.messages = types.SimpleNamespace(create=lambda **kw: calls.append(kw["system"]) or _Msg())
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Anth))
    from aom_i18n import use_lang
    from aom_i18n_fixtures import fixture_ctx
    ctx = fixture_ctx()
    aom._generate_chocosuc_ai(dict(ctx))
    assert calls and all("OUTPUT LANGUAGE" not in s for s in calls)
    calls.clear()
    with use_lang("de"):
        aom._generate_chocosuc_ai(dict(ctx))
    assert calls and all("German" in s and "Zelená podnikom" in s for s in calls)


def test_ai_fallback_v_jazyku(monkeypatch):
    """Keď AI skupina zlyhá, deterministický fallback musí byť v jazyku dokumentu (sk ostáva pôvodný)."""
    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("AI nedostupné")
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Boom))
    from aom_i18n import use_lang
    from aom_i18n_fixtures import fixture_ctx, slovak_leftovers
    ctx = fixture_ctx()
    sk = aom._generate_chocosuc_ai(dict(ctx))
    assert "Z technického hľadiska" in sk["expert"] and "(faktúra), podmienky" in sk["expert"]  # SK bez straty čiarok (predtým replace(",", " "))
    assert sk["recommendations"][0][0] == "Odporúčanie k navrhnutému variantu"
    for lang in ("en", "de"):
        with use_lang(lang):
            r = aom._generate_chocosuc_ai(fixture_ctx())
        texts = [r["expert"], r["summary"]] + [t for pair in r["recommendations"] for t in pair]
        left = [w for w in slovak_leftovers(" ".join(texts))]
        assert not left, (lang, left)
