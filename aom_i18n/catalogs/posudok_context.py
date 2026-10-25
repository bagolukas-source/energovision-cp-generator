"""Deterministické vety a popisky kontextu posudku (posudok_chocosuc/context.py, profile_classifier.py)."""
EN = {
    # --- profil odberu (profile_classifier) ---
    "nepretržitá prevádzka (24/7)": "continuous operation (24/7)",
    "viaczmenná prevádzka": "multi-shift operation",
    "prevažne denná prevádzka s vysokým využitím": "predominantly daytime operation with high utilisation",
    "jednozmenná/denná prevádzka s poklesom mimo pracovného času": "single-shift/daytime operation with reduced load outside working hours",
    "nepravidelný profil s výraznými výkyvmi odberu": "irregular profile with significant load fluctuations",
    ", znížená prevádzka cez víkendy": ", reduced operation at weekends",
    "minimálna sezónnosť, rovnomerná spotreba počas roka": "minimal seasonality, even consumption throughout the year",
    "mierna sezónnosť": "moderate seasonality",
    "výrazná sezónnosť": "pronounced seasonality",
    "ranný špic": "morning peak",
    "poludňajší špic": "midday peak",
    "večerný špic": "evening peak",
    "nočný špic": "night-time peak",
    "vysoká vhodnosť pre FVE — odber sa kryje s dennou výrobou": "high suitability for a PV plant — consumption coincides with daytime generation",
    "FVE vhodná najmä s batériou — časť odberu je mimo slnečných hodín": "PV plant suitable mainly in combination with a battery — part of the consumption falls outside sunshine hours",
    "dobrá vhodnosť pre FVE pri dennom využití": "good suitability for a PV plant with daytime use",
    "odporúčaná batéria na presun výroby do odberu": "battery recommended to shift generation to consumption",
    "profil odberu (z dostupných dát)": "load profile (from available data)",
    "sezónnosť nehodnotená": "seasonality not assessed",
    "Profil je charakteristický ako {rezim}; {sezonnost}.": "Profile characteristics: {rezim}; {sezonnost}.",
    "Špička: {spicka}.": "Peak: {spicka}.",
    # --- návratnosť, arbitráž ---
    "{pb} (báza: nevráti sa)": "{pb} (base case: no payback)",
    "bez batérie — arbitráž sa neuplatňuje": "no battery — arbitrage does not apply",
    "spotová arbitráž z hodinového EMS (nabíjanie v lacných hodinách, vybíjanie v drahých)":
        "spot price arbitrage from the hourly energy management system (EMS): charging in cheap hours, discharging in expensive hours",
    "batéria obchoduje v merchant režime bilančnej skupiny — výnos v riadku Obchodovanie BS":
        "the battery trades in merchant mode within the balancing group — revenue shown in the row “BESS trading in the balancing group”",
    "klient nie je na spotovej tarife — arbitráž sa nepočíta (0 €)": "the client is not on a spot price tariff — arbitrage is not calculated (0 €)",
    "chýba hodinový spotový výpočet (EMS) — arbitráž nevyčíslená (0 €)": "hourly spot price calculation of the energy management system (EMS) missing — arbitrage not quantified (0 €)",
    "spread nedosiahol prah rentability (≥ 30 €/MWh) — arbitráž 0 €": "the spread did not reach the profitability threshold (≥ 30 €/MWh) — arbitrage 0 €",
    # --- scenáre, tornado ---
    "Báza (ÚRSO 2026 + spot OKTE)": "Base case (2026 tariffs of the Slovak energy regulator ÚRSO + OKTE spot price)",
    "Báza": "Base case",
    "Defenzívny (nižší výkup/cena)": "Defensive (lower feed-in/prices)",
    "Defenzívny": "Defensive",
    "Optimistický (rast cien energie)": "Optimistic (rising energy prices)",
    "Optimistický": "Optimistic",
    "Cena elektriny": "Electricity price",
    "Špecifický výnos FVE": "PV specific yield",
    "CAPEX (cena diela)": "CAPEX (project price)",
    # --- verdikt ---
    "Audit odberu: investícia je ekonomicky výhodná vo všetkých scenároch": "Consumption audit: the investment is economically viable in all scenarios",
    "Audit odberu: investícia je v báze výhodná, pri nepriaznivom vývoji môže byť stratová":
        "Consumption audit: the investment is viable in the base case but may be loss-making under adverse conditions",
    "Audit odberu: investícia sa v navrhnutej konfigurácii neoplatí": "Consumption audit: the investment is not viable in the proposed configuration",
    "Realizovať — návratnosť {pb}, NPV {npv} (báza); aj v pesimistickom scenári P10 zostáva NPV kladné ({p10}).":
        "Proceed — payback {pb}, NPV {npv} (base case); even in the pessimistic P10 scenario the NPV remains positive ({p10}).",
    "Realizovať s rezervou — návratnosť {pb}, NPV {npv} (báza); v pesimistickom scenári P10 klesá NPV pod nulu ({p10}), pravdepodobnosť kladného NPV {prob} %.":
        "Proceed with caution — payback {pb}, NPV {npv} (base case); in the pessimistic P10 scenario the NPV falls below zero ({p10}), probability of a positive NPV {prob} %.",
    "V tejto konfigurácii neodporúčame realizovať — NPV {npv} (báza), návratnosť {pb}; pravdepodobnosť kladného NPV {prob} %. Odporúčame preveriť menšiu batériu, inú veľkosť FVE alebo cenu diela.":
        "We do not recommend implementation in this configuration — NPV {npv} (base case), payback {pb}; probability of a positive NPV {prob} %. We recommend reviewing a smaller battery, a different PV plant size or the project price.",
    # --- komponenty, prínosy ---
    "FVE {kwp} kWp — moduly podľa cenovej ponuky": "PV plant {kwp} kWp — modules as per the price offer",
    "meniče ~{kw} kW AC + optimizéry": "inverters ~{kw} kW AC + optimisers",
    "batéria {kwh} kWh": "battery {kwh} kWh",
    "E-W / Juh": "E-W / South",
    "FVE priama samospotreba": "PV direct self-consumption",
    "{mwh} MWh priamo do odberu": "{mwh} MWh directly to consumption",
    "FVE export prebytkov": "PV surplus export",
    "{mwh} MWh do siete": "{mwh} MWh to the grid",
    "Batéria — posun PV do odberu": "Battery — shifting PV to consumption",
    "{mwh} MWh PV uskladnené a využité neskôr": "{mwh} MWh of PV stored and used later",
    "BESS arbitráž (spot v BS)": "BESS arbitrage (spot price in the balancing group)",
    "nabíjanie lacno / vybíjanie draho": "charging when cheap / discharging when expensive",
    "BESS obchodovanie v BS (merchant)": "BESS trading in the balancing group (merchant)",
    "spotové obchodovanie batérie cez bilančnú skupinu": "spot price trading of the battery via the balancing group",
    "Peak shaving (zníženie RK)": "Peak shaving (RK reduction)",
    "redukcia mesačného 15-min maxima": "reduction of the monthly 15-minute maximum",
    "Samospotreba": "Self-consumption",
    "Export": "Export",
    "Batéria": "Battery",
    "Arbitráž": "Arbitrage",
    "Daňový štít": "Tax shield",
    # --- hlavička, podklady, záver ---
    "faktúra": "invoice",
    "orientačné (ÚRSO 2026)": "indicative (ÚRSO 2026)",
    "skutočné namerané 15-min dáta": "actual measured 15-minute data",
    "15-min tvar z časti roka, extrapolovaný na celý rok": "15-minute load shape from part of the year, extrapolated to the full year",
    "modelovaný profil z faktúry (orientačný)": "profile modelled from the invoice (indicative)",
    "skutočné 15-min dáta": "actual 15-minute data",
    "modelovaný profil": "modelled profile",
    "Navrhnutý variant — batériové úložisko {kwh} kWh k existujúcej FVE {kwp} kWp":
        "Proposed option — {kwh} kWh battery storage for the existing {kwp} kWp PV plant",
    "Navrhnutý variant — FVE {kwp} kWp": "Proposed option — PV plant {kwp} kWp",
    "Analýza odberu, simulácia výroby a dispatch, ekonomické posúdenie v 3 scenároch s rizikovou analýzou.":
        "Consumption analysis, generation and dispatch simulation, economic assessment in 3 scenarios with risk analysis.",
    "15-min profil · ": "15-minute profile · ",
    "15-min profil (časť roka) · ": "15-minute profile (part of the year) · ",
    "profil z faktúry · ": "profile from invoice · ",
    "Odporúčanie pre klienta": "Recommendation for the client",
    "Investícia {capex} do batériového úložiska {kwh} kWh k existujúcej FVE {kwp} kWp":
        "An investment of {capex} in a {kwh} kWh battery storage system for the existing {kwp} kWp PV plant",
    "Investícia {capex} do FVE {kwp} kWp": "An investment of {capex} in a {kwp} kWp PV plant",
    " prinesie ročné úspory {s_base} (báza) až {s_opti} (optimistický scenár). Návratnosť {pb}, NPV 20 r. {npv} (báza), IRR {irr} %.":
        " will deliver annual savings of {s_base} (base case) up to {s_opti} (optimistic scenario). Payback {pb}, 20-year NPV {npv} (base case), IRR {irr} %.",
    # --- deterministické naratívy ---
    "priemyselná prevádzka": "industrial operation",
    "rovnomerná": "even",
    "dát": "data",
    "Charakter prevádzky: <b>{rezim}</b>. Z analyzovaných {src} vychádza priemerný odber {avg} kW a 15-min špička {peak} kW":
        "Type of operation: <b>{rezim}</b>. Based on the analysed {src}, the average load is {avg} kW and the 15-minute peak is {peak} kW",
    "(load factor {lf})": "(load factor {lf})",
    "Cez deň (slnečné hodiny) prebehne približne {pct} % spotreby — to priamo určuje, koľko FVE energie sa využije bez batérie.":
        "Approximately {pct} % of consumption occurs during the day (sunshine hours) — this directly determines how much PV energy can be used without a battery.",
    "Sezónnosť odberu: {sez}; ročná spotreba OM je {mwh} MWh.":
        "Seasonality of consumption: {sez}; the annual consumption of the supply point is {mwh} MWh.",
    "Priemer {avg} kW, špička {peak} kW": "Average {avg} kW, peak {peak} kW",
    ", špičková hodina {h}:00.": ", peak hour {h}:00.",
    "Ročná spotreba {mwh} MWh.": "Annual consumption {mwh} MWh.",
    "Dopočítané z priemeru: {m}.": "Filled in from the average: {m}.",
    "Z {prod} MWh ročnej výroby FVE sa <b>{sams} %</b> ({selfu} MWh) spotrebuje priamo v prevádzke a iba {exp_pct} % ({exp} MWh) odchádza ako prebytok do siete. ":
        "Of the {prod} MWh of annual PV generation, <b>{sams} %</b> ({selfu} MWh) is consumed directly on site and only {exp_pct} % ({exp} MWh) is exported to the grid as surplus. ",
    "Relatívne malá FVE ({fve} kWp) voči spotrebe ({year} MWh/r) je dôvod vysokej samospotreby — vyrobená energia sa takmer celá využije na mieste. ":
        "The relatively small PV plant ({fve} kWp) compared with consumption ({year} MWh/yr) explains the high self-consumption — almost all generated energy is used on site. ",
    "FVE pokryje {cov} % ročnej spotreby OM; zvyšných {imp} MWh zostáva zo siete.":
        "The PV plant covers {cov} % of the supply point's annual consumption; the remaining {imp} MWh is still drawn from the grid.",
    "~{nmod} ks modulov á ~580 Wp (orientačne — spresní cenová ponuka)":
        "~{nmod} modules of ~580 Wp each (indicative — to be specified in the price offer)",
    "meniče ~{ac} kW AC (DC/AC ~1,1)": "inverters ~{ac} kW AC (DC/AC ~1.1)",
    "Juh 13° (orientačne — spresní cenová ponuka)": "South 13° (indicative — to be specified in the price offer)",
    "Juh {t}°": "South {t}°",
    "Východ-Západ {t}°": "East-West {t}°",
    "PVGIS · OKTE 2025": "PVGIS · OKTE 2025 (Slovak electricity market operator, day-ahead spot)",
    # kódy analyza_om.tarif_source (len en/de)
    "zadané v CRM": "entered in CRM",
    "zadané ručne": "entered manually",
    "cenník ÚRSO": "ÚRSO price list",
    "spot OKTE": "OKTE spot price",
    "Bázový výpočet z hodinovej simulácie (ceny ÚRSO 2026 + spot OKTE). Ročná úspora {save} €, návratnosť {pb}.":
        "Base calculation from the hourly simulation, using 2026 tariffs of ÚRSO (Slovak energy regulator) and prices of "
        "OKTE (Slovak electricity market operator, day-ahead spot). Annual saving {save} €, payback {pb}.",
    "Stresový pohľad — nižší výkup prebytkov a opatrnejšie ceny energie. Ukazuje odolnosť investície: úspora {save} €/r, návratnosť {pb}.":
        "Stress view — lower feed-in prices for surplus energy and more cautious energy prices. It shows the resilience of the investment: saving {save} €/yr, payback {pb}.",
    "Pri raste trhových cien energie hodnota vlastnej výroby rastie — úspora {save} €/r, NPV 20 r. {npv} €, IRR {irr} %.":
        "As market energy prices rise, the value of own generation increases — saving {save} €/yr, 20-year NPV {npv} €, IRR {irr} %.",
    "Predikovateľnosť ceny energie na 20+ rokov": "Predictable energy costs for 20+ years",
    "Vlastná výroba pokryje {cov} % ročnej spotreby pri {sams} % samospotrebe — prirodzený hedge proti rastu cien elektriny a regulačných poplatkov.":
        "Own generation covers {cov} % of annual consumption at {sams} % self-consumption — a natural hedge against rising electricity prices and regulated charges.",
    "Daňová optimalizácia": "Tax optimisation",
    "6-ročný rovnomerný odpis predstavuje úsporu 21 % × {capex} € = {dan} € na dani z príjmu (priemerne {dan_y} €/rok počas prvých 6 rokov).":
        "Straight-line depreciation over 6 years yields an income tax saving of 21 % × {capex} € = {dan} € (on average {dan_y} €/year over the first 6 years).",
    "Špičkový odber {peak} kW je len {pct} % zazmluvnenej MRK {mrk} kW. Odberné miesto má rezervu znížiť rezervovanú kapacitu (RK, min. 50 % MRK) a šetriť na pevnej zložke distribúcie":
        "The peak load of {peak} kW is only {pct} % of the contracted maximum reserved capacity (MRK) of {mrk} kW. "
        "The supply point has headroom to reduce its reserved capacity (RK, min. 50 % of MRK) and save on the fixed distribution charge",
    "; batéria toto zníženie zabezpečuje proti špičkám (modelovaná úspora {save} €/r).":
        "; the battery secures this reduction against load peaks (modelled saving {save} €/yr).",
    " (úspora nezávislá od FVE/batérie — odporúčame preveriť s prevádzkovateľom distribučnej sústavy).":
        " (a saving independent of the PV plant/battery — we recommend verifying it with the regional distribution system operator).",
    "Optimalizácia rezervovanej kapacity (MRK/RK)": "Optimisation of reserved capacity (MRK/RK)",
    "Peak shaving cez batériu": "Peak shaving with the battery",
    "Batéria {bess} kWh znižuje 15-min špičky zo siete (modelovaná úspora {save} €/r) — podklad pre zníženie rezervovanej kapacity (RK {rk} kW).":
        "The {bess} kWh battery reduces 15-minute peaks drawn from the grid (modelled saving {save} €/yr) — a basis for reducing the reserved capacity (RK {rk} kW).",
    "ESG profil a CSRD reporting": "ESG profile and CSRD reporting",
    "Ročná úspora ~{co2} t CO₂ (pri SK emisnej intenzite ~0,25 kg/kWh) — doložiteľný podklad pre EU Taxonómiu a CSRD disclosures.":
        "Annual saving of ~{co2} t CO₂ (at the Slovak grid emission intensity of ~0.25 kg/kWh) — verifiable evidence for EU Taxonomy and CSRD disclosures.",
    "Reziduálna hodnota po 20 rokoch": "Residual value after 20 years",
    "Ekonomika neráta s poklesom výkonu FVE; konzervatívne 10 % CAPEX = {res} € reziduálnej hodnoty.":
        "The economics assume no degradation of PV output; conservatively 10 % of CAPEX = {res} € residual value.",
    # --- 2026-10-25: záporná arbitráž ako náklad, distribútor OM ---
    'pri spotovej tarife spread medzi lacnými a drahými hodinami nepokryl straty batérie pri nabíjaní a vybíjaní': "on the spot tariff, the spread between cheap and expensive hours did not cover the battery's charging and discharging losses",
    'pri fixnej tarife nemá nabíjanie zo siete cenový rozdiel, takže straty batérie pri nabíjaní a vybíjaní sú čistý náklad': "on a fixed-price tariff, charging from the grid brings no price difference, so the battery's charging and discharging losses are a pure cost",
    '; náklad je už odpočítaný v ročnej úspore': '; this cost is already deducted from the annual savings',
    'Batéria — nabíjanie zo siete (náklad)': 'Battery — charging from the grid (cost)',
    '{mwh} MWh zo siete, straty pri nabíjaní a vybíjaní': '{mwh} MWh from the grid, charging and discharging losses',
    'tarif {dist}': '{dist} tariff (regional distribution system operator)',
    'tarif distribútora': 'distribution system operator tariff',
}

