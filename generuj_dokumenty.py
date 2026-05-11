"""
Generuj dokumenty pre vyhrate leady — Zmluva, Splnomocnenie, GDPR suhlas, Dotaznik.

Templaty su v templates_zmluvy/. Pouzivaju Word docx ako vzor.
- Zmluva o dielo: "XXX" placeholdery (12x) nahradime postupne
- Splnomocnenie: pole "Meno a priezvisko ___", "Cislo OP ___" etc — najdeme po riadkoch
- GDPR: rovnako
- Dotaznik: xlsx s prazdnymi bunkami pre kazde pole

Vystup: bytes (docx/xlsx) — Make uploadne do Dropbox + Notion.
"""
import re
import shutil
import zipfile
import tempfile
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from docx import Document

TEMPLATES_DIR = Path(__file__).parent / "templates_zmluvy"


def _slovne_eur(amount_eur):
    """Konvertuj cislo na slovenske slova. 12345.67 -> 'dvanasttisicstosorokpat eur a sestdesiatsedem centov'"""
    # Zjednodusena verzia — vrati formatovany retazec
    eur = int(amount_eur)
    cents = round((amount_eur - eur) * 100)
    return f"{eur} eur a {cents} centov"


def _pol_text(items):
    """Pripoj zoznam stringov ako Word text inline (bez formatting). Pouzite pri tvorbe novych runov."""
    return "\n".join(s for s in items if s)


# === ZMLUVA O DIELO ===
def naplnif_zmluvu(lead_data, output_path):
    """
    Naplni Zmluvu o dielo z templatu. lead_data ma:
    - meno_priezvisko, adresa, telefon, email
    - vykon_kwp, cislo_cp, datum_cp, miesto_vykonu
    - cena_eur (bez DPH), cena_slovom

    XXX placeholdery v poradi:
    1. meno_priezvisko (Objednavatel: XXX)
    2. adresa (Adresa: XXX)
    3. telefon (Telefon: XXX)
    4. email (E-mail: XXX)
    5. vykon_kwp (s vykonom XXX kWp)
    6. cislo_cp (Cenovej ponuky dodavatela XXX)
    7. datum_cp (zo dna XXX)
    8. miesto_vykonu (Miesto vykonu: XXX)
    9. cena_eur (XXX EUR + DPH)
    10. cena_slovom (Slovom: XXX Eur ...)
    11. cena_centov_slovom (... a XXX centov)
    12. (mozno este 1 zostatkove XXX — preskocim)
    """
    template = TEMPLATES_DIR / "Zmluva_o_dielo_template.docx"
    shutil.copy(template, output_path)

    # Otvor document.xml a nahrad XXX postupne
    with zipfile.ZipFile(output_path, 'r') as z:
        members = {n: z.read(n) for n in z.namelist()}
    xml = members['word/document.xml'].decode('utf-8')

    # Cena slovom split
    eur = int(lead_data.get('cena_eur', 0))
    cents = round((lead_data.get('cena_eur', 0) - eur) * 100)

    nahrady = [
        lead_data.get('meno_priezvisko', ''),         # 1
        lead_data.get('adresa', ''),                  # 2
        lead_data.get('telefon', ''),                 # 3
        lead_data.get('email', ''),                   # 4
        f"{lead_data.get('vykon_kwp', 0):.2f}",       # 5
        lead_data.get('cislo_cp', ''),                # 6
        lead_data.get('datum_cp', ''),                # 7
        lead_data.get('miesto_vykonu', ''),           # 8
        f"{lead_data.get('cena_eur', 0):,.2f}".replace(",", " "),  # 9
        f"{eur}",                                      # 10 — cena slovom
        f"{cents}",                                    # 11 — centov
    ]

    # Nahrad postupne kazdu instanciu "XXX" v document.xml
    counter = [0]
    def repl(m):
        idx = counter[0]
        counter[0] += 1
        if idx < len(nahrady):
            # Word XML escape
            val = nahrady[idx].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return val
        return "XXX"  # ak je viac XXX nez nahrad, ponechaj
    new_xml = re.sub(r'XXX', repl, xml)

    members['word/document.xml'] = new_xml.encode('utf-8')
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)

    return output_path


