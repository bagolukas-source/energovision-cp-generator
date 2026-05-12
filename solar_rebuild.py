"""
SolarEdge raw PDF -> branded Energovision rozlozenie report.

Stiahne PDF z URL (Notion file property), extrahuje obrazky + data,
vyrobi 2-stranovy branded PDF cez WeasyPrint.
"""
import re
import base64
from io import BytesIO

import fitz
import requests
from weasyprint import HTML


# === KONSTANTY ===
ROKY = 25
DEGRADACIA_PCT = 0.5  # rocne
CENA_EL = 0.16  # EUR/kWh
VYKUP = 0.05  # EUR/kWh
SAMOSPOTREBA = 0.70  # 70% pri samotnej FVE


def parse_num(s):
    """Bezpecny parse cisla z textu (ignoruje medzery a non-breaking)."""
    if not s or s == "—":
        return 0
    cleaned = re.sub(r"[\s ]+", "", str(s))
    m = re.search(r"[\d,\.]+", cleaned)
    if not m:
        return 0
    val = m.group(0).replace(",", ".").strip(".")
    try:
        return float(val) if val else 0
    except ValueError:
        return 0


def azimut_label(deg):
    """Stupne -> svetova strana."""
    try:
        deg = int(deg)
    except (TypeError, ValueError):
        return "?"
    if 337 <= deg or deg < 23: return "Sever"
    if 23 <= deg < 68: return "Severovýchod"
    if 68 <= deg < 113: return "Východ"
    if 113 <= deg < 158: return "Juhovýchod"
    if 158 <= deg < 203: return "Juh"
    if 203 <= deg < 248: return "Juhozápad"
    if 248 <= deg < 293: return "Západ"
    if 293 <= deg < 338: return "Severozápad"
    return "?"


def cz(n, dec=0):
    """Slovensky formatter cisla — medzera ako tisicova oddielac."""
    if isinstance(n, str):
        return n
    if dec == 0:
        return f"{int(round(n)):,}".replace(",", " ")
    return f"{n:,.{dec}f}".replace(",", " ").replace(".", ",")


def _extract_via_vision_api(pdf_bytes):
    """Pre nový SolarEdge formát (bez text layeru) — Claude Vision API."""
    import os, json, requests, logging as _log
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_imgs_b64 = []
        for i in range(min(3, len(doc))):
            page = doc[i]
            pix = page.get_pixmap(dpi=150)
            page_imgs_b64.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
        prompt_lines = [
            "Toto je SolarEdge Designer report pre fotovoltickú elektráreň.",
            "Extrahuj LEN ČISTÝ JSON v presnej štruktúre:",
            "{",
            '  "klient": "Meno Priezvisko",',
            '  "adresa": "ulica číslo, PSČ mesto, Slovensko",',
            '  "mesto": "iba mesto",',
            '  "datum": "DD.MM.YYYY",',
            '  "vykon_kwp": 8.56,',
            '  "vykon_ac_kw": 7.0,',
            '  "rocna_vyroba_kwh": 8513,',
            '  "rocna_spotreba_kwh": 8000,',
            '  "co2_uspora_t": 1.12,',
            '  "stromy_ekv": 52,',
            '  "panely": [',
            '    {"pocet": 8, "model": "LONGi LR7-60HVH-535M", "kwp": 4.3, "azimut": 268, "sklon": 24}',
            '  ]',
            "}",
            "DÔLEŽITÉ: vykon_kwp ako float s bodkou. rocna_vyroba_kwh celé číslo. Iba surový JSON, žiadne markdown bloky.",
        ]
        prompt = "\n".join(prompt_lines)
        content_msg = [{"type": "text", "text": prompt}]
        for img_b64 in page_imgs_b64:
            content_msg.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
            })
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": content_msg}],
            },
            timeout=120,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        return json.loads(text)
    except Exception as e:
        _log.warning("[solar_rebuild vision] zlyhalo: %s", e)
        return None


