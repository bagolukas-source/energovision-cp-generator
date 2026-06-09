# -*- coding: utf-8 -*-
"""Predajná prezentácia pre zákazníka — premium branded PDF deck (landscape)."""
import os, base64
BASE = os.path.dirname(os.path.abspath(__file__))

def _b64img(name):
    try:
        with open(os.path.join(BASE, name), "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    except Exception:
        return ""

def _esc(x):
    return (str(x) if x not in (None, "") else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def generuj_prezentaciu_pdf(g: dict) -> bytes:
    from weasyprint import HTML
    cust = _esc(g.get("zakaznik") or "Vážený zákazník")
    miesto = _esc(g.get("miesto") or "")
    vykon = _esc(g.get("vykon") or "")
    panely = _esc(g.get("panely") or "")
    poznamka = _esc(g.get("poznamka") or "")
    datum = _esc(g.get("datum") or "")
    disp = g.get("dispozicia_img") or ""   # data URI alebo base64
    if disp and not disp.startswith("data:"):
        disp = "data:image/png;base64," + disp
    variants = g.get("varianty") or []     # [{nazov, cena, popis}]
    kontakt = g.get("kontakt") or "Dominik Galaba · +421 917 424 564 · dominik.galaba@energovision.sk"

    vcards = ""
    for i, v in enumerate(variants[:3]):
        hl = "hl" if i == 1 else ""
        vcards += (f"<div class='vcard {hl}'><div class='vn'>{_esc(v.get('nazov'))}</div>"
                   f"<div class='vp'>{_esc(v.get('cena'))}</div>"
                   f"<div class='vd'>{_esc(v.get('popis'))}</div></div>")

    disp_block = (f"<div class='disp'><img src='{disp}'/></div>" if disp else
                  "<div class='disp ph'>Dispozícia / rozloženie panelov</div>")

    sluzby = [("☀️", "Fotovoltické elektrárne", "Návrh, dodávka a montáž FVE na kľúč."),
              ("🔌", "Trafostanice", "Údržba a servis trafostaníc."),
              ("🛡️", "Odborné revízie", "Revízie VTZ a elektro zariadení."),
              ("⚡", "Elektrotechnické práce", "Realizačné a technické činnosti v energetike.")]
    scards = "".join(f"<div class='scard'><div class='si'>{i}</div><div class='st'>{t}</div><div class='sd'>{d}</div></div>" for i, t, d in sluzby)

    benefits = [("Úspora", "Nižšie účty za elektrinu už od prvého dňa."),
                ("Dotácia", "Pomôžeme so žiadosťou o dotáciu (Zelená podnikom / domácnostiam)."),
                ("Záruky", "12 r. panely · 10 r. menič/batéria · 5 r. inštalácia."),
                ("Servis", "Vlastný servisný tím a monitoring po celú životnosť.")]
    bcards = "".join(f"<div class='bcard'><div class='bt'>{t}</div><div class='bd'>{d}</div></div>" for t, d in benefits)

    css = """
    @page { size: A4 landscape; margin: 0; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Carlito','Calibri','Helvetica',sans-serif; color: #0f172a; }
    .slide { width: 297mm; height: 210mm; page-break-after: always; position: relative; overflow: hidden; padding: 18mm 20mm; }
    .slide:last-child { page-break-after: auto; }
    .dark { background: linear-gradient(135deg,#0b1220 0%,#0d2818 58%,#0b1220 100%); color: #fff; }
    .glow { position: absolute; width: 130mm; height: 130mm; border-radius: 50%; filter: blur(60px); opacity: .28; background: #92D050; right: -40mm; top: -40mm; }
    .brand { font-size: 13pt; font-weight: 800; letter-spacing: 3pt; color: #92D050; }
    .tag { color: #cbd5e1; font-size: 10pt; margin-top: 2mm; }
    h1 { font-size: 34pt; font-weight: 800; line-height: 1.05; margin: 18mm 0 6mm; }
    h2 { font-size: 22pt; font-weight: 800; color: #1C3A05; margin-bottom: 6mm; }
    .dark h2 { color: #fff; }
    .lead { font-size: 12pt; color: #475569; max-width: 200mm; }
    .dark .lead { color: #cbd5e1; }
    .for { position: absolute; bottom: 18mm; left: 20mm; }
    .for .k { color: #92D050; font-size: 9pt; letter-spacing: 2pt; }
    .for .v { font-size: 16pt; font-weight: 700; }
    .grid4 { display: flex; gap: 8mm; margin-top: 8mm; }
    .scard { flex: 1; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4mm; padding: 7mm; }
    .si { font-size: 22pt; } .st { font-weight: 700; font-size: 12pt; margin: 3mm 0 1.5mm; } .sd { font-size: 9.5pt; color: #64748b; }
    .row { display: flex; gap: 10mm; margin-top: 6mm; }
    .disp { flex: 1.3; border-radius: 4mm; overflow: hidden; border: 1px solid #e2e8f0; background: #f1f5f9; display: flex; align-items: center; justify-content: center; min-height: 110mm; }
    .disp img { width: 100%; height: 100%; object-fit: contain; }
    .disp.ph { color: #94a3b8; font-size: 12pt; }
    .specs { flex: 1; }
    .spec { display: flex; justify-content: space-between; padding: 4mm 0; border-bottom: 1px solid #e2e8f0; font-size: 12pt; }
    .spec b { color: #1C3A05; }
    .vrow { display: flex; gap: 7mm; margin-top: 8mm; }
    .vcard { flex: 1; border: 1.5px solid #e2e8f0; border-radius: 5mm; padding: 8mm; text-align: center; }
    .vcard.hl { border-color: #92D050; box-shadow: 0 10mm 20mm -10mm rgba(146,208,80,.5); }
    .vn { font-weight: 700; font-size: 13pt; color: #1C3A05; } .vp { font-size: 24pt; font-weight: 800; margin: 4mm 0; } .vd { font-size: 9.5pt; color: #64748b; }
    .bgrid { display: flex; gap: 8mm; margin-top: 8mm; flex-wrap: wrap; }
    .bcard { flex: 1 1 40%; background: #f8fafc; border-left: 4px solid #92D050; border-radius: 3mm; padding: 6mm; }
    .bt { font-weight: 700; font-size: 12pt; color: #1C3A05; } .bd { font-size: 10pt; color: #64748b; margin-top: 1.5mm; }
    .cta { background: #92D050; color: #10220a; border-radius: 5mm; padding: 10mm; font-size: 14pt; font-weight: 700; margin-top: 10mm; }
    .foot { position: absolute; bottom: 12mm; left: 20mm; right: 20mm; font-size: 9pt; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 4mm; }
    .dark .foot { color: #64748b; border-color: #1e293b; }
    .pill { display:inline-block; background:#92D050; color:#10220a; padding:2mm 5mm; border-radius:999px; font-weight:700; font-size:11pt; }
    """

    html = f"""<!DOCTYPE html><html lang='sk'><head><meta charset='utf-8'><style>{css}</style></head><body>

    <div class='slide dark'><div class='glow'></div>
      <div class='brand'>ENERGOVISION</div><div class='tag'>Moderné energetické riešenia, ktoré hľadáte</div>
      <h1>Návrh fotovoltického<br>riešenia na mieru</h1>
      <div class='lead'>Cenová ponuka a predstavenie riešenia pripravené pre Vás.</div>
      <div class='for'><div class='k'>PRIPRAVENÉ PRE</div><div class='v'>{cust}{(' · ' + miesto) if miesto else ''}</div><div style='color:#94a3b8;font-size:10pt;margin-top:1mm'>{datum}</div></div>
    </div>

    <div class='slide'>
      <div class='brand' style='color:#2E5008'>O SPOLOČNOSTI ENERGOVISION</div>
      <h2 style='margin-top:4mm'>Komplexný partner v energetike</h2>
      <div class='lead'>Nie sme len fotovoltika — pokrývame celý životný cyklus energetických riešení, od návrhu po servis.</div>
      <div class='grid4'>{scards}</div>
      <div class='foot'>Energovision s.r.o. · IČO 53036280 · Lamačská cesta 1738/111, Bratislava · energovision.sk</div>
    </div>

    <div class='slide'>
      <div class='brand' style='color:#2E5008'>VAŠE RIEŠENIE</div>
      <h2 style='margin-top:4mm'>Rozloženie a parametre</h2>
      <div class='row'>{disp_block}
        <div class='specs'>
          <div class='spec'><span>Inštalovaný výkon</span><b>{vykon or '—'}</b></div>
          <div class='spec'><span>Počet panelov</span><b>{panely or '—'}</b></div>
          <div class='spec'><span>Miesto inštalácie</span><b>{miesto or '—'}</b></div>
          {('<div class=spec><span>Poznámka</span><b>'+poznamka+'</b></div>') if poznamka else ''}
          <div style='margin-top:8mm'><span class='pill'>Riešenie na kľúč</span></div>
        </div>
      </div>
    </div>

    <div class='slide'>
      <div class='brand' style='color:#2E5008'>CENOVÁ PONUKA</div>
      <h2 style='margin-top:4mm'>Vyberte si variant</h2>
      <div class='vrow'>{vcards if vcards else "<div class='lead'>Varianty budú doplnené.</div>"}</div>
      <div class='foot'>Ceny sú orientačné s DPH; finálna ponuka po obhliadke. Platnosť 30 dní.</div>
    </div>

    <div class='slide'>
      <div class='brand' style='color:#2E5008'>PREČO ENERGOVISION</div>
      <h2 style='margin-top:4mm'>Čo získate</h2>
      <div class='bgrid'>{bcards}</div>
    </div>

    <div class='slide dark'><div class='glow'></div>
      <div class='brand'>ĎALŠIE KROKY</div>
      <h1 style='margin-top:14mm;font-size:30pt'>Poďme to zrealizovať</h1>
      <div class='lead'>Stačí potvrdiť variant — pripravíme zmluvu, dotáciu a termín montáže.</div>
      <div class='cta'>Kontaktujte nás: {_esc(kontakt)}</div>
      <div class='foot'>Energovision s.r.o. · +421 948 302 137 · energovision.sk</div>
    </div>

    </body></html>"""
    return HTML(string=html, base_url=BASE).write_pdf()