# === SPLNOMOCNENIE ===
def naplnif_splnomocnenie(lead_data, output_path):
    """
    Splnomocnenie ma polia:
    - Meno a priezvisko ___
    - Cislo OP ___
    - Datum narodenia ___
    - Bydlisko ___ (2 riadky)
    - V Bratislave, dna XX.XX.2024 — datum

    Pouzijeme python-docx pre paragraph-level iteration.
    """
    template = TEMPLATES_DIR / "Splnomocnenie_template.docx"
    doc = Document(str(template))

    meno = lead_data.get('meno_priezvisko', '')
    cislo_op = lead_data.get('cislo_op', '')
    datum_narodenia = lead_data.get('datum_narodenia', '')
    bydlisko = lead_data.get('trvale_bydlisko') or lead_data.get('adresa', '')
    datum_dnes = lead_data.get('datum_dnes', '')

    # Iteruj paragraphs a najdi hodnoty po stitkoch
    # Mapovanie: ak paragraph obsahuje "Meno a priezvisko", nasleduje text s ___
    for para in doc.paragraphs:
        text = para.text
        # Replace _____ (variabilna dlzka podciarknikov) podla stitku v PREDOSLOM paragrafe
        # Jednoduche: ak para obsahuje stitok aj ___, replace inline
        if "Meno a priezvisko" in text and "___" in text:
            _replace_underscores_in_para(para, meno)
        elif "Číslo OP" in text and "___" in text:
            _replace_underscores_in_para(para, cislo_op)
        elif "Dátum narodenia" in text and "___" in text:
            _replace_underscores_in_para(para, datum_narodenia)
        elif "Bydlisko" in text and "___" in text:
            _replace_underscores_in_para(para, bydlisko)

    # Datum: "V Bratislave, dna XX. XX .2024" -> nahradime XX. XX .2024 datumom
    # Forma datumu: "10.05.2026" -> "10. 05 . 2026"
    if datum_dnes:
        for para in doc.paragraphs:
            if "V Bratislave" in para.text and "202" in para.text:
                # Replace celu vetu
                full = "V Bratislave, dňa " + datum_dnes
                # Clear all runs and write new text
                for run in para.runs:
                    run.text = ""
                if para.runs:
                    para.runs[0].text = full

    doc.save(str(output_path))
    return output_path


def _replace_underscores_in_para(para, value):
    """Nahrad sekvenciu podciarknikov (___...) v paragrafe za hodnotu."""
    # Combinuj run.text a hladaj ___+ pattern
    full_text = para.text
    # Najdi prvy ___+ sekvenciu
    m = re.search(r'_{3,}', full_text)
    if not m:
        return
    new_text = full_text[:m.start()] + str(value) + full_text[m.end():]
    # Clear runs a nastav prvy run na new_text
    for run in para.runs:
        run.text = ""
    if para.runs:
        para.runs[0].text = new_text


# === GDPR SUHLAS ===
def naplnif_gdpr(lead_data, output_path):
    """GDPR suhlas — Meno a priezvisko, Datum narodenia, Datum podpisu (V Bratislave dna XX)"""
    template = TEMPLATES_DIR / "GDPR_suhlas_template.docx"
    doc = Document(str(template))

    meno = lead_data.get('meno_priezvisko', '')
    datum_narodenia = lead_data.get('datum_narodenia', '')
    datum_dnes = lead_data.get('datum_dnes', '')

    for para in doc.paragraphs:
        text = para.text
        if "Meno a priezvisko" in text and "___" in text:
            _replace_underscores_in_para(para, meno)
        elif "Dátum narodenia" in text and "___" in text:
            _replace_underscores_in_para(para, datum_narodenia)

    if datum_dnes:
        for para in doc.paragraphs:
            if "V Bratislave" in para.text:
                full = "V Bratislave, dňa " + datum_dnes
                for run in para.runs:
                    run.text = ""
                if para.runs:
                    para.runs[0].text = full

    doc.save(str(output_path))
    return output_path


