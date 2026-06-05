# -*- coding: utf-8 -*-
"""Generátor dokumentov pre Správu trafostaníc (DOCX).
Vstup: dict `ts` (transformer_stations row vrátane tech_details) + voliteľne contract/inspection.
Fáza 1: Preberací protokol + Zmluva. Ďalej: MPP, Revízna správa."""
from io import BytesIO
from datetime import date
from docx import Document
from docx.shared import Pt, Mm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

ENERGO = {
    "nazov": "Energovision s.r.o.",
    "sidlo": "Lamačská cesta 1738/111, 841 03 Bratislava",
    "office": "Tomášikova 19, 821 02 Bratislava",
    "ico": "53 036 280", "dic": "2121238526", "icdph": "SK2121238526",
    "orsr": "OR OS Bratislava I, odd: Sro, vložka č. 158744/B",
    "tel": "+421 948 302 137", "email": "info@energovision.sk",
    "banka": "Tatra banka, a.s.", "iban": "SK48 1100 0000 0029 4708 4971", "swift": "TATRSKBX",
}
GREEN = RGBColor(0x1B, 0x5E, 0x3F)


def _h(doc, text, size=14, color=GREEN, align=WD_ALIGN_PARAGRAPH.LEFT, bold=True, after=6):
    p = doc.add_paragraph(); p.alignment = align
    r = p.add_run(text); r.bold = bold; r.font.size = Pt(size)
    if color: r.font.color.rgb = color
    p.paragraph_format.space_after = Pt(after)
    return p


def _kv_table(doc, rows, widths=(60, 40)):
    t = doc.add_table(rows=0, cols=2); t.style = "Table Grid"
    for k, v in rows:
        c = t.add_row().cells
        c[0].text = str(k); c[1].text = "" if v is None else str(v)
        for run in c[0].paragraphs[0].runs: run.bold = True
    return t


def _two_col(doc, left_title, left_lines, right_title, right_lines):
    t = doc.add_table(rows=1, cols=2); t.style = "Table Grid"
    for cell, title, lines in ((t.rows[0].cells[0], left_title, left_lines),
                               (t.rows[0].cells[1], right_title, right_lines)):
        p = cell.paragraphs[0]; r = p.add_run(title); r.bold = True; r.font.color.rgb = GREEN
        for ln in lines:
            cp = cell.add_paragraph(ln); cp.paragraph_format.space_after = Pt(0)
    return t


# ============================================================
# 1) PREBERACÍ PROTOKOL
# ============================================================
def generate_preberaci_protokol(ts: dict) -> bytes:
    td = ts.get("tech_details") or {}
    prev = td.get("prevadzkovatel") or {}
    doc = Document()
    _h(doc, "PREBERACÍ PROTOKOL", size=18, align=WD_ALIGN_PARAGRAPH.CENTER)
    p = doc.add_paragraph("Týmto preberacím protokolom preberáme uvedenú transformátorovú stanicu "
                          "do našej správy na základe servisnej zmluvy.")
    p.paragraph_format.space_after = Pt(12)

    _two_col(doc,
        "Dodávateľ – zhotoviteľ:",
        [ENERGO["nazov"], ENERGO["sidlo"], f"IČO: {ENERGO['ico']}", f"DIČ: {ENERGO['dic']}",
         f"IČ DPH: {ENERGO['icdph']}", ENERGO["orsr"]],
        "Doručovacia adresa / kontakt:",
        [ENERGO["nazov"], ENERGO["office"], f"tel.: {ENERGO['tel']}", ENERGO["email"],
         f"{ENERGO['banka']}  SWIFT: {ENERGO['swift']}", f"IBAN: {ENERGO['iban']}"])
    doc.add_paragraph()

    _h(doc, "Objednávateľ:", size=12)
    _kv_table(doc, [
        ("Názov spoločnosti", prev.get("nazov") or ts.get("name")),
        ("IČO / DIČ", f"{prev.get('ico','—')} / {prev.get('dic','—')}"),
        ("Adresa inštalácie zariadenia", ts.get("location_address") or prev.get("sidlo")),
        ("Mesto / PSČ", f"{ts.get('location_city','')} {ts.get('location_psc','')}".strip()),
        ("Kontaktná osoba", prev.get("kontakt")),
        ("Telefón", prev.get("tel")),
    ])
    doc.add_paragraph()

    _h(doc, "Technická špecifikácia:", size=12)
    tr = td.get("transformator") or {}
    _kv_table(doc, [
        ("Označenie", ts.get("ts_code")),
        ("Názov / typ TS", ts.get("ts_type")),
        ("Umiestnenie", f"{ts.get('location_address','')}, {ts.get('location_psc','')} {ts.get('location_city','')}".strip(", ")),
        ("Výkon transformátora", f"{ts.get('rated_power_kva','—')} kVA"),
        ("Napätie VN/NN", f"{ts.get('vn_voltage_kv','—')} kV / {ts.get('nn_voltage_v','—')} V"),
        ("Počet kusov", "1"),
    ])
    doc.add_paragraph(); doc.add_paragraph()

    _two_col(doc, "Za objednávateľa:", ["", "Meno: ............................", "Dátum: ............................",
                                        "Podpis: ............................"],
             "Za Energovision s.r.o.:", ["", "Meno: Lukáš Bago", "Dátum: ............................",
                                         "Podpis: ............................"])
    b = BytesIO(); doc.save(b); return b.getvalue()
