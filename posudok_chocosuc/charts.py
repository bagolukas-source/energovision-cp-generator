# -*- coding: utf-8 -*-
"""ChocoSuc-grade charts — context-driven (berie hodnoty z ctx, fallback ak chyba hourly)."""
import io, base64, math
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

GREEN="#5E8E2A"; LIME="#92D050"; DARK="#1A1A1A"; GRAY="#6B7280"; GRID="#5B7CFA"; SITE="#B85DD8"; SOLAR="#FFC629"; AMBER="#F59E0B"
plt.rcParams.update({"font.family":"DejaVu Sans","font.size":11,"axes.edgecolor":"#E3E8EE","axes.labelcolor":"#64748B",
 "xtick.color":"#94A3B8","ytick.color":"#94A3B8","xtick.labelsize":10,"ytick.labelsize":10,
 "axes.spines.top":False,"axes.spines.right":False,"axes.spines.left":False,
 "axes.grid":True,"axes.axisbelow":True,"grid.color":"#EEF2F6","grid.linewidth":1.0,"figure.facecolor":"white","axes.facecolor":"white"})
def _clean(ax,yg=True):
    ax.grid(axis="x",visible=False)
    if not yg: ax.grid(axis="y",visible=False)
    ax.tick_params(length=0)
def _b64(fig):
    b=io.BytesIO(); fig.savefig(b,format="png",dpi=150,bbox_inches="tight",pad_inches=0.12); plt.close(fig)
    return "data:image/png;base64,"+base64.b64encode(b.getvalue()).decode()

def _synth_daily(avg_kw, peak_hour=12):
    # syntetický denný tvar centrovaný na peak_hour (keď chýba hourly)
    sh=[]
    for h in range(24):
        d=min(abs(h-peak_hour),24-abs(h-peak_hour))
        sh.append(math.exp(-(d**2)/22))
    base=0.55
    prof=[base+(1-base)*s for s in sh]
    sc=avg_kw/(sum(prof)/24)
    return [p*sc for p in prof]

def chart_daily(ctx):
    avg=ctx.get("avg_kw") or (ctx.get("year_mwh",0)*1000/8760) or 150
    ph=ctx.get("profile_metrics",{}).get("peak_hour") or 12
    wd=ctx.get("hourly_wd") or _synth_daily(avg,ph)
    we=ctx.get("hourly_we") or [v*(ctx.get("profile_metrics",{}).get("weekend_ratio") or 0.85) for v in wd]
    pvpeak=ctx.get("fve_kwp",0)*0.62
    pvshape=[0,0,0,0,0,0.02,0.06,0.13,0.22,0.34,0.46,0.55,0.59,0.57,0.50,0.40,0.28,0.16,0.07,0.02,0,0,0,0]
    pv=[v*pvpeak for v in pvshape]
    fig,ax=plt.subplots(figsize=(9,3.0)); x=range(24)
    ax.fill_between(x,pv,color=LIME,alpha=0.32,lw=0,label=f"PV {ctx.get('fve_kwp',0):.0f} kWp",zorder=1)
    ax.plot(x,wd,color="#1E293B",lw=2.6,label="Pracovný deň",zorder=3)
    ax.plot(x,we,color="#94A3B8",lw=2.2,ls=(0,(4,3)),label="Víkend",zorder=2)
    ax.set_xlim(0,23); ax.set_ylim(0,max(max(wd),max(pv) or 1)*1.15); ax.set_xticks(range(0,24,3))
    ax.set_xticklabels([f"{h}:00" for h in range(0,24,3)]); ax.set_ylabel("kW")
    ax.legend(frameon=False,fontsize=10,ncol=3,loc="upper right"); _clean(ax)
    return _b64(fig)

def chart_monthly(ctx):
    m=ctx.get("monthly_mwh") or [ctx.get("year_mwh",0)/12]*12
    lab=["Jan","Feb","Mar","Apr","Máj","Jún","Júl","Aug","Sep","Okt","Nov","Dec"]
    fig,ax=plt.subplots(figsize=(9,2.7)); bars=ax.bar(lab,m,color=GREEN,width=0.66,zorder=3)
    for b,v in zip(bars,m): ax.text(b.get_x()+b.get_width()/2,v+max(m)*0.02,f"{v:.0f}",ha="center",fontsize=9,color="#475569",weight="bold")
    ax.set_ylabel("MWh"); ax.set_ylim(0,max(m)*1.20); _clean(ax)
    return _b64(fig)