# === DOTAZNIK XLSX ===
def naplnif_dotaznik(lead_data, output_path):
    """
    Dotaznik xlsx — najde bunky podla stitkov v 1. stlpci a vyplni hodnoty do nasledujucej.
    Stitky: Meno Priezvisko titul, Datum narodenia, Cislo OP, Trvale bydlisko, atd.
    """
    template = TEMPLATES_DIR / "Dotaznik_template.xlsx"
    shutil.copy(template, output_path)
    wb = load_workbook(output_path)
    ws = wb.active

    # Mapovanie stitkov na lead_data klúče
    stitky_mapping = {
        'Meno, Priezvisko, titul': 'meno_priezvisko',
        'Meno a priezvisko': 'meno_priezvisko',
        'Tel. Kontakt': 'telefon',
        'Telefón': 'telefon',
        'Emailový kontakt': 'email',
        'Email': 'email',
        'E-mail': 'email',
        'Mesto': 'mesto',
        'Ulica, Číslo': 'ulica_cislo',
        'PSČ': 'psc',
        'IBAN': 'iban',
        'Banka': 'banka',
        'Číslo OP': 'cislo_op',
        'Dátum narodenia': 'datum_narodenia',
        'Trvalé bydlisko žiadateľa': 'trvale_bydlisko',
        'Korešpondenčná adresa': 'adresa',
        'EIC odberného miesta': 'eic',
        'Číslo obchodného partnera': 'cislo_obch_partnera',
        'Predpokladaná ročná spotreba odberného miesta': 'spotreba',
        'Hodnota hlavného ističa pred elektromerom-meraním': 'hlavny_istic',
        'Predajca energií': 'predajca_energii',
        'Katastrálne územie': 'katastralne_uzemie',
        'Parcelné čísla pozemkov, na ktorých bude umiestená FVE': 'parcelne_cisla',
        'Parcelné čísla pozemkov, na ktorých bude umiestená FVZ': 'parcelne_cisla',
        'Adresa odberného miesta, na ktorom bude pripojený lokálny zdroj (FVE)': 'adresa_om',
    }

    # Iteruj cez vsetky bunky, najdi stitky a vypln susedne
    for row in ws.iter_rows():
        for cell in row:
            if cell.value and isinstance(cell.value, str):
                txt = cell.value.strip()
                # Najdi exact alebo partial match
                for stitok, key in stitky_mapping.items():
                    if stitok == txt or stitok in txt:
                        # Hodnota ide do bunky vpravo (cell.column + 1) alebo nizsie
                        try:
                            target = ws.cell(row=cell.row, column=cell.column + 1)
                            if not target.value:  # ak je prazdna
                                target.value = lead_data.get(key, '')
                        except Exception:
                            pass
                        break

    wb.save(output_path)
    return output_path


