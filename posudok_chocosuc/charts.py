# -*- coding: utf-8 -*-
"""ChocoSuc-grade charts — context-driven (berie hodnoty z ctx, fallback ak chyba hourly)."""
import io, base64, math
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

GREEN="#16A34A"; LIME="#92D050"; DARK="#1A1A1A"; GRAY="#6B7280"; GRID="#5B7CFA"; SITE="#B85DD8"; SOLAR="#FFC629"; AMBER="#F59E0B"
plt.rcParams.update({"font.family":"DejaVu Sans","font.size":11,"axes.edgecolor":"#E3E8EE","axes.labelcolor":"#64748B",
 "xtick.color":"#94A3B8","ytick.color":"#94A3B8","xtick.labelsize":10,"ytick.labelsize":10,
 "axes.spines.top":False,"axes.spines.right":False,"axes.spines.left":False,
 "axes.grid":True,"axes.axisbelow":True,"grid.color":"#EEF2F6","grid.linewidth":1.0,"figure.facecolor":"white","axes.facecolor":"white"})
def _clean(ax,yg=True):
    ax.grid(axis="x",visible=False)
    if not yg: ax.grid(axis="y",visible=False)
    ax.tick_params(length=0)
def _b64(fig):
    b=io.BytesIO(); fig.savefig(b,format="png",dpi=200,bbox_inches="tight",pad_inches=0.12); plt.close(fig)
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
