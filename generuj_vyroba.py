# -*- coding: utf-8 -*-
"""Generovanie výrobných dokumentov rozvádzača (Atest, Zhoda, 2× Záručný list) — branded PDF."""
import os, base64
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))

def _b64img(name):
    try:
        with open(os.path.join(BASE, name), "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    except Exception:
        return ""

def _esc(x):
    return (str(x) if x not in (None, "") else "—").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _d(x):
    s = str(x or "")
    return s[:10] if len(s) >= 10 else (s or "—")

def _row(k, v):
    return f"<tr><td class='k'>{_esc(k)}</td><td>{_esc(v)}</td></tr>"

def generuj_vyroba_pdf(g: dict) -> bytes:
    from weasyprint import HTML
    header = _b64img("energovision_header.png")
    footer = _b64img("energovision_footer.png")
    head_html = f"<div class='lh'><img src='{header}'/></div><hr class='grn'/>" if header else ""
    footer_css = (f"@bottom-center {{ content:''; background:url({footer}) no-repeat center; background-size:contain; height:16mm; }}") if footer else ""

    vc=g.get("vyrobne_cislo"); naz=g.get("nazov_rozvadzaca"); akc=g.get("akcia") or g.get("cislo_zakazky")
    exp=_d(g.get("expedicia_real") or g.get("expedicia_plan"))
    firma="Energovision, s.r.o., Lamačská cesta 1738/111, 841 03 Bratislava — IČO 53036280, DIČ SK2121238526"

    checks=[("Kontrola stupňa ochrany krytov","11.2"),("Povrchové cesty a vzdušné vzdialenosti","11.3"),
    ("Ochrana pred zásahom el. prúdom a celistvosť ochranných obvodov","11.4"),("Zabudovanie vstavaných súčastí","11.5"),
    ("Vnútorné elektrické obvody a prípoje","11.6"),("Svorky na vonkajšie vodiče","11.7"),("Mechanická činnosť","11.8"),
    ("Skúška dielektrických vlastností","11.9"),("Zapojenie, prevádzková funkčnosť a funkcia","11.10")]
    checks_rows="".join(f"<tr><td>{_esc(n)}</td><td class='c'>{a}</td><td class='c'>Vyhovuje</td></tr>" for n,a in checks)

    # 1) ATEST
    atest=f"""
    <h1>OSVEDČENIE</h1>
    <p class='ctr'>o preverení kusovými skúškami NN rozvádzača (konštrukcia a funkčné vlastnosti podľa STN EN 61439-1, 2 / 2012)
    a o kvalite a kompletnosti rozvádzača</p>
    <table>
      {_row('Výrobné číslo', vc)}{_row('Názov a číslo zákazky', akc)}{_row('Názov rozvádzača', naz)}
      {_row('Mesiac a rok výroby', g.get('mesiac_rok_vyroby'))}{_row('Dátum', exp)}
      {_row('Oprávnenie číslo', g.get('opravnenie_cislo') or '220600452/06/2022/EZ/V')}
      {_row('Výrobca', firma)}
      {_row('Výkres zostavy rozvádzača', g.get('vykres_zostavy'))}{_row('Výkres elektrického zapojenia', g.get('vykres_zapojenia'))}
      {_row('Technické údaje (rozmery/krytie)', g.get('tech_udaje'))}
      {_row('Prevádzkové napätie hlavných obvodov', g.get('napatie_hlavne'))}
      {_row('Prevádzkové napätie pomocných obvodov', g.get('napatie_pomocne'))}
      {_row('Menovitý skratový prúd Ic', g.get('ic'))}{_row('Menovitý prúd In', g.get('in_prud'))}
    </table>
    <h3>Vykonané kusové skúšky / kontroly</h3>
    <table><tr><th>Názov skúšky/kontroly</th><th class='c'>Článok</th><th class='c'>Výsledok</th></tr>{checks_rows}</table>
    <p><strong>Celkový výsledok:</strong> Rozvádzač vyhovuje bezpečnostno-technickým požiadavkám STN EN 61439-1, 2 / 2012.</p>
    <p>Kontroloval: Lukáš Bago &nbsp;&nbsp; Overil/uvoľnil: Lukáš Bago</p>
    <p class='small'>Zoznam použitých prístrojov: SONEL MPI-540-PV, výrobné číslo KO1546</p>
    """

    # 2) ZHODA
    zhoda=f"""
    <h1>EÚ / ES VYHLÁSENIE O ZHODE</h1>
    <p class='small'>V zmysle § 13 zák. č. 264/1999 Z.z., nariadenia vlády č. 148/2016 Z.z. a č. 127/2016 Z.z. (EMC).</p>
    <table>
      {_row('Výrobca', 'Energovision s. r. o.')}{_row('Adresa', 'Lamačská cesta 1738/111, 841 03 Bratislava')}
      {_row('IČO / DIČ', '53036280 / SK2121238526')}{_row('Zastúpený, telefón', 'Lukáš Bago, +421 948 302 137')}
      {_row('Výrobok (rozvádzač)', naz)}{_row('Výrobné číslo', vc)}{_row('Číslo zákazky', akc)}
      {_row('Použitie', 'Napájanie elektrických zariadení podľa projektovej dokumentácie.')}
      {_row('Posúdené podľa', 'Vyhl. 508/2009 Z.z., STN EN 61439-1 ed.2 a súvisiace STN.')}
      {_row('Dátum upevnenia značky CE', exp)}
    </table>
    <p><strong>Prehlásenie:</strong> Výrobca odskúšal vlastnosti výrobku a na základe vykonaných skúšok vyhlasuje, že
    zariadenie spĺňa požiadavky bezpečnosti technických zariadení.</p>
    <p>Vyhlasovateľ: Energovision s. r. o. — Lukáš Bago</p>
    """

    # 3) ZÁRUČNÝ LIST — ROZVÁDZAČ
    zr=f"""
    <h1>ZÁRUČNÝ LIST — prístrojová náplň rozvádzača</h1>
    <table>
      {_row('Dodávateľ', firma)}{_row('Zastúpený, telefón', 'Lukáš Bago, +421 948 302 137')}
      {_row('Výrobok (rozvádzač)', naz)}{_row('Výrobné číslo', vc)}{_row('Číslo zákazky', akc)}
    </table>
    <h3>Záruka a záručné podmienky</h3>
    <ol>
      <li>Záručná lehota na prístrojovú náplň rozvádzača je <strong>24 mesiacov</strong> odo dňa prevzatia rozvádzača odberateľom.</li>
      <li>Dodávateľ zodpovedá za vlastnosti podľa technických noriem a za kompletnosť výrobku.</li>
      <li>Dodávateľ zodpovedá za chyby zistené v záručnej lehote a včas písomne reklamované.</li>
      <li>Pri reklamácii je potrebné predložiť tento záručný list.</li>
      <li>Záruka sa nevzťahuje na poškodenie spôsobené zlým skladovaním, dopravou, neodborným zásahom alebo neodvrátiteľnými udalosťami.</li>
    </ol>
    <p>Dňa: {exp} &nbsp;&nbsp;&nbsp; ................................................ (podpis a pečiatka dodávateľa)</p>
    """

    # 4) ZÁRUČNÝ LIST — FVZ
    zf=f"""
    <h1>ZÁRUČNÝ LIST — Fotovoltické zariadenie</h1>
    <table>
      {_row('Zhotoviteľ', firma)}{_row('Zastúpený, telefón', 'Lukáš Bago, +421 948 302 137')}
      {_row('Objednávateľ', g.get('zakaznik_nazov'))}{_row('Adresa', g.get('zakaznik_adresa'))}
      {_row('IČO', g.get('zakaznik_ico'))}{_row('Kontakt', g.get('zakaznik_kontakt'))}
      {_row('FV zariadenie / zákazka', akc)}
    </table>
    <h3>Záruka na dielo a záručné podmienky</h3>
    <ol>
      <li>Zhotoviteľ zodpovedá za odbornú a správnu inštaláciu diela a jeho kompletnosť.</li>
      <li>Záručné lehoty v zmysle Zmluvy o dielo:
        <ul><li>2 roky na funkčnosť diela</li><li>5 rokov na fotovoltický menič (striedač)</li>
        <li>15 rokov materiálová záruka na panely</li><li>25 rokov na lineárny pokles výkonu panelov</li></ul></li>
      <li>Pri reklamácii je potrebné predložiť tento záručný list spolu s protokolom o odovzdaní a prebratí diela.</li>
      <li>Zhotoviteľ nezodpovedá za poškodenie spôsobené objednávateľom alebo treťou stranou neodborným zásahom.</li>
    </ol>
    <p>Dňa: {exp} &nbsp;&nbsp;&nbsp; ................................................ (podpis a pečiatka zhotoviteľa)</p>
    """

    pages = "".join(f"<section>{head_html}{doc}</section>" for doc in [atest, zhoda, zr, zf])
    css = f"""
    @page {{ size:A4; margin:14mm 18mm 22mm 18mm; {footer_css} }}
    body {{ font-family:'Carlito','Calibri','Helvetica',sans-serif; font-size:10.5pt; color:#1a1a1a; line-height:1.45; }}
    section {{ page-break-after: always; }}
    section:last-child {{ page-break-after: auto; }}
    .lh img {{ width:100%; display:block; }}
    hr.grn {{ border:none; border-top:2.5px solid #92D050; margin:4pt 0 10pt; }}
    h1 {{ font-size:15pt; text-align:center; margin:8pt 0 6pt; color:#1C3A05; }}
    h3 {{ font-size:11pt; margin:10pt 0 4pt; color:#2E5008; }}
    p {{ margin:4pt 0; }} p.ctr {{ text-align:center; }} .small {{ font-size:9pt; color:#555; }}
    table {{ border-collapse:collapse; width:100%; margin:6pt 0; }}
    td,th {{ border:0.5pt solid #ccc; padding:4pt 6pt; vertical-align:top; }}
    th {{ background:#EAF3DC; }} td.k {{ width:38%; color:#555; }} td.c, th.c {{ text-align:center; }}
    ol,ul {{ margin:4pt 0 4pt 16pt; }} li {{ margin:2pt 0; }}
    """
    html = f"<!DOCTYPE html><html lang='sk'><head><meta charset='utf-8'><style>{css}</style></head><body>{pages}</body></html>"
    return HTML(string=html, base_url=BASE).write_pdf()


def _qr_data_uri(url: str) -> str:
    try:
        import segno, io
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="png", scale=4, border=2)
        import base64 as _b
        return "data:image/png;base64," + _b.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def generuj_fat_protokol(g: dict, body: list) -> bytes:
    """FAT protokol PDF z vyplnených bodov vyroba_fat."""
    from weasyprint import HTML
    header = _b64img("energovision_header.png"); footer = _b64img("energovision_footer.png")
    head_html = f"<div class='lh'><img src='{header}'/></div><hr class='grn'/>" if header else ""
    footer_css = (f"@bottom-center {{ content:''; background:url({footer}) no-repeat center; background-size:contain; height:16mm; }}") if footer else ""
    ST = {"ok": ("✓ OK", "#16a34a"), "chyba": ("✗ Chyba", "#dc2626"), "nerelevantne": ("— N/A", "#94a3b8")}
    rows = ""
    for b in body:
        s = ST.get(b.get("stav") or "nerelevantne", ST["nerelevantne"])
        rows += (f"<tr><td>{_esc(b.get('bod'))}</td>"
                 f"<td class='c' style='color:{s[1]};font-weight:700'>{s[0]}</td>"
                 f"<td class='c'>{_esc(b.get('hodnota'))}</td><td>{_esc(b.get('poznamka'))}</td>"
                 f"<td class='c'>{_esc(b.get('zodpovedny'))}</td></tr>")
    schv = (g.get("fat_stav") == "schvaleny")
    verdikt = ("ROZVÁDZAČ VYHOVUJE — FAT SCHVÁLENÝ" if schv else "FAT NEUKONČENÝ / NESCHVÁLENÝ")
    vcol = "#16a34a" if schv else "#d97706"
    body_html = f"""
    <h1>FAT PROTOKOL — výstupná kontrola rozvádzača</h1>
    <table>
      {_row('Výrobné číslo', g.get('vyrobne_cislo'))}{_row('Názov / typ', g.get('typ_rozvadzaca') or g.get('nazov_rozvadzaca'))}
      {_row('Zákazka', g.get('akcia'))}{_row('Zákazník', g.get('zakaznik_nazov'))}
      {_row('Výkon', g.get('vykon'))}{_row('Napätie / Ic / In', (g.get('napatie_hlavne') or '') + ' · ' + (g.get('ic') or '') + ' · ' + (g.get('in_prud') or ''))}
    </table>
    <h3>Kontrolné body</h3>
    <table><tr><th>Bod</th><th class='c'>Stav</th><th class='c'>Meranie</th><th>Poznámka</th><th class='c'>Zodp.</th></tr>{rows}</table>
    <p style='margin-top:10pt;font-weight:700;color:{vcol};font-size:12pt'>{verdikt}</p>
    <p>Kontroloval / schválil: {_esc(g.get('zodpovedna_osoba') or 'Tinák Ondrej')} &nbsp;&nbsp; Dátum: {_d(g.get('fat_datum'))}</p>
    <p class='small'>Schválenie FAT vykonáva výhradne zodpovedná osoba (nie AI).</p>
    """
    css = f"""@page {{ size:A4; margin:14mm 18mm 22mm 18mm; {footer_css} }}
    body {{ font-family:'Carlito','Calibri',sans-serif; font-size:10.5pt; color:#1a1a1a; }}
    .lh img {{ width:100%; }} hr.grn {{ border:none; border-top:2.5px solid #92D050; margin:4pt 0 10pt; }}
    h1 {{ font-size:15pt; text-align:center; color:#1C3A05; }} h3 {{ font-size:11pt; color:#2E5008; margin-top:10pt; }}
    table {{ border-collapse:collapse; width:100%; margin:6pt 0; }} td,th {{ border:.5pt solid #ccc; padding:4pt 6pt; }}
    th {{ background:#EAF3DC; }} td.k {{ width:30%; color:#555; }} .c {{ text-align:center; }} .small {{ font-size:9pt; color:#555; }}"""
    html = f"<!DOCTYPE html><html lang='sk'><head><meta charset='utf-8'><style>{css}</style></head><body>{head_html}{body_html}</body></html>"
    return HTML(string=html, base_url=BASE).write_pdf()


