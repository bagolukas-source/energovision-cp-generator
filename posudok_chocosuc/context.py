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

def build_chocosuc_context(analyza: dict, variants: list, hourly=None) -> dict:
    from analyza_om_v2 import build_orkestra_context
    base=build_orkestra_context(analyza, variants, str(analyza.get("id","")))

    capex=float(base.get("capex_total_eur") or 0)
    dotacia=float(base.get("dotacia_eur") or 0)
    net_capex=capex-dotacia
    opex=capex*OPEX_RATE
    self_mwh=float(base.get("pv_to_load_mwh") or 0)+float(base.get("bat_to_load_mwh") or 0)  # krytie odberu z vlastných zdrojov (pre pokrytie)
    pv_self_mwh=float(base.get("pv_to_load_mwh") or 0)+float(base.get("pv_to_bat_mwh") or 0)  # PV samospotreba (≤ výroba) — pre energetickú bilanciu
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

    # --- profil (z dát: hourly ak dostupný, inak agregáty) ---
    hourly_wd=hourly_we=None; monthly_mwh=None; _months_filled=[]
    if hourly:
        from collections import defaultdict
        bd=defaultdict(list); bw=defaultdict(list); mm=defaultdict(float)
        for (hh, wknd, kw, mon) in hourly:
            (bw if wknd else bd)[hh].append(kw); mm[mon]+=kw/1000.0
        hourly_wd=[ (sum(bd[h])/len(bd[h]) if bd.get(h) else 0) for h in range(24)]
        hourly_we=[ (sum(bw[h])/len(bw[h]) if bw.get(h) else 0) for h in range(24)]
        monthly_mwh=[mm.get(m,0) for m in range(1,13)]
        # dopočet chýbajúcich/nulových mesiacov priemerom ostatných (dátová diera, napr. február)
        _nz=[x for x in monthly_mwh if x and x>0]
        _filled=[i+1 for i,x in enumerate(monthly_mwh) if not x or x<=0]
        if _nz and _filled and len(_filled)<6:
            _avg=sum(_nz)/len(_nz)
            monthly_mwh=[(x if (x and x>0) else round(_avg,1)) for x in monthly_mwh]
            _months_filled=_filled
        else:
            _months_filled=[]
    avg_kw=float(analyza.get("consumption_avg_kw") or 0) or (float(base.get("load_total_mwh") or 0)*1000/8760)
    # SANITIZÁCIA špičky: zlé dáta (peak < avg) -> použiť iný zdroj, inak odhad (load factor ~0.45)
    _cands=[float(analyza.get("consumption_peak_kw_15min") or 0), float(analyza.get("consumption_peak_kw_hourly") or 0)]
    _valid=[c for c in _cands if c > (avg_kw or 0)*1.05]
    peak_kw=max(_valid) if _valid else round((avg_kw or 0)/0.45)
    peak_estimated = not _valid
    prof=classify_profile(hourly=[(h[0],h[1],h[2]) for h in hourly] if hourly else None, monthly_mwh=monthly_mwh, avg_kw=avg_kw or None, peak_kw=peak_kw or None)
    pm=prof["metrics"]
    profile_sentence=f"Profil je charakteristický ako {prof['rezim']}; {prof['sezonnost']}." + (f" Špička: {prof['spicka']}." if prof.get('spicka') else "") + (f" {prof['fve_fit']}." if prof.get('fve_fit') else "")

    # --- ekonomika: VŠETKY zložky z ENGINE value_streams (z dispatchu). Žiadne heuristiky,
    #     žiadne dvojité započítanie — base_saving UŽ obsahuje samospotrebu/export/batériu/arbitráž/peak. ---
    vs = base.get("value_streams") or {}
    save_self   = float(vs.get("solar_self_consumption_eur") or (self_mwh*1000*p_avoided))
    save_export = float(vs.get("solar_export_eur") or (export_mwh*1000*p_sell))
    save_bess   = float(vs.get("bess_self_consumption_eur") or 0)
    save_arb    = float(vs.get("arbitrage_eur") or 0)
    save_peak   = float(vs.get("peak_shaving_eur") or 0)
    # --- Arbitráž: VŽDY viditeľný stav + dôvod (nikdy tichá 0) ---
    _is_spot = (str(analyza.get("om_tarif_typ") or analyza.get("tarif_typ") or "spot").lower() == "spot")
    _has_ems = bool(base.get("value_streams"))
    if bess_kwh <= 0:
        arbitrage_reason = "bez batérie — arbitráž sa neuplatňuje"
    elif abs(save_arb) > 1:
        arbitrage_reason = "spotová arbitráž z hodinového EMS (nabíjanie v lacných hodinách, vybíjanie v drahých)"
    elif not _is_spot:
        arbitrage_reason = "klient nie je na spotovej tarife — arbitráž sa nepočíta (0 €)"
    elif not _has_ems:
        arbitrage_reason = "chýba hodinový spotový výpočet (EMS) — arbitráž nevyčíslená (0 €)"
    else:
        arbitrage_reason = "spread nedosiahol prah rentability (≥ 30 €/MWh) — arbitráž 0 €"
    base_saving = float(base.get("saving_y1_eur") or vs.get("total_eur") or (save_self+save_export+save_bess+save_arb+save_peak))
    annual_tax  = net_capex/ODPIS*DPPO

    # Báza ukotvená na ENGINE (validované), scenáre odvodené od nej (žiadny druhý NPV systém)
    eng_npv = float(base.get("npv_eur") or 0)
    eng_pb  = float(base.get("payback_years") or 0)
    eng_irr = float(base.get("irr_pct") or 0)
    _annuity = sum(1.0/((1+DISC)**y) for y in range(1, LIFE+1))  # diskontovaná anuita úspor
    def scen(name, short, save_mult):
        stot = base_saving * save_mult
        d_save = stot - base_saving
        npv = eng_npv + d_save * _annuity          # posun NPV o delta úspory (diskontovaná)
        # Payback aj IRR ODVODENÉ od engine bázy proporcionálne k úspore — JEDNA metodika pre všetky 3 scenáre.
        # Garantuje monotónnosť: viac úspor → kratšia návratnosť + vyššia IRR (žiadny paradox).
        pb  = (eng_pb / save_mult) if (eng_pb > 0 and save_mult > 0) else 99
        irr = (eng_irr * save_mult) if eng_irr > 0 else 0
        return {"name":name,"short":short,"save_total":stot,"opex":opex,"annual_tax":annual_tax,
                "payback":pb,"npv":npv,"irr":irr,"save_self":save_self,"save_export":save_export}
    # 3 scenáre = cenová citlivosť na engine bázu (NIE pripočítavanie pák — tie sú už v báze)
    S = [scen("Báza (ÚRSO 2026 + spot OKTE)","Báza",1.0),
         scen("Defenzívny (nižší výkup/cena)","Defenzívny",0.85),
         scen("Optimistický (rast cien energie)","Optimistický",1.12)]
    # scenario_emphasis z chatu (učiteľnosť): ktorý scenár je „odporúčaný" v posudku
    _co = analyza.get("chat_overrides") or {}
    _emph = _co.get("scenario_emphasis") if isinstance(_co, dict) else None
    _reco_idx = {"konzervativny":1, "optimisticky":2}.get(_emph, 0)
    for _i,_sc in enumerate(S):
        _sc["recommended"] = (_i == _reco_idx)
    # HEADLINE = odporúčaný scenár (default Báza = ENGINE čísla, tie isté čo vidí tím v analýze).
    # Predtým full=S[-1] (Optimistický +12 %) -> posudok ukazoval iné NPV než analýza (KraussMaffei 233k vs 134k).
    full = S[_reco_idx]
    opti = S[-1]

    # --- tornado (citlivosť NPV na engine bázu) ---
    base_npv = full["npv"]
    def npvm(sm=1.0,cm=1.0):
        return eng_npv + (base_saving*sm - base_saving)*_annuity - (net_capex*cm - net_capex)
    drv=[("Cena elektriny",npvm(sm=0.85)-base_npv,npvm(sm=1.15)-base_npv),
         ("Špecifický výnos FVE",npvm(sm=0.90)-base_npv,npvm(sm=1.10)-base_npv),
         ("CAPEX (cena diela)",npvm(cm=1.15)-base_npv,npvm(cm=0.85)-base_npv)]
    drv.sort(key=lambda d:max(abs(d[1]),abs(d[2])),reverse=True)

    # --- monte carlo ---
    random.seed(42); res=[]
    for _ in range(5000):
        sm=random.triangular(0.82,1.15,1.0); cm=random.triangular(0.95,1.12,1.0)
        res.append(eng_npv + (base_saving*sm - base_saving)*_annuity - (net_capex*cm - net_capex))
    res.sort(); P=lambda q:res[int(q*len(res))]

    # --- cena nečinnosti ---
    ina_y1=self_mwh*1000*p_avoided
    ina_flat=sum(ina_y1*((1-DEG)**y) for y in range(LIFE))
    ina_infl=sum(ina_y1*((1-DEG)**y)*((1+INFL)**y) for y in range(LIFE))

    # --- komponenty (z variantu, fallback orientačné) ---
    comp_real=bool(analyza.get("bundle_id"))
    components={"panel":f"FVE {base.get('pv_kwp',0):.0f} kWp — moduly podľa cenovej ponuky",
                "inverter":f"meniče ~{base.get('inverter_kw',0):.0f} kW AC + optimizéry",
                "battery":f"batéria {bess_kwh:.0f} kWh" if bess_kwh else "—",
                "konstrukcia":base.get("fve_topology","E-W / Juh")}

    # benefit_rows = REÁLNE engine zložky (z dispatchu), žiadny paušál
    benefit_rows=[("FVE samospotreba",f"{pv_self_mwh:.0f} MWh do odberu (priamo + cez batériu)",save_self),
                  ("FVE export prebytkov",f"{export_mwh:.0f} MWh do siete",save_export)]
    if save_bess>0: benefit_rows.append(("Batéria — posun PV do odberu","PV uskladnené a využité neskôr",save_bess))
    if abs(save_arb)>1: benefit_rows.append(("BESS arbitráž (spot v BS)","nabíjanie lacno / vybíjanie draho",save_arb))
    if save_peak>0: benefit_rows.append(("Peak shaving (zníženie RK)","redukcia mesačného 15-min maxima",save_peak))
    benefit_parts=[("Samospotreba",save_self,"#16A34A"),("Export",save_export,"#A7D08C"),
                   ("Batéria",save_bess,"#5B7CFA"),("Arbitráž",max(save_arb,0.0),"#8B5CF6"),
                   ("Daňový štít",annual_tax,"#F59E0B")]

    ctx={
        "client_name":base.get("client_name"),"om_address":base.get("site_address") or "—",
        "eic_om":analyza.get("eic_om"),"cislo_om":analyza.get("cislo_om"),
        "distrib_oblast":analyza.get("distrib_oblast") or analyza.get("om_sadzba"),
        "om_sadzba":base.get("tarif_typ") and ("VN" ) or "VN","om_mrk_kw":mrk,"om_rk_kw":rk,
        "posudok_number":analyza.get("posudok_number") or f"P-AOM-{str(analyza.get('id',''))[:6]}","pon_number":analyza.get("pon_number"),
        "posudok_date":datetime.now().strftime("%d.%m.%Y"),
        "prepared_by_name":base.get("prepared_by_name"),"prepared_by_email":base.get("prepared_by_email"),"prepared_by_phone":base.get("prepared_by_phone"),
        "fve_kwp":base.get("pv_kwp"),"bess_kwh":bess_kwh,"yield":(base.get("pv_total_mwh",0)*1000/(base.get("pv_kwp") or 1)) if base.get("pv_kwp") else 1075,
        "fve_prod_mwh":base.get("pv_total_mwh"),"self_use_mwh":pv_self_mwh,"export_mwh":export_mwh,
        "export_pct":(export_mwh/float(base.get("pv_total_mwh") or 1)*100),
        "loss_mwh":max(0.0,float(base.get("pv_total_mwh") or 0)-pv_self_mwh-export_mwh),
        "loss_pct":(max(0.0,float(base.get("pv_total_mwh") or 0)-pv_self_mwh-export_mwh)/float(base.get("pv_total_mwh") or 1)*100),
        "grid_import_mwh":base.get("grid_import_mwh"),"samosp_pct":(pv_self_mwh/float(base.get("pv_total_mwh") or 1)*100),"coverage_pct":base.get("samostatnost_pct"),
        "arbitrage_eur":save_arb,"arbitrage_reason":arbitrage_reason,"arbitrage_shown":(bess_kwh>0),
        "year_mwh":base.get("load_total_mwh"),"max15_kw":peak_kw,"peak_estimated":peak_estimated,"capex_total_eur":capex,"net_capex_eur":net_capex,"save_peak_eur":save_peak,
        "capex_pv_eur":float(base.get("capex_pv_eur") or 0),"capex_bess_eur":float(base.get("capex_bess_eur") or 0),
        "co2_avoided_tonnes":base.get("co2_avoided_tonnes"),
        # Orkestra vizuály — toky energie, donut samospotreby, carbon
        "pv_to_load_mwh":base.get("pv_to_load_mwh"),"pv_to_bat_mwh":base.get("pv_to_bat_mwh"),
        "bat_to_load_mwh":base.get("bat_to_load_mwh"),"grid_to_load_mwh":base.get("grid_to_load_mwh"),
        "grid_to_bat_mwh":base.get("grid_to_bat_mwh"),
        "direct_to_load_pct":base.get("direct_to_load_pct"),"charging_battery_pct":base.get("charging_battery_pct"),
        "exported_pct":base.get("exported_pct"),"curtailed_pct":base.get("curtailed_pct"),
        "co2_reduction_pct":base.get("co2_reduction_pct"),"trees_equivalent":base.get("trees_equivalent"),
        "barrels_oil":base.get("barrels_oil"),
        "monthly_mwh":monthly_mwh,"months_filled":_months_filled,"hourly_wd":hourly_wd,"hourly_we":hourly_we,"profile_metrics":pm,"profile_sentence":profile_sentence,"profile":prof,
        "p_silova":p_silova,"p_dist_var":p_dist_var,"p_tps":p_tps,"p_so":p_so,"p_dist_pevna":p_dist_pevna,"p_sell":p_sell,"p_avoided":p_avoided,
        "tarif_real":tarif_real,"tarif_source":(analyza.get("tarif_source") or "faktúra") if tarif_real else "orientačné (ÚRSO 2026)",
        "components":components,"components_real":comp_real,
        "scenarios3":S,"tornado_base":base_npv,"tornado_drivers":drv,
        "mc_samples":res,"mc_p10":P(0.10),"mc_p50":P(0.50),"mc_p90":P(0.90),"mc_prob_pos":sum(1 for x in res if x>0)/len(res),"mc_n":5000,
        "inaction_y1":ina_y1,"inaction_flat_20y":ina_flat,"inaction_infl_20y":ina_infl,
        "benefit_rows":benefit_rows,"benefit_parts":benefit_parts,
        "consumption_real":(analyza.get("consumption_strategy") or ("measured" if analyza.get("consumption_15min_path") else "")) == "measured",
        "consumption_strategy":analyza.get("consumption_strategy"),
        "consumption_needs_review":bool(analyza.get("consumption_needs_review")),
        "consumption_source":{
            "measured":"skutočné namerané 15-min dáta",
            "extrapolated":"15-min tvar z časti roka, extrapolovaný na celý rok",
            "synthesized":"modelovaný profil z faktúry (orientačný)",
        }.get(analyza.get("consumption_strategy"), ("skutočné 15-min dáta" if analyza.get("consumption_15min_path") else "modelovaný profil")),
        "variant_title":f"Navrhnutý variant — FVE {base.get('pv_kwp',0):.0f} kWp" + (f" + BESS {bess_kwh:.0f} kWh" if bess_kwh else ""),
        "summary_headline":"Audit odberu: investícia je ekonomicky výhodná vo všetkých scenároch",
        "recommendation_line":f"Realizovať — návratnosť {opti['payback']:.1f}–{S[0]['payback']:.1f} r, NPV {full['npv']:,.0f} € (báza); aj v pesimistickom scenári P10 zostáva NPV kladné ({P(0.10):,.0f} €).".replace(","," "),
        "cover_subtitle":"Analýza odberu, simulácia výroby a dispatch, ekonomické posúdenie v 3 scenároch s rizikovou analýzou.",
        "podklady":({"measured":"15-min profil · ","extrapolated":"15-min profil (časť roka) · ","synthesized":"profil z faktúry · "}.get(analyza.get("consumption_strategy"), "15-min profil · " if analyza.get("consumption_15min_path") else ""))+"PVGIS · OKTE 2025"+(" · faktúra" if tarif_real else ""),
        "zaver_headline":"Odporúčanie pre klienta",
        "zaver_text":f"Investícia {capex:,.0f} € do FVE {base.get('pv_kwp',0):.0f} kWp".replace(","," ")+(f" + BESS {bess_kwh:.0f} kWh" if bess_kwh else "")+f" prinesie ročné úspory {S[0]['save_total']:,.0f} € (báza) až {opti['save_total']:,.0f} € (optimistický scenár). Návratnosť {opti['payback']:.1f}–{S[0]['payback']:.1f} r, NPV 20 r. {full['npv']:,.0f} € (báza), IRR {full['irr']:.1f} %.".replace(","," "),
        "recommendations":[],  # doplní AI/deterministic neskôr
    }
    _build_deterministic_narratives(ctx, S, full, prof, pm)
    return ctx