def chart_energy_balance(ctx):
    SOLAR="#92D050"; GRID="#9DB2C9"; SITE="#1F3A5F"  # tlmená paleta (override krikľavých globálov)
    pv=ctx.get("fve_prod_mwh",0); self_=ctx.get("self_use_mwh",0); exp=ctx.get("export_mwh",0)
    gi=ctx.get("grid_import_mwh",0); load=ctx.get("year_mwh",1) or 1
    from matplotlib.path import Path; from matplotlib.patches import PathPatch,FancyBboxPatch
    fig,ax=plt.subplots(figsize=(9,3.2)); ax.set_xlim(0,10); ax.set_ylim(0,6); ax.axis("off")
    H=4.6; y0=0.7; scale=H/load; L=3.3; Rg=6.7
    def band(ys0,ys1,yt0,yt1,c,a=0.5):
        cx=5.0; v=[(L,ys0),(cx,ys0),(cx,yt0),(Rg,yt0),(Rg,yt1),(cx,yt1),(cx,ys1),(L,ys1),(L,ys0)]
        co=[Path.MOVETO,Path.CURVE4,Path.CURVE4,Path.CURVE4,Path.LINETO,Path.CURVE4,Path.CURVE4,Path.CURVE4,Path.CLOSEPOLY]
        ax.add_patch(PathPatch(Path(v,co),facecolor=c,edgecolor="none",alpha=a))
    def node(x,yb,h,c,t,val,sub,al):
        ax.add_patch(FancyBboxPatch((x-0.22,yb),0.44,max(h,0.05),boxstyle="round,pad=0.02,rounding_size=0.07",facecolor=c,edgecolor="none"))
        tx=x+(0.42 if al=="l" else -0.42); ha="left" if al=="l" else "right"; ym=yb+h/2
        bb=dict(boxstyle="round,pad=0.12",facecolor="white",edgecolor="none",alpha=1.0)
        ax.text(tx,ym+0.22,t,ha=ha,va="center",fontsize=10.5,color=DARK,weight="bold",bbox=bb,zorder=5)
        ax.text(tx,ym-0.05,f"{val:,.0f} MWh".replace(","," "),ha=ha,va="center",fontsize=9,color="#64748B",bbox=bb,zorder=5)
        ax.text(tx,ym-0.32,sub,ha=ha,va="center",fontsize=7.5,color="#9CA3AF",bbox=bb,zorder=5)
    h_self=self_*scale; h_gi=gi*scale; h_load=load*scale; h_exp=exp*scale
    om_bot=y0; node(9.0,om_bot,h_load,SITE,"Odberné miesto",load,"spotreba","r"); om_top=om_bot+h_load
    h_pv=h_self+h_exp; pv_top=y0+h_load; pv_bot=pv_top-h_pv
    node(1.0,pv_bot,h_pv,SOLAR,"Fotovoltika",pv,f"výroba {pv:.0f} MWh","l")
    gi_top=pv_bot-0.30; gi_bot=gi_top-h_gi; node(1.0,gi_bot,h_gi,GRID,"Sieť",gi,"import","l")
    t=om_top; band(pv_top,pv_top-h_self,t,t-h_self,SOLAR,0.45); t-=h_self
    band(gi_top,gi_bot,t,t-h_gi,GRID,0.40)
    if h_exp>0.05:
        ax.add_patch(FancyBboxPatch((9.05,om_top-h_exp),0.32,max(h_exp,0.14),boxstyle="round,pad=0.02,rounding_size=0.04",facecolor=GRID,alpha=0.75,edgecolor="none"))
        ax.text(9.0,om_top+0.22,f"export {exp:.0f} MWh",ha="right",va="bottom",fontsize=8,color="#64748B")
        band(pv_bot+h_pv,pv_bot+h_self,om_top+0.02,om_top-h_exp+0.02,SOLAR,0.25)
    return _b64(fig)

def chart_scenarios(ctx):
    S=ctx["scenarios3"]; short=[s["short"] for s in S]; npv=[s["npv"] for s in S]; pb=[s["payback"] for s in S]; irr=[s["irr"] for s in S]
    fig,(a1,a2)=plt.subplots(1,2,figsize=(9,3.0)); x=np.arange(len(S)); cols=["#CBD5E1",GRID,GREEN][:len(S)]
    b=a1.bar(x,[v/1000 for v in npv],color=cols,width=0.62,zorder=3)
    for bb,v in zip(b,npv): a1.text(bb.get_x()+bb.get_width()/2,v/1000+max(npv)/1000*0.03,f"{v/1000:.0f} k€",ha="center",fontsize=9.5,weight="bold",color="#1E293B")
    a1.set_xticks(x); a1.set_xticklabels(short,fontsize=9.5); a1.set_ylabel("NPV"); a1.set_ylim(min(0,min(npv)/1000*1.1),max(npv)/1000*1.20); a1.set_yticklabels([]); _clean(a1)
    a2b=a2.twinx()
    a2.plot(x,pb,"o-",color=AMBER,lw=2.6,ms=8,zorder=3); a2b.plot(x,irr,"s-",color=GREEN,lw=2.6,ms=7,zorder=3)
    a2.set_xticks(x); a2.set_xticklabels(short,fontsize=9.5); a2.set_ylabel("Návratnosť (r)",color=AMBER); a2b.set_ylabel("IRR (%)",color=GREEN)
    a2.grid(False); a2b.grid(False); a2.tick_params(length=0); a2b.tick_params(length=0); a2.spines["left"].set_visible(False); a2b.spines["right"].set_visible(False)
    bb=dict(boxstyle="round,pad=0.18",fc="white",ec="none",alpha=0.92)
    for xi,v in zip(x,pb): a2.annotate(f"{v:.1f} r",(xi,v),textcoords="offset points",xytext=(0,-15),ha="center",fontsize=9,color="#D97706",weight="bold",bbox=bb,zorder=6)
    for xi,v in zip(x,irr): a2b.annotate(f"{v:.0f} %",(xi,v),textcoords="offset points",xytext=(0,11),ha="center",fontsize=9,color=GREEN,weight="bold",bbox=bb,zorder=6)
    a2.set_ylim(0,max(pb)*1.55); a2b.set_ylim(0,max(irr)*1.45)
    return _b64(fig)

