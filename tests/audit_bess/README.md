# Audit batérie — property testy (2026-07-08)

Empirické testy správania BESS dispatchu a účtovania so známym analytickým výsledkom.
Vznikli pri hĺbkovom 3-agentovom audite enginu (nálezy N1-N10 + fixy, viď commit
"fix(engine): audit batérie").

Spustenie (z tohto adresára, potrebné numpy/pandas/pydantic/pyyaml/openpyxl/requests):
    ENERGO_SPOT_CSV=../../aom_data/sk_spot_2025_hourly.csv \
    ENERGO_TARIFF_YAML=../../aom_data/tariffs/2026.yaml \
    python3 t7_conservation.py   # atď.

- t1 nulový test (batéria bez príležitosti nesmie nič zarobiť/pokaziť)
- t2 učebnicová arbitráž (2-cenový deň vs ručný výpočet, tolerancia 15 %)
- t3 čistý PV posun (samospotreba ≈ RTE)
- t4 monotónnosť kapacity (+t4b: RTE atribúcia greedy dipu ≤1,25 %)
- t5 viac PV = viac hodnoty batérie (+t5c: rozklad delty po streamoch)
- t6 stres cyklov (throughput škáluje s budgetom)
- t7 konzervácia energie (0,0000 % @ 60 aj 15 min; curtail = explicitný sink)

Známe dokumentované limity (NIE chyby účtovania):
- greedy dispatch: P2b (PV→bat) neporovnáva cenu s neskorším grid-charge oknom
  → suboptimalita ohraničená ~1,25 % hodnoty batérie
- výkup exportu flat (export_price), nepoužíva spot formuly bilančných skupín
- PV model: hodinová špička ~0,65 kW/kWp (vyhladená) — ročný výnos kalibrovaný na PVGIS
  je OK, ale AC clipping meniča sa pri realistickom DC/AC 1,1–1,35 neprejaví; skutočné
  15-min špičky sú vyššie. Relevantné až pri extrémnom podsadení meniča.