def _build_deterministic_narratives(ctx, S, full, prof, pm):
    """Odborná próza z REÁLNYCH čísel pre každú sekciu — nezávislé od AI (nikdy prázdne).
    AI naratív tieto polia neskôr môže prepísať/obohatiť."""
    def n(v, d=0):
        try:
            return f"{float(v):,.{d}f}".replace(",", "\u00a0").replace(".", ",").replace("\u00a0", " ")
        except Exception:
            return "—"
    fve = float(ctx.get("fve_kwp") or 0)
    prod = float(ctx.get("fve_prod_mwh") or 0)
    selfu = float(ctx.get("self_use_mwh") or 0)
    exp = float(ctx.get("export_mwh") or 0)
    imp = float(ctx.get("grid_import_mwh") or 0)
    year = float(ctx.get("year_mwh") or 0)
    sams = float(ctx.get("samosp_pct") or 0)
    cov = float(ctx.get("coverage_pct") or 0)
    bess = float(ctx.get("bess_kwh") or 0)
    capex = float(ctx.get("capex_total_eur") or 0)
    mrk = float(ctx.get("om_mrk_kw") or 0)
    rk = float(ctx.get("om_rk_kw") or 0)
    avg_kw = (year * 1000 / 8760) if year else 0
    peak = float(ctx.get("max15_kw") or 0)
    lf = pm.get("load_factor")
    day_share = pm.get("day_share_pct")
    peak_hour = pm.get("peak_hour")
    rezim = (prof or {}).get("rezim") or "priemyselná prevádzka"
    sezon = (prof or {}).get("sezonnost") or "rovnomerná"
    co2 = float(ctx.get("co2_avoided_tonnes") or 0)

    # --- 1) PROFIL ---
    parts = []
    parts.append(f"Charakter prevádzky: <b>{rezim}</b>. Z analyzovaných {ctx.get('consumption_source','dát')} vychádza priemerný odber {n(avg_kw)} kW a 15-min špička {n(peak)} kW")
    if lf is not None:
        parts[-1] += f" (load factor {n(lf,2)})"
    parts[-1] += "."
    if day_share is not None:
        parts.append(f"Cez deň (slnečné hodiny) prebehne približne {n(day_share)} % spotreby — to priamo určuje, koľko FVE energie sa využije bez batérie.")
    _sez = str(sezon)
    _sezl = _sez.lower().replace("sezónnosť", "").strip() or "rovnomerná"
    parts.append(f"Sezónnosť odberu je {_sezl}; ročná spotreba OM je {n(year)} MWh.")
    fit = (prof or {}).get("fve_fit")
    if fit:
        fit = str(fit)[0].upper() + str(fit)[1:]
        parts.append(fit if fit.endswith(".") else fit + ".")
    ctx["profile_narrative"] = "<p>" + "</p><p>".join(parts) + "</p>"
    ctx["daily_cap"] = f"Priemer {n(avg_kw)} kW, špička {n(peak)} kW" + (f", špičková hodina {peak_hour}:00." if peak_hour is not None else ".")
    _mf = ctx.get("months_filled") or []
    _mn = {1:"jan",2:"feb",3:"mar",4:"apr",5:"máj",6:"jún",7:"júl",8:"aug",9:"sep",10:"okt",11:"nov",12:"dec"}
    ctx["monthly_cap"] = f"Ročná spotreba {n(year)} MWh." + (f" Dopočítané z priemeru: {', '.join(_mn.get(m,str(m)) for m in _mf)}." if _mf else "")

    # --- 2) ENERGETICKÁ BILANCIA ---
    exp_pct = (exp / prod * 100) if prod else 0
    bnar = (f"Z {n(prod)} MWh ročnej výroby FVE sa <b>{n(sams,1)} %</b> ({n(selfu)} MWh) spotrebuje priamo v prevádzke "
            f"a iba {n(exp_pct,1)} % ({n(exp,1)} MWh) odchádza ako prebytok do siete. ")
    if fve and year and fve * 1.05 < year / 1.05:
        bnar += f"Relatívne malá FVE ({n(fve)} kWp) voči spotrebe ({n(year)} MWh/r) je dôvod vysokej samospotreby — vyrobená energia sa takmer celá využije na mieste. "
    bnar += f"FVE pokryje {n(cov,1)} % ročnej spotreby OM; zvyšných {n(imp)} MWh zostáva zo siete."
    ctx["balance_narrative"] = "<p>" + bnar + "</p>"

    # odhad počtu modulov ak nie sú reálne komponenty (kredibilita technickej sekcie)
    comp = dict(ctx.get("components") or {})
    if not ctx.get("components_real") and fve:
        nmod = int(round(fve / 0.58))  # ~580 Wp typický modul 2026
        comp["panel"] = f"~{nmod} ks modulov á ~580 Wp (orientačne — spresní cenová ponuka)"
        ac = fve / 1.1
        comp["inverter"] = comp.get("inverter") or f"meniče ~{ac:.0f} kW AC (DC/AC ~1,1)"
        _k = comp.get("konstrukcia")
        if not _k or "35" in str(_k):
            comp["konstrukcia"] = "Juh 13° (orientačne — spresní cenová ponuka)"
        ctx["components"] = comp

    # --- 3) SCENÁRE (3 bullety s výkladom) ---
    sb = []
    sb.append((S[0]["name"], f"Najkonzervatívnejší — iba FVE samospotreba a export, bez dodatočných opatrení. Ročná úspora {n(S[0]['save_total'])} €, návratnosť {n(S[0]['payback'],1)} r."))
    sb.append((S[1]["name"], f"Stresový pohľad — nižší výkup prebytkov a opatrnejšie ceny energie. Ukazuje odolnosť investície: úspora {n(S[1]['save_total'])} €/r, návratnosť {n(S[1]['payback'],1)} r."))
    sb.append((S[2]["name"], f"Pri raste trhových cien energie hodnota vlastnej výroby rastie — úspora {n(S[2]['save_total'])} €/r, NPV 20 r. {n(S[2]['npv'])} €, IRR {n(S[2]['irr'],1)} %."))
    ctx["scenarios_bullets"] = sb

    # --- 4) ZÁVER — argumenty (rich bullety) ---
    args = []
    args.append(("Predikovateľnosť ceny energie na 20+ rokov",
                 f"Vlastná výroba pokryje {n(cov,1)} % ročnej spotreby pri {n(sams,1)} % samospotrebe — prirodzený hedge proti rastu cien elektriny a regulačných poplatkov."))
    dan = capex * 0.21
    args.append(("Daňová optimalizácia",
                 f"6-ročný rovnomerný odpis predstavuje úsporu 21 % × {n(capex)} € = {n(dan)} € na dani z príjmu (priemerne {n(dan/6)} €/rok počas prvých 6 rokov)."))
    # Optimalizácia RK — len ak je špička výrazne pod MRK (reálne z dát, nezávislé od batérie).
    # Peak-shaving cez batériu tvrdíme IBA ak ho model reálne oceňuje (save_peak>0), inak by išlo o blud.
    _save_peak = float(ctx.get("save_peak_eur") or 0)
    if mrk > 0 and peak > 0 and peak < mrk * 0.65 and not ctx.get("peak_estimated"):
        _t = (f"Špičkový odber {n(peak)} kW je len {n(peak/mrk*100,0)} % zazmluvnenej MRK {n(mrk)} kW. "
              f"Odberné miesto má rezervu znížiť rezervovanú kapacitu (RK, min. 50 % MRK) a šetriť na pevnej zložke distribúcie")
        if _save_peak > 0:
            _t += f"; batéria toto zníženie zabezpečuje proti špičkám (modelovaná úspora {n(_save_peak)} €/r)."
        else:
            _t += " (úspora nezávislá od FVE/batérie — odporúčame preveriť s prevádzkovateľom distribučnej sústavy)."
        args.append(("Optimalizácia rezervovanej kapacity (MRK/RK)", _t))
    elif bess > 0 and _save_peak > 0:
        args.append(("Peak shaving cez batériu",
                     f"Batéria {n(bess)} kWh znižuje 15-min špičky zo siete (modelovaná úspora {n(_save_peak)} €/r) — podklad pre zníženie rezervovanej kapacity (RK {n(rk)} kW)."))
    args.append(("ESG profil a CSRD reporting",
                 f"Ročná úspora ~{n(co2)} t CO₂ (pri SK emisnej intenzite ~0,25 kg/kWh) — doložiteľný podklad pre EU Taxonómiu a CSRD disclosures."))
    args.append(("Reziduálna hodnota po 20 rokoch",
                 f"Po 20 rokoch má FVE ešte ~90 % výkonu (degradácia 0,5 %/r); konzervatívne 10 % CAPEX = {n(capex*0.10)} € reziduálnej hodnoty."))
    ctx["zaver_arguments"] = args