def _find_hero_image(doc):
    """Najväčší obrázok = vizualizácia strechy (pre nový formát)."""
    biggest = None
    biggest_size = 0
    for page_num in range(min(3, len(doc))):
        page = doc[page_num]
        for img in page.get_images(full=True):
            try:
                xref = img[0]
                base = doc.extract_image(xref)
                img_bytes = base["image"]
                if len(img_bytes) > biggest_size:
                    biggest_size = len(img_bytes)
                    biggest = base64.b64encode(img_bytes).decode("ascii")
            except Exception:
                pass
    return biggest


def extract_pdf_data(pdf_bytes):
    """
    Z raw bytes PDF extrahuj obrazky a numericke data.
    Auto-detekuje legacy (text layer) vs new (vektorová grafika → Vision API).
    Vrati dict: {imgs: {hero, orto, shading}, data: {kwp, vyroba, ...}, panely: [...]}
    """
    import logging as _log
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) < 1:
        raise ValueError("PDF nema ziadne strany")

    page1 = doc[0]
    text_p1 = page1.get_text()
    text_p2 = doc[1].get_text() if len(doc) > 1 else ""

    is_new_format = not text_p1.strip()

    # === HERO IMAGE ===
    imgs_b64 = {"hero": "", "orto": "", "shading": ""}
    if is_new_format:
        _log.info("[solar_rebuild] NEW vector PDF — používam Vision API")
        hero = _find_hero_image(doc)
        if hero:
            imgs_b64["hero"] = hero
    else:
        # Pôvodný formát — preskoc 1. (logo) a vezmi 3 hero
        images = page1.get_images(full=True)
        keys = ["hero", "orto", "shading"]
        for idx, img in enumerate(images[1:4]):
            try:
                xref = img[0]
                base = doc.extract_image(xref)
                imgs_b64[keys[idx]] = base64.b64encode(base["image"]).decode("ascii")
            except Exception:
                pass

    # === EXTRAKCIA DÁT — NEW vs LEGACY ===
    klient = ""
    adresa = ""
    mesto = ""
    datum = ""
    kwp_str = "—"
    ac_str = "—"
    vyroba_str = "—"
    co2_str = "—"
    stromy_str = "—"
    panely = []

    if is_new_format:
        vision = _extract_via_vision_api(pdf_bytes)
        if vision:
            klient = vision.get("klient") or ""
            adresa = vision.get("adresa") or ""
            mesto = vision.get("mesto") or ""
            datum = vision.get("datum") or ""
            if vision.get("vykon_kwp") is not None:
                kwp_str = f"{vision['vykon_kwp']:.2f} kWp".replace(".", ",")
            if vision.get("vykon_ac_kw") is not None:
                ac_str = f"{vision['vykon_ac_kw']:.2f} kW".replace(".", ",")
            if vision.get("rocna_vyroba_kwh") is not None:
                vyroba_str = f"{int(vision['rocna_vyroba_kwh']):,} kWh".replace(",", " ")
            if vision.get("co2_uspora_t") is not None:
                co2_str = f"{vision['co2_uspora_t']:.2f} t".replace(".", ",")
            if vision.get("stromy_ekv") is not None:
                stromy_str = str(vision["stromy_ekv"])
            for p in vision.get("panely") or []:
                kwp_v = p.get("kwp", 0) or 0
                panely.append({
                    "pocet": str(p.get("pocet", "?")),
                    "kwp": f"{kwp_v:.2f} kWp".replace(".", ","),
                    "azimut": str(p.get("azimut", "—")),
                    "sklon": str(p.get("sklon", "—")),
                    "model": p.get("model", ""),
                })
        return {
            "imgs": imgs_b64,
            "klient": klient,
            "priezvisko": klient.split()[-1] if klient else "",
            "adresa": adresa,
            "mesto": mesto,
            "datum": datum,
            "kwp_str": kwp_str,
            "ac_str": ac_str,
            "vyroba_str": vyroba_str,
            "co2_str": co2_str,
            "stromy_str": stromy_str,
            "panely": panely,
            "_format": "new_vector",
        }

    # === LEGACY regex parser ===
    def grab(pattern, txt, default="—"):
        m = re.search(pattern, txt)
        return m.group(1).strip() if m else default

    # Klient nazov
    nazov_match = re.search(r"^P-\S+\s+(.+?)(?:\n|OBH|$)", text_p1, re.M)
    klient = nazov_match.group(1).strip().rstrip(".") if nazov_match else ""
    klient = re.sub(r"^(BC\.?|Bc\.?|Ing\.?|MUDr\.?|Mgr\.?)\s+", "", klient).strip()
    klient = re.sub(r"\s+OBH\s*$", "", klient, flags=re.I).strip()

    adresa_match = re.search(r"^(\d+,\s+[^,\n]+,\s+\d{3}\s?\d{2}[^\n]+)$", text_p1, re.M)
    adresa = adresa_match.group(1).strip() if adresa_match else ""
    adresa = re.sub(r",\s*Slovakia\s*$", "", adresa, flags=re.I)

    datum_match = re.search(r"(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})", text_p1)
    datum = datum_match.group(1) if datum_match else ""

    # Mesto z adresy (medzi 2. a 3. ciarkou)
    mesto = ""
    mm = re.search(r",\s*([^,]+),\s*\d{3}", adresa)
    if mm:
        mesto = mm.group(1).strip()

    # Numericke
    kwp_str = grab(r"Instalovaný DC Výkon\s*\n?\s*([\d,]+\s*kWp)", text_p1)
    ac_str = grab(r"Max Dosažitelný AC Výkon\s*\n?\s*([\d,]+\s*kW)", text_p1)
    vyroba_str = grab(r"Roční Výroba Energie\s*\n?\s*([\d\s ,]+\s*kWh)", text_p1)
    co2_str = grab(r"Úspora Emisí CO2[^\n]*\n?\s*([\d,]+\s*t)", text_p1)
    stromy_str = grab(r"Ekvivalent Vysazených\s*\n?\s*Stromů\s*\n?\s*(\d+)", text_p1)

    # Tabulka panelov z page 2
    panely = []
    panels_section = text_p2.split("FV PANELY", 1)[-1].split("Celkem:", 1)[0]
    lines = [l.strip() for l in panels_section.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        m_kwp = re.match(r"^([\d,]+\s*kWp)$", line)
        if m_kwp and i + 2 < len(lines):
            m_az = re.match(r"^(\d+)°$", lines[i+1])
            m_sk = re.match(r"^(\d+)°$", lines[i+2])
            if m_az and m_sk:
                pocet = None
                for j in range(i-1, -1, -1):
                    if re.match(r"^\d+$", lines[j]):
                        pocet = lines[j]
                        break
                panely.append({
                    "pocet": pocet or "?",
                    "kwp": m_kwp.group(1),
                    "azimut": m_az.group(1),
                    "sklon": m_sk.group(1),
                })

    return {
        "imgs": imgs_b64,
        "klient": klient,
        "priezvisko": klient.split()[-1] if klient else "",
        "adresa": adresa,
        "mesto": mesto,
        "datum": datum,
        "kwp_str": kwp_str,
        "ac_str": ac_str,
        "vyroba_str": vyroba_str,
        "co2_str": co2_str,
        "stromy_str": stromy_str,
        "panely": panely,
        "_format": "legacy_text",
    }


def build_branded_pdf(data, ma_bateriu=False):
    """Vyrob branded 2-stranovy PDF z extrahovanych dat. Vrati bytes."""
    imgs = data["imgs"]

    # Numericke prepocty
    vyroba_num = parse_num(data["vyroba_str"])
    kwp_num = parse_num(data["kwp_str"])
    co2_num = parse_num(data["co2_str"])
    stromy_num = int(parse_num(data["stromy_str"]))

    # Samospotreba 70% bez baterii, 90% s bateriou
    samosp = 0.90 if ma_bateriu else SAMOSPOTREBA
    hodnota_kwh = samosp * CENA_EL + (1 - samosp) * VYKUP
    rocna_uspora = vyroba_num * hodnota_kwh

    lifetime_vyroba = sum(vyroba_num * (1 - DEGRADACIA_PCT/100 * y) for y in range(ROKY))
    lifetime_uspora = lifetime_vyroba * hodnota_kwh
    lifetime_co2 = co2_num * ROKY
    lifetime_stromy = stromy_num * ROKY

    # SK domacnost typicka spotreba
    nasob = vyroba_num / 4500 if vyroba_num else 0

    # Celkovy pocet panelov
    celkovy_pocet = sum(int(p['pocet']) for p in data["panely"] if str(p['pocet']).isdigit())

    # Tabulka rows
    rows = ""
    for i, p in enumerate(data["panely"], 1):
        rows += f"""
      <tr>
        <td><span class="strecha-num">{i}</span></td>
        <td><strong>{p['pocet']}×</strong></td>
        <td><strong>{p['kwp']}</strong></td>
        <td>{azimut_label(p['azimut'])} <span class="deg">({p['azimut']}°)</span></td>
        <td>{p['sklon']}°</td>
      </tr>"""

    priezvisko = data["priezvisko"] or "Vás"
    mesto = data["mesto"]
    adresa = data["adresa"]

    html = f"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<style>
  @page {{
    size: A4;
    margin: 14mm 14mm 14mm 14mm;
    @bottom-left {{
      content: "energovision  ·  www.energovision.sk";
      font: 7.5pt 'Helvetica', sans-serif; color: #888;
    }}
    @bottom-right {{
      content: "Strana " counter(page) " z " counter(pages);
      font: 7.5pt 'Helvetica', sans-serif; color: #888;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; padding: 0;
    font: 10pt/1.5 'Helvetica', 'Arial', sans-serif;
    color: #2c2c2c;
  }}

  .top-header {{
    display: flex; justify-content: space-between; align-items: center;
    padding-bottom: 4mm; border-bottom: 1px solid #e5e5e5; margin-bottom: 7mm;
  }}
  .logo {{ font-size: 16pt; font-weight: 700; letter-spacing: -0.4pt; color: #1a1a1a; line-height: 1; }}
  .logo .accent {{ color: #6FB022; }}
  .badge {{
    background: #1a1a1a; color: #fff; font-size: 7.5pt; font-weight: 700;
    padding: 2mm 4mm; border-radius: 2px; letter-spacing: 1.5pt; text-transform: uppercase;
  }}

  .hero-section {{ margin-bottom: 7mm; }}
  .hero-eyebrow {{
    font-size: 8pt; color: #6FB022; font-weight: 700;
    text-transform: uppercase; letter-spacing: 2pt; margin-bottom: 3mm;
  }}
  .hero-title {{
    font-size: 26pt; line-height: 1.05; color: #1a1a1a;
    margin: 0 0 2mm 0; font-weight: 700; letter-spacing: -0.8pt;
  }}
  .hero-title .accent {{ color: #6FB022; }}
  .hero-subtitle {{
    font-size: 11pt; color: #555; line-height: 1.5;
    margin: 0 0 5mm 0; max-width: 165mm;
  }}
  .hero-img-wrap {{
    width: 100%; height: 56mm; border-radius: 8px; overflow: hidden;
    background: #f0f0f0; box-shadow: 0 1px 3px rgba(0,0,0,0.06); position: relative;
  }}
  .hero-img-wrap img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .hero-caption {{
    position: absolute; bottom: 3mm; left: 3mm;
    background: rgba(26,26,26,0.85); color: #fff;
    padding: 1.5mm 3mm; font-size: 8pt; border-radius: 3px;
  }}
  .hero-caption strong {{ color: #92D050; }}

  .hero-stat {{
    background: linear-gradient(135deg, #6FB022 0%, #5a9b1c 100%);
    color: #fff; border-radius: 6px; padding: 5mm 6mm;
    margin-top: -8mm; margin-left: 6mm; margin-right: 6mm; margin-bottom: 6mm;
    position: relative; z-index: 2;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 4px 12px rgba(111,176,34,0.3);
  }}
  .hero-stat-value {{ font-size: 32pt; font-weight: 700; line-height: 1; letter-spacing: -1pt; }}
  .hero-stat-value .unit {{ font-size: 14pt; font-weight: 600; margin-left: 2mm; opacity: 0.85; }}
  .hero-stat-text {{ text-align: right; font-size: 9pt; line-height: 1.4; opacity: 0.95; max-width: 80mm; }}
  .hero-stat-text strong {{ font-weight: 700; }}

  .kpi-row {{ display: flex; gap: 3mm; margin-bottom: 7mm; }}
  .kpi {{
    flex: 1; background: #fafafa; border-radius: 5px;
    padding: 4mm 3mm; text-align: center; border: 1px solid #f0f0f0;
  }}
  .kpi-value {{
    font-size: 16pt; font-weight: 700; color: #1a1a1a;
    line-height: 1.1; letter-spacing: -0.3pt; margin-top: 1mm;
  }}
  .kpi-value .unit {{ font-size: 9pt; font-weight: 600; color: #6FB022; }}
  .kpi-label {{
    font-size: 7pt; color: #777; margin-top: 1mm;
    text-transform: uppercase; letter-spacing: 0.6pt; line-height: 1.2; font-weight: 600;
  }}
  .kpi-context {{ font-size: 7.5pt; color: #6FB022; margin-top: 1.5mm; font-weight: 600; line-height: 1.3; }}

  .dual {{ display: flex; gap: 4mm; margin-bottom: 6mm; }}
  .dual-cell {{
    flex: 1; border-radius: 5px; overflow: hidden;
    border: 1px solid #e3e3e3; background: #fff;
  }}
  .dual-cell .img-wrap {{ height: 36mm; overflow: hidden; background: #f0f0f0; }}
  .dual-cell img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .dual-caption {{ padding: 2.5mm 3mm 3mm 3mm; font-size: 8.5pt; color: #555; line-height: 1.4; }}
  .dual-caption strong {{ color: #1a1a1a; display: block; font-size: 9pt; margin-bottom: 0.5mm; }}

  h2 {{
    font-size: 11pt; color: #1a1a1a; margin: 0 0 4mm 0;
    padding-bottom: 2mm; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1.5pt;
    border-bottom: 1px solid #e5e5e5;
  }}
  h2 .accent {{ color: #6FB022; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 9.5pt; }}
  th {{
    text-align: left; padding: 2.5mm 2mm; font-weight: 700;
    font-size: 7.5pt; text-transform: uppercase; letter-spacing: 0.7pt;
    color: #888; border-bottom: 1.5px solid #1a1a1a;
  }}
  td {{ padding: 2.8mm 2mm; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  .strecha-num {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 7mm; height: 7mm; background: #6FB022; color: #fff;
    border-radius: 50%; font-weight: 700; font-size: 9pt;
  }}
  .deg {{ color: #999; font-size: 8pt; }}

  .lifetime {{
    background: #1a1a1a; color: #fff; border-radius: 6px;
    padding: 5mm 6mm; margin-bottom: 4mm;
    position: relative; overflow: hidden;
  }}
  .lifetime::before {{
    content: "25"; position: absolute; right: -5mm; top: -8mm;
    font-size: 90pt; font-weight: 800; color: #6FB022; opacity: 0.12; line-height: 1;
  }}
  .lifetime-eyebrow {{
    font-size: 7.5pt; color: #92D050; font-weight: 700;
    text-transform: uppercase; letter-spacing: 2pt; margin-bottom: 2mm;
  }}
  .lifetime-title {{ font-size: 14pt; font-weight: 700; margin: 0 0 4mm 0; letter-spacing: -0.3pt; }}
  .lifetime-stats {{ display: flex; gap: 6mm; }}
  .lifetime-stat {{ flex: 1; }}
  .lifetime-value {{ font-size: 18pt; font-weight: 700; line-height: 1.1; letter-spacing: -0.5pt; }}
  .lifetime-value .unit {{ font-size: 10pt; font-weight: 600; color: #92D050; margin-left: 1mm; }}
  .lifetime-label {{
    font-size: 7.5pt; color: #aaa; text-transform: uppercase;
    letter-spacing: 0.6pt; margin-top: 1mm; font-weight: 600;
  }}

  .trust-line {{
    text-align: center; font-size: 8.5pt; color: #777;
    margin-top: 3mm; letter-spacing: 0.5pt;
  }}
  .trust-line strong {{ color: #1a1a1a; font-weight: 600; }}
  .trust-line .dot {{ color: #6FB022; margin: 0 2mm; font-weight: 700; }}

  .disclaimer {{
    font-size: 7.5pt; color: #888; line-height: 1.5;
    margin-top: 3mm; font-style: italic; text-align: center;
  }}

  .page-break {{ page-break-after: always; }}
</style>
</head>
<body>

<div class="top-header">
  <div class="logo">energo<span class="accent">vision</span></div>
  <span class="badge">Návrh pre Vašu strechu</span>
</div>

<div class="hero-section">
  <div class="hero-eyebrow">Pripravené pre {priezvisko}{(' · ' + mesto) if mesto else ''}</div>
  <h1 class="hero-title">Vaša strecha sa môže stať <span class="accent">elektrárňou.</span></h1>
  <p class="hero-subtitle">
    Naša projekčná dielňa pripravila návrh fotovoltickej elektrárne pre {('adresu ' + adresa) if adresa else 'Vašu nehnuteľnosť'}.
    Pozrite si, ako by mohol vyzerať Váš dom — a aký potenciál pre Vás strecha skrýva.
  </p>

  <div class="hero-img-wrap">
    <img src="data:image/png;base64,{imgs['hero']}" alt="3D vizualizácia"/>
    <div class="hero-caption"><strong>Vizualizácia</strong> — Vaša strecha s {celkovy_pocet} panelmi</div>
  </div>
</div>

<div class="hero-stat">
  <div>
    <div class="hero-stat-value">~{cz(vyroba_num)}<span class="unit">kWh/rok</span></div>
  </div>
  <div class="hero-stat-text">
    <strong>Toľko elektriny by mohla strecha vyrobiť ročne.</strong><br>
    Predpokladaná výroba podľa simulácie — približne {nasob:.1f}× spotreby priemernej slovenskej domácnosti.
  </div>
</div>

<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-value">{cz(kwp_num, 2)}<span class="unit"> kWp</span></div>
    <div class="kpi-label">Inštalovaný<br>výkon</div>
    <div class="kpi-context">{celkovy_pocet} panelov LONGi</div>
  </div>
  <div class="kpi">
    <div class="kpi-value">~{cz(rocna_uspora)}<span class="unit"> €</span></div>
    <div class="kpi-label">Možná úspora<br>za rok</div>
    <div class="kpi-context">orientačný odhad</div>
  </div>
  <div class="kpi">
    <div class="kpi-value">~{cz(co2_num, 2)}<span class="unit"> t CO₂</span></div>
    <div class="kpi-label">Menej emisií<br>ročne</div>
    <div class="kpi-context">≈ {data['stromy_str']} stromov</div>
  </div>
  <div class="kpi">
    <div class="kpi-value">{len(data['panely'])}<span class="unit"></span></div>
    <div class="kpi-label">Strešné<br>plochy</div>
    <div class="kpi-context">optimálne sklony</div>
  </div>
</div>

<div class="page-break"></div>

<div class="top-header" style="margin-bottom:6mm;">
  <div class="logo">energo<span class="accent">vision</span></div>
  <span class="badge">Architektúra inštalácie</span>
</div>

<h2 style="margin-top:0;">Architektúra <span class="accent">Vašej elektrárne</span></h2>

<div class="dual">
  <div class="dual-cell">
    <div class="img-wrap"><img src="data:image/png;base64,{imgs['orto']}" alt="Pohľad zhora"/></div>
    <div class="dual-caption">
      <strong>Pohľad zhora</strong>
      Presné rozloženie {celkovy_pocet} panelov optimalizované pre maximálny výkon.
    </div>
  </div>
  <div class="dual-cell">
    <div class="img-wrap"><img src="data:image/png;base64,{imgs['shading']}" alt="Mapa slnečného zisku"/></div>
    <div class="dual-caption">
      <strong>Slnečný zisk počas roka</strong>
      Oranžové oblasti = oblasti s najvyšším celoročným výnosom slnka.
    </div>
  </div>
</div>

<table>
  <thead>
    <tr>
      <th style="width:12%">Strecha</th>
      <th style="width:14%">Panelov</th>
      <th style="width:18%">Výkon</th>
      <th>Orientácia</th>
      <th style="width:14%">Sklon</th>
    </tr>
  </thead>
  <tbody>{rows}
  </tbody>
</table>

<div class="lifetime">
  <div class="lifetime-eyebrow">Za 25 rokov životnosti</div>
  <div class="lifetime-title">Aký potenciál Vám strecha skrýva.</div>
  <div class="lifetime-stats">
    <div class="lifetime-stat">
      <div class="lifetime-value">~{cz(lifetime_vyroba/1000)}<span class="unit">MWh</span></div>
      <div class="lifetime-label">Predpokladaná<br>výroba</div>
    </div>
    <div class="lifetime-stat">
      <div class="lifetime-value">~{cz(lifetime_co2)}<span class="unit">t CO₂</span></div>
      <div class="lifetime-label">Menej emisií<br>do ovzdušia</div>
    </div>
    <div class="lifetime-stat">
      <div class="lifetime-value">~{cz(lifetime_stromy)}<span class="unit">×</span></div>
      <div class="lifetime-label">Stromov<br>ekvivalent</div>
    </div>
    <div class="lifetime-stat">
      <div class="lifetime-value">~{cz(lifetime_uspora/1000, 1)}<span class="unit">k €</span></div>
      <div class="lifetime-label">Možný prínos<br>na účte za el.</div>
    </div>
  </div>
</div>

<div class="disclaimer">
  Hodnoty sú orientačné — vychádzajú zo simulácie a priemerných údajov.
  Skutočná výroba a úspora závisia od počasia, Vašej spotreby, samospotreby a vývoja cien elektriny.
  Nezáväzná projekcia, nie záruka výnosu.
</div>

<div class="trust-line" style="margin-top:5mm;">
  <strong>Inštalácia za 1–2 dni</strong> <span class="dot">·</span>
  <strong>Záruka 30 rokov</strong> na panely <span class="dot">·</span>
  <strong>Servis 25+ rokov</strong> <span class="dot">·</span>
  Slovenská spoločnosť, vlastná projekčná dielňa
</div>

</body>
</html>"""

    out = BytesIO()
    HTML(string=html).write_pdf(out)
    return out.getvalue()


def process_solaredge_pdf(pdf_url, ma_bateriu=False):
    """
    Hlavny entry-point: stiahne SolarEdge PDF z URL, vytvori branded.
    Vrati (pdf_bytes, klient_priezvisko_safe, summary_dict).
    """
    r = requests.get(pdf_url, timeout=60)
    r.raise_for_status()
    raw_pdf = r.content

    data = extract_pdf_data(raw_pdf)
    branded_pdf = build_branded_pdf(data, ma_bateriu=ma_bateriu)

    # Bezpecny filename suffix
    priezvisko = data["priezvisko"] or "Klient"
    import unicodedata
    nfd = unicodedata.normalize("NFD", priezvisko)
    safe = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", safe).strip("_") or "Klient"

    summary = {
        "klient": data["klient"],
        "mesto": data["mesto"],
        "kwp": data["kwp_str"],
        "vyroba": data["vyroba_str"],
        "panely_count": len(data["panely"]),
    }
    return branded_pdf, safe, summary