DE = {
    # --- profil odberu (profile_classifier) ---
    "nepretržitá prevádzka (24/7)": "Dauerbetrieb (24/7)",
    "viaczmenná prevádzka": "Mehrschichtbetrieb",
    "prevažne denná prevádzka s vysokým využitím": "überwiegend Tagesbetrieb mit hoher Auslastung",
    "jednozmenná/denná prevádzka s poklesom mimo pracovného času": "Einschicht-/Tagesbetrieb mit geringerer Last außerhalb der Arbeitszeit",
    "nepravidelný profil s výraznými výkyvmi odberu": "unregelmäßiges Profil mit starken Lastschwankungen",
    ", znížená prevádzka cez víkendy": ", reduzierter Betrieb an Wochenenden",
    "minimálna sezónnosť, rovnomerná spotreba počas roka": "minimale Saisonalität, gleichmäßiger Verbrauch über das Jahr",
    "mierna sezónnosť": "mäßige Saisonalität",
    "výrazná sezónnosť": "ausgeprägte Saisonalität",
    "ranný špic": "Morgenspitze",
    "poludňajší špic": "Mittagsspitze",
    "večerný špic": "Abendspitze",
    "nočný špic": "Nachtspitze",
    "vysoká vhodnosť pre FVE — odber sa kryje s dennou výrobou": "hohe Eignung für eine PV-Anlage — der Verbrauch deckt sich mit der Tageserzeugung",
    "FVE vhodná najmä s batériou — časť odberu je mimo slnečných hodín": "PV-Anlage vor allem mit Batterie geeignet — ein Teil des Verbrauchs liegt außerhalb der Sonnenstunden",
    "dobrá vhodnosť pre FVE pri dennom využití": "gute Eignung für eine PV-Anlage bei Tagesnutzung",
    "odporúčaná batéria na presun výroby do odberu": "Batterie empfohlen, um die Erzeugung in den Verbrauch zu verlagern",
    "profil odberu (z dostupných dát)": "Lastprofil (aus verfügbaren Daten)",
    "sezónnosť nehodnotená": "Saisonalität nicht bewertet",
    "Profil je charakteristický ako {rezim}; {sezonnost}.": "Profilcharakteristik: {rezim}; {sezonnost}.",
    "Špička: {spicka}.": "Spitze: {spicka}.",
    # --- návratnosť, arbitráž ---
    "{pb} (báza: nevráti sa)": "{pb} (Basisfall: keine Amortisation)",
    "bez batérie — arbitráž sa neuplatňuje": "ohne Batterie — keine Arbitrage",
    "spotová arbitráž z hodinového EMS (nabíjanie v lacných hodinách, vybíjanie v drahých)":
        "Spotpreis-Arbitrage aus dem stündlichen Energiemanagementsystem (EMS): Laden in günstigen Stunden, Entladen in teuren Stunden",
    "batéria obchoduje v merchant režime bilančnej skupiny — výnos v riadku Obchodovanie BS":
        "die Batterie handelt im Merchant-Modus im Bilanzkreis — Ertrag in der Zeile „BESS-Handel im Bilanzkreis“",
    "klient nie je na spotovej tarife — arbitráž sa nepočíta (0 €)": "der Kunde hat keinen Spotpreistarif — Arbitrage wird nicht berechnet (0 €)",
    "chýba hodinový spotový výpočet (EMS) — arbitráž nevyčíslená (0 €)": "stündliche Spotpreisberechnung des Energiemanagementsystems (EMS) fehlt — Arbitrage nicht beziffert (0 €)",
    "spread nedosiahol prah rentability (≥ 30 €/MWh) — arbitráž 0 €": "der Spread hat die Rentabilitätsschwelle (≥ 30 €/MWh) nicht erreicht — Arbitrage 0 €",
    # --- scenáre, tornado ---
    "Báza (ÚRSO 2026 + spot OKTE)": "Basisfall (Tarife 2026 der slowakischen Regulierungsbehörde ÚRSO + OKTE-Spotpreis)",
    "Báza": "Basisfall",
    "Defenzívny (nižší výkup/cena)": "Defensiv (niedrigere Einspeisevergütung/Preise)",
    "Defenzívny": "Defensiv",
    "Optimistický (rast cien energie)": "Optimistisch (steigende Energiepreise)",
    "Optimistický": "Optimistisch",
    "Cena elektriny": "Strompreis",
    "Špecifický výnos FVE": "Spezifischer Ertrag der PV-Anlage",
    "CAPEX (cena diela)": "CAPEX (Anlagenpreis)",
    # --- verdikt ---
    "Audit odberu: investícia je ekonomicky výhodná vo všetkých scenároch": "Verbrauchsaudit: Die Investition ist in allen Szenarien wirtschaftlich vorteilhaft",
    "Audit odberu: investícia je v báze výhodná, pri nepriaznivom vývoji môže byť stratová":
        "Verbrauchsaudit: Die Investition ist im Basisfall vorteilhaft, kann bei ungünstiger Entwicklung jedoch verlustbringend sein",
    "Audit odberu: investícia sa v navrhnutej konfigurácii neoplatí": "Verbrauchsaudit: Die Investition lohnt sich in der vorgeschlagenen Konfiguration nicht",
    "Realizovať — návratnosť {pb}, NPV {npv} (báza); aj v pesimistickom scenári P10 zostáva NPV kladné ({p10}).":
        "Umsetzen — Amortisation {pb}, NPV {npv} (Basisfall); auch im pessimistischen P10-Szenario bleibt der NPV positiv ({p10}).",
    "Realizovať s rezervou — návratnosť {pb}, NPV {npv} (báza); v pesimistickom scenári P10 klesá NPV pod nulu ({p10}), pravdepodobnosť kladného NPV {prob} %.":
        "Mit Vorbehalt umsetzen — Amortisation {pb}, NPV {npv} (Basisfall); im pessimistischen P10-Szenario sinkt der NPV unter null ({p10}), Wahrscheinlichkeit eines positiven NPV {prob} %.",
    "V tejto konfigurácii neodporúčame realizovať — NPV {npv} (báza), návratnosť {pb}; pravdepodobnosť kladného NPV {prob} %. Odporúčame preveriť menšiu batériu, inú veľkosť FVE alebo cenu diela.":
        "In dieser Konfiguration empfehlen wir keine Umsetzung — NPV {npv} (Basisfall), Amortisation {pb}; Wahrscheinlichkeit eines positiven NPV {prob} %. Wir empfehlen, eine kleinere Batterie, eine andere Größe der PV-Anlage oder den Anlagenpreis zu prüfen.",
    # --- komponenty, prínosy ---
    "FVE {kwp} kWp — moduly podľa cenovej ponuky": "PV-Anlage {kwp} kWp — Module gemäß Angebot",
    "meniče ~{kw} kW AC + optimizéry": "Wechselrichter ~{kw} kW AC + Optimierer",
    "batéria {kwh} kWh": "Batterie {kwh} kWh",
    "E-W / Juh": "O-W / Süd",
    "FVE priama samospotreba": "Direkter PV-Eigenverbrauch",
    "{mwh} MWh priamo do odberu": "{mwh} MWh direkt in den Verbrauch",
    "FVE export prebytkov": "PV-Einspeisung von Überschüssen",
    "{mwh} MWh do siete": "{mwh} MWh ins Netz",
    "Batéria — posun PV do odberu": "Batterie — Verlagerung von PV-Strom in den Verbrauch",
    "{mwh} MWh PV uskladnené a využité neskôr": "{mwh} MWh PV-Strom gespeichert und später genutzt",
    "BESS arbitráž (spot v BS)": "BESS-Arbitrage (Spotpreis im Bilanzkreis)",
    "nabíjanie lacno / vybíjanie draho": "Laden bei niedrigen / Entladen bei hohen Preisen",
    "BESS obchodovanie v BS (merchant)": "BESS-Handel im Bilanzkreis (Merchant)",
    "spotové obchodovanie batérie cez bilančnú skupinu": "Spothandel der Batterie über den Bilanzkreis",
    "Peak shaving (zníženie RK)": "Peak Shaving (Senkung der RK)",
    "redukcia mesačného 15-min maxima": "Senkung des monatlichen 15-Minuten-Maximums",
    "Samospotreba": "Eigenverbrauch",
    "Export": "Einspeisung",
    "Batéria": "Batterie",
    "Arbitráž": "Arbitrage",
    "Daňový štít": "Steuervorteil",
    # --- hlavička, podklady, záver ---
    "faktúra": "Rechnung",
    "orientačné (ÚRSO 2026)": "Richtwerte (ÚRSO 2026)",
    "skutočné namerané 15-min dáta": "tatsächlich gemessene 15-Minuten-Daten",
    "15-min tvar z časti roka, extrapolovaný na celý rok": "15-Minuten-Lastgang aus einem Teil des Jahres, auf das ganze Jahr hochgerechnet",
    "modelovaný profil z faktúry (orientačný)": "aus der Rechnung modelliertes Profil (Richtwert)",
    "skutočné 15-min dáta": "tatsächliche 15-Minuten-Daten",
    "modelovaný profil": "modelliertes Profil",
    "Navrhnutý variant — batériové úložisko {kwh} kWh k existujúcej FVE {kwp} kWp":
        "Vorgeschlagene Variante — Batteriespeicher {kwh} kWh für die bestehende PV-Anlage {kwp} kWp",
    "Navrhnutý variant — FVE {kwp} kWp": "Vorgeschlagene Variante — PV-Anlage {kwp} kWp",
    "Analýza odberu, simulácia výroby a dispatch, ekonomické posúdenie v 3 scenároch s rizikovou analýzou.":
        "Verbrauchsanalyse, Simulation von Erzeugung und Dispatch, wirtschaftliche Bewertung in 3 Szenarien mit Risikoanalyse.",
    "15-min profil · ": "15-Minuten-Profil · ",
    "15-min profil (časť roka) · ": "15-Minuten-Profil (Teil des Jahres) · ",
    "profil z faktúry · ": "Profil aus Rechnung · ",
    "Odporúčanie pre klienta": "Empfehlung für den Kunden",
    "Investícia {capex} do batériového úložiska {kwh} kWh k existujúcej FVE {kwp} kWp":
        "Eine Investition von {capex} in einen Batteriespeicher mit {kwh} kWh für die bestehende PV-Anlage mit {kwp} kWp",
    "Investícia {capex} do FVE {kwp} kWp": "Eine Investition von {capex} in eine PV-Anlage mit {kwp} kWp",
    " prinesie ročné úspory {s_base} (báza) až {s_opti} (optimistický scenár). Návratnosť {pb}, NPV 20 r. {npv} (báza), IRR {irr} %.":
        " erzielt jährliche Einsparungen von {s_base} (Basisfall) bis {s_opti} (optimistisches Szenario). Amortisation {pb}, NPV über 20 Jahre {npv} (Basisfall), IRR {irr} %.",
    # --- deterministické naratívy ---
    "priemyselná prevádzka": "Industriebetrieb",
    "rovnomerná": "gleichmäßig",
    "dát": "Daten",
    "Charakter prevádzky: <b>{rezim}</b>. Z analyzovaných {src} vychádza priemerný odber {avg} kW a 15-min špička {peak} kW":
        "Betriebsart: <b>{rezim}</b>. Auf Basis der analysierten Verbrauchsdaten ({src}) ergeben sich eine durchschnittliche Last von {avg} kW und eine 15-Minuten-Spitze von {peak} kW",
    "(load factor {lf})": "(Lastfaktor {lf})",
    "Cez deň (slnečné hodiny) prebehne približne {pct} % spotreby — to priamo určuje, koľko FVE energie sa využije bez batérie.":
        "Rund {pct} % des Verbrauchs fallen tagsüber (in den Sonnenstunden) an — dies bestimmt direkt, wie viel PV-Energie ohne Batterie genutzt wird.",
    "Sezónnosť odberu: {sez}; ročná spotreba OM je {mwh} MWh.":
        "Saisonalität des Verbrauchs: {sez}; der Jahresverbrauch der Abnahmestelle beträgt {mwh} MWh.",
    "Priemer {avg} kW, špička {peak} kW": "Durchschnitt {avg} kW, Spitze {peak} kW",
    ", špičková hodina {h}:00.": ", Spitzenstunde {h}:00 Uhr.",
    "Ročná spotreba {mwh} MWh.": "Jahresverbrauch {mwh} MWh.",
    "Dopočítané z priemeru: {m}.": "Aus dem Durchschnitt ergänzt: {m}.",
    "Z {prod} MWh ročnej výroby FVE sa <b>{sams} %</b> ({selfu} MWh) spotrebuje priamo v prevádzke a iba {exp_pct} % ({exp} MWh) odchádza ako prebytok do siete. ":
        "Von der jährlichen PV-Erzeugung von {prod} MWh werden <b>{sams} %</b> ({selfu} MWh) direkt im Betrieb verbraucht und nur {exp_pct} % ({exp} MWh) als Überschuss ins Netz eingespeist. ",
    "Relatívne malá FVE ({fve} kWp) voči spotrebe ({year} MWh/r) je dôvod vysokej samospotreby — vyrobená energia sa takmer celá využije na mieste. ":
        "Die im Verhältnis zum Verbrauch ({year} MWh/Jahr) relativ kleine PV-Anlage ({fve} kWp) erklärt den hohen Eigenverbrauch — nahezu die gesamte erzeugte Energie wird vor Ort genutzt. ",
    "FVE pokryje {cov} % ročnej spotreby OM; zvyšných {imp} MWh zostáva zo siete.":
        "Die PV-Anlage deckt {cov} % des Jahresverbrauchs der Abnahmestelle; die restlichen {imp} MWh werden weiterhin aus dem Netz bezogen.",
    "~{nmod} ks modulov á ~580 Wp (orientačne — spresní cenová ponuka)":
        "~{nmod} Module mit je ~580 Wp (Richtwert — wird im Angebot präzisiert)",
    "meniče ~{ac} kW AC (DC/AC ~1,1)": "Wechselrichter ~{ac} kW AC (DC/AC ~1,1)",
    "Juh 13° (orientačne — spresní cenová ponuka)": "Süd 13° (Richtwert — wird im Angebot präzisiert)",
    "Juh {t}°": "Süd {t}°",
    "Východ-Západ {t}°": "Ost-West {t}°",
    "PVGIS · OKTE 2025": "PVGIS · OKTE 2025 (slowakischer Strommarktbetreiber, Day-Ahead-Spot)",
    "zadané v CRM": "im CRM erfasst",
    "zadané ručne": "manuell erfasst",
    "cenník ÚRSO": "ÚRSO-Preisblatt",
    "spot OKTE": "OKTE-Spotpreis",
    "Bázový výpočet z hodinovej simulácie (ceny ÚRSO 2026 + spot OKTE). Ročná úspora {save} €, návratnosť {pb}.":
        "Basisberechnung aus der stündlichen Simulation mit Tarifen 2026 der ÚRSO (slowakische Regulierungsbehörde für Netzindustrien) "
        "und Preisen der OKTE (slowakischer Strommarktbetreiber, Day-Ahead-Spot). Amortisation {pb} bei einer jährlichen Einsparung von {save} €.",
    "Stresový pohľad — nižší výkup prebytkov a opatrnejšie ceny energie. Ukazuje odolnosť investície: úspora {save} €/r, návratnosť {pb}.":
        "Stressbetrachtung — niedrigere Vergütung für Überschüsse und vorsichtigere Energiepreise. Sie zeigt die Robustheit der Investition: Amortisation {pb} bei einer Einsparung von {save} €/Jahr.",
    "Pri raste trhových cien energie hodnota vlastnej výroby rastie — úspora {save} €/r, NPV 20 r. {npv} €, IRR {irr} %.":
        "Bei steigenden Marktpreisen für Energie steigt der Wert der Eigenerzeugung — Einsparung {save} €/Jahr, NPV über 20 Jahre {npv} €, IRR {irr} %.",
    "Predikovateľnosť ceny energie na 20+ rokov": "Planbare Energiekosten über 20+ Jahre",
    "Vlastná výroba pokryje {cov} % ročnej spotreby pri {sams} % samospotrebe — prirodzený hedge proti rastu cien elektriny a regulačných poplatkov.":
        "Die Eigenerzeugung deckt {cov} % des Jahresverbrauchs bei {sams} % Eigenverbrauch — eine natürliche Absicherung gegen steigende Strompreise und regulierte Entgelte.",
    "Daňová optimalizácia": "Steuerliche Optimierung",
    "6-ročný rovnomerný odpis predstavuje úsporu 21 % × {capex} € = {dan} € na dani z príjmu (priemerne {dan_y} €/rok počas prvých 6 rokov).":
        "Die lineare Abschreibung über 6 Jahre ergibt eine Ertragsteuerersparnis von 21 % × {capex} € = {dan} € (durchschnittlich {dan_y} €/Jahr in den ersten 6 Jahren).",
    "Špičkový odber {peak} kW je len {pct} % zazmluvnenej MRK {mrk} kW. Odberné miesto má rezervu znížiť rezervovanú kapacitu (RK, min. 50 % MRK) a šetriť na pevnej zložke distribúcie":
        "Die Spitzenlast von {peak} kW beträgt nur {pct} % der vertraglich vereinbarten maximal reservierten Leistung (MRK) von {mrk} kW. "
        "Die Abnahmestelle hat Spielraum, die reservierte Leistung (RK, min. 50 % der MRK) zu senken und beim festen Netzentgeltanteil zu sparen",
    "; batéria toto zníženie zabezpečuje proti špičkám (modelovaná úspora {save} €/r).":
        "; die Batterie sichert diese Senkung gegen Lastspitzen ab (modellierte Einsparung {save} €/Jahr).",
    " (úspora nezávislá od FVE/batérie — odporúčame preveriť s prevádzkovateľom distribučnej sústavy).":
        " (Einsparung unabhängig von PV-Anlage/Batterie — wir empfehlen, dies mit dem regionalen Verteilnetzbetreiber abzustimmen).",
    "Optimalizácia rezervovanej kapacity (MRK/RK)": "Optimierung der reservierten Leistung (MRK/RK)",
    "Peak shaving cez batériu": "Peak Shaving mit der Batterie",
    "Batéria {bess} kWh znižuje 15-min špičky zo siete (modelovaná úspora {save} €/r) — podklad pre zníženie rezervovanej kapacity (RK {rk} kW).":
        "Die Batterie mit {bess} kWh senkt 15-Minuten-Lastspitzen aus dem Netz (modellierte Einsparung {save} €/Jahr) — Grundlage für eine Senkung der reservierten Leistung (RK {rk} kW).",
    "ESG profil a CSRD reporting": "ESG-Profil und CSRD-Berichterstattung",
    "Ročná úspora ~{co2} t CO₂ (pri SK emisnej intenzite ~0,25 kg/kWh) — doložiteľný podklad pre EU Taxonómiu a CSRD disclosures.":
        "Jährliche Einsparung von ~{co2} t CO₂ (bei der slowakischen Emissionsintensität von ~0,25 kg/kWh) — belastbare Grundlage für die EU-Taxonomie und die CSRD-Berichterstattung.",
    "Reziduálna hodnota po 20 rokoch": "Restwert nach 20 Jahren",
    "Ekonomika neráta s poklesom výkonu FVE; konzervatívne 10 % CAPEX = {res} € reziduálnej hodnoty.":
        "Die Wirtschaftlichkeitsrechnung berücksichtigt keine Leistungsminderung der PV-Anlage; konservativ 10 % des CAPEX = {res} € Restwert.",
    # --- 2026-10-25: záporná arbitráž ako náklad, distribútor OM ---
    'pri spotovej tarife spread medzi lacnými a drahými hodinami nepokryl straty batérie pri nabíjaní a vybíjaní': 'beim Spot-Tarif deckte die Preisspanne zwischen günstigen und teuren Stunden die Lade- und Entladeverluste der Batterie nicht',
    'pri fixnej tarife nemá nabíjanie zo siete cenový rozdiel, takže straty batérie pri nabíjaní a vybíjaní sú čistý náklad': 'beim Festpreistarif bringt das Laden aus dem Netz keinen Preisunterschied, daher sind die Lade- und Entladeverluste der Batterie reine Kosten',
    '; náklad je už odpočítaný v ročnej úspore': '; diese Kosten sind in der jährlichen Einsparung bereits abgezogen',
    'Batéria — nabíjanie zo siete (náklad)': 'Batterie — Laden aus dem Netz (Kosten)',
    '{mwh} MWh zo siete, straty pri nabíjaní a vybíjaní': '{mwh} MWh aus dem Netz, Lade- und Entladeverluste',
    'tarif {dist}': '{dist}-Tarif (regionaler Verteilnetzbetreiber)',
    'tarif distribútora': 'Tarif des Verteilnetzbetreibers',
}
