# -*- coding: utf-8 -*-
"""Context builder pre ChocoSuc-grade posudok — z analyza+variants -> ctx.
Deterministické fakty + profil (z dát) + tarif (fallback) + 3 scenáre + tornado + MC + cena nečinnosti.
"""
import random, statistics as st
from datetime import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from profile_classifier import classify_profile
from aom_i18n import _, get_lang
from aom_i18n.fmt import eur, num, pbt, fmt_date, months, volt

DEG=0.0; INFL=0.025; DISC=0.06; LIFE=20; OPEX_RATE=0.0; ODPIS=6; DPPO=0.21; RESID=0.10   # DEG/OPEX 0: bez poklesu výnosu a prevádzkových nákladov (2026-10-24)

def _pb(v):
    """Návratnosť alebo None (= nevráti sa); 99 je sentinel enginu, 0/None chýbajúca hodnota."""
    try: f=float(v)
    except (TypeError, ValueError): return None
    return f if 0 < f < 99 else None

def pb_txt(v):
    return pbt(_pb(v))

def _pb_range(lo, hi):
    """„7,8–9,0 r" / „7,8 r (báza: nevráti sa)" / „nevráti sa"."""
    lo, hi = _pb(lo), _pb(hi)
    if lo is None and hi is None: return pbt(None)
    if hi is None: return _("{pb} (báza: nevráti sa)").format(pb=pbt(lo))
    if lo is None: return pbt(hi)
    return f"{num(lo,1)}–{pbt(hi)}"

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
    for _it in range(60):
        m=(lo+hi)/2
        if f(m)>0: lo=m
        else: hi=m
    return m*100