def chart_cumcf(ctx):
    fig,ax=plt.subplots(figsize=(9,3.0)); yrs=list(range(0,21)); capex=ctx["net_capex_eur"]
    cols=["#94A3B8",GRID,GREEN]
    for i,s in enumerate(ctx["scenarios3"]):
        cf=[-capex]; acc=-capex
        for y in range(1,21):
            sv=s["save_total"]*((1-0.005)**(y-1))*((1+0.025)**(y-1)); tax=s.get("annual_tax",0) if y<=6 else 0
            acc+=sv-s.get("opex",capex*0.015)+tax; cf.append(acc)
        ax.plot(yrs,[c/1000 for c in cf],lw=2.8,color=cols[i%3],label=s["short"],zorder=3,solid_capstyle="round")
    ax.axhline(0,color="#CBD5E1",lw=1.2); ax.set_xlabel("Rok prevádzky"); ax.set_ylabel("Kumulatívny CF (k€)")
    ax.legend(frameon=False,fontsize=10,loc="upper left"); ax.set_xlim(0,20); _clean(ax)
    return _b64(fig)

def chart_benefit(ctx):
    parts=ctx["benefit_parts"]  # list (name,val,color)
    fig,ax=plt.subplots(figsize=(9,1.7)); left=0
    for name,val,c in parts:
        if val<=0: continue
        ax.barh(0,val,left=left,color=c,height=0.55,zorder=3)
        if val>4000: ax.text(left+val/2,0,f"{val/1000:.0f}k",ha="center",va="center",fontsize=9.5,color="white",weight="bold")
        left+=val
    ax.set_xlim(0,max(left,1)*1.02); ax.set_ylim(-0.5,0.5); ax.axis("off")
    import matplotlib.patches as mp
    handles=[mp.Patch(color=c) for _,v,c in parts if v>0]
    labels=[f"{n} ({v/1000:.0f}k)" for n,v,c in parts if v>0]
    ax.legend(handles,labels,frameon=False,fontsize=9.5,ncol=len(labels) or 1,loc="upper center",bbox_to_anchor=(0.5,2.1),handlelength=1,columnspacing=1.2)
    return _b64(fig)

def chart_tornado(ctx):
    base=ctx["tornado_base"]; drivers=ctx["tornado_drivers"]
    fig,ax=plt.subplots(figsize=(9,2.6)); y=np.arange(len(drivers))
    for i,(name,lo,hi) in enumerate(drivers):
        ax.barh(i,hi/1000,color=GREEN,height=0.62,zorder=3); ax.barh(i,lo/1000,color="#EF6B6B",height=0.62,zorder=3)
        ax.text((hi/1000)+4,i,f"+{hi/1000:.0f}k",va="center",fontsize=8.5,color="#16A34A",weight="bold")
        ax.text((lo/1000)-4,i,f"{lo/1000:.0f}k",va="center",ha="right",fontsize=8.5,color="#DC2626",weight="bold")
    ax.set_yticks(list(y)); ax.set_yticklabels([d[0] for d in drivers],fontsize=10)
    ax.axvline(0,color="#1E293B",lw=1.4); ax.set_xlabel("Zmena NPV oproti báze (k€)  ·  driver ±15 %")
    ax.invert_yaxis(); ax.grid(False); ax.tick_params(length=0); ax.set_xticklabels([])
    pad=max(abs(d[1]) for d in drivers)/1000*1.35; ax.set_xlim(-pad,pad)
    return _b64(fig)

def chart_montecarlo(ctx):
    s=np.array(ctx["mc_samples"])/1000; p10=ctx["mc_p10"]; p50=ctx["mc_p50"]; p90=ctx["mc_p90"]
    fig,ax=plt.subplots(figsize=(9,2.7))
    n,bins,patches=ax.hist(s,bins=44,color="#C7E0C7",edgecolor="white",linewidth=0.4,zorder=2)
    ymax=max(n); ax.set_ylim(0,ymax*1.32); bb=dict(boxstyle="round,pad=0.2",fc="white",ec="none",alpha=0.95)
    for q,c,lab in [(p10,"#EF6B6B","P10"),(p50,GREEN,"Medián"),(p90,"#F59E0B","P90")]:
        ax.axvline(q/1000,color=c,lw=2.6,zorder=3,ymax=0.80)
        ax.text(q/1000,ymax*1.14,f"{lab}: {q/1000:.0f} k€",ha="center",va="bottom",fontsize=9.5,color=c,weight="bold",bbox=bb,zorder=5)
    ax.set_xlabel("NPV 20 r. (k€)"); ax.set_yticks([]); _clean(ax,yg=False)
    return _b64(fig)


def chart_solar_donut(ctx):
    """Donut: ako sa využije vyrobená FVE energia (tlmená paleta)."""
    SOLAR="#92D050"; GREEN="#5E8E2A"; GRID="#9DB2C9"
    direct=float(ctx.get("direct_to_load_pct") or 0); batt=float(ctx.get("charging_battery_pct") or 0)
    exp=float(ctx.get("exported_pct") or 0); curt=float(ctx.get("curtailed_pct") or 0)
    prod=float(ctx.get("fve_prod_mwh") or 0)
    segs=[("Priamo do odberu",direct,SOLAR),("Cez batériu",batt,GREEN),
          ("Export do siete",exp,GRID),("Nevyužité",curt,"#CBD5E1")]
    segs=[s for s in segs if s[1] and s[1]>0.05]
    fig,ax=plt.subplots(figsize=(5.6,3.2))
    ax.pie([s[1] for s in segs], colors=[s[2] for s in segs], startangle=90, counterclock=False,
           wedgeprops=dict(width=0.40, edgecolor="white", linewidth=2.2))
    ax.text(0,0.10,f"{prod:.0f}",ha="center",va="center",fontsize=21,weight="bold",color=DARK)
    ax.text(0,-0.20,"MWh / rok",ha="center",va="center",fontsize=9.5,color=GRAY)
    ax.legend([f"{s[0]}  {s[1]:.0f} %" for s in segs], loc="center left",
              bbox_to_anchor=(1.02,0.5), frameon=False, fontsize=10.5, handlelength=1.0, labelspacing=0.7)
    ax.set(aspect="equal")
    return _b64(fig)


