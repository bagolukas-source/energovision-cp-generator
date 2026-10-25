"""Jazykové mutácie dokumentov AOM (sk/en/de).

Kľúčom prekladu je slovenský text: pre ``sk`` sa nič nevyhľadáva, takže slovenský
výstup sa refaktorom nemení. Jazyk drží ContextVar — render obalí ``with use_lang(l):``.
"""
import contextlib
import contextvars
import logging

_LANG = contextvars.ContextVar("aom_lang", default="sk")
SUPPORTED = ("sk", "en", "de")
_log = logging.getLogger("evo")


def norm_lang(x) -> str:
    """sk/en/de; Rakúsko → de; čeština, neznáme a prázdne → sk."""
    v = str(x or "").strip().lower()
    if v == "at":
        return "de"
    return v if v in SUPPORTED else "sk"


def resolve_lang(explicit, analyza) -> str:
    """Prvý neprázdny kandidát vyhráva (aj „sk"): požiadavka → analyza_om.doc_lang → customers.lang."""
    a = analyza or {}
    cust = a.get("customers") if isinstance(a.get("customers"), dict) else {}
    for cand in (explicit, a.get("doc_lang"), cust.get("lang")):
        if cand:
            return norm_lang(cand)
    return "sk"


def get_lang() -> str:
    return _LANG.get()


@contextlib.contextmanager
def use_lang(lang):
    tok = _LANG.set(norm_lang(lang))
    try:
        yield
    finally:
        _LANG.reset(tok)


def _catalog(lang):
    if lang == "en":
        from .en import T
        return T
    if lang == "de":
        from .de import T
        return T
    return {}


def tr(text: str, lang: str) -> str:
    if lang == "sk":
        return text
    out = _catalog(lang).get(text)
    if out is None:
        _log.warning("aom_i18n: chýba preklad [%s]: %.80s", lang, text)
        return text
    return out


def _(text: str) -> str:
    return tr(text, _LANG.get())


def install_jinja(env):
    """Jinja globály: _() a formátovače v aktuálnom jazyku."""
    from . import fmt
    env.globals["_"] = _
    env.globals.update(num=fmt.num, eur=fmt.eur, pbt=fmt.pbt, dec=fmt.dec,
                       fmt_date=fmt.fmt_date, lang=get_lang)
    return env
