import sys, json, os
sys.path.insert(0, "/tmp/cpgen")
from analyza_om_v2 import build_orkestra_context
from posudok_orkestra import generate_orkestra_pdf

d = json.load(open("/tmp/cpgen/_harness/amg.json"))
analyza = d["analyza"]
variants = [d["selected_full"]] + d["others"]

ctx = build_orkestra_context(analyza, variants, "test-amg")

# AI stub (len pre preview narativnej sekcie)
ctx.update({
  "ai_commentary": "Odberné miesto má charakter dvojzmennej priemyselnej prevádzky s vysokým denným odberom, čo vytvára priaznivý profil pre fotovoltiku bez batérie — 94 % vyrobenej energie sa spotrebuje priamo. Inštalácia 780 kWp pokryje približne 26 % celkovej spotreby a zníži ročný odber zo siete o 761 MWh.",
  "ai_recommendations": ["Odporúčame variant 780 kWp bez BESS — najvyššie NPV pri návratnosti 3,5 roka.", "Batériu v tejto fáze neodporúčame — pri 24/7 odbere je takmer celá výroba spotrebovaná priamo, BESS by predĺžil návratnosť na 6+ rokov."],
  "ai_anomalies": [],
  "ai_open_questions": [],
})

html_pdf = generate_orkestra_pdf(ctx)
out = "/sessions/fervent-eloquent-edison/mnt/outputs/AMG_harness.pdf"
open(out, "wb").write(html_pdf)
print("PDF bytes:", len(html_pdf))

# key sanity values
print("client_name:", ctx.get("client_name"))
print("pv_total_mwh:", round(ctx.get("pv_total_mwh",0),1))
print("grid_import_mwh:", round(ctx.get("grid_import_mwh",0),1))
print("grid_export_mwh:", round(ctx.get("grid_export_mwh",0),1))
print("co2_avoided_tonnes:", round(ctx.get("co2_avoided_tonnes",0),1))
print("trees:", ctx.get("trees_equivalent"))
print("cf[0..2]:", [round(x) for x in ctx.get("cf_array",[])[:3]])
