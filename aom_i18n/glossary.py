"""Glosár slovenských pojmov bez ekvivalentu (spec 2026-10-25, tabuľka glosára)."""
GLOSSARY = {
    "en": {
        "Zelená podnikom": "Zelená podnikom (Slovak state subsidy scheme for businesses)",
        "MRK": "maximum reserved capacity (MRK)",
        "RK": "reserved capacity (RK)",
        "ZSD / SSD / VSD": "ZSD / SSD / VSD (regional distribution system operator)",
        "ÚRSO": "ÚRSO (Slovak energy regulator)",
        "OKTE": "OKTE (Slovak electricity market operator, day-ahead spot)",
        "DPH": "VAT",
        "odberné miesto (OM)": "supply point",
        "posudok": "techno-economic assessment",
    },
    "de": {
        "Zelená podnikom": "Zelená podnikom (slowakisches staatliches Förderprogramm für Unternehmen)",
        "MRK": "maximal reservierte Leistung (MRK)",
        "RK": "reservierte Leistung (RK)",
        "ZSD / SSD / VSD": "ZSD / SSD / VSD (regionaler Verteilnetzbetreiber)",
        "ÚRSO": "ÚRSO (slowakische Regulierungsbehörde für Netzindustrien)",
        "OKTE": "OKTE (slowakischer Strommarktbetreiber, Day-Ahead-Spot)",
        "DPH": "MwSt.",
        "odberné miesto (OM)": "Abnahmestelle",
        "posudok": "technisch-wirtschaftliches Gutachten",
    },
}
_LANG_NAME = {"en": "English", "de": "German"}
_NUM = {"en": "1,234.5 and 12,345 €", "de": "1.234,5 und 12.345 €"}


def glossary_prompt(lang: str) -> str:
    """Doplnok system promptu pre AI. Pre sk prázdny reťazec → SK prompt bez zmeny."""
    if lang not in GLOSSARY:
        return ""
    lines = "\n".join(f"- {k} → {v}" for k, v in GLOSSARY[lang].items())
    return (f"\n\nOUTPUT LANGUAGE: write ALL text values in {_LANG_NAME[lang]}, even though the instructions "
            f"above are in Slovak. Keep JSON keys unchanged. Slovak terms — use the full form on first use, "
            f"then the short name:\n{lines}\nNumber format: {_NUM[lang]}.")