def chart_energy_flow(ctx):
    """Čistý tok energie — malé uzly, tlmená paleta, bez prázdnej batérie."""
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
    g=lambda k: float(ctx.get(k) or 0)
    FVE_C="#92D050"; GRID_C="#9DB2C9"; LOAD_C="#1F3A5F"; BAT_C="#5E8E2A"; TXT="#374151"
    pv=g("fve_prod_mwh"); load=g("year_mwh") or g("load_total_mwh")
    pv_load=g("pv_to_load_mwh"); pv_bat=g("pv_to_bat_mwh"); exp=g("export_mwh")
    bat_load=g("bat_to_load_mwh"); grid_load=g("grid_to_load_mwh"); grid_bat=g("grid_to_bat_mwh")
    grid_imp=g("grid_import_mwh")
    has_bat=(pv_bat+grid_bat+bat_load)>0.05
    fig,ax=plt.subplots(figsize=(7.6,3.6)); ax.set_xlim(0,10); ax.set_ylim(0,6.2); ax.axis("off")
    # uzly ako malé zaoblené obdĺžniky (x,y,farba,názov,hodnota)
    N={"solar":(1.7,4.6,FVE_C,"Výroba FVE",pv),
       "load":(5.0,1.5,LOAD_C,"Spotreba",load),
       "grid":(1.7,1.5,GRID_C,"Sieť",grid_imp)}
    if has_bat: N["bat"]=(8.3,4.6,BAT_C,"Batéria",pv_bat+grid_bat)
    NW,NH=1.5,0.62
    def node(x,y,c,nm,val):
        ax.add_patch(FancyBboxPatch((x-NW/2,y-NH/2),NW,NH,boxstyle="round,pad=0.02,rounding_size=0.10",
            facecolor=c,edgecolor="none",zorder=3))
        ax.text(x,y+0.02,f"{val:,.0f}".replace(","," "),ha="center",va="center",color="white",fontsize=12,weight="bold",zorder=4)
        ax.text(x,y-0.34,f"{nm}",ha="center",va="top",color=TXT,fontsize=9,zorder=4)
        ax.text(x+NW/2-0.08,y-0.16,"MWh",ha="right",va="center",color="white",fontsize=6.5,alpha=0.85,zorder=4)
    def flow(a,b,val,col,rad=0.0):
        if val<=0.05: return
        (x1,y1,*_),(x2,y2,*_)=N[a],N[b]
        ax.add_patch(FancyArrowPatch((x1,y1),(x2,y2),connectionstyle=f"arc3,rad={rad}",
            arrowstyle="-|>",mutation_scale=11,lw=0.8+min(4.2,val/45.0),color=col,alpha=0.5,
            shrinkA=30,shrinkB=30,zorder=2))
        mx,my=(x1+x2)/2,(y1+y2)/2
        ax.text(mx,my,f"{val:,.0f}".replace(","," "),fontsize=8,color=col,weight="bold",ha="center",va="center",
                bbox=dict(boxstyle="round,pad=0.14",fc="white",ec="none",alpha=0.95),zorder=5)
    for _k,(_x,_y,_c,_nm,_v) in N.items():
        node(_x,_y,_c,_nm,_v)
    flow("solar","load",pv_load,FVE_C,-0.16)
    flow("grid","load",grid_load,GRID_C,0.10)
    if exp>0.05:
        ax.annotate(f"Export {exp:,.0f} MWh".replace(","," "),(N["solar"][0]+NW/2,N["solar"][1]+0.45),
            fontsize=8,color=TXT,ha="left",va="bottom")
    if has_bat:
        flow("solar","bat",pv_bat,FVE_C,-0.12)
        flow("bat","load",bat_load,BAT_C,0.16)
        flow("grid","bat",grid_bat,GRID_C,0.28)
    fig.subplots_adjust(left=0.02,right=0.98,top=0.96,bottom=0.04)
    return _b64(fig)


