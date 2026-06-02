# -*- coding: utf-8 -*-
"""Context builder pre ChocoSuc-grade posudok — z analyza+variants -> ctx.
Deterministické fakty + profil (z dát) + tarif (fallback) + 3 scenáre + tornado + MC + cena nečinnosti.
"""
import random, statistics as st
from datetime import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from profile_classifier import classify_profile

DEG=0.005; INFL=0.025; DISC=0.06; LIFE=20; OPEX_RATE=0.015; ODPIS=6; DPPO=0.21; RESID=0.10

def _npv(save_total, capex, opex, annual_tax):
    npv=-capex
    for y in range(1,LIFE+1):
        s=save_total*((1-DEG)**(y-1))*((1+INFL)**(y-1))
        tax=annual_tax if y<=ODPIS else 0
        npv+=(s-opex+tax)/((1+DISC)**y)
    npv+=(capex*RESID)/((1+DISC)**LIFE)
    return npv

def _irr(save_total, capex, opex, annual_tax):
    lo,hi=-0.5,1.5
    def f(r):
        v=-capex
        for y in range(1,LIFE+1):
            s=save_total*((1-DEG)**(y-1))*((1+INFL)**(y-1)); tax=annual_tax if y<=ODPIS else 0
            v+=(s-opex+tax)/((1+r)**y)
        v+=(capex*RESID)/((1+r)**LIFE); return v
    for _ in range(60):
        m=(lo+hi)/2
        if f(m)>0: lo=m
        else: hi=m
    return m*100

