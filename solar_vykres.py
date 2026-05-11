"""
SolarEdge raw PDF → technický výkres A3 landscape pre projektovú dokumentáciu.

Vstup:
- SolarEdge raw PDF bytes (alebo URL)
- lead_data dict (klient, adresa, parcely, vykon, panel, menic, ev_id...)

Výstup: PDF bytes (A3 landscape, technický výkres podľa STN 01 3420).

Layout:
- Hlavička hore: VÝKRES Č. 01 — ROZLOŽENIE FV PANELOV
- Hlavná plocha vľavo: hero image strechy zo SolarEdge
- Severka (vľavo hore), mierka 1:100 (vľavo dole)
- Vpravo hore: LEGENDA (panel, string, menič, rozvádzač)
- Vpravo stred: TECHNICKÉ ÚDAJE
- Vpravo dole: RAZÍTKO Energovision
- Pod hero image: TABUĽKA POLÍ (azimut, sklon, počet, string)

Render: WeasyPrint (HTML + CSS → PDF). A3 landscape 420×297 mm.
"""
import base64
import io
import logging
import re
from datetime import datetime

import fitz  # pymupdf
import requests
from weasyprint import HTML

log = logging.getLogger("solar_vykres")


# ============================================================
# 1. EXTRAHOVANIE DÁT ZO SOLAREDGE PDF
# ============================================================