def chart_soc_profile(ctx):
    """Orkestra-style denný SoC profil — riadený REÁLNYM ročným throughputom batérie z enginu
    (pv_to_bat/grid_to_bat nabíjanie, bat_to_load vybíjanie), rozložený do PV a večerných hodín."""
    cap=float(ctx.get("bess_kwh") or 0)
    if cap<=0: return None
    g=lambda k: float(ctx.get(k) or 0)
    chg_day=(g("pv_to_bat_mwh")+g("grid_to_bat_mwh"))*1000/365.0
    dis_day=g("bat_to_load_mwh")*1000/365.0
    if chg_day<=0 or dis_day<=0:  # fallback ~0.7 cyklu/deň
        chg_day=dis_day=cap*0.7
    # váhy: nabíjanie cez PV poludnie (8–16), vybíjanie do večernej špičky (17–22)
    cw=[0,0,0,0,0,0,0,0,0.06,0.12,0.17,0.19,0.18,0.14,0.09,0.05,0.01,0,0,0,0,0,0,0]
    dw=[0,0,0,0,0,0,0.04,0.06,0,0,0,0,0,0,0,0,0,0.10,0.20,0.24,0.22,0.14,0,0]
    cws=sum(cw) or 1; dws=sum(dw) or 1
    cw=[w/cws for w in cw]; dw=[w/dws for w in dw]
    chg=[chg_day*w for w in cw]; dis=[dis_day*w for w in dw]
    cr=cap*0.6
    chg=[min(c,cr) for c in chg]; dis=[min(d,cr) for d in dis]
    eff=0.95; soc=cap*0.20; socs=[0.0]*24
    for _ in range(2):
        socs=[0.0]*24
        for h in range(24):
            soc=min(cap,max(0.0, soc+chg[h]*eff-dis[h])); socs[h]=soc/cap*100
    fig,ax=plt.subplots(figsize=(9,3.0)); x=list(range(24))
    ax.bar(x,chg,color=GREEN,width=0.7,alpha=0.55,zorder=2,label="Nabíjanie (z PV)")
    ax.bar(x,[-d for d in dis],color=GRID,width=0.7,alpha=0.55,zorder=2,label="Vybíjanie (špička)")
    mx=max(max(chg),max(dis),1.0)
    ax.set_ylim(-mx*1.3,mx*1.3); ax.set_ylabel("Výkon kW"); ax.axhline(0,color="#CBD5E1",lw=1)
    ax2=ax.twinx(); ax2.plot(x,socs,color="#102D4C",lw=2.8,zorder=4,label="Stav nabitia (SoC)")
    ax2.fill_between(x,socs,color="#102D4C",alpha=0.06,zorder=1)
    ax2.set_ylim(0,105); ax2.set_ylabel("SoC %",color="#102D4C"); ax2.grid(False)
    ax2.tick_params(axis="y",colors="#102D4C")
    ax.set_xlim(-0.5,23.5); ax.set_xticks(range(0,24,3)); ax.set_xticklabels([f"{h}:00" for h in range(0,24,3)])
    _clean(ax); ax.spines["right"].set_visible(False)
    h1,l1=ax.get_legend_handles_labels(); h2,l2=ax2.get_legend_handles_labels()
    ax.legend(h1+h2,l1+l2,frameon=False,fontsize=9.5,ncol=3,loc="upper center",bbox_to_anchor=(0.5,1.16))
    return _b64(fig)


def chart_waterfall(ctx):
    """Orkestra-style NPV most — bridge ktorý sa SČÍTA PRESNE na engine NPV (žiadny druhý NPV systém).
    Prevádzkové úspory = reziduum (npv + net_capex - daň. štít - zostatok), aby bilancia sedela."""
    full=ctx["scenarios3"][-1]
    npv=float(full.get("npv") or 0)
    net=float(ctx.get("net_capex_eur") or 0)
    capex_gross=float(ctx.get("capex_total_eur") or net); dot=max(0.0,capex_gross-net)
    tax=float(full.get("annual_tax") or 0)
    DISC,LIFE,ODPIS,RESID=0.06,20,6,0.10
    pv_tax=sum(tax/((1+DISC)**y) for y in range(1,ODPIS+1))
    pv_res=(capex_gross*RESID)/((1+DISC)**LIFE)
    pv_ops=(npv+net)-pv_tax-pv_res   # diskontované prevádzkové úspory po OPEX (reziduum → bilancia sedí)
    steps=[("Investícia",-capex_gross,"#EF6B6B")]
    if dot>1: steps.append(("Dotácia",dot,LIME))
    steps += [("Prevádzkové\núspory (20 r.)",pv_ops,GREEN),("Daňový štít\n(r. 1–6)",pv_tax,AMBER),
              ("Zostatková\nhodnota",pv_res,"#A7D08C")]
    fig,ax=plt.subplots(figsize=(9,3.4)); acc=0.0; xs=[]
    for i,(nm,val,col) in enumerate(steps):
        bot=acc if val>=0 else acc+val
        ax.bar(i,abs(val)/1000,bottom=bot/1000,color=col,width=0.66,zorder=3)
        ax.text(i,(bot+abs(val)/2)/1000,f"{val/1000:+.0f}k",ha="center",va="center",fontsize=8.2,
                color="white",weight="bold")
        if i>0: ax.plot([i-1+0.33,i-0.33],[acc/1000,acc/1000],color="#CBD5E1",lw=1,ls=(0,(3,2)),zorder=1)
        acc+=val; xs.append(nm)
    ax.bar(len(steps),npv/1000,color="#102D4C",width=0.66,zorder=3)
    ax.text(len(steps),npv/1000+(abs(npv)/1000*0.04+2),f"{npv/1000:+.0f}k",ha="center",va="bottom",
            fontsize=9,color="#102D4C",weight="bold")
    xs.append("NPV 20 r.")
    ax.axhline(0,color="#1E293B",lw=1.2)
    ax.set_xticks(range(len(xs))); ax.set_xticklabels(xs,fontsize=8.2)
    ax.set_ylabel("k€ (diskontované)"); _clean(ax)
    return _b64(fig)


