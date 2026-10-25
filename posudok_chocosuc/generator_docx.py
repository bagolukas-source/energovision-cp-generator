# -*- coding: utf-8 -*-
"""ChocoSuc-grade posudok ako natívny DOCX (python-docx, funguje na Render bez libreoffice).
Mirror PDF: identifikácia, profil (grafy), technika+bilancia, ekonomika (tarif, 3 scenáre, grafy),
skladba+riziko (tornado/MC), cena nečinnosti, odporúčania, záver, predpoklady. Číta ten istý ctx.
Jazyk (sk/en/de) podľa aom_i18n.use_lang okolo volania; preklady v aom_i18n/catalogs/posudok_docx.py."""
import io, base64, re
from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from aom_i18n import _, get_lang
from aom_i18n.fmt import eur, num, pbt, dec, fmt_date  # noqa: F401  (dec/fmt_date pre jednotné API)

GREEN=RGBColor(0x16,0xA3,0x4A); LIME=RGBColor(0x92,0xD0,0x50); DARK=RGBColor(0x1A,0x1A,0x1A); GREY=RGBColor(0x6B,0x72,0x80)
_DOC_LANG={"en":"en-GB","de":"de-DE"}  # sk dokument ostáva bez zmeny

def _png(data_uri):
    if not data_uri or "," not in data_uri: return None
    return io.BytesIO(base64.b64decode(data_uri.split(",",1)[1]))
def _strip(html):
    return re.sub(r"<[^>]+>","",html or "").strip()

def _set_lang(doc):
    tag=_DOC_LANG.get(get_lang())
    if not tag: return
    doc.core_properties.language=tag
    rpr=doc.styles["Normal"].element.get_or_add_rPr()
    el=rpr.find(qn("w:lang"))
    if el is None: el=OxmlElement("w:lang"); rpr.append(el)
    el.set(qn("w:val"),tag)

def _nbsp_eur(doc):
    """en/de: „19.186 €" / „70 Tsd. €" sa nezalomí (NBSP pred €); sk dokument bez zmeny."""
    if get_lang()=="sk": return
    paras=list(doc.paragraphs)+[p for t in doc.tables for row in t.rows for c in row.cells for p in c.paragraphs]
    for p in paras:
        for r in p.runs:
            if " €" in r.text:
                r.text=re.sub(r"(\d) €","\\1\u00a0€",re.sub(r"(\d) (Tsd\.) €","\\1\u00a0\\2\u00a0€",r.text))

def _shade(cell,hexc):
    sh=OxmlElement("w:shd"); sh.set(qn("w:val"),"clear"); sh.set(qn("w:fill"),hexc); cell._tc.get_or_add_tcPr().append(sh)
def _kick(doc,t):
    p=doc.add_paragraph(); r=p.add_run(t.upper()); r.bold=True; r.font.size=Pt(8); r.font.color.rgb=GREEN
    r.font.name="Arial"; pf=p.paragraph_format; pf.space_after=Pt(2); pf.space_before=Pt(10)
    return p
def _h(doc,t,size=14):
    p=doc.add_paragraph(); r=p.add_run(t); r.bold=True; r.font.size=Pt(size); r.font.color.rgb=DARK; r.font.name="Arial"
    p.paragraph_format.space_after=Pt(6); return p
def _para(doc,t,size=10,color=DARK,italic=False):
    p=doc.add_paragraph(); r=p.add_run(t); r.font.size=Pt(size); r.font.color.rgb=color; r.italic=italic; r.font.name="Arial"
    p.paragraph_format.space_after=Pt(4); return p
def _table(doc,headers,rows,aligns=None,widths=None):
    t=doc.add_table(rows=1,cols=len(headers)); t.alignment=WD_TABLE_ALIGNMENT.CENTER
    try: t.style="Table Grid"
    except Exception: pass
    for i,hd in enumerate(headers):
        c=t.rows[0].cells[i]; _shade(c,"F0F7F0"); p=c.paragraphs[0]; r=p.add_run(str(hd)); r.bold=True; r.font.size=Pt(8.5); r.font.name="Arial"; r.font.color.rgb=GREY
    for row in rows:
        cells=t.add_row().cells
        for i,v in enumerate(row):
            p=cells[i].paragraphs[0]; r=p.add_run(str(v)); r.font.size=Pt(9); r.font.name="Arial"
            if aligns and i<len(aligns) and aligns[i]=="r": p.alignment=WD_ALIGN_PARAGRAPH.RIGHT
    return t
