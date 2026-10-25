"""aom_i18n — jazyk, preklad (kľúč = SK text), formáty sk/en/de (spec 2026-10-25)."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aom_i18n import _, get_lang, norm_lang, resolve_lang, tr, use_lang  # noqa: E402
from aom_i18n.fmt import dec, eur, fmt_date, months, num, pbt  # noqa: E402
from aom_i18n.glossary import glossary_prompt  # noqa: E402


def test_norm_lang():
    assert [norm_lang(x) for x in ("en", "DE", "cs", None, "", "xx", "at")] == ["en", "de", "sk", "sk", "sk", "sk", "de"]


def test_resolve_lang_order():
    a = {"doc_lang": "de", "customers": {"lang": "en"}}
    assert resolve_lang("en", a) == "en"
    assert resolve_lang(None, a) == "de"
    assert resolve_lang(None, {"doc_lang": None, "customers": {"lang": "en"}}) == "en"
    assert resolve_lang(None, {"doc_lang": "sk", "customers": {"lang": "en"}}) == "sk"
    assert resolve_lang(None, {"customers": {"lang": "cs"}}) == "sk"
    assert resolve_lang(None, {}) == "sk" and resolve_lang(None, None) == "sk"


def test_sk_is_identity_and_default():
    assert get_lang() == "sk"
    assert _("Návratnosť") == "Návratnosť"
    assert tr("Ľubovoľný text bez prekladu", "sk") == "Ľubovoľný text bez prekladu"


def test_context_switch_restores():
    with use_lang("en"):
        assert get_lang() == "en"
        assert _("Ročná spotreba") == "Annual consumption"
        with use_lang("de"):
            assert _("Ročná spotreba") == "Jahresverbrauch"
        assert get_lang() == "en"
    assert get_lang() == "sk"


def test_missing_translation_falls_back_to_sk():
    with use_lang("de"):
        assert _("__neexistuje__") == "__neexistuje__"


def test_formats_sk_unchanged():
    assert num(1234.5, 1) == "1 234,5" and eur(12345) == "12 345 €" and num(-1234567) == "-1 234 567"
    assert pbt(4.94) == "4,9 r" and pbt(None) == "nevráti sa" and pbt(99) == "nevráti sa" and pbt(0) == "nevráti sa"
    assert dec(12.34) == "12,3" and dec(12.0) == "12"
    assert fmt_date(dt.date(2026, 10, 25)) == "25.10.2026" and fmt_date("2026-01-05T10:00") == "05.01.2026"
    assert months()[4] == "Maj"


def test_formats_en_de():
    with use_lang("en"):
        assert (num(1234.5, 1), eur(12345), pbt(4.94), pbt(None), dec(12.34)) == \
            ("1,234.5", "12,345 €", "4.9 yrs", "no payback", "12.3")
        assert fmt_date(dt.date(2026, 10, 25)) == "25 Oct 2026" and months()[4] == "May"
    with use_lang("de"):
        assert (num(1234.5, 1), eur(12345), pbt(4.94), pbt(None), dec(12.34)) == \
            ("1.234,5", "12.345 €", "4,9 J.", "keine Amortisation", "12,3")
        assert fmt_date(dt.date(2026, 10, 25)) == "25.10.2026" and months()[2] == "Mär"


def test_bad_values():
    assert num("x") == "—" and eur(None) == "—"


def test_glossary_prompt():
    assert glossary_prompt("sk") == ""
    assert "English" in glossary_prompt("en") and "Zelená podnikom" in glossary_prompt("en")
    assert "German" in glossary_prompt("de") and "MwSt." in glossary_prompt("de")
