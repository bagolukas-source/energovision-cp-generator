# -*- coding: utf-8 -*-
"""ChocoSuc-grade posudok generator — HTML -> PDF (WeasyPrint), context-driven."""
import base64, os
from pathlib import Path
from weasyprint import HTML
from . import charts as C

_HERE = Path(__file__).parent
_LOGO = _HERE.parent / "analyza_om" / "logo.png"

def _logo_b64():
    try:
        return "data:image/png;base64,"+base64.b64encode(open(_LOGO,"rb").read()).decode()
    except Exception:
        return ""

def eur(v):
    try: return f"{float(v):,.0f} €".replace(","," ")
    except Exception: return "—"
def num(v,d=0):
    try: return (f"{float(v):,.{d}f}").replace(","," ").replace(".",",")
    except Exception: return "—"

_CA=[]
def trow(cells, head=False, em=None, align=None):
    global _CA
    if align is not None: _CA=align
    elif head: _CA=['l']*len(cells)
    a=_CA if _CA else ['l']*len(cells)
    tag="th" if head else "td"
    return f'<tr class="{em or ""}">'+ "".join(f'<{tag} class="c{(a[i] if i<len(a) else "l")}">{c}</{tag}>' for i,c in enumerate(cells)) + '</tr>'

def render_chocosuc_html(ctx: dict) -> str:
    g_daily=C.chart_daily(ctx); g_month=C.chart_monthly(ctx); g_bal=C.chart_energy_balance(ctx)
    g_scen=C.chart_scenarios(ctx); g_cum=C.chart_cumcf(ctx); g_ben=C.chart_benefit(ctx)
    g_tor=C.chart_tornado(ctx); g_mc=C.chart_montecarlo(ctx)
    g_donut=C.chart_solar_donut(ctx); g_flow=C.chart_energy_flow(ctx)
    S=ctx["scenarios3"]; bza=S[0]; full=S[-1]
    pm=ctx.get("profile_metrics",{})
    recs=ctx.get("recommendations",[])
    summ_nar=ctx.get("ai_summary_html") or ctx.get("ai_commentary_html") or ""
    exp_nar=ctx.get("ai_expert_html") or ctx.get("ai_commentary_html") or ""
    has_addr = ctx.get("om_address") and ctx.get("om_address")!="—"
    # tarif rows (s fallback flagom)
    tflag = "" if ctx.get("tarif_real") else " <span style='color:#B45309'>(predpoklad)</span>"
    comp = ctx.get("components") or {}
    comp_real = ctx.get("components_real")
    cflag = "" if comp_real else " <span style='color:#B45309'>(orientačné)</span>"

    # --- identifikačná tabuľka: vynechaj chýbajúce voliteľné riadky, zvyšok do "na doplnenie" ---
    _missing=[]
    def _orow(label, value, comment="", required=False):
        v=value
        if v in (None,"","—","None / None","— / —") or (isinstance(v,str) and v.strip() in ("—","None")):
            if not required:
                _missing.append(label); return ""
            v="—"
        return trow([label, v, comment])
    eic=ctx.get('eic_om'); cislo=ctx.get('cislo_om')
    eic_val=(f"{eic} / {cislo}" if (eic or cislo) else None)
    _rows="".join([
        trow(["Klient",ctx.get('client_name',''),"VN odberateľ"]),
        _orow("Adresa OM",ctx.get('om_address'),ctx.get('lokalita_note','')),
        _orow("EIC OM / č. miesta",eic_val,"z faktúry"),
        _orow("Distribučná oblasť",ctx.get('distrib_oblast'),""),
        _orow("Sadzba / tarif",ctx.get('om_sadzba'),ctx.get('vtstnt','')),
        trow(["Ročná spotreba",f"{num(ctx.get('year_mwh'))} MWh",ctx.get('consumption_source','')]),
        trow(["Max 15-min odber",f"{num(ctx.get('max15_kw'))} kW","odhad z profilu" if ctx.get("peak_estimated") else ""]),
        trow(["Priemerný odber",f"{num(pm.get('avg_kw'))} kW",f"load factor {num(pm.get('load_factor'),2)}"]),
        trow(["MRK / RK",f"{num(ctx.get('om_mrk_kw'))} / {num(ctx.get('om_rk_kw'))} kW",f"špička {num(ctx.get('max15_kw'))} kW = {num((ctx.get('max15_kw') or 0)/(ctx.get('om_mrk_kw') or 1)*100,0)} % MRK"]),
    ])
    id_table=f'<table>{trow(["Údaj","Hodnota","Komentár"],head=True)}{_rows}</table>'
    if _missing:
        id_table+=f'<p class="note">Údaje na doplnenie z faktúry/zmluvy: {", ".join(_missing)}.</p>'

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size:A4; margin:18mm 16mm 16mm 16mm;
  @top-left {{ content: element(hdr); }}
  @bottom-center {{ content: "Energovision, s.r.o.  ·  IČO 53 036 280  ·  www.energovision.sk  ·  strana " counter(page) " / " counter(pages); font-family:Arial; font-size:7pt; color:#9CA3AF; }} }}
#hdr {{ position: running(hdr); font-family:Arial; font-size:7.5pt; color:#9CA3AF; }}
#hdr b {{ color:#5E8E2A; }}
* {{ box-sizing:border-box; }}
body {{ font-family:Arial, sans-serif; color:#1A1A1A; font-size:9.3pt; line-height:1.5; margin:0; }}
h1 {{ font-size:17pt; margin:0 0 8px; }} h2 {{ font-size:13pt; margin:2px 0 8px; page-break-after:avoid; }}
.kick {{ font-size:7.6pt; font-weight:bold; letter-spacing:2.6px; color:#5E8E2A; text-transform:uppercase; margin:0 0 4px; page-break-after:avoid; }}
.sub {{ color:#6B7280; font-size:9pt; margin:0 0 12px; }}
.cover-pg {{ page-break-after:always; }} .newpage {{ page-break-before:always; }}
section {{ page-break-inside:auto; }}
table {{ width:100%; border-collapse:collapse; margin:6px 0 10px; font-size:8.7pt; page-break-inside:avoid; }}
th {{ background:#F0F7F0; color:#374151; padding:6px 9px; border-bottom:1.5px solid #C7E0C7; font-size:8pt; }}
td {{ padding:5px 9px; border-bottom:1px solid #EEF1F4; vertical-align:top; }}
td,th {{ text-align:left; }} .cl {{ text-align:left; }} .cr {{ text-align:right; }}
tr.em td {{ background:#F0F7F0; font-weight:bold; }}
.kpis {{ display:flex; gap:10px; margin:10px 0 6px; page-break-inside:avoid; }}
.kpi {{ flex:1; background:#F8FAFB; border-radius:8px; padding:11px 13px; border-left:3px solid #92D050; }}
.kpi .l {{ font-size:7pt; letter-spacing:1.4px; color:#9CA3AF; text-transform:uppercase; font-weight:bold; }}
.kpi .v {{ font-size:15pt; font-weight:bold; color:#1A1A1A; margin-top:2px; }}
.kpi .n {{ font-size:7.4pt; color:#6B7280; font-style:italic; }}
.banner {{ background:#F0F7F0; border-left:4px solid #5E8E2A; padding:12px 16px; border-radius:6px; margin:8px 0 12px; page-break-inside:avoid; }}
.banner .t {{ font-size:7.6pt; font-weight:bold; letter-spacing:2px; color:#5E8E2A; text-transform:uppercase; }}
.banner .b {{ font-size:11pt; font-weight:bold; margin-top:3px; }}
.hero {{ background:#EFF6E8; border-radius:10px; padding:14px 18px; margin:12px 0; display:flex; align-items:center; justify-content:space-between; page-break-inside:avoid; }}
.hero .big {{ font-size:23pt; font-weight:bold; color:#5E8E2A; }} .hero .lbl {{ font-size:8.6pt; color:#475569; max-width:46%; }}
.benefits {{ display:flex; gap:9px; margin:12px 0 4px; page-break-inside:avoid; }}
.bcard {{ flex:1; background:#FFF; border:1px solid #E8EDF2; border-top:3px solid #92D050; border-radius:8px; padding:10px 11px; }}
.bcard .h {{ font-weight:bold; font-size:9.2pt; margin-bottom:2px; }} .bcard .d {{ font-size:7.8pt; color:#64748B; line-height:1.35; }}
.note {{ font-size:7.8pt; color:#9CA3AF; margin:4px 0 0; }}
.img {{ width:100%; margin:4px 0 2px; page-break-inside:avoid; }} .cap {{ font-size:7.6pt; color:#9CA3AF; font-style:italic; margin:0 0 12px; }}
.rec {{ margin:0 0 9px; padding-left:30px; position:relative; page-break-inside:avoid; }}
.rec .nthe {{ position:absolute; left:0; top:0; font-size:13pt; font-weight:bold; color:#92D050; }}
.rec b {{ display:block; font-size:9.6pt; }} .rec span {{ color:#374151; font-size:8.7pt; }}
.two {{ display:flex; gap:16px; }} .two>div {{ flex:1; }}
ul.green {{ list-style:none; padding:0; margin:6px 0; }}
ul.green li {{ position:relative; padding-left:16px; margin-bottom:6px; font-size:8.9pt; }}
ul.green li:before {{ content:"●"; color:#92D050; position:absolute; left:0; }}
.narr p {{ margin:0 0 8px; }}
</style></head><body>
<div id="hdr"><b>energovision</b>  ·  Posudok · {ctx.get('client_name','')} · {ctx.get('posudok_number','')}</div>

<section class="cover-pg">
  <div style="text-align:right; margin-bottom:30px;">{f'<img src="{_logo_b64()}" style="height:42px;">' if _logo_b64() else '<b style="font-size:20pt;color:#5E8E2A;">energovision</b>'}</div>
  <div class="kick">Technicko-ekonomický posudok &nbsp;·&nbsp; {ctx.get('posudok_number','')}{(' (k '+ctx['pon_number']+')') if ctx.get('pon_number') else ''}</div>
  <h1 style="font-size:28pt; margin:6px 0;">{ctx.get('client_name','')}</h1>
  <div style="font-size:11pt; color:#374151;">FVE {num(ctx.get('fve_kwp'),0)} kWp{(' + BESS '+num(ctx.get('bess_kwh'),0)+' kWh') if ctx.get('bess_kwh') else ''}</div>
  <div style="font-size:9pt; color:#9CA3AF; font-style:italic; margin-top:8px;">{ctx.get('cover_subtitle','Analýza odberu, simulácia výroby a ekonomické posúdenie v 3 scenároch s rizikovou analýzou.')}</div>
  <div style="display:flex; gap:36px; margin-top:30px; align-items:flex-end;">
    <div><div class="kick">Čistý prínos · NPV 20 r.</div><div style="font-size:32pt; font-weight:bold; color:#5E8E2A; line-height:1.0;">+{eur(full['npv'])}</div></div>
    <div><div class="kick">Návratnosť</div><div style="font-size:32pt; font-weight:bold; color:#1A1A1A; line-height:1.0;">{num(full['payback'],1)} r</div></div>
    <div><div class="kick">IRR</div><div style="font-size:32pt; font-weight:bold; color:#1A1A1A; line-height:1.0;">{num(full['irr'],0)} %</div></div>
  </div>
  <div class="two" style="margin-top:40px;">
    <div><div class="kick">Pre</div><div style="font-weight:bold;">{ctx.get('client_name','')}</div>
      <div style="color:#374151; font-size:8.6pt;">{ctx.get('om_address','') if has_addr else ''}{('<br>EIC OM: '+ctx['eic_om']) if ctx.get('eic_om') else ''}</div>
      <div class="kick" style="margin-top:14px;">Parametre OM</div>
      <div style="font-size:8.6pt;">Spotreba {num(ctx.get('year_mwh'))} MWh/r<br>MRK {num(ctx.get('om_mrk_kw'))} kW · RK {num(ctx.get('om_rk_kw'))} kW · {ctx.get('om_sadzba','VN')}{('<br>'+str(ctx['distrib_oblast'])) if (ctx.get('distrib_oblast') and str(ctx.get('distrib_oblast')).lower()!='none') else ''}</div></div>
    <div><div class="kick">Vystavené</div><div style="font-weight:bold;">{ctx.get('posudok_date','')}</div>
      <div class="kick" style="margin-top:14px;">Podklady</div>
      <div style="font-size:8.6pt;">{ctx.get('podklady','15-min profil · faktúra · PVGIS · OKTE')}</div></div>
  </div>
  <div style="background:#EFF6E8; border-left:3px solid #92D050; border-radius:6px; padding:12px 16px; margin-top:30px;">
    <div class="kick">Pripravil pre Vás</div><div style="font-weight:bold;">{ctx.get('prepared_by_name','Lukáš Bago')}</div>
    <div style="font-size:8.6pt; color:#374151;">Energovision, s.r.o. · {ctx.get('prepared_by_email','')} · {ctx.get('prepared_by_phone','')}</div></div>
</section>

<section class="newpage">
  <div class="kick">Manažérske zhrnutie</div>
  <h2>{ctx.get('summary_headline','Investícia je ekonomicky výhodná')}</h2>
  <div class="banner"><div class="t">Odporúčanie</div><div class="b">{ctx.get('recommendation_line','')}</div></div>
  <div class="hero"><div class="lbl"><b>Čistý prínos investície (NPV 20 r.)</b> pri diskonte 6 % — po odpočítaní celej investície a prevádzkových nákladov.</div><div class="big">+{eur(full['npv'])}</div></div>
  <div class="kpis">
    <div class="kpi"><div class="l">Investícia</div><div class="v">{eur(ctx.get('capex_total_eur'))}</div><div class="n">bez DPH</div></div>
    <div class="kpi"><div class="l">Ročný prínos · rok 1</div><div class="v">{num(bza['save_total']/1000,0)}–{num(full['save_total']/1000,0)} tis. €</div><div class="n">báza → plný scenár</div></div>
    <div class="kpi"><div class="l">Návratnosť</div><div class="v">{num(full['payback'],1)}–{num(bza['payback'],1)} r</div><div class="n">s daňovým odpisom</div></div>
    <div class="kpi"><div class="l">IRR · NPV</div><div class="v">{num(full['irr'],0)} %</div><div class="n">NPV {eur(full['npv'])}</div></div>
  </div>
  <div class="kick" style="margin-top:10px;">Čo získate</div>
  <div class="benefits">
    <div class="bcard"><div class="h">Nižšie účty</div><div class="d">Úspora {eur(bza['save_total'])}–{eur(full['save_total'])}/rok na elektrine a distribúcii.</div></div>
    <div class="bcard"><div class="h">Stabilná cena 20+ r.</div><div class="d">Vlastná výroba je hedge proti rastu cien — chráni hodnotu až {eur(ctx.get('inaction_infl_20y',0))}.</div></div>
    <div class="bcard"><div class="h">Nezávislosť</div><div class="d">{num(ctx.get('coverage_pct'),1)} % spotreby z vlastného zdroja pri {num(ctx.get('samosp_pct'),1)} % samospotrebe.</div></div>
    <div class="bcard"><div class="h">ESG</div><div class="d">−{num(ctx.get('co2_avoided_tonnes'),0)} t CO₂ ročne; doložiteľný príspevok k udržateľnosti.</div></div>
  </div>
  <div class="narr" style="margin-top:8px;">{summ_nar}</div>
</section>

<section class="newpage">
  <div class="kick">1 — Východiská a identifikácia OM</div><h2>Stav odberného miesta a vstupné dáta</h2>
  {id_table}
  <div class="banner"><div class="t">Charakteristika prevádzky (odvodená z dát)</div><div style="font-size:8.8pt; margin-top:3px;">{ctx.get('profile_sentence','')}</div></div>
</section>

<section class="newpage">
  <div class="kick">2 — Profil odberu</div><h2>Charakteristika spotreby</h2>
  <div class="narr">{ctx.get('profile_narrative','')}</div>
  <img class="img" src="{g_daily}"><div class="cap">Graf 1: Denný profil odberu — pracovný deň vs víkend, s PV produkciou. {ctx.get('daily_cap','')}</div>
  <img class="img" src="{g_month}"><div class="cap">Graf 2: Mesačná spotreba. {ctx.get('monthly_cap','')}</div>
</section>

<section class="newpage">
  <div class="kick">3 — Technické riešenie</div><h2>{ctx.get('variant_title','Navrhnutý variant')}</h2>
  <table>{trow(["Parameter","Hodnota"],head=True)}
    {trow(["Inštalovaný výkon FVE",f"{num(ctx.get('fve_kwp'),2)} kWp DC"])}
    {trow([f"Moduly{cflag}",comp.get('panel','—')])}
    {trow(["Optimizéry / meniče",comp.get('inverter','—')])}
    {trow(["Batériové úložisko",comp.get('battery','—') if ctx.get('bess_kwh') else "—"])}
    {trow(["Konštrukcia",comp.get('konstrukcia','—')])}
    {trow(["Špecifický výnos",f"{num(ctx.get('yield'))} kWh/kWp"])}
    {trow(["Predpokladaná ročná výroba",f"{num(ctx.get('fve_prod_mwh'))} MWh"],em="em")}
  </table>
  <div class="narr">{ctx.get('technical_narrative','')}</div>
  <div class="kick" style="margin-top:8px;">Energetická bilancia</div>
  <table>{trow(["Veličina","Hodnota","Podiel"],head=True,align=['l','r','r'])}
    {trow(["Ročná výroba FVE",f"{num(ctx.get('fve_prod_mwh'))} MWh","100 %"])}
    {trow(["Samospotreba",f"{num(ctx.get('self_use_mwh'))} MWh",f"{num(ctx.get('samosp_pct'),1)} %"],em="em")}
    {trow(["Export prebytkov",f"{num(ctx.get('export_mwh'))} MWh",f"{num(100-(ctx.get('samosp_pct') or 0),1)} %"])}
    {trow(["Import zo siete",f"{num(ctx.get('grid_import_mwh'))} MWh","—"])}
    {trow(["Pokrytie spotreby OM",f"{num(ctx.get('coverage_pct'),1)} %","FVE vs spotreba"])}
  </table>
  <img class="img" src="{g_bal}"><div class="cap">Graf 3: Energetická bilancia.</div>
  <div class="kick" style="margin-top:10px;">Tok energie a využitie výroby</div>
  <img class="img" src="{g_flow}"><div class="cap">Graf 4: Ročný tok energie — výroba FVE, priama samospotreba, batéria a sieť (MWh/rok).</div>
  <img class="img" src="{g_donut}"><div class="cap">Graf 5: Ako sa využije vyrobená FVE energia — priamo, cez batériu, export.</div>
  <div class="kick" style="margin-top:12px;">Environmentálny prínos (CO₂)</div>
  <div class="benefits">
    <div class="bcard" style="border-top-color:#5E8E2A; background:#F4F8EE;"><div class="h" style="font-size:15pt; color:#5E8E2A;">−{num(ctx.get('co2_avoided_tonnes'),0)} t</div><div class="d">CO₂ ročne menej</div></div>
    <div class="bcard"><div class="h" style="font-size:15pt;">{num(ctx.get('co2_reduction_pct'),0)} %</div><div class="d">zníženie uhlíkovej stopy</div></div>
    <div class="bcard"><div class="h" style="font-size:15pt;">{num(ctx.get('trees_equivalent'),0)}</div><div class="d">ekvivalent vysadených stromov</div></div>
    <div class="bcard"><div class="h" style="font-size:15pt;">{num(ctx.get('barrels_oil'),0)}</div><div class="d">barelov ropy ušetrených</div></div>
  </div>
  <div class="narr">{ctx.get('balance_narrative','')}</div>
</section>

<section class="newpage">
  <div class="kick">4 — Ekonomické posúdenie</div><h2>Cenové predpoklady{tflag}</h2>
  <table>{trow(["Zložka ceny","Hodnota","Zdroj / komentár"],head=True,align=['l','r','l'])}
    {trow(["Silová zložka",f"{num(ctx['p_silova']*1000,2)} €/MWh",ctx.get('tarif_source','')])}
    {trow(["Variabilná distribúcia",f"{num(ctx['p_dist_var']*1000,2)} €/MWh","VSD tarif"])}
    {trow(["TPS + systémové služby",f"{num((ctx['p_tps']+ctx['p_so'])*1000,2)} €/MWh","pásmo 2"])}
    {trow(["Avoided cost samospotreby",f"{num(ctx['p_avoided']*1000,1)} €/MWh","kompozit"],em="em")}
    {trow(["Pevná zložka distribúcie",f"{num(ctx['p_dist_pevna'],2)} €/kW/mes",f"× RK {num(ctx.get('om_rk_kw'))} kW"])}
    {trow(["Výkupná cena prebytkov",f"{num(ctx['p_sell']*1000,0)} €/MWh","export"])}
  </table>
  <h2 style="margin-top:8px;">Tri scenáre prínosu</h2>
  <div class="narr">{ctx.get('scenarios_intro_html','')}</div>
  <table>{trow(["Scenár","Úspora €/r","Návratnosť","NPV 20 r.","IRR"],head=True,align=['l','r','r','r','r'])}
    {"".join(trow([s['name'],eur(s['save_total']),f"{num(s['payback'],1)} r",eur(s['npv']),f"{num(s['irr'],1)} %"],em=("em" if s is full else None)) for s in S)}
  </table>
  <div class="scenexpl">{"".join(f'<div class="rec"><b>{nm}</b><span>{tx}</span></div>' for nm,tx in ctx.get('scenarios_bullets',[]))}</div>
  <img class="img" src="{g_scen}"><div class="cap">Graf 4: Porovnanie 3 scenárov.</div>
  <img class="img" src="{g_cum}"><div class="cap">Graf 5: Kumulatívny cashflow 20 rokov.</div>
</section>

<section class="newpage">
  <div class="kick">5 — Skladba prínosu a riziková analýza</div><h2>Skladba ročného prínosu (plný scenár)</h2>
  <table>{trow(["Zdroj prínosu","Vzorec","Hodnota"],head=True,align=['l','r','r'])}
    {"".join(trow([n,f,eur(v)]) for n,f,v in ctx.get('benefit_rows',[]))}
    {trow(["Ročná úspora SPOLU","",eur(full['save_total'])],em="em")}
    {trow(["Daňový štít z odpisu (r. 1–6)","6-r lineárny odpis × DPPO 21 %",eur(full.get('annual_tax',0))])}
  </table>
  <img class="img" src="{g_ben}"><div class="cap">Graf 6: Skladba ročného prínosu.</div>
  <h2 style="margin-top:8px;">Citlivosť a riziko</h2>
  <img class="img" src="{g_tor}"><div class="cap">Graf 7: Tornado — citlivosť NPV na ±15 % driverov.</div>
  <img class="img" src="{g_mc}"><div class="cap">Graf 8: Monte Carlo ({ctx.get('mc_n',5000)} simulácií). P10 {eur(ctx['mc_p10'])}, medián {eur(ctx['mc_p50'])}, P90 {eur(ctx['mc_p90'])}; pravdepodobnosť kladného NPV {num(ctx.get('mc_prob_pos',0)*100,0)} %.</div>
</section>

<section class="newpage">
  <div class="kick">6 — Cena nečinnosti</div><h2>Koľko stojí ponechať súčasný stav</h2>
  <p>Energiu, ktorú FVE pokryje priamo do odberu, dnes nakupujete zo siete. Pri zachovaní súčasného stavu za ňu počas 20-ročnej životnosti zaplatíte:</p>
  <table>{trow(["Scenár cien energie","Náklad za 20 rokov","Rok 1"],head=True,align=['l','r','r'])}
    {trow(["Konštantné ceny",eur(ctx.get('inaction_flat_20y')),eur(ctx.get('inaction_y1'))])}
    {trow(["Rast cien o 2,5 %/r",eur(ctx.get('inaction_infl_20y')),eur(ctx.get('inaction_y1'))],em="em")}
  </table>
  <p class="note">Nečinnosť nie je neutrálna voľba — je to rozhodnutie naďalej platiť plnú trhovú cenu za energiu, ktorú si viete vyrobiť pri náklade {eur(ctx.get('capex_total_eur'))}.</p>
</section>

<section class="newpage">
  <div class="kick">7 — Expert posúdenie a odporúčania</div><h2>Odborné posúdenie</h2>
  <div class="narr">{exp_nar}</div>
  <div class="kick" style="margin-top:10px;">Odporúčané kroky</div>
  {"".join(f'<div class="rec"><span class="nthe">{i+1:02d}</span><b>{t}</b><span>{d}</span></div>' for i,(t,d) in enumerate(recs))}
</section>

<section class="newpage">
  <div class="kick">8 — Záver</div><h2>{ctx.get('zaver_headline','Odporúčanie pre klienta')}</h2>
  <div class="banner"><div style="font-size:9.4pt;">{ctx.get('zaver_text','')}</div></div>
  <div class="kick" style="margin-top:10px;">Argumenty pre realizáciu</div>
  {"".join(f'<div class="rec"><b>{t}</b><span>{d}</span></div>' for t,d in ctx.get('zaver_arguments',[]))}
  <div class="kick" style="margin-top:10px;">Záruky a istoty</div>
  <ul class="green">
    <li>Výkonová záruka na panely (typicky 25–30 r.) a záruka výrobcu na meniče a batériu.</li>
    <li>Realizácia, revízie a uvedenie do prevádzky pod jednou zodpovednosťou Energovision.</li>
    <li>Monitoring výroby a úspor po spustení; servis počas celej životnosti zdroja.</li>
    <li>Skúsenosť s riešeniami pre porovnateľné priemyselné prevádzky (FVE + BESS na VN).</li>
  </ul>
  <div class="kick" style="margin-top:10px;">Ako začneme</div>
  <table>{trow(["Krok","Obsah"],head=True)}
    {trow(["01 · Spätná väzba a obhliadka","Prejdeme posudok, doplníme vstupy, technická obhliadka OM."])}
    {trow(["02 · Akceptácia a zmluva","Odsúhlasenie špecifikácie a finančných podmienok, projekt a zmluva."])}
    {trow(["03 · Realizácia","Inštalácia FVE + BESS, integrácia s VN, žiadosť o zníženie RK."])}
    {trow(["04 · Spustenie a monitoring","Uvedenie do prevádzky, revízie, optimalizácia dispatchu, dohľad."])}
  </table>
  <div class="banner"><div class="t">Komplexná zodpovednosť — Energovision</div><div style="font-size:8.6pt; margin-top:3px;">Fotovoltika a batérie, údržba a servis trafostaníc, odborné revízie, elektrotechnické práce, energetický manažment a monitoring — jeden partner pre celý životný cyklus zdroja.</div></div>
</section>

<section>
  <div class="kick">9 — Predpoklady a zdroje</div><h2>Metodika a východiská</h2>
  <table>{trow(["Predpoklad / vstup","Hodnota","Zdroj"],head=True)}
    {trow(["Bilančná simulácia","8 760 h",ctx.get('consumption_source','15-min dáta')])}
    {trow(["Špecifický výnos FVE",f"{num(ctx.get('yield'))} kWh/kWp","PVGIS"])}
    {trow(["Diskont / horizont / odpis","6 % / 20 r / 6 r","WACC / životnosť / zákon"])}
    {trow(["Degradácia FVE / inflácia energie","0,5 % / 2,5 % ročne","výrobca / makro"])}
    {trow(["Tarify a ceny",ctx.get('tarif_source','—') if ctx.get('tarif_real') else "orientačné (faktúra nedodaná)","faktúra / ÚRSO 2026"])}
    {trow(["Komponenty a CAPEX",ctx.get('pon_number','—') if comp_real else "orientačné","cenová ponuka"])}
  </table>
  <p class="note">Model má neistotu rádovo ±10–15 %. Reálne výsledky závisia od nábehu prevádzky, finálnej tarify a podmienok pripojenia. {('Hodnoty samospotreby sú odvodené zo skutočných nameraných dát.' if ctx.get('consumption_real') else 'Časť vstupov je modelovaná — pri doplnení faktúry a 15-min dát sa presnosť zvýši.')} Posudok je nezáväzný odborný odhad.</p>
  <div style="background:#F8FAFB; border-radius:8px; padding:12px 16px; margin-top:10px;">
    <div class="kick">Kontakt</div><div style="font-weight:bold;">{ctx.get('prepared_by_name','Lukáš Bago')}</div>
    <div style="font-size:8.6pt; color:#374151;">{ctx.get('prepared_by_email','')} · {ctx.get('prepared_by_phone','')}</div></div>
</section>
</body></html>"""

def generate_chocosuc_pdf(ctx: dict) -> bytes:
    return HTML(string=render_chocosuc_html(ctx)).write_pdf()