def generuj_stitok(g: dict, qr_url: str) -> bytes:
    """Malý štítok 30x40mm (š x v) s QR na nálepku."""
    from weasyprint import HTML
    qr = _qr_data_uri(qr_url)
    html = f"""<!DOCTYPE html><html lang='sk'><head><meta charset='utf-8'><style>
    @page {{ size:30mm 40mm; margin:0; }}
    * {{ box-sizing:border-box; }}
    body {{ font-family:'Carlito','Calibri',sans-serif; color:#0f172a; margin:0; }}
    .box {{ width:30mm; height:40mm; padding:1.6mm; border:0.4mm solid #1C3A05; border-radius:1.5mm; text-align:center; }}
    .br {{ color:#2E5008; font-weight:800; font-size:5pt; letter-spacing:.2pt; }}
    .vc {{ font-weight:800; font-size:9pt; margin:.4mm 0 .8mm; }}
    .qr img {{ width:18mm; height:18mm; }}
    .kv {{ font-size:4.6pt; line-height:1.25; color:#334155; margin-top:.6mm; }}
    .kv b {{ color:#0f172a; }}
    </style></head><body><div class='box'>
    <div class='br'>ENERGOVISION</div>
    <div class='vc'>{_esc(g.get('vyrobne_cislo'))}</div>
    <div class='qr'>{('<img src="'+qr+'"/>' if qr else '')}</div>
    <div class='kv'><b>{_esc(g.get('vykon') or g.get('in_prud'))}</b> · {_esc(g.get('tech_udaje'))}<br>{_esc(g.get('mesiac_rok_vyroby'))}</div>
    </div></body></html>"""
    return HTML(string=html, base_url=BASE).write_pdf()
