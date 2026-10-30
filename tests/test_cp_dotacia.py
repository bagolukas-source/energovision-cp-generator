"""Ponuka B2C neodpočítava dotáciu: Zelená domácnostiam II ju vypláca až po inštalácii a schválení žiadosti.
Beží bez pytestu: `python3 tests/test_cp_dotacia.py` (treba openpyxl).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import generate_cp as gc  # noqa: E402

LEAD = {"vykon_kwp": 8.1, "rocna_spotreba_kwh": 6000, "dotacia": True}
fails = []


def check(name, cond, detail=""):
    print(("OK   " if cond else "FAIL ") + name, detail)
    if not cond:
        fails.append(name)


cennik = gc.load_cennik()
konfig = gc.vyrataj_konfig(LEAD, cennik)
ceny = gc.vyrataj_ceny(konfig, LEAD)
nav = gc.vyrataj_navratnost(konfig, ceny, LEAD)
check("dotácia sa neodpočíta", ceny["dotacia"] == 0)
check("konečná cena = cena s DPH", ceny["cena_finalna"] == ceny["cena_s_dph"])
check("návratnosť z plnej ceny", abs(nav["navratnost_rokov"] - ceny["cena_s_dph"] / nav["rocne_uspora_eur"]) < 1e-9)
check("poznámka je datovaná", "Stav k " in gc.DOTACIA_POZNAMKA and "1 000 €" in gc.DOTACIA_POZNAMKA)

tpl = open(os.path.join(ROOT, "cp_template.html"), encoding="utf8").read()
for zakazane in ("po dotácii", "Pevná cena", "Certifikovaný zhotoviteľ SIEA", "dotacia_eur"):
    check(f"šablóna bez „{zakazane}“", zakazane not in tpl)
check("šablóna nesie poznámku", "{{ dotacia_poznamka }}" in tpl)

sys.exit(1 if fails else 0)