def chart_capex_split(ctx):
    """Orkestra-style: rozpad investície FVE/BESS/ostatné + dotácia → čistá."""
    pv=float(ctx.get("capex_pv_eur") or 0); bess=float(ctx.get("capex_bess_eur") or 0)
    total=float(ctx.get("capex_total_eur") or (pv+bess)); other=max(0.0,total-pv-bess)
    net=float(ctx.get("net_capex_eur") or total); dot=max(0.0,total-net)
    fig,ax=plt.subplots(figsize=(9,2.2))
    segs=[("FVE",pv,SOLAR),("Batéria",bess,GREEN),("Ostatné / inžiniering",other,"#94A3B8")]
    segs=[s for s in segs if s[1]>1]
    left=0
    for nm,val,c in segs:
        ax.barh(1,val/1000,left=left/1000,color=c,height=0.5,zorder=3)
        if val>total*0.06: ax.text((left+val/2)/1000,1,f"{val/1000:.0f}k",ha="center",va="center",
                                    color="white",fontsize=9,weight="bold")
        left+=val
    ax.barh(0,net/1000,color="#102D4C",height=0.5,zorder=3)
    ax.text(net/1000/2,0,f"{net/1000:.0f}k",ha="center",va="center",color="white",fontsize=9,weight="bold")
    if dot>1:
        ax.barh(0,dot/1000,left=net/1000,color=LIME,height=0.5,zorder=3,alpha=0.85,hatch="///",edgecolor="white")
        ax.text((net+dot/2)/1000,0,f"dotácia −{dot/1000:.0f}k",ha="center",va="center",color="#1A1A1A",fontsize=8.2,weight="bold")
    ax.set_yticks([0,1]); ax.set_yticklabels(["Po dotácii","Hrubá investícia"],fontsize=10)
    ax.set_xlabel("tis. € (bez DPH)"); ax.set_xlim(0,total/1000*1.08); _clean(ax,yg=False); ax.grid(False)
    import matplotlib.patches as mp
    hs=[mp.Patch(color=c) for _,v,c in segs]; ls=[f"{n} ({v/1000:.0f}k)" for n,v,c in segs]
    ax.legend(hs,ls,frameon=False,fontsize=9,ncol=len(segs) or 1,loc="upper center",bbox_to_anchor=(0.5,1.28))
    return _b64(fig)


def chart_value_stream(ctx):
    """Orkestra-style earnings by value stream: ročný € prínos podľa zdroja (horizontálne)."""
    parts=[(n,v,c) for n,v,c in ctx.get("benefit_parts",[]) if v and v>1]
    if not parts: return None
    parts=sorted(parts,key=lambda p:p[1])
    fig,ax=plt.subplots(figsize=(9,2.6)); y=np.arange(len(parts))
    vals=[p[1]/1000 for p in parts]
    bars=ax.barh(y,vals,color=[p[2] for p in parts],height=0.62,zorder=3)
    for i,(b,(n,v,c)) in enumerate(zip(bars,parts)):
        ax.text(b.get_width()+max(vals)*0.015,i,f"{v/1000:.1f}k €",va="center",fontsize=9.5,color="#374151",weight="bold")
    ax.set_yticks(list(y)); ax.set_yticklabels([p[0] for p in parts],fontsize=10)
    ax.set_xlim(0,max(vals)*1.18); ax.set_xlabel("€ / rok (plný scenár)"); _clean(ax,yg=False); ax.grid(False)
    tot=sum(p[1] for p in parts)
    ax.set_title(f"Ročný prínos spolu {tot/1000:.0f} tis. €",fontsize=10,color="#374151",loc="left",pad=6)
    return _b64(fig)


# ============ Orkestra vlna 2 — monitoring-style vizuály ============
_MONTHS_SK=["Apr","Máj","Jún","Júl","Aug","Sep","Okt","Nov","Dec","Jan","Feb","Mar"]

def chart_energy_metrics(ctx):
    """Orkestra-style trio: energetická nezávislosť / solar utilizácia / batéria utilizácia (mesačné plochy + priemer)."""
    g=lambda k: float(ctx.get(k) or 0)
    indep=g("coverage_pct") or g("samostatnost_pct") or 0
    solar_u=max(0.0,100.0-g("curtailed_pct"))
    if solar_u<=0: solar_u=95.0
    cap=g("bess_kwh"); dis_day=g("bat_to_load_mwh")*1000/365.0
    batt_u=min(100.0,(dis_day/cap*100.0)) if cap>0 else 0.0
    # mesačný tvar (sezónnosť PV): leto vyššie, zima nižšie; ukotvený na ročný priemer
    season=[0.55,0.78,1.0,1.18,1.30,1.33,1.32,1.20,1.0,0.74,0.5,0.45]  # Apr..Mar
    sm=sum(season)/12
    def monthly(avg, amp=1.0, lo=0, hi=100):
        return [max(lo,min(hi, avg*(1+amp*(s/sm-1)))) for s in season]
    rows=[("Energetická nezávislosť",monthly(indep,1.0),"#5B7CFA",indep),
          ("Solar utilizácia",monthly(solar_u,0.10,hi=100),"#FFC629",solar_u),
          ("Batéria utilizácia",monthly(batt_u,0.18),"#2EA84F",batt_u)]
    fig,axs=plt.subplots(3,1,figsize=(9,4.3),sharex=True)
    for ax,(nm,vals,col,avg) in zip(axs,rows):
        ax.fill_between(range(12),vals,color=col,alpha=0.85,zorder=2,lw=0)
        ax.set_ylim(0,105); ax.set_yticks([0,100]); ax.set_yticklabels(["0 %","100 %"],fontsize=8)
        ax.set_xlim(0,11); ax.grid(False); ax.tick_params(length=0)
        for sp in ["top","right","left"]: ax.spines[sp].set_visible(False)
        ax.set_title(nm,fontsize=10,weight="bold",color="#1A1A1A",loc="left",pad=2)
        ax.text(11.2,52,f"Ø {avg:.0f} %",fontsize=11,weight="bold",color=col,va="center")
    axs[-1].set_xticks(range(12)); axs[-1].set_xticklabels(_MONTHS_SK,fontsize=8.5)
    fig.tight_layout(h_pad=1.4)
    return _b64(fig)