def build_chocosuc_context(analyza: dict, variants: list, hourly=None, selected_row: dict = None) -> dict:
    from analyza_om_v2 import build_orkestra_context
    base=build_orkestra_context(analyza, variants, str(analyza.get("id","")), selected_row=selected_row)

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
    profile_sentence=(_("Profil je charakteristický ako {rezim}; {sezonnost}.").format(rezim=prof['rezim'], sezonnost=prof['sezonnost'])
                      + (" " + _("Špička: {spicka}.").format(spicka=prof['spicka']) if prof.get('spicka') else "")
                      + (f" {prof['fve_fit']}." if prof.get('fve_fit') and get_lang() == "sk"  # SK quirk: malé písmeno po bodke
                         else (f" {prof['fve_fit'][:1].upper()}{prof['fve_fit'][1:]}." if prof.get('fve_fit') else "")))

    # --- ekonomika: VŠETKY zložky z ENGINE value_streams (z dispatchu). Žiadne heuristiky,
    #     žiadne dvojité započítanie — base_saving UŽ obsahuje samospotrebu/export/batériu/arbitráž/peak. ---
    vs = base.get("value_streams") or {}
    # audit V: `or` fallback prepisoval legitímnu 0.0 (delta retrofit) celosystémovým odhadom
    save_self   = float(vs["solar_self_consumption_eur"]) if vs.get("solar_self_consumption_eur") is not None else float(self_mwh*1000*p_avoided)
    save_export = float(vs["solar_export_eur"]) if vs.get("solar_export_eur") is not None else float(export_mwh*1000*p_sell)
    save_bess   = float(vs.get("bess_self_consumption_eur") or 0)
    save_arb    = float(vs.get("arbitrage_eur") or 0)
    save_merchant = float(vs.get("merchant_eur") or 0)  # BOD 11: obchodovanie batérie v bilančnej skupine (merchant mode)
    # Merchant detail (throughput/cykly/buy/sell/gross/fee/net) — pre bublinku a dátovú pasáž o BS
    _mdet = base.get("merchant_detail") or {}
    merch_throughput_mwh = float(_mdet.get("throughput_mwh") or 0)
    merch_cycles = float(_mdet.get("equiv_cycles") or 0)
    merch_buy_eur = float(_mdet.get("buy_eur") or 0)
    merch_sell_eur = float(_mdet.get("sell_eur") or 0)
    merch_gross_eur = float(_mdet.get("gross_eur") or 0)
    merch_fee_eur = float(_mdet.get("organizer_fee_eur") or 0)
    merch_fee_pct = float(_mdet.get("fee_pct") or 15.0)
    merch_net_eur = float(_mdet.get("merchant_net_eur") or save_merchant)
    merch_spread_eur_mwh = (merch_gross_eur / merch_throughput_mwh) if merch_throughput_mwh > 0 else 0.0
    # BUG 3 FIX: efektívna avoided sadzba = engine hodnota samospotreby / priama kWh
    # (engine oceňuje plným retailom; p_avoided=silová+distrib bola len časť → nesedelo s úsporou)
    _pv_direct_mwh = float(base.get("pv_to_load_mwh") or 0)
    p_avoided_eff = (save_self / (_pv_direct_mwh*1000)) if _pv_direct_mwh > 0 else p_avoided
    if p_avoided_eff < p_avoided: p_avoided_eff = p_avoided
    save_peak   = float(vs.get("peak_shaving_eur") or 0)
    # --- Arbitráž: VŽDY viditeľný stav + dôvod (nikdy tichá 0) ---
    _is_spot = (str(analyza.get("om_tarif_typ") or analyza.get("tarif_typ") or "spot").lower() == "spot")
    _has_ems = bool(base.get("value_streams"))
    if bess_kwh <= 0:
        arbitrage_reason = _("bez batérie — arbitráž sa neuplatňuje")
    elif save_arb < -1:  # nabíjanie zo siete stratové: náklad, nie prínos (už odpočítaný v ročnej úspore)
        arbitrage_reason = (_("pri spotovej tarife spread medzi lacnými a drahými hodinami nepokryl straty batérie pri nabíjaní a vybíjaní")
                            if _is_spot else
                            _("pri fixnej tarife nemá nabíjanie zo siete cenový rozdiel, takže straty batérie pri nabíjaní a vybíjaní sú čistý náklad"))
        arbitrage_reason += _("; náklad je už odpočítaný v ročnej úspore")
    elif save_arb > 1:
        arbitrage_reason = _("spotová arbitráž z hodinového EMS (nabíjanie v lacných hodinách, vybíjanie v drahých)")
    elif save_merchant > 1:
        arbitrage_reason = _("batéria obchoduje v merchant režime bilančnej skupiny — výnos v riadku Obchodovanie BS")
    elif not _is_spot:
        arbitrage_reason = _("klient nie je na spotovej tarife — arbitráž sa nepočíta (0 €)")
    elif not _has_ems:
        arbitrage_reason = _("chýba hodinový spotový výpočet (EMS) — arbitráž nevyčíslená (0 €)")
    else:
        arbitrage_reason = _("spread nedosiahol prah rentability (≥ 30 €/MWh) — arbitráž 0 €")
    base_saving = float(base.get("saving_y1_eur") or vs.get("total_eur") or (save_self+save_export+save_bess+save_arb+save_peak+save_merchant))
    annual_tax  = net_capex/ODPIS*DPPO
    # retrofit BESS (pridanie_bess): CAPEX v posudku je LEN batéria, FVE je existujúca
    _retrofit_bess = ((analyza.get("scenario_type") or "").lower() == "pridanie_bess") and bess_kwh > 0

    # Báza ukotvená na ENGINE (validované), scenáre odvodené od nej (žiadny druhý NPV systém)
    eng_npv = float(base.get("npv_eur") or 0)
    eng_pb  = _pb(base.get("payback_years"))  # None = nevráti sa
    eng_irr = float(base.get("irr_pct") or 0)
    _annuity = sum(1.0/((1+DISC)**y) for y in range(1, LIFE+1))  # diskontovaná anuita úspor
    def scen(name, short, save_mult):
        stot = base_saving * save_mult
        d_save = stot - base_saving
        npv = eng_npv + d_save * _annuity          # posun NPV o delta úspory (diskontovaná)
        # Payback aj IRR ODVODENÉ od engine bázy proporcionálne k úspore — JEDNA metodika pre všetky 3 scenáre.
        # Garantuje monotónnosť: viac úspor → kratšia návratnosť + vyššia IRR (žiadny paradox).
        pb  = _pb(eng_pb / save_mult) if (eng_pb is not None and save_mult > 0) else None
        irr = (eng_irr * save_mult) if eng_irr > 0 else 0
        return {"name":name,"short":short,"save_total":stot,"opex":opex,"annual_tax":annual_tax,
                "payback":pb,"npv":npv,"irr":irr,"save_self":save_self,"save_export":save_export}
    # 3 scenáre = cenová citlivosť na engine bázu (NIE pripočítavanie pák — tie sú už v báze)
    S = [scen(_("Báza (ÚRSO 2026 + spot OKTE)"),_("Báza"),1.0),
         scen(_("Defenzívny (nižší výkup/cena)"),_("Defenzívny"),0.85),
         scen(_("Optimistický (rast cien energie)"),_("Optimistický"),1.12)]
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
    drv=[(_("Cena elektriny"),npvm(sm=0.85)-base_npv,npvm(sm=1.15)-base_npv),
         (_("Špecifický výnos FVE"),npvm(sm=0.90)-base_npv,npvm(sm=1.10)-base_npv),
         (_("CAPEX (cena diela)"),npvm(cm=1.15)-base_npv,npvm(cm=0.85)-base_npv)]
    drv.sort(key=lambda d:max(abs(d[1]),abs(d[2])),reverse=True)

    # --- monte carlo ---
    random.seed(42); res=[]
    for _mc in range(5000):
        sm=random.triangular(0.82,1.15,1.0); cm=random.triangular(0.95,1.12,1.0)
        res.append(eng_npv + (base_saving*sm - base_saving)*_annuity - (net_capex*cm - net_capex))
    res.sort(); P=lambda q:res[int(q*len(res))]

    # --- verdikt podľa znamienka NPV (báza) a P10 — nikdy natvrdo kladný (audit 2026-10-24 #10) ---
    _p10 = P(0.10)
    _prob_pos = sum(1 for x in res if x > 0) / len(res)
    _pb_rng = _pb_range(opti["payback"], S[0]["payback"])
    if full["npv"] > 0 and _p10 > 0:
        summary_headline = _("Audit odberu: investícia je ekonomicky výhodná vo všetkých scenároch")
        recommendation_line = _("Realizovať — návratnosť {pb}, NPV {npv} (báza); "
                                "aj v pesimistickom scenári P10 zostáva NPV kladné ({p10}).").format(
                                    pb=_pb_rng, npv=eur(full['npv']), p10=eur(_p10))
    elif full["npv"] > 0:
        summary_headline = _("Audit odberu: investícia je v báze výhodná, pri nepriaznivom vývoji môže byť stratová")
        recommendation_line = _("Realizovať s rezervou — návratnosť {pb}, NPV {npv} (báza); "
                                "v pesimistickom scenári P10 klesá NPV pod nulu ({p10}), "
                                "pravdepodobnosť kladného NPV {prob} %.").format(
                                    pb=_pb_rng, npv=eur(full['npv']), p10=eur(_p10), prob=num(_prob_pos*100))
    else:
        summary_headline = _("Audit odberu: investícia sa v navrhnutej konfigurácii neoplatí")
        recommendation_line = _("V tejto konfigurácii neodporúčame realizovať — NPV {npv} (báza), "
                                "návratnosť {pb}; pravdepodobnosť kladného NPV {prob} %. "
                                "Odporúčame preveriť menšiu batériu, inú veľkosť FVE alebo cenu diela.").format(
                                    pb=_pb_rng, npv=eur(full['npv']), prob=num(_prob_pos*100))

    # --- cena nečinnosti ---
    ina_y1=self_mwh*1000*p_avoided_eff   # BUG 3 FIX: plný retail (konzistentne s engine úsporou)
    ina_flat=sum(ina_y1*((1-DEG)**y) for y in range(LIFE))
    ina_infl=sum(ina_y1*((1-DEG)**y)*((1+INFL)**y) for y in range(LIFE))

    # --- komponenty (z variantu, fallback orientačné) ---
    comp_real=bool(analyza.get("bundle_id"))
    components={"panel":_("FVE {kwp} kWp — moduly podľa cenovej ponuky").format(kwp=f"{base.get('pv_kwp',0):.0f}"),
                "inverter":_("meniče ~{kw} kW AC + optimizéry").format(kw=f"{base.get('inverter_kw',0):.0f}"),
                "battery":_("batéria {kwh} kWh").format(kwh=f"{bess_kwh:.0f}") if bess_kwh else "—",
                "konstrukcia":base.get("fve_topology") if "fve_topology" in base else _("E-W / Juh")}

    # benefit_rows = REÁLNE engine zložky (z dispatchu), žiadny paušál
    _pv_via_bat_mwh = float(base.get("pv_to_bat_mwh") or 0)
    # BUG 2 FIX: prvý riadok = PRIAMA samospotreba (save_self je z engine priama, 381 MWh);
    # batéria je samostatný riadok (žiadne dvojité počítanie 418 vs 381).
    benefit_rows=[(_("FVE priama samospotreba"),_("{mwh} MWh priamo do odberu").format(mwh=f"{_pv_direct_mwh:.0f}"),save_self),
                  (_("FVE export prebytkov"),_("{mwh} MWh do siete").format(mwh=f"{export_mwh:.0f}"),save_export)]
    if save_bess>0: benefit_rows.append((_("Batéria — posun PV do odberu"),_("{mwh} MWh PV uskladnené a využité neskôr").format(mwh=f"{_pv_via_bat_mwh:.0f}"),save_bess))
    if save_arb>1: benefit_rows.append((_("BESS arbitráž (spot v BS)"),_("nabíjanie lacno / vybíjanie draho"),save_arb))
    # záporná arbitráž nie je prínos — samostatný riadok nákladu (súčet s prínosmi = ročná úspora enginu)
    benefit_cost_rows=([(_("Batéria — nabíjanie zo siete (náklad)"),
                         _("{mwh} MWh zo siete, straty pri nabíjaní a vybíjaní").format(mwh=f"{float(base.get('grid_to_bat_mwh') or 0):.0f}"),
                         save_arb)] if save_arb<-1 else [])
    if save_merchant>1: benefit_rows.append((_("BESS obchodovanie v BS (merchant)"),_("spotové obchodovanie batérie cez bilančnú skupinu"),save_merchant))
    if save_peak>0: benefit_rows.append((_("Peak shaving (zníženie RK)"),_("redukcia mesačného 15-min maxima"),save_peak))
    benefit_parts=[(_("Samospotreba"),save_self,"#16A34A"),(_("Export"),save_export,"#A7D08C"),
                   (_("Batéria"),save_bess,"#5B7CFA"),(_("Arbitráž"),max(save_arb,0.0)+max(save_merchant,0.0),"#8B5CF6"),
                   (_("Daňový štít"),annual_tax,"#F59E0B")]

    _kwp = f"{base.get('pv_kwp',0):.0f}"; _kwh = f"{bess_kwh:.0f}"
    _tarif_src = analyza.get("tarif_source") or "faktúra"
    if _tarif_src == "faktúra": _tarif_src = _("faktúra")   # predvolená hodnota z intake; iný text = údaj z DB
    else:  # kódy zdroja tarify (mcp = zápis cez CRM nástroj) → čitateľný text vo všetkých jazykoch
        _tarif_src = {"mcp": _("zadané v CRM"), "crm": _("zadané v CRM"), "manual": _("zadané ručne"),
                      "faktura": _("faktúra"), "urso": _("cenník ÚRSO"), "úrso": _("cenník ÚRSO"),
                      "spot": _("spot OKTE")}.get(str(_tarif_src).strip().lower(), _tarif_src)
    # distribútor OM (SSE/ZSE/VSD…) — predtým natvrdo „VSD tarif" pre každé OM
    _dist = str(analyza.get("om_distributor")
                or ((((analyza.get("econ_results") or {}).get("full_response") or {}).get("tariff_detail") or {}).get("distribuutor"))
                or "").strip().upper()
    _dist_tarif = _("tarif {dist}").format(dist=_dist) if _dist else _("tarif distribútora")
    ctx={
        "client_name":base.get("client_name"),"om_address":base.get("site_address") or "—",
        "eic_om":analyza.get("eic_om"),"cislo_om":analyza.get("cislo_om"),
        "distrib_oblast":analyza.get("distrib_oblast") or analyza.get("om_sadzba"),
        "om_sadzba":(str(analyza.get("om_sadzba") or "").strip().upper() or "—"),"om_mrk_kw":mrk,"om_rk_kw":rk,
        "posudok_number":analyza.get("posudok_number") or f"P-AOM-{str(analyza.get('id',''))[:6]}","pon_number":analyza.get("pon_number"),
        "posudok_date":fmt_date(datetime.now().date()),
        "prepared_by_name":base.get("prepared_by_name"),"prepared_by_email":base.get("prepared_by_email"),"prepared_by_phone":base.get("prepared_by_phone"),
        "fve_kwp":base.get("pv_kwp"),"bess_kwh":bess_kwh,"yield":(base.get("pv_total_mwh",0)*1000/(base.get("pv_kwp") or 1)) if base.get("pv_kwp") else 1075,
        "fve_prod_mwh":base.get("pv_total_mwh"),"self_use_mwh":pv_self_mwh,"export_mwh":export_mwh,
        "export_pct":(export_mwh/float(base.get("pv_total_mwh") or 1)*100),
        "loss_mwh":max(0.0,float(base.get("pv_total_mwh") or 0)-pv_self_mwh-export_mwh),
        "loss_pct":(max(0.0,float(base.get("pv_total_mwh") or 0)-pv_self_mwh-export_mwh)/float(base.get("pv_total_mwh") or 1)*100),
        "grid_import_mwh":base.get("grid_import_mwh"),"samosp_pct":(pv_self_mwh/float(base.get("pv_total_mwh") or 1)*100),"coverage_pct":base.get("samostatnost_pct"),
        "arbitrage_eur":save_arb,"arbitrage_reason":arbitrage_reason,"arbitrage_shown":(bess_kwh>0),
        "merchant_revenue_eur":save_merchant,"merchant_mode":(save_merchant>1),
        "merchant_throughput_mwh":merch_throughput_mwh,"merchant_cycles":merch_cycles,
        "merchant_buy_eur":merch_buy_eur,"merchant_sell_eur":merch_sell_eur,"merchant_gross_eur":merch_gross_eur,
        "merchant_fee_eur":merch_fee_eur,"merchant_fee_pct":merch_fee_pct,"merchant_net_eur":merch_net_eur,
        "merchant_spread_eur_mwh":merch_spread_eur_mwh,
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
        "p_silova":p_silova,"p_dist_var":p_dist_var,"p_tps":p_tps,"p_so":p_so,"p_dist_pevna":p_dist_pevna,"p_sell":p_sell,"p_avoided":p_avoided,"p_avoided_eff":p_avoided_eff,
        "tarif_real":tarif_real,"tarif_source":(_tarif_src if tarif_real else _("orientačné (ÚRSO 2026)")),
        "om_distributor":_dist,
        "dist_tarif_source":(_dist_tarif if analyza.get("tarif_distribucia_eur_mwh") not in (None,"") else _("orientačné (ÚRSO 2026)")),
        "components":components,"components_real":comp_real,
        "scenarios3":S,"tornado_base":base_npv,"tornado_drivers":drv,
        "mc_samples":res,"mc_p10":P(0.10),"mc_p50":P(0.50),"mc_p90":P(0.90),"mc_prob_pos":sum(1 for x in res if x>0)/len(res),"mc_n":5000,
        "inaction_y1":ina_y1,"inaction_flat_20y":ina_flat,"inaction_infl_20y":ina_infl,
        "benefit_rows":benefit_rows,"benefit_cost_rows":benefit_cost_rows,"benefit_parts":benefit_parts,
        "consumption_real":(analyza.get("consumption_strategy") or ("measured" if analyza.get("consumption_15min_path") else "")) == "measured",
        "consumption_strategy":analyza.get("consumption_strategy"),
        "consumption_needs_review":bool(analyza.get("consumption_needs_review")),
        "consumption_source":{
            "measured":_("skutočné namerané 15-min dáta"),
            "extrapolated":_("15-min tvar z časti roka, extrapolovaný na celý rok"),
            "synthesized":_("modelovaný profil z faktúry (orientačný)"),
        }.get(analyza.get("consumption_strategy"), (_("skutočné 15-min dáta") if analyza.get("consumption_15min_path") else _("modelovaný profil"))),
        "variant_title":(_("Navrhnutý variant — batériové úložisko {kwh} kWh k existujúcej FVE {kwp} kWp").format(kwh=_kwh, kwp=_kwp)
                         if _retrofit_bess else
                         _("Navrhnutý variant — FVE {kwp} kWp").format(kwp=_kwp) + (f" + BESS {_kwh} kWh" if bess_kwh else "")),
        "summary_headline":summary_headline,
        "recommendation_line":recommendation_line,
        "cover_subtitle":_("Analýza odberu, simulácia výroby a dispatch, ekonomické posúdenie v 3 scenároch s rizikovou analýzou."),
        "podklady":({"measured":_("15-min profil · "),"extrapolated":_("15-min profil (časť roka) · "),"synthesized":_("profil z faktúry · ")}.get(analyza.get("consumption_strategy"), _("15-min profil · ") if analyza.get("consumption_15min_path") else ""))+_("PVGIS · OKTE 2025")+((" · " + _("faktúra")) if tarif_real else ""),
        "zaver_headline":_("Odporúčanie pre klienta"),
        "zaver_text":(_("Investícia {capex} do batériového úložiska {kwh} kWh k existujúcej FVE {kwp} kWp").format(capex=eur(capex), kwh=_kwh, kwp=_kwp)
                      if _retrofit_bess else
                      (_("Investícia {capex} do FVE {kwp} kWp").format(capex=eur(capex), kwp=_kwp)+(f" + BESS {_kwh} kWh" if bess_kwh else "")))
                     +_(" prinesie ročné úspory {s_base} (báza) až {s_opti} (optimistický scenár). Návratnosť {pb}, NPV 20 r. {npv} (báza), IRR {irr} %.").format(
                         s_base=eur(S[0]['save_total']), s_opti=eur(opti['save_total']), pb=_pb_rng, npv=eur(full['npv']),
                         irr=(f"{full['irr']:.1f}" if get_lang()=="sk" else num(full['irr'],1))),  # SK quirk: IRR v závere s bodkou (13.1 %)
        "recommendations":[],  # doplní AI/deterministic neskôr
    }
    if get_lang() != "sk":  # VN/NN → „MV (medium voltage)" …; sk ukazuje skratku ako doteraz
        ctx["om_sadzba"] = volt(ctx["om_sadzba"]); ctx["distrib_oblast"] = volt(ctx["distrib_oblast"])
    _build_deterministic_narratives(ctx, S, full, prof, pm)
    ctx["expert_fallback_html"], ctx["recommendations"] = deterministic_expert(ctx)
    return ctx