def _extract_solaredge_data(pdf_bytes):
    """
    Extrahuj hero image + numerické dáta + tabuľku polí zo SolarEdge raw PDF.
    Vráti dict.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page1 = doc[0]
    text_p1 = page1.get_text()
    text_p2 = doc[1].get_text() if len(doc) > 1 else ""

    # Hero image — prvý obrázok po logu (často je to pôdorys strechy)
    images = page1.get_images(full=True)
    hero_b64 = ""
    if len(images) >= 2:
        try:
            xref = images[1][0]
            base = doc.extract_image(xref)
            hero_b64 = base64.b64encode(base["image"]).decode("ascii")
        except Exception as e:
            log.warning("[solar_vykres] hero extract failed: %s", e)

    def grab(pattern, txt, default="—"):
        m = re.search(pattern, txt)
        return m.group(1).strip() if m else default

    # Výkon, výroba, atď.
    kwp_str = grab(r"Instalovan[ýy]\s*DC\s*V[yý]kon\s*\n?\s*([\d,]+\s*kWp)", text_p1)
    ac_str = grab(r"Max\s*Dosa[zž]iteln[yý]\s*AC\s*V[yý]kon\s*\n?\s*([\d,]+\s*kW)", text_p1)
    vyroba_str = grab(r"Ro[čc]n[ií]\s*V[yý]roba\s*Energie\s*\n?\s*([\d\s ,]+\s*kWh)", text_p1)

    # Tabuľka polí z page 2 (FV PANELY ... Celkem:)
    polia = []
    if "FV PANELY" in text_p2 or "FV PANEL" in text_p2:
        section = re.split(r"FV\s*PANELY?", text_p2, 1)[-1]
        section = re.split(r"Celkem:|Celkov[áy]", section, 1)[0]
        lines = [l.strip() for l in section.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            m_kwp = re.match(r"^([\d,]+\s*kWp)$", line)
            if m_kwp and i + 2 < len(lines):
                m_az = re.match(r"^(\d+)\s*°?$", lines[i + 1])
                m_sk = re.match(r"^(\d+)\s*°?$", lines[i + 2])
                if m_az and m_sk:
                    pocet = "?"
                    for j in range(i - 1, max(-1, i - 4), -1):
                        if re.match(r"^\d+$", lines[j]):
                            pocet = lines[j]
                            break
                    polia.append({
                        "pocet": pocet,
                        "kwp": m_kwp.group(1),
                        "azimut": m_az.group(1),
                        "sklon": m_sk.group(1),
                    })

    return {
        "hero_b64": hero_b64,
        "kwp_str": kwp_str,
        "ac_str": ac_str,
        "vyroba_str": vyroba_str,
        "polia": polia,
    }


# ============================================================
# 2. HTML TEMPLATE pre A3 landscape technický výkres
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<style>
@page {{
    size: A3 landscape;
    margin: 8mm;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Helvetica', sans-serif; font-size: 9pt; color: #1a1a1a; }}

.frame {{
    position: relative;
    width: 100%;
    height: 281mm;  /* A3 landscape 297 - 2*8 margin */
    border: 1.5pt solid #1a1a1a;
    padding: 4mm;
}}

.frame-inner {{
    position: relative;
    width: 100%;
    height: 100%;
    border: 0.5pt solid #1a1a1a;
}}

/* Hlavička */
.header {{
    height: 12mm;
    background: #f5f5f5;
    border-bottom: 0.5pt solid #1a1a1a;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: bold;
    font-size: 14pt;
    text-transform: uppercase;
    letter-spacing: 0.5pt;
}}

/* Hlavná plocha */
.content {{
    position: relative;
    width: 100%;
    height: calc(100% - 12mm);
    display: flex;
}}

.left-pane {{
    width: 70%;
    height: 100%;
    position: relative;
    border-right: 0.5pt solid #1a1a1a;
}}

.right-pane {{
    width: 30%;
    height: 100%;
    display: flex;
    flex-direction: column;
}}

/* Hero image area */
.hero-area {{
    position: relative;
    width: 100%;
    height: 65%;
    background: #fafafa;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    border-bottom: 0.5pt solid #1a1a1a;
}}

.hero-area img {{
    max-width: 95%;
    max-height: 95%;
    object-fit: contain;
}}

.hero-placeholder {{
    color: #999;
    font-size: 12pt;
    text-align: center;
}}

/* Severka (vľavo hore) */
.north-rose {{
    position: absolute;
    top: 4mm;
    left: 4mm;
    width: 18mm;
    height: 22mm;
    text-align: center;
}}
.north-rose svg {{
    width: 100%;
    height: auto;
}}
.north-rose .label {{
    font-size: 8pt;
    font-weight: bold;
    margin-top: -2mm;
}}

/* Mierka (vľavo dole) */
.scale-bar {{
    position: absolute;
    bottom: 3mm;
    left: 4mm;
    width: 32mm;
}}
.scale-bar .scale-label {{
    font-size: 7pt;
    font-weight: bold;
    margin-bottom: 1mm;
}}
.scale-bar .scale-line {{
    height: 1.2mm;
    background: linear-gradient(to right,
        #1a1a1a 0%, #1a1a1a 25%,
        #fff 25%, #fff 50%,
        #1a1a1a 50%, #1a1a1a 75%,
        #fff 75%, #fff 100%);
    border: 0.5pt solid #1a1a1a;
}}
.scale-bar .scale-ticks {{
    display: flex;
    justify-content: space-between;
    font-size: 6pt;
    color: #666;
    margin-top: 0.5mm;
}}

/* Tabuľka polí (pod hero image) */
.fields-table {{
    width: 100%;
    height: 35%;
    padding: 3mm 4mm;
    overflow: hidden;
}}
.fields-table h3 {{
    font-size: 9pt;
    font-weight: bold;
    text-transform: uppercase;
    margin-bottom: 1.5mm;
    padding-bottom: 1mm;
    border-bottom: 0.3pt solid #999;
}}
.fields-table table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 8pt;
}}
.fields-table th {{
    text-align: left;
    padding: 1mm 1.5mm;
    font-weight: 600;
    color: #555;
    border-bottom: 0.3pt solid #ccc;
    font-size: 7.5pt;
}}
.fields-table td {{
    padding: 1.2mm 1.5mm;
    border-bottom: 0.2pt solid #eee;
}}
.fields-table tr.total td {{
    border-top: 0.5pt solid #1a1a1a;
    font-weight: bold;
    border-bottom: none;
}}

/* Pravý panel - legenda, technické údaje, razítko */
.panel-block {{
    padding: 3mm;
    border-bottom: 0.5pt solid #1a1a1a;
}}
.panel-block:last-child {{
    border-bottom: none;
}}
.panel-block h3 {{
    font-size: 9pt;
    font-weight: bold;
    text-transform: uppercase;
    margin-bottom: 2mm;
    padding-bottom: 1mm;
    border-bottom: 0.3pt solid #999;
}}

/* Legenda */
.legend {{
    height: 30%;
}}
.legend-item {{
    display: flex;
    align-items: center;
    margin-bottom: 1.5mm;
    font-size: 8pt;
}}
.legend-icon {{
    width: 8mm;
    height: 5mm;
    margin-right: 2mm;
    flex-shrink: 0;
    border: 0.5pt solid #1a1a1a;
}}
.legend-icon.panel {{ background: #2563eb; }}
.legend-icon.string1 {{ border: 0; height: 1.2mm; background: #dc2626; }}
.legend-icon.string2 {{
    border: 0;
    height: 1.2mm;
    background: repeating-linear-gradient(to right, #dc2626 0, #dc2626 1.5mm, transparent 1.5mm, transparent 2.5mm);
}}
.legend-icon.menic {{
    background: #e5e7eb;
    border-radius: 50%;
    width: 5mm;
    margin-left: 1.5mm;
}}
.legend-icon.roof {{
    border: 1pt dashed #1a1a1a;
    background: transparent;
}}

/* Technické údaje */
.tech-data {{
    height: 35%;
    font-size: 8pt;
}}
.tech-data table {{
    width: 100%;
    border-collapse: collapse;
}}
.tech-data td {{
    padding: 0.8mm 0;
}}
.tech-data td.label {{
    color: #555;
    font-weight: 600;
    width: 50%;
}}

/* Razítko */
.stamp {{
    height: 35%;
    background: #f5f5f5;
    border-top: 1pt solid #1a1a1a;
}}
.stamp-header {{
    padding: 2mm 3mm 1mm 3mm;
    border-bottom: 0.3pt solid #999;
}}
.stamp-company {{
    font-weight: bold;
    font-size: 10pt;
}}
.stamp-address {{
    font-size: 6.5pt;
    color: #666;
    margin-top: 0.5mm;
}}
.stamp-body {{
    padding: 1.5mm 3mm;
    font-size: 7.5pt;
}}
.stamp-row {{
    display: flex;
    margin-bottom: 0.8mm;
}}
.stamp-row .stamp-label {{
    color: #666;
    font-weight: 600;
    width: 36mm;
}}
.stamp-row .stamp-value {{
    flex: 1;
}}
.stamp-footer {{
    display: flex;
    padding: 1.5mm 3mm 2mm 3mm;
    border-top: 0.3pt solid #999;
    font-size: 7pt;
}}
.stamp-footer .stamp-person {{
    flex: 1;
    text-align: center;
}}
.stamp-footer .stamp-person .role {{
    color: #666;
    font-weight: 600;
    font-size: 6.5pt;
    text-transform: uppercase;
    margin-bottom: 0.5mm;
}}
.stamp-footer .stamp-person .name {{
    font-weight: 500;
}}

/* Vykres číslo (pod razítkom) */
.drawing-no {{
    position: absolute;
    bottom: 0;
    right: 0;
    padding: 2mm 3mm;
    font-size: 8pt;
    font-weight: bold;
    background: #1a1a1a;
    color: #fff;
}}
</style>
</head>
<body>
<div class="frame">
<div class="frame-inner">

    <!-- HLAVIČKA -->
    <div class="header">
        VÝKRES Č. 01 — Rozloženie fotovoltických panelov
    </div>

    <div class="content">

        <!-- ĽAVÝ PANEL -->
        <div class="left-pane">

            <!-- HERO IMAGE strechy -->
            <div class="hero-area">
                {hero_section}

                <!-- Severka -->
                <div class="north-rose">
                    <svg viewBox="-30 -30 60 60" xmlns="http://www.w3.org/2000/svg">
                        <circle cx="0" cy="0" r="25" fill="white" stroke="#1a1a1a" stroke-width="1"/>
                        <path d="M 0,-22 L -6,0 L 0,-10 L 6,0 Z" fill="#dc2626"/>
                        <path d="M 0,22 L -6,0 L 0,10 L 6,0 Z" fill="white" stroke="#1a1a1a" stroke-width="0.5"/>
                        <text x="0" y="-12" text-anchor="middle" font-size="10" font-weight="bold" fill="#1a1a1a">N</text>
                    </svg>
                </div>

                <!-- Mierka -->
                <div class="scale-bar">
                    <div class="scale-label">Mierka 1:100</div>
                    <div class="scale-line"></div>
                    <div class="scale-ticks">
                        <span>0</span>
                        <span>2,5m</span>
                        <span>5m</span>
                        <span>7,5m</span>
                        <span>10m</span>
                    </div>
                </div>
            </div>

            <!-- TABUĽKA POLÍ -->
            <div class="fields-table">
                <h3>Polia fotovoltických panelov</h3>
                <table>
                    <thead>
                    <tr>
                        <th style="width:8%">Pole</th>
                        <th style="width:14%">Počet panelov</th>
                        <th style="width:14%">Výkon poľa</th>
                        <th style="width:12%">Azimut</th>
                        <th style="width:10%">Sklon</th>
                        <th style="width:12%">String</th>
                        <th style="width:30%">Poznámka</th>
                    </tr>
                    </thead>
                    <tbody>
                        {table_rows}
                        <tr class="total">
                            <td>SPOLU</td>
                            <td>{total_pocet} ks</td>
                            <td>{total_kwp} kWp</td>
                            <td colspan="4"></td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- PRAVÝ PANEL -->
        <div class="right-pane">

            <!-- LEGENDA -->
            <div class="panel-block legend">
                <h3>Legenda</h3>
                <div class="legend-item">
                    <span class="legend-icon panel"></span>
                    <span>FV panel {panel_typ} ({panel_rozmer})</span>
                </div>
                <div class="legend-item">
                    <span class="legend-icon string1"></span>
                    <span>String 1 (DC kabeláž → MPPT 1)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-icon string2"></span>
                    <span>String 2 (DC kabeláž → MPPT 2)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-icon menic">&nbsp;</span>
                    <span>Menič (striedač)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-icon roof"></span>
                    <span>Hranica strechy</span>
                </div>
            </div>

            <!-- TECHNICKÉ ÚDAJE -->
            <div class="panel-block tech-data">
                <h3>Technické údaje</h3>
                <table>
                    <tr><td class="label">Inštalovaný výkon DC:</td><td>{vykon_dc} kWp</td></tr>
                    <tr><td class="label">Menovitý výkon AC:</td><td>{vykon_ac} kW</td></tr>
                    <tr><td class="label">Počet panelov:</td><td>{pocet_panelov} ks</td></tr>
                    <tr><td class="label">Typ panela:</td><td>{panel_typ}</td></tr>
                    <tr><td class="label">Menič:</td><td>{menic}</td></tr>
                    <tr><td class="label">Reťazce:</td><td>{pocet_stringov}× (rozloženie viď. tabuľka)</td></tr>
                    <tr><td class="label">Konštrukcia:</td><td>{konstrukcia}</td></tr>
                    <tr><td class="label">Ročná výroba:</td><td>{vyroba}</td></tr>
                </table>
            </div>

            <!-- RAZÍTKO -->
            <div class="stamp">
                <div class="stamp-header">
                    <div class="stamp-company">ENERGOVISION s.r.o.</div>
                    <div class="stamp-address">Lamačská cesta 1738/111, 841 03 Bratislava | IČO: 53 036 280</div>
                </div>
                <div class="stamp-body">
                    <div class="stamp-row"><span class="stamp-label">INVESTOR:</span><span class="stamp-value">{investor}</span></div>
                    <div class="stamp-row"><span class="stamp-label">MIESTO STAVBY:</span><span class="stamp-value">{miesto}</span></div>
                    <div class="stamp-row"><span class="stamp-label">PARCELY:</span><span class="stamp-value">{parcely}</span></div>
                    <div class="stamp-row"><span class="stamp-label">ČÍSLO ZÁKAZKY:</span><span class="stamp-value">{ev_id}</span></div>
                    <div class="stamp-row"><span class="stamp-label">STUPEŇ:</span><span class="stamp-value">DPP — Dok. pre pripojenie</span></div>
                    <div class="stamp-row"><span class="stamp-label">DÁTUM:</span><span class="stamp-value">{datum}</span></div>
                </div>
                <div class="stamp-footer">
                    <div class="stamp-person">
                        <div class="role">Vypracoval</div>
                        <div class="name">{vypracoval}</div>
                    </div>
                    <div class="stamp-person">
                        <div class="role">Kontroloval</div>
                        <div class="name">{kontroloval}</div>
                    </div>
                    <div class="stamp-person">
                        <div class="role">Zodp. projektant</div>
                        <div class="name">{zodp_projektant}</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Číslo výkresu vpravo dole -->
    <div class="drawing-no">VÝKRES Č.: {drawing_no} | Rev. 0</div>

</div>
</div>
</body>
</html>"""


