# -*- coding: utf-8 -*-
"""Predajná prezentácia pre zákazníka — premium branded PDF deck (landscape)."""
import os, base64
BASE = os.path.dirname(os.path.abspath(__file__))

def _b64img(name):
    try:
        ext = os.path.splitext(name)[1].lower()
        mime = {".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp",".svg":"image/svg+xml",".gif":"image/gif"}.get(ext,"image/png")
        with open(os.path.join(BASE, name), "rb") as fh:
            return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()
    except Exception:
        return ""

def _fileuri(name):
    """Absolútna file:// cesta — proven path na Render (weasyprint číta priamo z disku,
    bez data-URI, bez MIME, bez veľkostného limitu). Pre fotky (JPEG) spoľahlivejšie než base64."""
    p = os.path.join(BASE, name)
    return ("file://" + p) if os.path.exists(p) else ""

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
    kontakt = g.get("kontakt") or "obchod@energovision.sk · energovision.sk"

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


def _eur(v):
    try: return f"{float(v):,.0f} €".replace(",", " ")
    except Exception: return "—"

def _eur_c(v):
    """Kompaktné euro pre karty: >=1 mil. → '1,24 mil. €'."""
    try:
        f = float(v)
        if abs(f) >= 1_000_000:
            return (f"{f/1e6:.2f}".replace(".", ",")) + " mil. €"
        return _eur(v)
    except Exception:
        return _eur(v)

def _stat(label, val, sub=""):
    return f"<div class='stat'><div class='sv'>{_esc(val)}</div><div class='sl'>{_esc(label)}</div>{('<div class=ss>'+_esc(sub)+'</div>') if sub else ''}</div>"


def _ai_prez_texty(payload: dict) -> dict:
    """Claude napíše B2B prezentačné texty NA MIERU, grounded v reálnych číslach.
    Pri akejkoľvek chybe vráti {} → deck použije statické fallbacky (nikdy nespadne)."""
    import os, json, re
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {}
    try:
        import requests
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
        system = (
            "Si senior energetický konzultant firmy Energovision (priemyselné FVE, batériové úložiská, "
            "trafostanice a VN, odborné revízie, elektrotechnické práce). Píšeš texty do B2B prezentácie "
            "pre vedenie veľkej firmy.\n\n"
            "PRAVIDLÁ:\n"
            "- Slovenčina, vecne, profesionálne, sebavedomo. ŽIADNE reklamné superlatívy ani vata.\n"
            "- Používaj IBA čísla z podkladu. Nič nevymýšľaj, nedopĺňaj žiadne nové hodnoty.\n"
            "- Krátke vety. Hovor jazykom úspor, rizika, návratnosti a prevádzkovej istoty.\n"
            "- Kde sa hodí, prepoj na širšie kompetencie Energovision (trafostanice, revízie, elektro), ale nenásilne.\n"
            "- Vráť IBA platný JSON, žiadny markdown, presne v tejto štruktúre:\n"
            "{\n"
            '  "podtitul": "1 veta pod titulok na titulke (max 140 znakov)",\n'
            '  "vychodisko_lead": "2 vety o východiskovej situácii OM, použi reálne čísla spotreby/špičky/MRK",\n'
            '  "riesenie_lead": "2 vety prečo je navrhnutá konfigurácia vhodná pre túto prevádzku",\n'
            '  "ekonomika_lead": "2 vety rámcujúce ekonomiku (návratnosť, NPV, dotácia) bez vymýšľania",\n'
            '  "prinosy": [{"t":"názov (2-4 slová)","d":"1 veta, konkrétne"}, ...presne 4 položky...],\n'
            '  "zaver_lead": "1-2 vety výzva na ďalší krok (obhliadka, projekt, dotácia)"\n'
            "}\n"
        )
        user = "Podklad k odbernému miestu a návrhu (reálne dáta):\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": model, "max_tokens": 1400, "temperature": 0.4,
                "system": system, "messages": [{"role": "user", "content": user}]}
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=60)
        r.raise_for_status()
        content = r.json().get("content") or []
        txt = (content[0].get("text") if content and isinstance(content[0], dict) else "") or ""
        txt = re.sub(r"^```(?:json)?\s*", "", txt.strip()); txt = re.sub(r"\s*```$", "", txt).strip()
        data = json.loads(txt)
        if isinstance(data, dict) and isinstance(data.get("prinosy"), list):
            data["prinosy"] = [p for p in data["prinosy"] if isinstance(p, dict) and p.get("t") and p.get("d")][:4]
        return data if isinstance(data, dict) else {}
    except Exception as e:
        try:
            import logging; logging.getLogger("evo").warning("AI prez texty zlyhali: %s", e)
        except Exception:
            pass
        return {}