def deterministic_expert(ctx):
    """Sekcia 7 bez AI: technicko-rizikové posúdenie + odporúčané kroky z čísel posudku
    (aj fallback AI v analyza_om_v2 — sekcia 7 nikdy nemá prázdne nadpisy)."""
    from aom_i18n.fmt import dec
    S = ctx["scenarios3"]
    exp = ("<p>" + _("Z technického hľadiska je navrhnutý variant {kwp} kWp primeraný profilu OM — pri samospotrebe "
                     "{sam} % sa vyrobená energia využije takmer celá. Rozhodujúce predpoklady sú špecifický výnos "
                     "{vyn} kWh/kWp (PVGIS) a stabilita odberového profilu.").format(
               kwp=num(round(float(ctx.get("fve_kwp") or 0))), sam=dec(round(float(ctx.get("samosp_pct") or 0), 1)),
               vyn=num(round(float(ctx.get("yield") or 0))))
           + "</p><p>" + _("Citlivosť: medzi defenzívnym a optimistickým scenárom je rozpätie NPV {lo} – {hi} €. "
                           "Pred realizáciou odporúčam overiť finálnu tarifu (faktúra), podmienky pripojenia u "
                           "prevádzkovateľa distribučnej sústavy a presnú konštrukciu z cenovej ponuky.").format(
               lo=num(S[1]["npv"]), hi=num(S[2]["npv"])) + "</p>")
    recs = [(_("Odporúčanie k navrhnutému variantu"), ctx.get("recommendation_line", ""))]
    if not ctx.get("tarif_real"):
        recs.append((_("Doplniť faktúru za elektrinu"), _("Spresní silovú aj distribučnú cenu — posudok prejde z orientačného na presný.")))
    if not ctx.get("bess_kwh"):
        recs.append((_("Preveriť variant s batériou"), _("Batéria posúva výrobu do špičiek a zvyšuje samospotrebu — vhodné preveriť ako ďalší variant.")))
    else:
        recs.append((_("Preveriť zníženie RK"), _("Po inštalácii batérie klesá špičkové zaťaženie zo siete — možnosť znížiť rezervovanú kapacitu.")))
    recs.append((_("Technická obhliadka OM"), _("Overenie strechy/plochy, statiky a bodu pripojenia pred finálnym projektom.")))
    return exp, recs