def _week_dispatch(ctx):
    """Reprezentatívny 7-dňový (168h) rad: solar, load_before(net po PV), battery(+chg/-dis), load_after(grid), SoC%.
    Batéria riadená REÁLNYM ročným throughputom (pv_to_bat+grid_to_bat / bat_to_load), rozloženým do PV a večerných hodín."""
    g=lambda k: float(ctx.get(k) or 0)
    daily_pv=g("fve_prod_mwh")*1000/365.0
    daily_load=(g("year_mwh") or g("load_total_mwh"))*1000/365.0
    pvsh=[0,0,0,0,0,0.01,0.03,0.06,0.09,0.12,0.13,0.14,0.14,0.13,0.11,0.09,0.06,0.03,0.01,0,0,0,0,0]
    ldsh=ctx.get("hourly_wd")
    if not ldsh or sum(ldsh)<=0:
        ldsh=[0.025,0.024,0.024,0.024,0.025,0.028,0.033,0.045,0.058,0.065,0.068,0.068,0.065,0.060,0.058,0.058,0.052,0.048,0.043,0.040,0.038,0.034,0.030,0.027]
    pvs=sum(pvsh) or 1; lds=sum(ldsh) or 1
    cap=g("bess_kwh"); cr=cap*0.6; eff=0.95
    chg_day=(g("pv_to_bat_mwh")+g("grid_to_bat_mwh"))*1000/365.0
    dis_day=g("bat_to_load_mwh")*1000/365.0
    if cap>0 and (chg_day<=0 or dis_day<=0): chg_day=dis_day=cap*0.7
    # váhy nabíjania (PV poludnie) / vybíjania (ráno+večer)
    cw=[0,0,0,0,0,0,0,0,0.06,0.12,0.17,0.19,0.18,0.14,0.09,0.05,0.01,0,0,0,0,0,0,0]
    dw=[0,0,0,0,0,0,0.04,0.06,0,0,0,0,0,0,0,0,0,0.10,0.20,0.24,0.22,0.14,0,0]
    cws=sum(cw) or 1; dws=sum(dw) or 1; cw=[w/cws for w in cw]; dw=[w/dws for w in dw]
    daymul=[1.05,0.98,1.10,0.92,1.0,0.78,0.7]
    pvmul=[1.0,0.85,1.1,0.65,1.05,0.95,1.0]
    soc=cap*0.3
    SOL=[];LB=[];BAT=[];LA=[];SOC=[]
    for d in range(7):
        for h in range(24):
            pv=daily_pv*pvsh[h]/pvs*pvmul[d]
            load=daily_load*ldsh[h]/lds*daymul[d]
            c=min(chg_day*cw[h],cr); dd=min(dis_day*dw[h],cr)
            if cap<=0: c=dd=0.0
            if c>0:
                room=(cap-soc)/eff; c=min(c,room); soc+=c*eff
            if dd>0:
                dd=min(dd,soc); soc-=dd
            direct=min(pv,load)
            net_before=load-pv                 # po PV (môže byť záporné = export)
            net_after=max(0.0,load-direct-dd)+c # grid import: zostatok po PV a vybití + nabíjanie zo siete
            SOL.append(pv); LB.append(net_before); BAT.append(c-dd); LA.append(net_after)
            SOC.append(soc/cap*100 if cap>0 else 0)
    return SOL,LB,BAT,LA,SOC


def chart_interval_week(ctx):
    """Orkestra-style interval activity: stacked týždeň — batéria, solar, net load pred/po."""
    SOL,LB,BAT,LA,SOC=_week_dispatch(ctx); x=list(range(168))
    fig,ax=plt.subplots(figsize=(9,3.4))
    ax.bar(x,SOL,width=1.0,color="#FFE08A",zorder=2,label="Solárna výroba")
    ax.bar(x,[max(0,v) for v in BAT],width=1.0,color="#2EA84F",zorder=3,label="Batéria — nabíjanie")
    ax.bar(x,[min(0,v) for v in BAT],width=1.0,color="#2EA84F",alpha=0.6,zorder=3,label="Batéria — vybíjanie")
    ax.fill_between(x,LA,color="#AFC7F7",alpha=0.6,zorder=1,label="Net load po (zo siete)")
    ax.plot(x,LB,color="#3B5BDB",lw=1.0,ls=(0,(2,2)),zorder=4,label="Net load pred")
    ax.axhline(0,color="#CBD5E1",lw=1)
    ax.set_xlim(0,167); ax.set_xticks([i*24+12 for i in range(7)])
    ax.set_xticklabels(["Po","Ut","St","Št","Pi","So","Ne"],fontsize=9)
    ax.set_ylabel("kW"); _clean(ax)
    ax.legend(frameon=False,fontsize=8.2,ncol=5,loc="upper center",bbox_to_anchor=(0.5,1.17),columnspacing=1.0,handlelength=1.2)
    return _b64(fig)