def build_chocosuc_context(analyza: dict, variants: list) -> dict:
    from analyza_om_v2 import build_orkestra_context
    base=build_orkestra_context(analyza, variants, str(analyza.get("id","")))

    capex=float(base.get("capex_total_eur") or 0)
    dotacia=float(base.get("dotacia_eur") or 0)
    net_capex=capex-dotacia
    opex=capex*OPEX_RATE
    self_mwh=float(base.get("pv_to_load_mwh") or 0)+float(base.get("bat_to_load_mwh") or 0)
    export_mwh=float(base.get("pv_to_grid_mwh") or 0)
    bess_kwh=float(base.get("bess_kwh") or 0)
    mrk=float(base.get("mrk_kw") or 0); rk=float(analyza.get("om_rk_kw") or mrk*0.9 or 0)

    # --- tarif (z faktúry, inak ÚRSO fallback) ---
    def g(k,default):
        v=analyza.get(k)
        try: return float(v) if v not in (None,"") else default
        except Exception: return default
    p_silova=g("tarif_silova_eur_mwh",None)
    tarif_real = p_silova is not None
    p_silova=(p_silova/1000) if p_silova is not None else 0.0955
    p_dist_var=g("tarif_distribucia_eur_mwh",None); p_dist_var=(p_dist_var/1000) if p_dist_var is not None else 0.0101
    p_tps=g("tarif_tps_eur_mwh",None); p_tps=(p_tps/1000) if p_tps is not None else 0.0114
    p_so=g("tarif_oze_eur_mwh",None); p_so=(p_so/1000) if p_so is not None else 0.0048
    p_dist_pevna=g("tarif_fix_mes_eur",None) or 8.02
    p_sell=g("tarif_sell",None); p_sell=(p_sell) if (p_sell and p_sell<1) else 0.06
    p_avoided=p_silova+p_dist_var

    # --- profil (z dát: agregáty + mesačné; hourly ak je) ---
    monthly_eur=base.get("monthly_solar_to_load") or []
    monthly_mwh=base.get("_monthly_mwh") or None
    avg_kw=float(analyza.get("consumption_avg_kw") or 0) or (float(base.get("load_total_mwh") or 0)*1000/8760)
    # SANITIZÁCIA špičky: zlé dáta (peak < avg) -> použiť iný zdroj, inak odhad (load factor ~0.45)
    _cands=[float(analyza.get("consumption_peak_kw_15min") or 0), float(analyza.get("consumption_peak_kw_hourly") or 0)]
    _valid=[c for c in _cands if c > (avg_kw or 0)*1.05]
    peak_kw=max(_valid) if _valid else round((avg_kw or 0)/0.45)
    peak_estimated = not _valid
    prof=classify_profile(hourly=base.get("_hourly"), monthly_mwh=monthly_mwh, avg_kw=avg_kw or None, peak_kw=peak_kw or None)
    pm=prof["metrics"]
    profile_sentence=f"Profil je charakteristický ako {prof['rezim']}; {prof['sezonnost']}." + (f" Špička: {prof['spicka']}." if prof.get('spicka') else "") + (f" {prof['fve_fit']}." if prof.get('fve_fit') else "")

    # --- ekonomika: Báza z ENGINE (deterministicky správne), tarif len pre rozpad/display ---
    vs=base.get("value_streams") or {}
    save_self=float(vs.get("solar_self_consumption_eur") or 0) or (self_mwh*1000*p_avoided)
    save_export=float(vs.get("solar_export_eur") or 0) or (export_mwh*1000*p_sell)
    base_saving=float(base.get("saving_y1_eur") or (save_self+save_export))
    annual_tax=net_capex/ODPIS*DPPO
    # increments — len pri BESS (peak shaving / arbitráž potrebujú batériu)
    new_p95=float(base.get("_new_p95_kw") or 0)
    rk_new=max(round((new_p95 or rk*0.55)/10)*10, rk*0.55) if rk else rk
    save_peak=((rk-rk_new)*p_dist_pevna*12) if (bess_kwh>0 and rk and rk_new<rk) else 0
    save_arb=bess_kwh*60 if bess_kwh>0 else 0
    def scen(name,short,extra,use_engine=False):
        stot=base_saving+extra
        npv=_npv(stot,net_capex,opex,annual_tax); irr=_irr(stot,net_capex,opex,annual_tax)
        pb=(net_capex/(stot+annual_tax-opex)) if (stot+annual_tax-opex)>0 else 99
        return {"name":name,"short":short,"save_self":save_self,"save_export":save_export,
                "save_peak":(save_peak if 'Peak' in name or extra>=save_peak>0 else 0),
                "save_total":stot,"opex":opex,"annual_tax":annual_tax,"payback":pb,"npv":npv,"irr":irr}
    if bess_kwh>0:
        S=[scen("Báza (samospotreba + export)","Báza",0,use_engine=True),
           scen("+ Peak shaving (zníženie RK)","+ Peak shaving",save_peak),
           scen("+ BESS arbitráž (plný)","+ BESS arbitráž",save_peak+save_arb)]
    else:
        # FVE-only: cenové scenáre (Báza / nízky výkup / optimistický)
        S=[scen("Báza (ÚRSO 2026)","Báza",0,use_engine=True),
           scen("Nízky výkup (defenzívny)","Nízky výkup",-save_export*0.5),
           scen("Optimistický (rast cien)","Optimistický",base_saving*0.10)]
    full=max(S,key=lambda x:x["npv"]); 
    # zoradiť: Báza prvá, full posledná
    S=[s for s in S if s is not full and s["name"].startswith("Báza")]+[s for s in S if not s["name"].startswith("Báza") and s is not full]+[full]

    # --- tornado ---
    base_npv=full["npv"]
    def npvm(sm=1.0,cm=1.0): return _npv(full["save_total"]*sm,net_capex*cm,opex,annual_tax)
    drv=[("Cena elektriny",npvm(sm=0.85)-base_npv,npvm(sm=1.15)-base_npv),
         ("Špecifický výnos FVE",npvm(sm=0.90)-base_npv,npvm(sm=1.10)-base_npv),
         ("CAPEX (cena diela)",npvm(cm=1.15)-base_npv,npvm(cm=0.85)-base_npv)]
    if bess_kwh>0: drv.append(("BESS arbitráž",-save_arb*6,save_arb*3))
    drv.sort(key=lambda d:max(abs(d[1]),abs(d[2])),reverse=True)

    # --- monte carlo ---
    random.seed(42); res=[]
    for _ in range(5000):
        sm=random.triangular(0.82,1.15,1.0); cm=random.triangular(0.95,1.12,1.0)
        res.append(_npv(full["save_total"]*sm,net_capex*cm,opex,annual_tax))
    res.sort(); P=lambda q:res[int(q*len(res))]

    # --- cena nečinnosti ---
    ina_y1=self_mwh*1000*p_avoided
    ina_flat=sum(ina_y1*((1-DEG)**y) for y in range(LIFE))
    ina_infl=sum(ina_y1*((1-DEG)**y)*((1+INFL)**y) for y in range(LIFE))

    # --- komponenty (z variantu, fallback orientačné) ---
    sel=base.get("_selected") or {}
    comp_real=bool(analyza.get("bundle_id"))
    components={"panel":f"FVE {base.get('pv_kwp',0):.0f} kWp — moduly podľa cenovej ponuky",
                "inverter":f"meniče ~{base.get('inverter_kw',0):.0f} kW AC + optimizéry",
                "battery":f"batéria {bess_kwh:.0f} kWh" if bess_kwh else "—",
                "konstrukcia":base.get("fve_topology","E-W / Juh")}

    benefit_rows=[("FVE samospotreba",f"{self_mwh:.0f} MWh × {p_avoided*1000:.1f} €/MWh",save_self),
                  ("FVE export",f"{export_mwh:.0f} MWh × {p_sell*1000:.0f} €/MWh",save_export)]
    if save_peak>0: benefit_rows.append(("Peak shaving (RK)",f"({rk:.0f}→{rk_new:.0f} kW) × {p_dist_pevna:.2f} €/kW/mes × 12",save_peak))
    if save_arb>0: benefit_rows.append(("BESS arbitráž",f"{bess_kwh:.0f} kWh × 60 €/kWh/r",save_arb))
    benefit_parts=[("Samospotreba",save_self,"#16A34A"),("Export",save_export,"#A7D08C"),("Peak shaving",save_peak,"#5B7CFA"),("Arbitráž",save_arb,"#8B5CF6"),("Daňový štít",annual_tax,"#F59E0B")]

    ctx={
        "client_name":base.get("client_name"),"om_address":base.get("site_address") or "—",
        "eic_om":analyza.get("eic_om"),"cislo_om":analyza.get("cislo_om"),
        "distrib_oblast":analyza.get("distrib_oblast") or analyza.get("om_sadzba"),
        "om_sadzba":base.get("tarif_typ") and ("VN" ) or "VN","om_mrk_kw":mrk,"om_rk_kw":rk,
        "posudok_number":analyza.get("posudok_number") or f"P-AOM-{str(analyza.get('id',''))[:6]}","pon_number":analyza.get("pon_number"),
        "posudok_date":datetime.now().strftime("%d.%m.%Y"),
        "prepared_by_name":base.get("prepared_by_name"),"prepared_by_email":base.get("prepared_by_email"),"prepared_by_phone":base.get("prepared_by_phone"),
        "fve_kwp":base.get("pv_kwp"),"bess_kwh":bess_kwh,"yield":(base.get("pv_total_mwh",0)*1000/(base.get("pv_kwp") or 1)) if base.get("pv_kwp") else 1075,
        "fve_prod_mwh":base.get("pv_total_mwh"),"self_use_mwh":self_mwh,"export_mwh":export_mwh,
        "grid_import_mwh":base.get("grid_import_mwh"),"samosp_pct":base.get("samospotreba_pct"),"coverage_pct":base.get("samostatnost_pct"),
        "year_mwh":base.get("load_total_mwh"),"max15_kw":peak_kw,"peak_estimated":peak_estimated,"capex_total_eur":capex,"net_capex_eur":net_capex,
        "co2_avoided_tonnes":base.get("co2_avoided_tonnes"),
        "monthly_mwh":monthly_mwh,"profile_metrics":pm,"profile_sentence":profile_sentence,"profile":prof,
        "p_silova":p_silova,"p_dist_var":p_dist_var,"p_tps":p_tps,"p_so":p_so,"p_dist_pevna":p_dist_pevna,"p_sell":p_sell,"p_avoided":p_avoided,
        "tarif_real":tarif_real,"tarif_source":(analyza.get("tarif_source") or "faktúra") if tarif_real else "orientačné (ÚRSO 2026)",
        "components":components,"components_real":comp_real,
        "scenarios3":S,"tornado_base":base_npv,"tornado_drivers":drv,
        "mc_samples":res,"mc_p10":P(0.10),"mc_p50":P(0.50),"mc_p90":P(0.90),"mc_prob_pos":sum(1 for x in res if x>0)/len(res),"mc_n":5000,
        "inaction_y1":ina_y1,"inaction_flat_20y":ina_flat,"inaction_infl_20y":ina_infl,
        "benefit_rows":benefit_rows,"benefit_parts":benefit_parts,
        "consumption_real":bool(analyza.get("consumption_15min_path")),
        "consumption_source":("skutočné 15-min dáta" if analyza.get("consumption_15min_path") else "modelovaný profil"),
        "variant_title":f"Navrhnutý variant — FVE {base.get('pv_kwp',0):.0f} kWp" + (f" + BESS {bess_kwh:.0f} kWh" if bess_kwh else ""),
        "summary_headline":"Audit odberu: investícia je ekonomicky výhodná vo všetkých scenároch",
        "recommendation_line":f"Realizovať — návratnosť {full['payback']:.1f}–{S[0]['payback']:.1f} r, NPV {full['npv']:,.0f} €, kladné NPV s pravdepodobnosťou {sum(1 for x in res if x>0)/len(res)*100:.0f} %.".replace(","," "),
        "cover_subtitle":"Analýza odberu, simulácia výroby a dispatch, ekonomické posúdenie v 3 scenároch s rizikovou analýzou.",
        "podklady":("15-min profil · " if analyza.get("consumption_15min_path") else "")+"PVGIS · OKTE 2025"+(" · faktúra" if tarif_real else ""),
        "zaver_headline":"Odporúčanie pre klienta",
        "zaver_text":f"Investícia {capex:,.0f} € do FVE {base.get('pv_kwp',0):.0f} kWp".replace(","," ")+(f" + BESS {bess_kwh:.0f} kWh" if bess_kwh else "")+f" prinesie ročné úspory {S[0]['save_total']:,.0f} € (báza) až {full['save_total']:,.0f} € (plný scenár). Návratnosť {full['payback']:.1f}–{S[0]['payback']:.1f} r, NPV 20 r. {full['npv']:,.0f} €, IRR {full['irr']:.1f} %.".replace(","," "),
        "recommendations":[],  # doplní AI/deterministic neskôr
    }
    return ctx
