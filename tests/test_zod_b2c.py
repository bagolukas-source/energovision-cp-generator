"""ZoD B2C — vyplnenie šablóny templates_zmluvy/Zmluva_o_dielo_template.docx (verzia 2026-10).
Beží bez pytestu: `python3 tests/test_zod_b2c.py` (treba python-docx, requests, openpyxl).

Chráni: 12 pozičných XXX + dátum, jeden variant platieb podľa payment_terms (predtým sa platobné
podmienky nikdy neprepísali — každá zmluva bola 60/40), záruky na panely podľa typu panela, cena „s DPH".
"""
import os
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from docx import Document  # noqa: E402
import generuj_dokumenty as gd  # noqa: E402

BASE = dict(meno_priezvisko="Ján Testovací", adresa="Hlavná 1, 811 01 Bratislava", telefon="+421900000000",
            email="test@example.sk", vykon_kwp=10.7, cislo_cp="LEAD-2026-0001-xyz-A", datum_cp="02.10.2026",
            miesto_vykonu="Hlavná 1, 811 01 Bratislava", cena_eur=8725.26, datum_dnes="09.10.2026",
            zaruka_panely_produkt=25, zaruka_panely_linear=30)
OCAKAVANE = {
    "60_30_10": ["60%", "30%", "10%"],
    "60_40": ["60%", "40%"],
    "50_50": ["50%", "50%"],
    "30_70": ["30%", "70%"],
}
fails = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


with tempfile.TemporaryDirectory() as tmp:
    for pt, perc in OCAKAVANE.items():
        out = os.path.join(tmp, f"zmluva_{pt}.docx")
        gd.naplnif_zmluvu({**BASE, "payment_terms": pt}, out)
        xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
        riadky = [p.text for p in Document(out).paragraphs]
        text = "\n".join(riadky)
        platby = [r for r in riadky if re.match(r"^\s*\d+\s*%\s*-", r)]
        check(f"{pt}: žiadne XXX / XX.XX", "XXX" not in xml and "XX.XX" not in xml)
        check(f"{pt}: jeden variant platieb", [r.split(" ")[0] for r in platby] == perc, str(platby))
        check(f"{pt}: bez nadpisov Variant A/B", "Variant A:" not in text and "Variant B:" not in text)
        if pt == "60_30_10":
            check("cena s DPH", "8 725,26 EUR s DPH" in text)
            check("slovom", "osemtisícsedemstodvadsaťpäť Eur a dvadsaťšesť centov" in text)
            check("číslo a dátum CP", "LEAD-2026-0001-xyz-A zo dňa 02.10.2026" in text)
            check("záruka panely podľa typu", "25 rokov produktová záruka na fotovoltické panely" in text
                  and "Minimálne 30 rokov lineárna výkonová záruka" in text)
            check("záruka menič 10 rokov", "10 rokov záruka na fotovoltický menič" in text)
            check("lehota 4 mesiace", "Najneskôr do 4 mesiacov od uhradenia" in text)
            check("dátum podpisu", "V Bratislave, dňa 09.10.2026" in text)
            check("podpis objednávateľa", any(r.endswith("Ján Testovací") and "Lukáš Bago" in r for r in riadky))

if fails:
    print(f"\n{len(fails)} FAIL")
    sys.exit(1)
print("\nvšetko OK")