def _build_deterministic_narratives(ctx, S, full, prof, pm):
    """Odborná próza z REÁLNYCH čísel pre každú sekciu — nezávislé od AI (nikdy prázdne).
    AI naratív tieto polia neskôr môže prepísať/obohatiť."""
    n = num
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
    rezim = (prof or {}).get("rezim") or _("priemyselná prevádzka")
    sezon = (prof or {}).get("sezonnost") or _("rovnomerná")
    co2 = float(ctx.get("co2_avoided_tonnes") or 0)

    # --- 1) PROFIL ---
    parts = []
    parts.append(_("Charakter prevádzky: <b>{rezim}</b>. Z analyzovaných {src} vychádza priemerný odber {avg} kW a 15-min špička {peak} kW").format(
        rezim=rezim, src=ctx.get('consumption_source', _("dát")), avg=n(avg_kw), peak=n(peak)))
    if lf is not None:
        parts[-1] += " " + _("(load factor {lf})").format(lf=n(lf,2))
    parts[-1] += "."
    if day_share is not None:
        parts.append(_("Cez deň (slnečné hodiny) prebehne približne {pct} % spotreby — to priamo určuje, koľko FVE energie sa využije bez batérie.").format(pct=n(day_share)))
    if get_lang() == "sk":
        # SK quirk: z „minimálna sezónnosť, rovnomerná …" ostane „minimálna , rovnomerná …" (zlaté výstupy)
        _sezl = str(sezon).lower().replace("sezónnosť", "").strip() or "rovnomerná"
        parts.append(f"Sezónnosť odberu je {_sezl}; ročná spotreba OM je {n(year)} MWh.")
    else:
        parts.append(_("Sezónnosť odberu: {sez}; ročná spotreba OM je {mwh} MWh.").format(sez=sezon, mwh=n(year)))
    fit = (prof or {}).get("fve_fit")
    if fit:
        fit = str(fit)[0].upper() + str(fit)[1:]
        parts.append(fit if fit.endswith(".") else fit + ".")
    ctx["profile_narrative"] = "<p>" + "</p><p>".join(parts) + "</p>"
    ctx["daily_cap"] = _("Priemer {avg} kW, špička {peak} kW").format(avg=n(avg_kw), peak=n(peak)) + (_(", špičková hodina {h}:00.").format(h=peak_hour) if peak_hour is not None else ".")
    _mf = ctx.get("months_filled") or []
    _mn = ({1:"jan",2:"feb",3:"mar",4:"apr",5:"máj",6:"jún",7:"júl",8:"aug",9:"sep",10:"okt",11:"nov",12:"dec"}
           if get_lang() == "sk" else dict(enumerate(months(), 1)))
    ctx["monthly_cap"] = _("Ročná spotreba {mwh} MWh.").format(mwh=n(year)) + (" " + _("Dopočítané z priemeru: {m}.").format(m=", ".join(_mn.get(m,str(m)) for m in _mf)) if _mf else "")

    # --- 2) ENERGETICKÁ BILANCIA ---
    exp_pct = (exp / prod * 100) if prod else 0
    bnar = _("Z {prod} MWh ročnej výroby FVE sa <b>{sams} %</b> ({selfu} MWh) spotrebuje priamo v prevádzke "
             "a iba {exp_pct} % ({exp} MWh) odchádza ako prebytok do siete. ").format(
                 prod=n(prod), sams=n(sams,1), selfu=n(selfu), exp_pct=n(exp_pct,1), exp=n(exp,1))
    if fve and year and fve * 1.05 < year / 1.05:
        bnar += _("Relatívne malá FVE ({fve} kWp) voči spotrebe ({year} MWh/r) je dôvod vysokej samospotreby — vyrobená energia sa takmer celá využije na mieste. ").format(fve=n(fve), year=n(year))
    bnar += _("FVE pokryje {cov} % ročnej spotreby OM; zvyšných {imp} MWh zostáva zo siete.").format(cov=n(cov,1), imp=n(imp))
    ctx["balance_narrative"] = "<p>" + bnar + "</p>"

    # odhad počtu modulov ak nie sú reálne komponenty (kredibilita technickej sekcie)
    comp = dict(ctx.get("components") or {})
    if not ctx.get("components_real") and fve:
        nmod = int(round(fve / 0.58))  # ~580 Wp typický modul 2026
        comp["panel"] = _("~{nmod} ks modulov á ~580 Wp (orientačne — spresní cenová ponuka)").format(nmod=nmod)
        ac = fve / 1.1
        comp["inverter"] = comp.get("inverter") or _("meniče ~{ac} kW AC (DC/AC ~1,1)").format(ac=f"{ac:.0f}")
        _k = comp.get("konstrukcia")
        if not _k or "35" in str(_k):
            comp["konstrukcia"] = _("Juh 13° (orientačne — spresní cenová ponuka)")
        ctx["components"] = comp

    # --- 3) SCENÁRE (3 bullety s výkladom) ---
    sb = []
    sb.append((S[0]["name"], _("Bázový výpočet z hodinovej simulácie (ceny ÚRSO 2026 + spot OKTE). Ročná úspora {save} €, návratnosť {pb}.").format(
        save=n(S[0]['save_total']), pb=pb_txt(S[0]['payback']))))
    sb.append((S[1]["name"], _("Stresový pohľad — nižší výkup prebytkov a opatrnejšie ceny energie. Ukazuje odolnosť investície: úspora {save} €/r, návratnosť {pb}.").format(
        save=n(S[1]['save_total']), pb=pb_txt(S[1]['payback']))))
    sb.append((S[2]["name"], _("Pri raste trhových cien energie hodnota vlastnej výroby rastie — úspora {save} €/r, NPV 20 r. {npv} €, IRR {irr} %.").format(
        save=n(S[2]['save_total']), npv=n(S[2]['npv']), irr=n(S[2]['irr'],1))))
    ctx["scenarios_bullets"] = sb

    # --- 4) ZÁVER — argumenty (rich bullety) ---
    args = []
    args.append((_("Predikovateľnosť ceny energie na 20+ rokov"),
                 _("Vlastná výroba pokryje {cov} % ročnej spotreby pri {sams} % samospotrebe — prirodzený hedge proti rastu cien elektriny a regulačných poplatkov.").format(cov=n(cov,1), sams=n(sams,1))))
    dan = capex * 0.21
    args.append((_("Daňová optimalizácia"),
                 _("6-ročný rovnomerný odpis predstavuje úsporu 21 % × {capex} € = {dan} € na dani z príjmu (priemerne {dan_y} €/rok počas prvých 6 rokov).").format(
                     capex=n(capex), dan=n(dan), dan_y=n(dan/6))))
    # Optimalizácia RK — len ak je špička výrazne pod MRK (reálne z dát, nezávislé od batérie).
    # Peak-shaving cez batériu tvrdíme IBA ak ho model reálne oceňuje (save_peak>0), inak by išlo o blud.
    _save_peak = float(ctx.get("save_peak_eur") or 0)
    if mrk > 0 and peak > 0 and peak < mrk * 0.65 and not ctx.get("peak_estimated"):
        _t = _("Špičkový odber {peak} kW je len {pct} % zazmluvnenej MRK {mrk} kW. "
               "Odberné miesto má rezervu znížiť rezervovanú kapacitu (RK, min. 50 % MRK) a šetriť na pevnej zložke distribúcie").format(
                   peak=n(peak), pct=n(peak/mrk*100,0), mrk=n(mrk))
        if _save_peak > 0:
            _t += _("; batéria toto zníženie zabezpečuje proti špičkám (modelovaná úspora {save} €/r).").format(save=n(_save_peak))
        else:
            _t += _(" (úspora nezávislá od FVE/batérie — odporúčame preveriť s prevádzkovateľom distribučnej sústavy).")
        args.append((_("Optimalizácia rezervovanej kapacity (MRK/RK)"), _t))
    elif bess > 0 and _save_peak > 0:
        args.append((_("Peak shaving cez batériu"),
                     _("Batéria {bess} kWh znižuje 15-min špičky zo siete (modelovaná úspora {save} €/r) — podklad pre zníženie rezervovanej kapacity (RK {rk} kW).").format(
                         bess=n(bess), save=n(_save_peak), rk=n(rk))))
    args.append((_("ESG profil a CSRD reporting"),
                 _("Ročná úspora ~{co2} t CO₂ (pri SK emisnej intenzite ~0,25 kg/kWh) — doložiteľný podklad pre EU Taxonómiu a CSRD disclosures.").format(co2=n(co2))))
    args.append((_("Reziduálna hodnota po 20 rokoch"),
                 _("Ekonomika neráta s poklesom výkonu FVE; konzervatívne 10 % CAPEX = {res} € reziduálnej hodnoty.").format(res=n(capex*0.10))))
    ctx["zaver_arguments"] = args