# === REVIZNA SPRAVA ===
def naplnif_reviznu_spravu(lead_data, output_path):
    """
    Revizna sprava — naplni meno zakaznika, adresu, vykon FVE, baterii.
    Originalny template ma BYTTERM data — najdeme & nahradime.
    """
    template = TEMPLATES_DIR / "Revizna_sprava_template.docx"
    shutil.copy(template, output_path)

    with zipfile.ZipFile(output_path, 'r') as z:
        members = {n: z.read(n) for n in z.namelist()}
    xml = members['word/document.xml'].decode('utf-8')

    # Zoznam nahradzaccich texto v xml. Hladame BYTTERM-specifik a nahradime za nase data.
    nahrady = [
        ("BYTTERM a.s.", lead_data.get('meno_priezvisko', '')),
        ("BYTTERM a.s .", lead_data.get('meno_priezvisko', '')),
        ("Saleziánska 4", lead_data.get('adresa', '')),
        ("01077 Žilina", lead_data.get('psc_mesto', '')),
        # Vykon a baterii
        ("25 k W + Batériové úložisko 20,7 kWh", f"{lead_data.get('vykon_kwp', 0):.2f} kWp + Batériové úložisko {lead_data.get('bateria_kwh', 0):.2f} kWh"),
        ("25 k W", f"{lead_data.get('vykon_kwp', 0):.2f} kWp"),
        ("20,7 kWh", f"{lead_data.get('bateria_kwh', 0):.2f} kWh"),
        # Datumy
        ("22 .0 4 .2026", lead_data.get('datum_zahajenia', '')),
        ("22 .0 4 .202 6", lead_data.get('datum_zahajenia', '')),
        ("23 .0 4 .202 6", lead_data.get('datum_odovzdania', '')),
        # Objekt
        ("Budova údržby a garáže BYTTERM a.s .", "Rodinný dom"),
        ("Budova údržby a garáže BYTTERM a.s.", "Rodinný dom"),
    ]
    for old, new in nahrady:
        if old in xml:
            xml = xml.replace(old, new.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    members['word/document.xml'] = xml.encode('utf-8')
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)

    return output_path


# === PROTOKOL ODOVZDANIA ===
def naplnif_protokol_odovzdania(lead_data, output_path):
    """
    Protokol o odovzdani a prebrati diela.
    Cislo protokolu, meno, telefon, miesto montaze, polozky (panely/menic/baterii/konstrukcia ks)
    """
    template = TEMPLATES_DIR / "Protokol_odovzdania_template.docx"
    doc = Document(str(template))

    meno_priezvisko = lead_data.get('meno_priezvisko', '')
    parts = meno_priezvisko.rsplit(" ", 1)
    meno = parts[0] if len(parts) >= 2 else meno_priezvisko
    priezvisko = parts[1] if len(parts) >= 2 else ""

    telefon = lead_data.get('telefon', '')
    adresa = lead_data.get('adresa', '')
    cislo_protokolu = lead_data.get('cislo_protokolu', lead_data.get('ev_id', ''))

    pocet_panelov = lead_data.get('pocet_panelov', 0)
    pocet_menic = 1
    pocet_baterii = lead_data.get('pocet_baterii', 0)
    vykon_kwp = lead_data.get('vykon_kwp', 0)

    # Iteruj paragraphs a tabuľky, nahrad podciarkne
    for para in doc.paragraphs:
        text = para.text
        if "PROTOKOL" in text and "...." in text and not "OBJEDNÁVATEĽ" in text:
            _replace_underscores_in_para(para, cislo_protokolu)
        elif "Meno:" in text and "..." in text:
            _replace_underscores_in_para(para, meno)
        elif "Priezvisko:" in text and "..." in text:
            _replace_underscores_in_para(para, priezvisko)
        elif "Tel" in text and "..." in text:
            _replace_underscores_in_para(para, telefon)
        elif "Miesto montáže" in text and "..." in text:
            _replace_underscores_in_para(para, adresa)

    # Pre tabuľky (panely ks atď.) — najdi rows
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                txt = cell.text.strip()
                if "Panely" == txt or "Panely ks" == txt:
                    # Hodnota v nasledujucej bunke
                    pass  # zatial nemenime tabulkove hodnoty — uvedieme do prilohy
        # Pridaj poznamku do tabulky (jednoduchu)

    # Pridaj zdola popis konfig
    doc.add_paragraph()
    p = doc.add_paragraph()
    r = p.add_run("Konfigurácia FVZ:")
    r.bold = True
    doc.add_paragraph(f"Výkon FVE: {vykon_kwp:.2f} kWp")
    doc.add_paragraph(f"Panely: {pocet_panelov} ks (LONGi 535 Wp)")
    doc.add_paragraph(f"Menič: {pocet_menic} ks ({lead_data.get('menic', 'Solinteg MHT-10K-25')})")
    if pocet_baterii > 0:
        doc.add_paragraph(f"Batérie: {pocet_baterii} ks ({lead_data.get('bateria_typ', '')})")
    doc.add_paragraph(f"Konštrukcia: 1 sada ({lead_data.get('konstrukcia', 'Škridla')})")
    doc.add_paragraph(f"Rozvádzač ENERGOVISION: 1 ks")
    if lead_data.get('ma_wallbox'):
        doc.add_paragraph(f"Wallbox: 1 ks ({lead_data.get('wallbox_typ', '')})")

    # Datum odovzdania
    doc.add_paragraph()
    doc.add_paragraph(f"Dátum odovzdania: {lead_data.get('datum_odovzdania', '')}")

    doc.save(str(output_path))
    return output_path