def generuj_prezentaciu_b2b(g: dict) -> bytes:
    """Podrobná B2B prezentácia z technickej analýzy — editorial premium dizajn."""
    from weasyprint import HTML
    cust = _esc(g.get("zakaznik") or "Vážený klient")
    om = g.get("om") or {}; rec = g.get("variant") or {}; variants = g.get("varianty") or []
    charts = g.get("charts") or []; datum = _esc(g.get("datum") or "")
    kontakt = _esc(g.get("kontakt") or "obchod@energovision.sk · energovision.sk")
    logo_w = _b64img("energovision_logo_white.png"); logo_c = _b64img("energovision_logo.png")
    hero_cover = _b64img("ref_cover.png"); hero_close = _b64img("ref_close.png")
    ref_items = [(_b64img("ref_cover.png"),"Žarnovica","1,56 MWp"),
                 (_b64img("ref_partizanske.png"),"Partizánske","1,32 MWp"),
                 (_b64img("ref_vlkanova.png"),"Vlkanová · KÜSTER","500,5 kWp"),
                 (_b64img("ref_krup.png"),"Nitra · KRUP","BESS 100 kW / 218 kWh")]
    refcards = "".join(f"<div class='refcard'><div class='ri' style=\"background-image:url('{im}')\"></div>"
                       f"<div class='rl'><b>{_esc(t)}</b><span>{_esc(v)}</span></div></div>" for im,t,v in ref_items)

    def num(x, suf="", dec=1):
        try:
            f = float(x); v = f"{f:,.{dec}f}".replace(",", " ")
            if dec and v.endswith("." + "0"*dec): v = v[:-(dec+1)]
            return v.replace(".", ",") + suf
        except Exception: return "—"
    def i_num(x, suf=""): return num(x, suf, 0)

    spotreba = num(om.get("consumption_annual_mwh"), " MWh"); peak = i_num(om.get("consumption_peak_kw_hourly") or om.get("consumption_peak_kw_15min"), " kW")
    mrk = i_num(om.get("om_mrk_kw"), " kW"); mrk_util = i_num(om.get("consumption_mrk_utilization_pct"), " %")
    samosp_v = rec.get("result_samosp_pct"); samostat_v = rec.get("result_samostat_pct")
    samosp = i_num(samosp_v, " %"); samostat = i_num(samostat_v, " %")
    npv = _eur(rec.get("result_npv_eur_base")); irr = num(rec.get("result_irr_pct_base"), " %")
    payback = num(rec.get("result_payback_y_base"), ""); capex = _eur(rec.get("capex_eur")); dotacia = _eur(rec.get("result_dotacia_eur"))
    fve = num(rec.get("fve_kwp"), " kWp"); bess = num(rec.get("bess_kwh"), " kWh") if rec.get("bess_kwh") else "—"

    # AI texty na mieru (grounded; fallback = statika)
    _ai = _ai_prez_texty({
        "zakaznik": g.get("zakaznik"),
        "rocna_spotreba_mwh": om.get("consumption_annual_mwh"),
        "spicka_kw": om.get("consumption_peak_kw_hourly") or om.get("consumption_peak_kw_15min"),
        "mrk_kw": om.get("om_mrk_kw"), "vyuzitie_mrk_pct": om.get("consumption_mrk_utilization_pct"),
        "navrh_variant": rec.get("name"), "fve_kwp": rec.get("fve_kwp"), "bess_kwh": rec.get("bess_kwh"),
        "capex_eur": rec.get("capex_eur"), "dotacia_eur": rec.get("result_dotacia_eur"),
        "npv_eur": rec.get("result_npv_eur_base"), "irr_pct": rec.get("result_irr_pct_base"),
        "navratnost_rokov": rec.get("result_payback_y_base"),
        "samospotreba_pct": samosp_v, "sebestacnost_pct": samostat_v,
        "varianty": [{"nazov": v.get("name"), "fve_kwp": v.get("fve_kwp"), "bess_kwh": v.get("bess_kwh"),
                      "capex_eur": v.get("capex_eur"), "npv_eur": v.get("result_npv_eur_base")} for v in variants[:4]],
    }) or {}
    ai_podtitul = _esc(_ai.get("podtitul") or "Technicko-ekonomická analýza odberného miesta a riešenie na mieru pre Vašu prevádzku.")
    ai_vych = _esc(_ai.get("vychodisko_lead") or "Vychádzame z reálnych 15-minútových meraných dát Vášho odberného miesta.")
    ai_ries = _esc(_ai.get("riesenie_lead") or "Konfigurácia je optimalizovaná na maximálnu samospotrebu a návratnosť.")
    ai_ekon = _esc(_ai.get("ekonomika_lead") or "Vychádza z reálneho profilu spotreby a aktuálnych tarív; zahŕňa daňový odpis a dotáciu podľa platnej schémy.")
    ai_zaver = _esc(_ai.get("zaver_lead") or "Navrhujeme obhliadku a spresnenie projektu. Pripravíme zmluvu, dotáciu a harmonogram realizácie.")
    _ai_pr = _ai.get("prinosy") or []

    def donut(pct, big):
        try: p = max(0, min(100, float(pct)))
        except Exception: p = 0
        C = 2*3.14159*52; off = C*(1-p/100)
        return (f"<svg viewBox='0 0 120 120' width='42mm' height='42mm'>"
                f"<circle cx='60' cy='60' r='52' fill='none' stroke='#E6E3DB' stroke-width='10'/>"
                f"<circle cx='60' cy='60' r='52' fill='none' stroke='#92D050' stroke-width='10' stroke-linecap='round'"
                f" stroke-dasharray='{C:.0f}' stroke-dashoffset='{off:.0f}' transform='rotate(-90 60 60)'/>"
                f"<text x='60' y='58' text-anchor='middle' font-size='26' font-weight='800' fill='#14181F'>{big}</text>"
                f"<text x='60' y='74' text-anchor='middle' font-size='8' fill='#6B7280'>samospotreba</text></svg>")

    # NPV bary
    npvs = [(_esc(v.get('name')), float(v.get('result_npv_eur_base') or 0)) for v in variants[:4]]
    mx = max([n for _,n in npvs] + [1])
    bars = ""
    best = max(npvs, key=lambda t:t[1])[0] if npvs else None
    for nm, val in npvs:
        w = max(2, val/mx*100); hl = (nm==best)
        bars += (f"<div class='bar'><div class='bl'>{nm}</div>"
                 f"<div class='bt'><div class='bf' style='width:{w:.0f}%;background:{'#92D050' if hl else '#cfd8c4'}'></div></div>"
                 f"<div class='bv'>{_eur(val)}</div></div>")

    vrows = ""
    for v in variants[:4]:
        vrows += (f"<tr><td class='vn'>{_esc(v.get('name'))}</td><td>{num(v.get('fve_kwp'),' kWp')}</td>"
                  f"<td>{num(v.get('bess_kwh'),' kWh') if v.get('bess_kwh') else '—'}</td>"
                  f"<td>{_eur(v.get('capex_eur'))}</td><td>{i_num(v.get('result_samosp_pct'),' %')}</td>"
                  f"<td>{num(v.get('result_payback_y_base'),' r')}</td></tr>")

    sluzby = [("Fotovoltika & batériové úložiská","Návrh, dodávka a montáž na kľúč pre priemyselné prevádzky."),
              ("Trafostanice a VN pripojenia","Servis, údržba a realizácia vysokonapäťových rozvodov."),
              ("Odborné revízie VTZ","Prehliadky a skúšky vyhradených technických zariadení."),
              ("Elektrotechnické práce","Komplexné realizačné a technické činnosti v energetike.")]
    srows = "".join(f"<div class='srow'><div class='sno'>{i+1:02d}</div><div><div class='st'>{t}</div><div class='sd'>{d}</div></div></div>" for i,(t,d) in enumerate(sluzby))

    benefits = ([(p["t"], p["d"]) for p in _ai_pr] if len(_ai_pr) == 4 else
                [("Nižšie prevádzkové náklady","Zníženie odberu zo siete a faktúr za elektrinu."),
                 ("Energetická sebestačnosť",f"Samospotreba {samosp}, sebestačnosť {samostat}."),
                 ("ESG & dekarbonizácia","Zníženie uhlíkovej stopy — výhoda v tendroch a reportingu."),
                 ("Garancia a servis","Vlastný servisný tím, monitoring a garancia výkonu.")])
    bcards = "".join(f"<div class='bcard'><div class='bt2'>{t}</div><div class='bd2'>{d}</div></div>" for t,d in benefits)

    chart_slides = ""
    for idx,c in enumerate(charts[:2]):
        chart_slides += (f"<div class='slide light'>{_hdr(logo_c,'Analýza','A'+str(idx+1))}"
                         f"<div class='chartwrap'><img src='{c}'/></div>{_ftr('— ')}</div>")

    css = """
    @page { size:A4 landscape; margin:0; }
    * { box-sizing:border-box; margin:0; padding:0; }
    body { font-family:'Helvetica Neue','Arial',sans-serif; color:#14181F; -webkit-font-smoothing:antialiased; }
    .slide { width:297mm; height:210mm; page-break-after:always; position:relative; overflow:hidden; }
    .slide:last-child { page-break-after:auto; }
    .light { background:#FBFAF7; padding:20mm 22mm 16mm; }
    .dark { background:radial-gradient(120% 120% at 80% 0%,#143020 0%,#0a140d 60%); color:#fff; padding:24mm 22mm; }
    .hdr { display:flex; align-items:center; justify-content:space-between; border-bottom:0.4mm solid #E6E3DB; padding-bottom:5mm; }
    .hdr img { height:7mm; } .hdr .sec { font-size:8.5pt; letter-spacing:2.5pt; text-transform:uppercase; color:#6B7280; }
    .kicker { font-size:9pt; letter-spacing:3pt; text-transform:uppercase; color:#92D050; font-weight:700; margin-top:14mm; }
    h2 { font-size:27pt; font-weight:800; letter-spacing:-0.3pt; margin-top:3mm; line-height:1.04; }
    .lead { font-size:11pt; color:#6B7280; max-width:175mm; margin-top:4mm; line-height:1.5; }
    .ftr { position:absolute; left:22mm; right:22mm; bottom:11mm; display:flex; justify-content:space-between; font-size:8pt; color:#9aa3af; border-top:0.4mm solid #E6E3DB; padding-top:3.5mm; }
    /* metriky bez boxíkov */
    .metrics { display:flex; margin-top:14mm; }
    .metric { flex:1; padding:0 8mm; border-left:0.4mm solid #E6E3DB; }
    .metric:first-child { padding-left:0; border-left:none; }
    .mv { font-size:30pt; font-weight:800; letter-spacing:-0.5pt; } .mv.acc { color:#1f7a1f; }
    .ml { font-size:9pt; letter-spacing:1.5pt; text-transform:uppercase; color:#6B7280; margin-top:2.5mm; }
    .split { display:flex; gap:16mm; margin-top:14mm; align-items:center; }
    .split .l { flex:1.2; } .split .r { flex:1; text-align:center; }
    .srow { display:flex; gap:8mm; padding:6mm 0; border-bottom:0.4mm solid #E6E3DB; }
    .sno { font-size:18pt; font-weight:300; color:#cbd2c2; width:14mm; } .st { font-size:13pt; font-weight:700; } .sd { font-size:10pt; color:#6B7280; margin-top:1mm; }
    table { width:100%; border-collapse:collapse; margin-top:12mm; font-size:11pt; }
    th { text-align:left; font-size:8.5pt; letter-spacing:1.5pt; text-transform:uppercase; color:#6B7280; padding:0 4mm 4mm; border-bottom:0.5mm solid #14181F; }
    td { padding:4.5mm 4mm; border-bottom:0.4mm solid #E6E3DB; } td.vn { font-weight:700; }
    .bars { margin-top:12mm; } .bar { display:flex; align-items:center; gap:6mm; margin:3.5mm 0; }
    .bl { width:60mm; font-size:10pt; } .bt { flex:1; height:6mm; background:#F0EEE7; border-radius:3mm; overflow:hidden; }
    .bf { height:100%; border-radius:3mm; } .bv { width:34mm; text-align:right; font-weight:700; font-size:11pt; }
    .bgrid { display:flex; flex-wrap:wrap; gap:0; margin-top:12mm; }
    .bcard { flex:1 1 50%; padding:8mm 10mm 8mm 0; border-top:0.6mm solid #92D050; margin-right:10mm; margin-top:8mm; }
    .bt2 { font-size:13pt; font-weight:700; } .bd2 { font-size:10pt; color:#6B7280; margin-top:2mm; }
    .chartwrap { height:150mm; display:flex; align-items:center; justify-content:center; margin-top:8mm; } .chartwrap img { max-width:100%; max-height:100%; object-fit:contain; }
    /* cover */
    .cov-logo { height:9mm; } .cov-rule { width:30mm; height:0.8mm; background:#92D050; margin:18mm 0 7mm; }
    .cov-t { font-size:40pt; font-weight:800; letter-spacing:-1pt; line-height:1.02; max-width:230mm; }
    .cov-s { font-size:12pt; color:#b9c4bd; margin-top:7mm; max-width:170mm; }
    .cov-for { position:absolute; left:22mm; bottom:24mm; } .cov-for .k { font-size:8.5pt; letter-spacing:3pt; color:#92D050; } .cov-for .v { font-size:18pt; font-weight:700; margin-top:2mm; } .cov-for .d { font-size:9.5pt; color:#7c8a80; margin-top:1mm; }
    .cov-pg { position:absolute; right:22mm; bottom:24mm; font-size:8.5pt; color:#5f6f64; letter-spacing:2pt; }
    .cta { margin-top:12mm; font-size:13pt; } .cta b { color:#92D050; }
    .cards { display:flex; gap:7mm; margin-top:11mm; }
    .card { flex:1; border:0.4mm solid #E6E3DB; border-radius:2.6mm; overflow:hidden; background:#fff; }
    .card .ch { padding:3.6mm 5mm; color:#fff; font-size:8.5pt; letter-spacing:2pt; text-transform:uppercase; font-weight:700; }
    .card .cb { padding:7mm 5mm 6mm; }
    .card .cval { font-size:29pt; font-weight:800; color:#1f7a1f; letter-spacing:-0.5pt; line-height:1; }
    .card .cunit { font-size:9.5pt; color:#6B7280; margin-top:2.5mm; }
    .card .cbul { margin-top:5mm; padding-top:4mm; border-top:0.4mm solid #EEEBE3; font-size:8.5pt; color:#6B7280; line-height:1.75; }
    .cover2 { position:relative; overflow:hidden; padding:24mm 22mm; color:#fff; background:#0a140d; }
    .cover2 .cimg { position:absolute; inset:0; background-size:cover; background-position:center; z-index:0; }
    .cover2 .covl { position:absolute; inset:0; z-index:1;
       background:linear-gradient(105deg, rgba(7,16,10,.96) 0%, rgba(7,16,10,.88) 38%, rgba(7,16,10,.5) 72%, rgba(7,16,10,.22) 100%); }
    .cover2 .cc { position:relative; z-index:3; }
    .cover2 .cov-for, .cover2 .cov-pg { z-index:3; }
    .refgrid { display:flex; flex-wrap:wrap; gap:6mm; margin-top:9mm; }
    .refcard { flex:1 1 45%; height:60mm; position:relative; border-radius:2.2mm; overflow:hidden; }
    .refcard .ri { position:absolute; inset:0; background-size:cover; background-position:center; }
    .refcard .rl { position:absolute; left:0; right:0; bottom:0; padding:5mm 6mm 4.5mm;
       background:linear-gradient(to top, rgba(7,16,10,.9) 0%, rgba(7,16,10,.45) 55%, rgba(7,16,10,0) 100%); color:#fff; }
    .refcard .rl b { font-size:12.5pt; font-weight:800; } .refcard .rl span { display:block; font-size:9pt; color:#bfe39a; letter-spacing:1.5pt; margin-top:1mm; }
    """
    def hdr(no, name): return _hdr(logo_c, name, no)
    cover = (f"<div class='slide cover2'>"
             f"<div class='cimg' style=\"background-image:url('{hero_cover}')\"></div><div class='covl'></div>"
             f"<div class='cc'><img class='cov-logo' src='{logo_w}'/><div class='cov-rule'></div>"
             f"<div class='cov-t'>Návrh fotovoltického<br/>a batériového riešenia</div>"
             f"<div class='cov-s'>{ai_podtitul}</div></div>"
             f"<div class='cov-for'><div class='k'>PRIPRAVENÉ PRE</div><div class='v'>{cust}</div><div class='d'>{datum}</div></div>"
             f"<div class='cov-pg'>ENERGOVISION · DÔVERNÉ</div></div>")
    s_firma = (f"<div class='slide light'>{hdr('01','Spoločnosť')}"
               f"<div class='kicker'>Energovision</div><h2>Partner pre priemyselnú energetiku</h2>"
               f"<div class='lead'>Pokrývame celý životný cyklus — od analýzy a projektu cez realizáciu po servis a monitoring.</div>"
               f"<div style='margin-top:8mm'>{srows}</div>{_ftr('01')}</div>")
    s_vych = (f"<div class='slide light'>{hdr('03','Východisko')}"
              f"<div class='kicker'>Profil odberného miesta</div><h2>Analyzovali sme Vašu reálnu spotrebu</h2>"
              f"<div class='lead'>{ai_vych}</div>"
              f"<div class='metrics'>"
              f"<div class='metric'><div class='mv'>{spotreba}</div><div class='ml'>Ročná spotreba</div></div>"
              f"<div class='metric'><div class='mv'>{peak}</div><div class='ml'>Špička odberu</div></div>"
              f"<div class='metric'><div class='mv'>{mrk}</div><div class='ml'>Rezervovaná kapacita</div></div>"
              f"<div class='metric'><div class='mv'>{mrk_util}</div><div class='ml'>Využitie MRK</div></div>"
              f"</div>{_ftr('07')}</div>")
    GR, LM, DK = "#2e7d32", "#6cb33f", "#14181F"
    s_ries = (f"<div class='slide light'>{hdr('04','Riešenie')}"
              f"<div class='kicker'>{_esc(rec.get('name') or 'Odporúčaná konfigurácia')}</div><h2>Navrhované riešenie</h2>"
              f"<div class='cards'>"
              + _card("FVE na kľúč", fve, "Inštalovaný výkon", ["Strešná / pozemná inštalácia","Optimalizované na samospotrebu","Projekt, montáž a revízie"], GR)
              + _card("Batéria (BESS)", bess, "Kapacita úložiska", ["Ukladanie denných prebytkov","Špičkovanie a záloha","Vyššia sebestačnosť"], DK)
              + _card("Samospotreba", samosp, f"Sebestačnosť {samostat}", ["Podiel vlastnej spotreby","Nižší odber zo siete","Ochrana pred rastom cien"], LM)
              + f"</div>"
              f"<div class='lead' style='margin-top:11mm'>{ai_ries}</div>{_ftr('04')}</div>")
    s_ekon = (f"<div class='slide light'>{hdr('05','Ekonomika')}"
              f"<div class='kicker'>Návratnosť investície</div><h2>Ekonomika riešenia</h2>"
              f"<div class='cards'>"
              + _card("Investícia", _eur_c(rec.get("capex_eur")), "CAPEX na kľúč", ["Dodávka a montáž","Projekt + revízie"], "#14181F")
              + _card("Dotácia", _eur_c(rec.get("result_dotacia_eur")), "Nenávratný príspevok", ["Zelená podnikom / FST","Znižuje vstup"], "#6cb33f")
              + _card("NPV", _eur_c(rec.get("result_npv_eur_base")), "Čistá hodnota · 20 r", ["Po zdanení, reálne ceny","Vrátane daň. odpisu"], "#2e7d32")
              + _card("Návratnosť", f"{payback} r", f"IRR {irr}", ["Prostá návratnosť","Z reálneho profilu"], "#2e7d32")
              + f"</div>"
              f"<div class='lead' style='margin-top:12mm'>{ai_ekon}</div>{_ftr('05')}</div>")
    s_var = (f"<div class='slide light'>{hdr('06','Varianty')}"
             f"<div class='kicker'>Porovnanie</div><h2>Vyberte si úroveň riešenia</h2>"
             f"<table><tr><th>Variant</th><th>FVE</th><th>Batéria</th><th>Investícia</th><th>Samospotreba</th><th>Návratnosť</th></tr>{vrows}</table>"
             f"<div class='bars'>{bars}</div><div style='font-size:8.5pt;color:#9aa3af;margin-top:4mm'>Stĺpce: čistá súčasná hodnota (NPV) za 20 rokov.</div>{_ftr('05')}</div>")
    s_pri = (f"<div class='slide light'>{hdr('07','Prínosy')}"
             f"<div class='kicker'>Pridaná hodnota</div><h2>Čo Vám riešenie prinesie</h2>"
             f"<div class='bgrid'>{bcards}</div>{_ftr('06')}</div>")
    s_ref = (f"<div class='slide light'>{hdr('02','Realizácie')}"
             f"<div class='kicker'>Vybrané realizácie</div><h2>163 inštalácií naprieč Slovenskom</h2>"
             f"<div class='lead'>Od priemyselných megawattových striech až po batériové úložiská — realizácia na kľúč.</div>"
             f"<div class='refgrid'>{refcards}</div>{_ftr('02')}</div>")
    s_close = (f"<div class='slide cover2'>"
               f"<div class='cimg' style=\"background-image:url('{hero_close}')\"></div><div class='covl'></div>"
               f"<div class='cc'><img class='cov-logo' src='{logo_w}'/><div class='cov-rule'></div>"
               f"<div class='cov-t' style='font-size:34pt'>Poďme overiť<br/>potenciál naživo</div>"
               f"<div class='cov-s'>{ai_zaver}</div>"
               f"<div class='cta'>Kontakt: <b>{kontakt}</b></div></div>"
               f"<div class='cov-pg'>ENERGOVISION · energovision.sk</div></div>")
    html = (f"<!DOCTYPE html><html lang='sk'><head><meta charset='utf-8'><style>{css}</style></head><body>"
            f"{cover}{s_firma}{s_ref}{s_vych}{s_ries}{s_ekon}{s_var}{chart_slides}{s_pri}{s_close}</body></html>")
    return HTML(string=html, base_url=BASE).write_pdf()


def _hdr(logo, name, no):
    return (f"<div class='hdr'><img src='{logo}'/><div class='sec'>{_esc(name)} · {_esc(no)}</div></div>")

def _ftr(page):
    return (f"<div class='ftr'><span>Energovision s.r.o. · Dôverné</span><span>{_esc(page)}</span></div>")

def _card(label, val, unit, bullets, color):
    bl = "".join(f"<div>{_esc(b)}</div>" for b in bullets if b)
    return (f"<div class='card'><div class='ch' style='background:{color}'>{_esc(label)}</div>"
            f"<div class='cb'><div class='cval'>{val}</div><div class='cunit'>{_esc(unit)}</div>"
            f"<div class='cbul'>{bl}</div></div></div>")