def chart_daily_activity(ctx):
    """Orkestra-style priemerný deň: load pred/po (plochy) + solar + SoC krivka (pravá os)."""
    SOL,LB,BAT,LA,SOC=_week_dispatch(ctx)
    # priemer cez 7 dní -> 24h
    def avg24(arr): return [sum(arr[h::24][:7])/7 for h in range(24)]
    sol=avg24(SOL); la=avg24(LA); soc=avg24(SOC)
    g=lambda k: float(ctx.get(k) or 0)
    daily_load=(g("year_mwh") or g("load_total_mwh"))*1000/365.0
    ldsh=ctx.get("hourly_wd") or [0.025,0.024,0.024,0.024,0.025,0.028,0.033,0.045,0.058,0.065,0.068,0.068,0.065,0.060,0.058,0.058,0.052,0.048,0.043,0.040,0.038,0.034,0.030,0.027]
    lds=sum(ldsh) or 1; load_before=[daily_load*s/lds for s in ldsh]
    x=list(range(24)); has_bat=g("bess_kwh")>0
    fig,ax=plt.subplots(figsize=(9,3.2))
    ax.fill_between(x,load_before,color="#C7D7F7",alpha=0.7,zorder=1,label="Odber pred")
    ax.fill_between(x,[max(0,v) for v in la],color="#3B5BDB",alpha=0.45,zorder=2,label="Odber po (zo siete)")
    ax.fill_between(x,sol,color="#FFC629",alpha=0.55,zorder=2,label="Solárna výroba")
    ax.set_ylabel("kW"); ax.set_xlim(0,23); ax.set_xticks(range(0,24,3)); ax.set_xticklabels([f"{h}:00" for h in range(0,24,3)])
    _clean(ax)
    handles=[]
    if has_bat:
        ax2=ax.twinx(); ax2.plot(x,soc,color="#2EA84F",lw=2.8,zorder=5,label="SoC batérie")
        ax2.set_ylim(0,105); ax2.set_ylabel("SoC %",color="#2EA84F"); ax2.grid(False); ax2.tick_params(axis="y",colors="#2EA84F")
        h2,l2=ax2.get_legend_handles_labels()
    else:
        h2,l2=[],[]
    h1,l1=ax.get_legend_handles_labels()
    ax.legend(h1+h2,l1+l2,frameon=False,fontsize=8.4,ncol=4,loc="upper center",bbox_to_anchor=(0.5,1.15),columnspacing=1.1,handlelength=1.2)
    return _b64(fig)


def chart_demand_mrk(ctx):
    """Orkestra-style demand reduction: net load pred (špička odberu) vs MRK/RK rezervovaná kapacita."""
    SOL,LB,BAT,LA,SOC=_week_dispatch(ctx); x=list(range(168))
    g=lambda k: float(ctx.get(k) or 0)
    mrk=g("om_mrk_kw"); rk=g("om_rk_kw") or (mrk*0.9 if mrk else 0)
    fig,ax=plt.subplots(figsize=(9,3.0))
    ax.fill_between(x,[max(0,v) for v in LB],color="#C7D2DE",alpha=0.8,zorder=1,label="Odber pred (net load)")
    ax.fill_between(x,[max(0,v) for v in LA],color="#3B5BDB",alpha=0.45,zorder=2,label="Odber po (s batériou)")
    if mrk>0: ax.axhline(mrk,color="#DC2626",lw=1.6,ls=(0,(6,3)),zorder=4,label=f"MRK {mrk:.0f} kW")
    if rk>0 and abs(rk-mrk)>1: ax.axhline(rk,color="#F59E0B",lw=1.5,ls=(0,(4,3)),zorder=4,label=f"RK {rk:.0f} kW")
    ax.set_xlim(0,167); ax.set_xticks([i*24+12 for i in range(7)]); ax.set_xticklabels(["Po","Ut","St","Št","Pi","So","Ne"],fontsize=9)
    ax.set_ylabel("kW"); _clean(ax)
    top=max(max(LB),mrk or 0)*1.12 or 1; ax.set_ylim(0,top)
    ax.legend(frameon=False,fontsize=8.4,ncol=4,loc="upper center",bbox_to_anchor=(0.5,1.16),columnspacing=1.0,handlelength=1.4)
    return _b64(fig)


def chart_emissions_intensity(ctx):
    """Orkestra-style emisná intenzita pred/po (tCO2/MWh) + % zmena."""
    g=lambda k: float(ctx.get(k) or 0)
    red=g("co2_reduction_pct")
    before=0.25  # SK grid 2024 ~0.25 tCO2/MWh
    after=before*(1-red/100.0)
    fig,ax=plt.subplots(figsize=(4.4,3.2))
    bars=ax.bar([0,1],[before,after],color=["#C7CDD4","#5B7CFA"],width=0.5,zorder=3)
    for b,v in zip(bars,[before,after]):
        ax.text(b.get_x()+b.get_width()/2,v+0.008,f"{v:.2f}",ha="center",fontsize=11,weight="bold",color="#374151")
    ax.set_xticks([0,1]); ax.set_xticklabels(["Pred","Po"],fontsize=10)
    ax.set_ylim(0,before*1.25); ax.set_yticks([]); _clean(ax,yg=False); ax.grid(False)
    for sp in ["left"]: ax.spines[sp].set_visible(False)
    ax.text(1.62,before*0.55,f"−{red:.0f} %",fontsize=20,weight="bold",color="#5B7CFA",ha="center")
    ax.text(1.62,before*0.40,"zmena",fontsize=9.5,color="#5B7CFA",ha="center")
    ax.set_xlim(-0.5,2.3)
    return _b64(fig)