# === DODATOK K ZMLUVE ===
def naplnif_dodatok(lead_data, output_path):
    """Dodatok ku zmluve o dielo — upravuje cenu napriklad pri zmene konfig."""
    template = TEMPLATES_DIR / "Dodatok_zmluvy_template.docx"
    shutil.copy(template, output_path)

    with zipfile.ZipFile(output_path, 'r') as z:
        members = {n: z.read(n) for n in z.namelist()}
    xml = members['word/document.xml'].decode('utf-8')

    nahrady = [
        ("Xxxxxx Xxxxxxxxxx", lead_data.get('meno_priezvisko', '')),
        ("Xxxxxxxxxxxxxxxxxxxxxxxxx", lead_data.get('adresa', '')),
        ("XXXX XXX XXX", lead_data.get('telefon', '')),
        ("xxxxxxxxxxxx @gmail.com", lead_data.get('email', '')),
        ("00 . 0 0 .202 3", lead_data.get('datum_povodnej_zmluvy', '')),
        ("0 4 .0 6 .202 2", lead_data.get('datum_povodnej_zmluvy', '')),
        ("7 599,56 €", f"{lead_data.get('cena_eur', 0):.2f} €".replace(".", ",")),
    ]
    for old, new in nahrady:
        if old in xml:
            xml = xml.replace(old, new.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    members['word/document.xml'] = xml.encode('utf-8')
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return output_path


# === BALIK REALIZACIE ===
def vygeneruj_realizacne_dokumenty(lead_data, out_dir):
    """Po realizacii vyrobi revíznu správu + preberací protokol."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    priezvisko = lead_data.get('meno_priezvisko', 'Klient').split()[-1]
    base = re.sub(r'[^A-Za-z0-9]+', '_', priezvisko).strip('_') or 'Klient'
    ev_id = lead_data.get('ev_id', 'EV-XX')

    out = {}
    out['revizia'] = naplnif_reviznu_spravu(lead_data, out_dir / f"{ev_id}_Reviznasprava_{base}.docx")
    out['protokol'] = naplnif_protokol_odovzdania(lead_data, out_dir / f"{ev_id}_Preberaciprotokol_{base}.docx")

    return out


# === ENTRY POINT ===
def vygeneruj_balik_dokumentov(lead_data, out_dir):
    """
    Vyrobi vsetky 4 dokumenty pre balik post-vyhry.
    Vrati dict {'zmluva': path, 'splnomocnenie': path, 'gdpr': path, 'dotaznik': path}.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    priezvisko = lead_data.get('meno_priezvisko', 'Klient').split()[-1]
    base = re.sub(r'[^A-Za-z0-9]+', '_', priezvisko).strip('_') or 'Klient'
    ev_id = lead_data.get('ev_id', 'EV-XX')

    out = {}
    out['zmluva'] = naplnif_zmluvu(lead_data, out_dir / f"{ev_id}_Zmluva_{base}.docx")
    out['splnomocnenie'] = naplnif_splnomocnenie(lead_data, out_dir / f"{ev_id}_Splnomocnenie_{base}.docx")
    out['gdpr'] = naplnif_gdpr(lead_data, out_dir / f"{ev_id}_GDPR_{base}.docx")
    out['dotaznik'] = naplnif_dotaznik(lead_data, out_dir / f"{ev_id}_Dotaznik_{base}.xlsx")

    return out
