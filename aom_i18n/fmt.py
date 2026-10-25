"""Formáty čísel, súm, návratnosti a dátumov podľa jazyka. Pre sk presne pôvodné správanie
posudok_chocosuc/generator.py (medzera tisícky, desatinná čiarka, „X r" / „nevráti sa")."""
import datetime as _dt

from . import get_lang

_MONTHS = {
    "sk": ["Jan", "Feb", "Mar", "Apr", "Maj", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dec"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "de": ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"],
}
_NO_PB = {"sk": "nevráti sa", "en": "no payback", "de": "keine Amortisation"}
_YRS = {"sk": "r", "en": "yrs", "de": "J."}


def _sep(s: str) -> str:
    """Reťazec vo formáte Pythonu (1,234.5) → formát jazyka."""
    lang = get_lang()
    if lang == "en":
        return s
    if lang == "de":
        return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return s.replace(",", " ").replace(".", ",")


def num(v, d=0):
    try:
        return _sep(f"{float(v):,.{d}f}")
    except Exception:
        return "—"


def eur(v):
    try:
        return _sep(f"{float(v):,.0f}") + " €"
    except Exception:
        return "—"


def dec(x, d=1):
    """Ekvivalent ("%g" % round(x, d)).replace(".", ",") — bez oddeľovača tisícok."""
    s = "%g" % round(float(x), d)
    return s if get_lang() == "en" else s.replace(".", ",")


def pbt(v):
    """Návratnosť: None/99 (sentinel) → „nevráti sa"."""
    lang = get_lang()
    try:
        f = float(v)
    except Exception:
        return _NO_PB[lang]
    return f"{num(f, 1)} {_YRS[lang]}" if 0 < f < 99 else _NO_PB[lang]


def fmt_date(d):
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d[:10])
    if get_lang() == "en":
        return f"{d.day} {_MONTHS['en'][d.month - 1]} {d.year}"
    return d.strftime("%d.%m.%Y")


def months():
    return list(_MONTHS[get_lang()])


_VOLT = {
    "en": {"VN": "MV (medium voltage)", "NN": "LV (low voltage)", "VVN": "HV (high voltage)"},
    "de": {"VN": "MS (Mittelspannung)", "NN": "NS (Niederspannung)", "VVN": "HS (Hochspannung)"},
}


def volt(x):
    """Napäťová hladina VN/NN/VVN → en/de popis; sk a iné hodnoty bez zmeny."""
    return _VOLT.get(get_lang(), {}).get(str(x or "").strip().upper(), x)
