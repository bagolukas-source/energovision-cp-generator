# -*- coding: utf-8 -*-
"""Pekny interaktivny HTML posudok (Chart.js) — zdroj pre verejny link AJ Chromium PDF."""
import json


def _num(v, d=0):
    try:
        return round(float(v or 0), d)
    except Exception:
        return 0


def _eur(x):
    try:
        return ("{:,.0f}".format(round(float(x or 0)))).replace(",", " ") + " €"
    except Exception:
        return "0 €"


def build_web_posudok_html(ctx, meta=None):
    meta = meta or {}
    g = lambda k, d=0: _num(ctx.get(k), d)
    client = meta.get("client_name") or ctx.get("client_name") or "Vazeny zakaznik"
    fve_kwp = g("fve_kwp") or g("pv_kwp")
    if not fve_kwp:
        fve_kwp = _num((ctx.get("scenarios") or [{}])[0].get("fve_kwp"))
    bess_kwh = g("bess_kwh")
    npv = g("npv_eur"); payback = g("payback_years", 1); irr = g("irr_pct", 1)
    capex = g("capex_total_eur"); net_capex = g("net_capex_eur") or capex
    saving = g("saving_y1_eur")
    samosp = g("samospotreba_pct", 1); samostat = g("samostatnost_pct", 1)
    pv_total = g("pv_total_mwh", 0); load_total = g("load_total_mwh") or g("annual_kwh") / 1000
    imp = g("grid_import_mwh", 0); exp = g("grid_export_mwh", 0)
    coverage = round(pv_total / load_total * 100, 1) if load_total else 0
    tax_y = g("tax_shield_annual_eur"); tax_tot = g("tax_shield_total_eur"); dppo = g("dppo_pct") or 21
    direct = g("direct_to_load_pct", 1); batt = g("charging_battery_pct", 1)
    exported = g("exported_pct", 1); curt = g("curtailed_pct", 1)
    mse = ctx.get("monthly_solar_export") or []; msl = ctx.get("monthly_solar_to_load") or []
    n = min(len(msl), len(mse), 12)
    msum = [(_num(msl[i]) + _num(mse[i])) for i in range(n)]
    tot = sum(msum) or 1
    monthly_mwh = [round(pv_total * x / tot, 1) for x in msum] if msum else []
    if not monthly_mwh:
        shape = [0.04, 0.06, 0.09, 0.11, 0.12, 0.12, 0.12, 0.11, 0.09, 0.07, 0.04, 0.03]
        monthly_mwh = [round(pv_total * c, 1) for c in shape]
    before = [round(_num(x), 1) for x in (ctx.get("hourly_load_kw_before") or [0] * 24)][:24]
    after = [round(_num(x), 1) for x in (ctx.get("hourly_load_kw_after") or [0] * 24)][:24]
    m = min(len(before), len(after))
    pv_kw = [round(max(0.0, before[i] - after[i]), 1) for i in range(m)]
    pv_to_load = g("pv_to_load_mwh")
    scen = ctx.get("scenarios") or []
    D = {
        "donutFve": {"l": ["Priamo do odberu", "Cez bateriu", "Export do siete", "Nevyuzite"],
                     "v": [direct, batt, exported, curt], "c": ["#92D050", "#5E8E2A", "#9DB2C9", "#CBD5E1"]},
        "donutOm": {"l": ["Solar (priamo + BESS)", "Siet (import)"], "v": [round(coverage, 0), round(100 - coverage, 0)],
                    "c": ["#92D050", "#9DB2C9"]},
        "monthly": {"labels": ["Jan", "Feb", "Mar", "Apr", "Maj", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dec"], "v": monthly_mwh},
        "daily": {"before": before, "after": after, "pv": pv_kw, "x": ["%02d" % h for h in range(24)]},
    }
    data_json = json.dumps(D, ensure_ascii=False)
    bars = [("Samospotreba FVE", samosp, "#92D050"), ("Energeticka nezavislost OM", samostat, "#5E8E2A"),
            ("Pokrytie spotreby vyrobou", coverage, "#9DB2C9")]
    bars_html = "".join(
        '<div class="bar-row"><div class="bar-top"><span>%s</span><b>%s %%</b></div>'
        '<div class="bar-track"><div class="bar-fill" style="width:%s%%;background:%s"></div></div></div>'
        % (nm, _num(v, 1), min(100, _num(v, 1)), c) for nm, v, c in bars)
    flow_nodes = (
        '<div class="fnode"><div class="fbar" style="background:#92D050"></div>'
        '<div class="fmeta"><div class="fname">Vyroba FVE</div><div class="fval">%s <span>MWh</span></div></div></div>'
        '<div class="fnode"><div class="fbar" style="background:#9DB2C9"></div>'
        '<div class="fmeta"><div class="fname">Siet - import</div><div class="fval">%s <span>MWh</span></div></div></div>'
        '<div class="fnode fnode-r"><div class="fmeta" style="text-align:right"><div class="fname">Odberne miesto</div>'
        '<div class="fval">%s <span>MWh</span></div><div class="fhint">export %s MWh - samospotreba %s MWh</div></div>'
        '<div class="fbar" style="background:#1F3A5F"></div></div>'
        % (_num(pv_total), _num(imp), _num(load_total), _num(exp), _num(pv_to_load)))
    def sg(s, *keys):
        for k in keys:
            if s.get(k) is not None:
                return s.get(k)
        return 0
    scen_rows = "".join(
        '<tr><td>%s</td><td class="num">%s</td><td class="num">%s r</td><td class="num">%s</td><td class="num">%s %%</td></tr>'
        % (s.get("name", ""),
           _eur(sg(s, "annual_save_eur", "save_total")),
           _num(sg(s, "payback_years", "payback"), 1),
           _eur(sg(s, "npv_eur", "npv")),
           _num(sg(s, "irr_pct", "irr"), 1)) for s in scen)
    bess_txt = (" + BESS %s kWh" % _num(bess_kwh)) if bess_kwh else ""
    repl = {
        "@@CLIENT@@": client, "@@FVE@@": "%s kWp%s" % (_num(fve_kwp), bess_txt),
        "@@NPV@@": _eur(npv),
        "@@INVEST@@": _eur(capex),
        "@@SAVING@@": _eur(saving),
        "@@PAYBACK@@": "%s r" % _num(payback, 1), "@@IRR@@": "%s %%" % _num(irr, 1),
        "@@SAMOSP@@": "%s %%" % _num(samosp, 1), "@@SAMOSTAT@@": "%s %%" % _num(samostat, 1),
        "@@TAXY@@": _eur(tax_y),
        "@@TAXTOT@@": _eur(tax_tot),
        "@@NETCAPEX@@": _eur(net_capex), "@@DPPO@@": "%s" % _num(dppo),
        "@@BARS@@": bars_html, "@@FLOW@@": flow_nodes, "@@SCEN@@": scen_rows, "@@DATA@@": data_json,
    }
    html = TEMPLATE
    for k, v in repl.items():
        html = html.replace(k, str(v))
    return html


TEMPLATE = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Technicko-ekonomicky posudok</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box}
body{font-family:'Segoe UI',Arial,Helvetica,sans-serif;color:#1A1A1A;margin:0;background:#F6F7F9;line-height:1.5}
.page{max-width:1080px;margin:0 auto;padding:28px}
.kick{font-size:11px;letter-spacing:2px;color:#5E8E2A;font-weight:600;text-transform:uppercase}
h1{font-size:30px;margin:6px 0 2px;font-weight:600}
.sub{color:#6B7280;font-size:14px}
.hero{background:#EFF6E8;border-radius:14px;padding:22px 26px;margin:22px 0;display:flex;justify-content:space-between;align-items:center}
.hero .lbl{max-width:60%;color:#374151;font-size:14px}
.hero .big{font-size:40px;font-weight:700;color:#5E8E2A;white-space:nowrap}
.grid{display:grid;gap:16px}
.cards{grid-template-columns:repeat(4,1fr)}
.card{background:#fff;border:1px solid #ECEEF1;border-left:4px solid #92D050;border-radius:0 12px 12px 0;padding:16px 18px}
.card .l{font-size:11px;letter-spacing:1px;color:#9CA3AF;text-transform:uppercase}
.card .v{font-size:26px;font-weight:700;margin-top:6px}
.card .n{font-size:12px;color:#6B7280;margin-top:2px}
.two{grid-template-columns:1fr 1fr;margin-top:8px}
.panel{background:#fff;border:1px solid #ECEEF1;border-radius:14px;padding:20px 22px}
.panel h2{font-size:16px;margin:0 0 2px;font-weight:600}
.panel .ps{font-size:12px;color:#9CA3AF;margin-bottom:14px}
.bar-row{margin:14px 0}
.bar-top{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}
.bar-track{height:10px;background:#EEF0F3;border-radius:6px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px}
.flow{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-top:6px}
.fnode{display:flex;align-items:center;gap:10px}
.fbar{width:12px;height:64px;border-radius:4px}
.fname{font-size:13px;font-weight:600}
.fval{font-size:22px;font-weight:700}
.fval span{font-size:12px;color:#9CA3AF;font-weight:400}
.fhint{font-size:11px;color:#9CA3AF;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #F0F1F3}
th{color:#9CA3AF;font-weight:500;font-size:11px;letter-spacing:1px;text-transform:uppercase}
td.num,th.num{text-align:right}
.taxbox{background:#F4FAEE;border-left:4px solid #92D050;border-radius:0 10px 10px 0;padding:14px 18px;margin-top:14px;font-size:13px;color:#374151}
.foot{text-align:center;color:#B8BEC7;font-size:11px;margin:26px 0 6px}
.chartbox{position:relative;height:240px}
</style></head><body><div class="page">
<div class="kick">Technicko-ekonomicky posudok FVE</div>
<h1>@@CLIENT@@</h1>
<div class="sub">Fotovoltika @@FVE@@ - analyza odberu, vyroby a ekonomiky</div>
<div class="hero"><div class="lbl"><b>Cisty prinos investicie (NPV 20 r.)</b> pri diskonte 6 % - po odpocitani celej investicie a prevadzkovych nakladov.</div><div class="big">+@@NPV@@</div></div>
<div class="grid cards">
  <div class="card"><div class="l">Investicia</div><div class="v">@@INVEST@@</div><div class="n">bez DPH</div></div>
  <div class="card"><div class="l">Rocne uspory</div><div class="v">@@SAVING@@</div><div class="n">rok 1</div></div>
  <div class="card"><div class="l">Navratnost</div><div class="v">@@PAYBACK@@</div><div class="n">so stitom</div></div>
  <div class="card"><div class="l">IRR</div><div class="v">@@IRR@@</div><div class="n">NPV +@@NPV@@</div></div>
</div>
<div class="grid two">
  <div class="panel"><h2>Spotreba FVE</h2><div class="ps">Rozdelenie vyrobenej energie podla aktivity</div><div class="chartbox"><canvas id="cFve"></canvas></div></div>
  <div class="panel"><h2>Energeticke ukazovatele</h2><div class="ps">Klucove KPI energetickeho profilu</div>@@BARS@@</div>
</div>
<div class="grid two">
  <div class="panel"><h2>Spotreba OM - zdroje energie</h2><div class="ps">Kde sa berie elektrina pre OM</div><div class="chartbox"><canvas id="cOm"></canvas></div></div>
  <div class="panel"><h2>Mesacna vyroba FVE</h2><div class="ps">Modelovana vyroba (MWh/mesiac)</div><div class="chartbox"><canvas id="cMon"></canvas></div></div>
</div>
<div class="panel" style="margin-top:16px"><h2>Denny profil odberu</h2><div class="ps">Priemerny den - pred/po + vyroba FVE (kW)</div><div class="chartbox" style="height:260px"><canvas id="cDay"></canvas></div></div>
<div class="panel" style="margin-top:16px"><h2>Rocny tok energie</h2><div class="ps">MWh za rok</div><div class="flow">@@FLOW@@</div></div>
<div class="panel" style="margin-top:16px"><h2>Scenare a ekonomika</h2><div class="ps">Baza - optimisticky</div>
  <table><thead><tr><th>Scenar</th><th class="num">Uspora &euro;/r</th><th class="num">Navratnost</th><th class="num">NPV 20 r.</th><th class="num">IRR</th></tr></thead><tbody>@@SCEN@@</tbody></table>
  <div class="taxbox"><b>Danovy stit z odpisu (roky 1-6):</b> 6-rocny linearny odpis zo zakladu @@NETCAPEX@@ pri DPPO @@DPPO@@ % = <b>@@TAXY@@/rok</b>, spolu <b>@@TAXTOT@@</b> danovej uspory. Je zahrnuty v NPV, IRR aj navratnosti.</div>
</div>
<div class="foot">Energovision, s.r.o. - www.energovision.sk - technicko-ekonomicky posudok</div>
</div>
<script>
window.__ready=false;
var D=@@DATA@@;
var GREEN="#92D050";
Chart.defaults.font.family="Segoe UI,Arial,sans-serif";Chart.defaults.font.size=12;Chart.defaults.color="#6B7280";
var done=0;function fin(){done++;if(done>=4)window.__ready=true;}
new Chart(document.getElementById('cFve'),{type:'doughnut',data:{labels:D.donutFve.l,datasets:[{data:D.donutFve.v,backgroundColor:D.donutFve.c,borderWidth:2,borderColor:'#fff'}]},options:{cutout:'62%',plugins:{legend:{position:'right',labels:{boxWidth:12,padding:10}}},animation:{onComplete:fin}}});
new Chart(document.getElementById('cOm'),{type:'doughnut',data:{labels:D.donutOm.l,datasets:[{data:D.donutOm.v,backgroundColor:D.donutOm.c,borderWidth:2,borderColor:'#fff'}]},options:{cutout:'62%',plugins:{legend:{position:'right',labels:{boxWidth:12,padding:10}}},animation:{onComplete:fin}}});
new Chart(document.getElementById('cMon'),{type:'bar',data:{labels:D.monthly.labels,datasets:[{data:D.monthly.v,backgroundColor:GREEN,borderRadius:4}]},options:{plugins:{legend:{display:false}},scales:{y:{ticks:{callback:function(v){return v+' MWh'}},grid:{color:'#F0F1F3'}},x:{grid:{display:false}}},animation:{onComplete:fin}}});
new Chart(document.getElementById('cDay'),{type:'line',data:{labels:D.daily.x,datasets:[{label:'Pred (odber)',data:D.daily.before,borderColor:'#9CA3AF',backgroundColor:'transparent',tension:.4,pointRadius:0,borderWidth:2},{label:'Po (siet)',data:D.daily.after,borderColor:'#4C7DF0',backgroundColor:'rgba(76,125,240,.06)',fill:true,tension:.4,pointRadius:0,borderWidth:2},{label:'Vyroba FVE',data:D.daily.pv,borderColor:GREEN,backgroundColor:'rgba(146,208,80,.10)',fill:true,tension:.4,pointRadius:0,borderWidth:2}]},options:{plugins:{legend:{position:'top',labels:{boxWidth:12,padding:12}}},scales:{y:{ticks:{callback:function(v){return v+' kW'}},grid:{color:'#F0F1F3'}},x:{grid:{display:false},ticks:{maxTicksLimit:12}}},animation:{onComplete:fin}}});
setTimeout(function(){window.__ready=true;},4000);
</script></body></html>"""