def _img(doc,data_uri,width_cm=16.5,cap=None):
    b=_png(data_uri)
    if b:
        doc.add_picture(b,width=Cm(width_cm))
        doc.paragraphs[-1].alignment=WD_ALIGN_PARAGRAPH.CENTER
    if cap: _para(doc,cap,size=8,color=GREY,italic=True)

def generate_chocosuc_docx(ctx: dict) -> bytes:
    import posudok_chocosuc.charts as C
    doc=Document(); _set_lang(doc)
    for sec in doc.sections:
        sec.top_margin=Cm(1.8); sec.bottom_margin=Cm(1.6); sec.left_margin=Cm(1.7); sec.right_margin=Cm(1.7)
    S=ctx["scenarios3"]; bza=S[0]; full=next((x for x in S if x.get("recommended")), bza); opti=S[-1]; pm=ctx.get("profile_metrics",{})

    # COVER
    _kick(doc, _("Technicko-ekonomický posudok · {n}").format(n=ctx.get('posudok_number',''))
          + ((" "+_("(k {pon})").format(pon=ctx['pon_number'])) if ctx.get('pon_number') else ""))
    _h(doc, ctx.get("client_name",""), size=22)
    _para(doc, _("FVE {kwp} kWp").format(kwp=num(ctx.get('fve_kwp'))) + (f" + BESS {num(ctx.get('bess_kwh'))} kWh" if ctx.get('bess_kwh') else ""), size=11)
    _para(doc, ctx.get("cover_subtitle",""), size=9, color=GREY, italic=True)

    # EXEC SUMMARY
    _kick(doc,_("Manažérske zhrnutie")); _h(doc, ctx.get("summary_headline",""))
    _para(doc, _("Odporúčanie:")+" "+ctx.get("recommendation_line",""), size=10.5)
    _table(doc,[_("Ukazovateľ"),_("Hodnota")],[
        [_("Investícia (Net CAPEX)"),eur(ctx.get("net_capex_eur") or ctx.get("capex_total_eur"))],
        [_("Ročný prínos (rok 1)"),eur(full["save_total"])],
        [_("Návratnosť"),pbt(full['payback'])],
        [_("NPV 20 r. / IRR"),f"{eur(full['npv'])} / {num(full['irr'],1)} %"],
        [_("Pokrytie spotreby / samospotreba"),f"{num(ctx.get('coverage_pct'),1)} % / {num(ctx.get('samosp_pct'),1)} %"],
    ],aligns=["l","r"])
    _summary_html = ctx.get("ai_summary_html") or ctx.get("ai_commentary_html") or ""
    if _strip(_summary_html):
        for chunk in re.split(r"</p>|\n\n", _summary_html):
            txt=_strip(chunk)
            if txt: _para(doc,txt,size=10)

    # 1 IDENTIFIKÁCIA
    _kick(doc,"1 — "+_("Východiská a identifikácia OM")); _h(doc,_("Stav odberného miesta a vstupné dáta"))
    idr=[[_("Klient"),ctx.get("client_name","")]]
    for lab,val in [(_("Adresa OM"),ctx.get("om_address")),(_("EIC OM / č. miesta"), (f"{ctx.get('eic_om')} / {ctx.get('cislo_om')}" if (ctx.get('eic_om') or ctx.get('cislo_om')) else None)),
                    (_("Distribučná oblasť"),ctx.get("distrib_oblast")),(_("Sadzba / tarif"),ctx.get("om_sadzba"))]:
        if val and str(val).strip() not in ("—","None","None / None"): idr.append([lab,val])
    idr+=[[_("Ročná spotreba"),f"{num(ctx.get('year_mwh'))} MWh"],[_("Max 15-min odber"),f"{num(ctx.get('max15_kw'))} kW"],
          [_("Priemerný odber / load factor"),f"{num(pm.get('avg_kw'))} kW / {num(pm.get('load_factor'),2)}"],
          [_("MRK / RK"),f"{num(ctx.get('om_mrk_kw'))} / {num(ctx.get('om_rk_kw'))} kW"]]
    _table(doc,[_("Údaj"),_("Hodnota")],idr,aligns=["l","l"])
    _para(doc,_("Charakteristika prevádzky (odvodená z dát):")+" "+ctx.get("profile_sentence",""),size=9.5,italic=True)

    # 2 PROFIL
    doc.add_page_break(); _kick(doc,"2 — "+_("Profil odberu")); _h(doc,_("Charakteristika spotreby"))
    _img(doc,C.chart_daily(ctx),cap=_("Graf 1: Denný profil odberu — pracovný deň vs víkend, s PV produkciou."))
    _img(doc,C.chart_monthly(ctx),cap=_("Graf 2: Mesačná spotreba."))

    # 3 TECHNIKA + BILANCIA
    doc.add_page_break(); _kick(doc,"3 — "+_("Technické riešenie")); _h(doc, ctx.get("variant_title",_("Navrhnutý variant")))
    comp=ctx.get("components") or {}
    _table(doc,[_("Parameter"),_("Hodnota")],[
        [_("Inštalovaný výkon FVE"),f"{num(ctx.get('fve_kwp'),2)} kWp DC"],
        [_("Moduly"),comp.get("panel","—")],[_("Meniče / optimizéry"),comp.get("inverter","—")],
        [_("Batériové úložisko"),comp.get("battery","—") if ctx.get("bess_kwh") else "—"],
        [_("Konštrukcia"),comp.get("konstrukcia","—")],
        [_("Predpokladaná ročná výroba"),f"{num(ctx.get('fve_prod_mwh'))} MWh"],
    ],aligns=["l","l"])
    _kick(doc,_("Energetická bilancia"))
    _table(doc,[_("Veličina"),_("Hodnota"),_("Podiel")],[
        [_("Ročná výroba FVE"),f"{num(ctx.get('fve_prod_mwh'))} MWh","100 %"],
        [_("Samospotreba"),f"{num(ctx.get('self_use_mwh'))} MWh",f"{num(ctx.get('samosp_pct'),1)} %"],
        [_("Export prebytkov"),f"{num(ctx.get('export_mwh'))} MWh",f"{num(ctx.get('export_pct') or 0,1)} %"],
        [_("Import zo siete"),f"{num(ctx.get('grid_import_mwh'))} MWh","—"],
        [_("Pokrytie spotreby OM"),f"{num(ctx.get('coverage_pct'),1)} %",_("FVE vs spotreba")],
    ],aligns=["l","r","r"])
    _img(doc,C.chart_energy_balance(ctx),cap=_("Graf 3: Energetická bilancia."))

    # 4 EKONOMIKA
    doc.add_page_break(); _kick(doc,"4 — "+_("Ekonomické posúdenie")); _h(doc,_("Cenové predpoklady"))
    _table(doc,[_("Zložka ceny"),_("Hodnota"),_("Zdroj")],[
        [_("Silová zložka"),f"{num(ctx['p_silova']*1000,2)} €/MWh",ctx.get("tarif_source","")],
        [_("Variabilná distribúcia"),f"{num(ctx['p_dist_var']*1000,2)} €/MWh",_("VSD tarif")],
        [_("TPS + systémové služby"),f"{num((ctx['p_tps']+ctx['p_so'])*1000,2)} €/MWh",_("pásmo 2")],
        [_("Avoided cost samospotreby"),f"{num(ctx['p_avoided']*1000,1)} €/MWh",_("kompozit")],
        [_("Pevná zložka distribúcie"),_("{v} €/kW/mes").format(v=num(ctx['p_dist_pevna'],2)),f"× RK {num(ctx.get('om_rk_kw'))} kW"],
    ],aligns=["l","r","l"])
    _h(doc,_("Tri scenáre prínosu"),size=12)
    _table(doc,[_("Scenár"),_("Úspora €/r"),_("Návratnosť"),_("NPV 20 r."),"IRR"],
        [[s["name"],eur(s["save_total"]),pbt(s['payback']),eur(s["npv"]),f"{num(s['irr'],1)} %"] for s in S],
        aligns=["l","r","r","r","r"])
    _img(doc,C.chart_scenarios(ctx),cap=_("Graf 4: Porovnanie 3 scenárov."))
    _img(doc,C.chart_cumcf(ctx),cap=_("Graf 5: Kumulatívny cashflow 20 rokov."))

    # 5 SKLADBA + RIZIKO
    doc.add_page_break(); _kick(doc,"5 — "+_("Skladba prínosu a riziková analýza")); _h(doc,_("Skladba ročného prínosu"))
    _table(doc,[_("Zdroj prínosu"),_("Detail"),_("Hodnota")],
        [[n,f,eur(v)] for n,f,v in ctx.get("benefit_rows",[])]+[[_("Ročná úspora SPOLU"),"",eur(full["save_total"])]],
        aligns=["l","l","r"])
    _img(doc,C.chart_benefit(ctx),cap=_("Graf 6: Skladba ročného prínosu."))
    mc_img=C.chart_montecarlo(ctx)
    _img(doc,C.chart_tornado(ctx),cap=_("Graf 7: Tornado — citlivosť NPV."))
    _img(doc,mc_img,cap=_("Graf 8: Monte Carlo — P10 {p10}, medián {p50}, P90 {p90}; pravdepodobnosť kladného NPV {prob} %.").format(
        p10=eur(ctx['mc_p10']),p50=eur(ctx['mc_p50']),p90=eur(ctx['mc_p90']),prob=num(ctx.get('mc_prob_pos',0)*100,0)))

    # 6 CENA NEČINNOSTI
    doc.add_page_break(); _kick(doc,"6 — "+_("Cena nečinnosti")); _h(doc,_("Koľko stojí ponechať súčasný stav"))
    _table(doc,[_("Scenár cien energie"),_("Náklad za 20 rokov"),_("Rok 1")],[
        [_("Konštantné ceny"),eur(ctx.get("inaction_flat_20y")),eur(ctx.get("inaction_y1"))],
        [_("Rast cien o 2,5 %/r"),eur(ctx.get("inaction_infl_20y")),eur(ctx.get("inaction_y1"))],
    ],aligns=["l","r","r"])

    # 7 ODPORÚČANIA
    _kick(doc,"7 — "+_("Expert posúdenie a odporúčania"))
    _expert_html = ctx.get("ai_expert_html") or ""
    if _strip(_expert_html):
        for chunk in re.split(r"</p>|\n\n", _expert_html):
            txt=_strip(chunk)
            if txt: _para(doc,txt,size=10)
    _h(doc,_("Odporúčané kroky"))
    for i,(t,d) in enumerate(ctx.get("recommendations",[])):
        p=doc.add_paragraph(); r=p.add_run(f"{i+1:02d}  {t}"); r.bold=True; r.font.size=Pt(10); r.font.name="Arial"
        _para(doc,d,size=9,color=GREY)

    # 8 ZÁVER
    doc.add_page_break(); _kick(doc,"8 — "+_("Záver")); _h(doc, ctx.get("zaver_headline",""))
    _para(doc, ctx.get("zaver_text",""), size=10.5)
    _kick(doc,_("Záruky a istoty"))
    for li in [_("Výkonová záruka na panely (25–30 r.) a záruka výrobcu na meniče a batériu."),
               _("Realizácia, revízie a uvedenie do prevádzky pod jednou zodpovednosťou Energovision."),
               _("Monitoring výroby a úspor po spustení; servis počas celej životnosti."),
               _("Skúsenosť s riešeniami pre porovnateľné priemyselné prevádzky.")]:
        doc.add_paragraph(li, style="List Bullet")

    # 9 PREDPOKLADY
    _kick(doc,"9 — "+_("Predpoklady a zdroje")); _h(doc,_("Metodika a východiská"))
    _table(doc,[_("Predpoklad / vstup"),_("Hodnota"),_("Zdroj")],[
        [_("Bilančná simulácia"),f"{num(8760)} h",ctx.get("consumption_source","")],
        [_("Špecifický výnos FVE"),f"{num(ctx.get('yield'))} kWh/kWp","PVGIS"],
        [_("Diskont / horizont / odpis"),_("6 % / 20 r / 6 r"),_("WACC / životnosť / zákon")],
        [_("Tarify"),ctx.get("tarif_source","") if ctx.get("tarif_real") else _("orientačné"),_("faktúra / ÚRSO 2026")],
    ],aligns=["l","r","l"])
    _para(doc,_("Model má neistotu ±10–15 %. Čísla pochádzajú zo simulácie dispatchu (energetická bilancia validovaná). Posudok je nezáväzný odborný odhad."),size=8.5,color=GREY)
    _para(doc,_("Kontakt: {name} · {email} · {phone}").format(name=ctx.get('prepared_by_name',''),email=ctx.get('prepared_by_email',''),phone=ctx.get('prepared_by_phone','')),size=9)

    _nbsp_eur(doc)
    out=io.BytesIO(); doc.save(out); return out.getvalue()