# ============================================================
# 3. GENEROVANIE TECHNICKÉHO VÝKRESU
# ============================================================

def vyrob_technicky_vykres(se_pdf_bytes, lead_data):
    """
    Z SolarEdge raw PDF bytes + lead_data vyrob technický výkres A3 landscape.
    Vráti PDF bytes.
    """
    # 1. Extrahuj zo SolarEdge
    se_data = _extract_solaredge_data(se_pdf_bytes)

    # 2. Príprav dáta pre template
    KOMISIA = {
        "vypracoval": "Lukáš Bago",
        "kontroloval": "Matej Horváth",
        "zodp_projektant": "Ing. Pavol Kaprál",
    }

    # Hero section — embedded base64 image alebo placeholder
    if se_data["hero_b64"]:
        hero_section = (
            '<img src="data:image/png;base64,' + se_data["hero_b64"] + '" alt="Strecha"/>'
        )
    else:
        hero_section = '<div class="hero-placeholder">Pôdorys strechy s panelmi<br>(SolarEdge raw nedostupný)</div>'

    # Polia tabuľka
    polia = se_data["polia"] or []
    table_rows_html = ""
    total_pocet = 0
    total_kwp = 0.0
    nazvy_poli = ["A", "B", "C", "D", "E"]
    poznamky = [
        "Hlavné pole",
        "Sekundárne pole",
        "Tretie pole",
        "Štvrté pole",
        "Piate pole",
    ]
    for i, p in enumerate(polia):
        try:
            poc = int(p.get("pocet", 0))
        except (ValueError, TypeError):
            poc = 0
        try:
            kwp = float(p.get("kwp", "0").replace(",", ".").replace("kWp", "").strip())
        except (ValueError, TypeError):
            kwp = 0
        total_pocet += poc
        total_kwp += kwp
        nazov = nazvy_poli[i] if i < len(nazvy_poli) else f"Pole{i+1}"
        poznamka = poznamky[i] if i < len(poznamky) else ""
        string_id = f"S{i+1}"
        table_rows_html += (
            f"<tr><td>{nazov}</td><td>{poc} ks</td><td>{kwp:.2f} kWp</td>"
            f"<td>{p.get('azimut', '—')}°</td><td>{p.get('sklon', '—')}°</td>"
            f"<td>{string_id}</td><td>{poznamka}</td></tr>"
        )
    # Ak nemáme žiadne polia (parse zlyhal), vyplníme z lead_data
    if not polia:
        poc = lead_data.get('pocet_panelov', 0)
        kwp = lead_data.get('vykon_kwp', 0)
        table_rows_html = (
            f"<tr><td>A</td><td>{poc} ks</td><td>{kwp:.2f} kWp</td>"
            f"<td>—°</td><td>—°</td><td>S1</td><td>Hlavné pole</td></tr>"
        )
        total_pocet = poc
        total_kwp = kwp

    # Panel rozmer (z cenníka)
    try:
        from generuj_pd import _resolve_panel
        panel_info = _resolve_panel(lead_data.get('panel_typ'))
        panel_rozmer = panel_info.get("Dimensions_WxHxD", "1990×1134 mm")
        panel_typ_full = f"{panel_info.get('Manufacturer', '')} {panel_info.get('Type', '')}"
    except Exception:
        panel_rozmer = "1990×1134 mm"
        panel_typ_full = lead_data.get('panel_typ', 'LONGi 535 Wp')

    # Adresa pre razítko
    adresa = (lead_data.get('trvale_bydlisko')
              or lead_data.get('adresa')
              or f"{lead_data.get('ulica_cislo', '')}, {lead_data.get('psc', '')} {lead_data.get('mesto', '')}".strip(", "))

    # Naformátuj
    html = HTML_TEMPLATE.format(
        hero_section=hero_section,
        table_rows=table_rows_html,
        total_pocet=total_pocet,
        total_kwp=f"{total_kwp:.2f}",
        panel_typ=panel_typ_full,
        panel_rozmer=panel_rozmer,
        vykon_dc=f"{lead_data.get('vykon_kwp', 0):.2f}",
        vykon_ac=(se_data["ac_str"] or "—").replace("kW", "").strip() or "—",
        pocet_panelov=lead_data.get('pocet_panelov', '—'),
        menic=lead_data.get('menic', '—'),
        pocet_stringov=len(polia) if polia else 1,
        konstrukcia=lead_data.get('konstrukcia', 'Šikmá strecha (škridla)'),
        vyroba=se_data["vyroba_str"] or "—",
        investor=lead_data.get('meno_priezvisko', '—'),
        miesto=adresa,
        parcely=lead_data.get('parcelne_cisla', '—'),
        ev_id=lead_data.get('ev_id', '—'),
        datum=lead_data.get('datum_dnes') or datetime.now().strftime("%d.%m.%Y"),
        vypracoval=KOMISIA["vypracoval"],
        kontroloval=KOMISIA["kontroloval"],
        zodp_projektant=KOMISIA["zodp_projektant"],
        drawing_no=f"01-FVE-{lead_data.get('ev_id', 'XX')}",
    )

    # Render PDF cez WeasyPrint
    pdf_buf = io.BytesIO()
    HTML(string=html).write_pdf(target=pdf_buf)
    pdf_bytes = pdf_buf.getvalue()

    log.info("[solar_vykres] PDF vygenerovaný — %d B, polia=%d", len(pdf_bytes), len(polia))
    return pdf_bytes


# ============================================================
# 4. ENTRY POINT — z URL alebo Notion File property
# ============================================================

def vyrob_z_url(se_pdf_url, lead_data, output_path):
    """Stiahne SolarEdge PDF z URL a vyrobí technický výkres."""
    r = requests.get(se_pdf_url, timeout=60)
    r.raise_for_status()
    pdf_bytes = vyrob_technicky_vykres(r.content, lead_data)
    with open(output_path, 'wb') as f:
        f.write(pdf_bytes)
    return output_path


def vyrob_z_bytes(se_pdf_bytes, lead_data, output_path):
    """Vyrob technický výkres z bytes."""
    pdf_bytes = vyrob_technicky_vykres(se_pdf_bytes, lead_data)
    with open(output_path, 'wb') as f:
        f.write(pdf_bytes)
    return output_path
