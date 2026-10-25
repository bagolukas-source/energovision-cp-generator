"""Report financovania (financovanie_report/generator.py + templates/report.html).
Texty variantov (label, items, pros, cons, description, ppa_note, event_label) a ai_text posiela CRM
už v cieľovom jazyku — tu sú len texty šablóny a generátora."""
EN = {
    # titulná strana
    "ANALÝZA FINANCOVANIA": "FINANCING ANALYSIS",
    "Technicko-ekonomická analýza · Modely financovania": "Techno-economic analysis · Financing models",
    "Financovanie fotovoltickej elektrárne": "Financing of the photovoltaic (PV) plant",
    "Moderné energetické riešenia, ktoré hľadáte.": "The modern energy solutions you are looking for.",
    "Výkon FVE": "PV plant capacity",
    "Investícia (CAPEX)": "Investment (CAPEX)",
    "Batéria (BESS)": "Battery storage (BESS)",
    "Výroba / rok": "Production / year",
    "Cena elektriny": "Electricity price",
    "Samospotreba": "Self-consumption",
    "Dátum": "Date",
    "Fotovoltická elektráreň": "Photovoltaic plant",
    # porovnanie modelov
    "Porovnanie modelov financovania": "Comparison of financing models",
    "Scenár: {s} · CAPEX: {c}": "Scenario: {s} · CAPEX: {c}",
    "Realistický": "Realistic",
    "realistický": "realistic",
    "Tento report porovnáva spôsoby financovania rovnakej zostavy":
        "This report compares ways of financing the same system",
    "{kwp} kWp FVE": "{kwp} kWp PV plant",
    "batéria {kwh} kWh": "battery {kwh} kWh",
    "investícia {c}": "investment {c}",
    "— vlastná kúpa, SIH zelený úver, lízing, PPA a dotácia. Pri každej možnosti vidíte počiatočný výdaj, "
    "návratnosť, kedy bude FVE 100 % vaša a celkový 25-ročný prínos. Čísla vychádzajú z reálnej spotreby a cien "
    "odberného miesta (scenár {s}). Na konci nájdete súhrnnú tabuľku a vyhodnotenie.":
        "— purchase from own funds, SIH green loan, leasing, PPA and subsidy. For each option you see the initial "
        "outlay, the payback, when the PV plant will be 100 % yours and the total 25-year benefit. The figures are "
        "based on the actual consumption and prices of the supply point ({s} scenario). At the end you will find "
        "a summary table and an evaluation.",
    "Odporúčaný model: {m}": "Recommended model: {m}",
    "— bez jediného eura z vrecka": "— without a single euro out of pocket",
    "Elektráreň postaví financujúci partner a vy ju splácate z úspory, ktorú sama vyrobí — od prvého mesiaca "
    "ste v pluse.":
        "The plant is built by a financing partner and you repay it from the savings it generates itself — you "
        "are in profit from the first month.",
    "Vlastníctvo máte od začiatku a váš kapitál medzitým ostáva pracovať vo firme.":
        "You own the plant from the start, and your capital keeps working in the company in the meantime.",
    "Čistá súčasná hodnota dosahuje <strong>{npv}</strong>.": "The net present value reaches <strong>{npv}</strong>.",
    "Najvyššiu NPV ({npv}) síce dosahuje {m}": "The highest NPV ({npv}) is achieved by {m}",
    ", no viaže na dlhoročnú PPA zmluvu bez vlastníctva":
        ", but it ties you to a long-term PPA contract without ownership",
    ", no vyžaduje plný vstupný kapitál naraz": ", but it requires the full upfront capital at once",
    ", no s 2-ročným čakaním a administratívou": ", but with a 2-year wait and paperwork",
    " — preto odporúčame vyváženejší model vyššie.": " — which is why we recommend the more balanced model above.",
    "Ktorá voľba je pre vás?": "Which option suits you?",
    "Máte voľný kapitál": "You have spare capital",
    "Vlastná kúpa": "Purchase from own funds",
    "Najvyšší celkový zisk a vlastníctvo od prvého dňa. Žiadny dlh, žiadne úroky.":
        "Highest total profit and ownership from day one. No debt, no interest.",
    "Chcete si nechať hotovosť": "You want to keep your cash",
    "SIH zelený úver": "SIH green loan",
    "0 € z vrecka, vlastníctvo od začiatku, splátka sa platí z úspor. Kapitál ostáva vo firme.":
        "0 € out of pocket, ownership from the start, the instalment is paid from the savings. Your capital "
        "stays in the company.",
    "Nechcete vklad ani záväzok": "No down payment, no commitment",
    "Elektráreň postaví a vlastní ENERY, vy platíte len nižšiu cenu za prúd. Po zmluve prejde zadarmo na vás.":
        "ENERY builds and owns the plant; you only pay a lower electricity price. After the contract it passes "
        "to you free of charge.",
    "Fixná cena energie 20+ rokov": "Fixed energy price for 20+ years",
    "— hedge proti rastu cien.": "— a hedge against rising prices.",
    "Daňová výhoda": "Tax advantage",
    "— odpis aj splátky znižujú základ dane.": "— depreciation and instalments reduce the tax base.",
    "— doložiteľné zníženie CO₂.": "— verifiable CO₂ reduction.",
    "Jeden partner": "One partner",
    "— projekt, realizácia, revízie aj servis.": "— design, construction, inspections and service.",
    "Model": "Model",
    "NPV (20 r)": "NPV (20 yrs)",
    "Kumulatív 20r": "Cumulative 20 yrs",
    "Počiatočný výdaj": "Initial outlay",
    "{n} r": "{n} yrs",
    "okamžite": "immediately",
    # graf + insights
    "Kumulatívny tok a čo z neho vyplýva": "Cumulative cash flow and what it shows",
    "20-ročný horizont · Scenár: {s}": "20-year horizon · Scenario: {s}",
    "Kumulatívny cashflow za 20 rokov (€)": "Cumulative cash flow over 20 years (€)",
    "bod zvratu (0 €)": "break-even (0 €)",
    "r{n}": "Y{n}",
    "Čo z grafu vyplýva": "What the chart shows",
    "Najvyššia hodnota": "Highest value",
    "NPV 20 r": "NPV 20 yrs",
    "Najrýchlejšie v pluse": "Fastest in profit",
    "rok {n}": "year {n}",
    "od začiatku": "from the start",
    "Najnižší vstup": "Lowest upfront cost",
    "Naše odporúčanie": "Our recommendation",
    "Bod zvratu — odkedy je model v pluse": "Break-even — when the model turns positive",
    "nad 20 r": "over 20 yrs",
    "Ako čítať graf:": "How to read the chart:",
    "každá krivka je kumulatívny čistý tok daného modelu. Kde pretne prerušovanú čiaru „bod zvratu (0 €)\", "
    "odvtedy vám elektráreň zarába. Čím vyššie krivka končí v roku 20, tým väčší je celkový prínos. Modely bez "
    "vstupného vkladu (PPA, SIH) sú v pluse prakticky od začiatku; vlastná kúpa nabieha pomalšie, no končí "
    "najvyššie.":
        "each curve is the cumulative net cash flow of the given model. Where it crosses the dashed "
        "“break-even (0 €)” line, the plant earns money for you from then on. The higher the curve ends in year "
        "20, the greater the total benefit. Models without a down payment (PPA, SIH) are in profit practically "
        "from the start; purchase from own funds ramps up more slowly but ends highest.",
    "elektráreň vyrobí za 20 rokov približne {mwh} MWh čistej energie — doložiteľné zníženie uhlíkovej stopy a "
    "nezávislosť od rastu cien elektriny.":
        "over 20 years the plant will produce approximately {mwh} MWh of clean energy — a verifiable reduction "
        "of the carbon footprint and independence from rising electricity prices.",
    # detail variantu
    "Model {i} z {n} · Detailná analýza · {s} scenár": "Model {i} of {n} · Detailed analysis · {s} scenario",
    "{kwh} kWh batéria": "{kwh} kWh battery",
    "Čistá súčasná hodnota (NPV)": "Net present value (NPV)",
    "diskontovaný prínos 20r": "discounted benefit over 20 yrs",
    "Celkový zisk 20 rokov": "Total profit over 20 years",
    "kumulatívny cashflow": "cumulative cash flow",
    "Prevod FVE (zadarmo)": "PV plant transfer (free of charge)",
    "Bod zvratu": "Break-even",
    "po 10 rokoch": "after 10 years",
    "po 15 rokoch": "after 15 years",
    "bezplatný prevod po zmluve": "free transfer after the contract",
    "investícia splatená": "investment repaid",
    "Vnútorná výnosnosť (IRR)": "Internal rate of return (IRR)",
    "N/A — nulová vstupná investícia": "N/A — zero upfront investment",
    "ročná výnosnosť projektu": "annual project return",
    "Rok po roku — cashflow ({s} scenár)": "Year by year — cash flow ({s} scenario)",
    "Rok": "Year",
    "Prínos FVE": "PV plant benefit",
    "Splátka": "Instalment",
    "Čistý CF": "Net CF",
    "Kumulatív": "Cumulative",
    "1 ½r": "1 ½ yr",
    "Spolu": "Total",
    "Kumulatívny vývoj (20 rokov)": "Cumulative development (20 years)",
    "Parametre & podmienky": "Parameters & conditions",
    "Výhody": "Advantages",
    "Nevýhody": "Disadvantages",
    # záver
    "Záverečné porovnanie — kumulatívny tok (20 rokov)": "Final comparison — cumulative cash flow (20 years)",
    "Kumulatív v míľnikových rokoch · Scenár: {s}": "Cumulative in milestone years · Scenario: {s}",
    "Rok / ukazovateľ": "Year / indicator",
    "Vklad dnes": "Payment today",
    "1. rok": "Year 1",
    "Rok {n}": "Year {n}",
    "NPV 20r": "NPV 20 yrs",
    "Kumulatívny čistý tok = súčet ročných prínosov mínus splátky, vrátane vstupného vkladu. Roky 10/15/20/25 sú "
    "zvýraznené. Zelená = v pluse, červená = ešte v mínuse.":
        "Cumulative net cash flow = sum of annual benefits minus instalments, including the initial payment. "
        "Years 10/15/20/25 are highlighted. Green = in profit, red = still negative.",
    "Záver — odporúčanie Energovision": "Conclusion — Energovision recommendation",
    "Najvyššiu čistú súčasnú hodnotu za 20 rokov dosahuje {label} ({npv}) — bez vstupného kapitálu, s kladným "
    "tokom od začiatku.":
        "The highest 20-year net present value is achieved by {label} ({npv}) — without upfront capital, with a "
        "positive cash flow from the start.",
    "Najvyššiu čistú súčasnú hodnotu za 20 rokov dosahuje {label} ({npv}); vyžaduje však jednorazový vstupný "
    "kapitál {capital}.":
        "The highest 20-year net present value is achieved by {label} ({npv}); however, it requires a one-off "
        "upfront capital of {capital}.",
    "Najnižší vstupný náklad ({upfront}) a kladný tok od začiatku majú modely bez vstupného vkladu (PPA, SIH) — "
    "pozitívny cashflow od prvého mesiaca.":
        "The lowest upfront cost ({upfront}) and a positive flow from the start come with the models without a "
        "down payment (PPA, SIH) — positive cash flow from the first month.",
    "Ako vyváženú voľbu odporúčame {label}: nulový vstup, vlastníctvo elektrárne od prvého dňa, fixná sadzba a "
    "kladný mesačný tok už počas splácania. Vlastná kúpa dáva najvyšší absolútny zisk za 20 rokov, ak má firma "
    "voľný kapitál.":
        "As the balanced choice we recommend {label}: zero upfront payment, ownership of the plant from day one, "
        "a fixed rate and a positive monthly flow already during repayment. Purchase from own funds gives the "
        "highest absolute profit over 20 years if the company has spare capital.",
    "IČO": "Company ID",
    "Správa vygenerovaná {d} · Hodnoty sú orientačné a slúžia pre rozhodovaciu podporu.":
        "Report generated on {d} · The values are indicative and intended to support decision-making.",
}

DE = {
    # titulná strana
    "ANALÝZA FINANCOVANIA": "FINANZIERUNGSANALYSE",
    "Technicko-ekonomická analýza · Modely financovania": "Technisch-wirtschaftliche Analyse · Finanzierungsmodelle",
    "Financovanie fotovoltickej elektrárne": "Finanzierung der Photovoltaikanlage (PV-Anlage)",
    "Moderné energetické riešenia, ktoré hľadáte.": "Moderne Energielösungen, die Sie suchen.",
    "Výkon FVE": "Leistung der PV-Anlage",
    "Investícia (CAPEX)": "Investition (CAPEX)",
    "Batéria (BESS)": "Batteriespeicher (BESS)",
    "Výroba / rok": "Erzeugung / Jahr",
    "Cena elektriny": "Strompreis",
    "Samospotreba": "Eigenverbrauch",
    "Dátum": "Datum",
    "Fotovoltická elektráreň": "Photovoltaikanlage",
    # porovnanie modelov
    "Porovnanie modelov financovania": "Vergleich der Finanzierungsmodelle",
    "Scenár: {s} · CAPEX: {c}": "Szenario: {s} · CAPEX: {c}",
    "Realistický": "Realistisch",
    "realistický": "realistisch",
    "Tento report porovnáva spôsoby financovania rovnakej zostavy":
        "Dieser Bericht vergleicht die Finanzierungsmöglichkeiten derselben Anlage",
    "{kwp} kWp FVE": "{kwp} kWp PV-Anlage",
    "batéria {kwh} kWh": "Batterie {kwh} kWh",
    "investícia {c}": "Investition {c}",
    "— vlastná kúpa, SIH zelený úver, lízing, PPA a dotácia. Pri každej možnosti vidíte počiatočný výdaj, "
    "návratnosť, kedy bude FVE 100 % vaša a celkový 25-ročný prínos. Čísla vychádzajú z reálnej spotreby a cien "
    "odberného miesta (scenár {s}). Na konci nájdete súhrnnú tabuľku a vyhodnotenie.":
        "— Kauf aus Eigenmitteln, SIH-Grünkredit, Leasing, PPA und Förderung. Für jede Option sehen Sie die "
        "Anfangsausgabe, die Amortisation, ab wann die PV-Anlage zu 100 % Ihnen gehört, und den gesamten Nutzen "
        "über 25 Jahre. Die Zahlen beruhen auf dem tatsächlichen Verbrauch und den Preisen der Abnahmestelle "
        "(Szenario {s}). Am Ende finden Sie eine Übersichtstabelle und eine Auswertung.",
    "Odporúčaný model: {m}": "Empfohlenes Modell: {m}",
    "— bez jediného eura z vrecka": "— ohne einen einzigen Euro aus eigener Tasche",
    "Elektráreň postaví financujúci partner a vy ju splácate z úspory, ktorú sama vyrobí — od prvého mesiaca "
    "ste v pluse.":
        "Die Anlage wird von einem Finanzierungspartner gebaut, und Sie zahlen sie aus den Einsparungen zurück, "
        "die sie selbst erzeugt — ab dem ersten Monat sind Sie im Plus.",
    "Vlastníctvo máte od začiatku a váš kapitál medzitým ostáva pracovať vo firme.":
        "Das Eigentum liegt von Anfang an bei Ihnen, und Ihr Kapital arbeitet in der Zwischenzeit weiter im "
        "Unternehmen.",
    "Čistá súčasná hodnota dosahuje <strong>{npv}</strong>.": "Der Kapitalwert beträgt <strong>{npv}</strong>.",
    "Najvyššiu NPV ({npv}) síce dosahuje {m}": "Den höchsten Kapitalwert (NPV, {npv}) erreicht zwar {m}",
    ", no viaže na dlhoročnú PPA zmluvu bez vlastníctva":
        ", bindet Sie jedoch an einen langfristigen PPA-Vertrag ohne Eigentum",
    ", no vyžaduje plný vstupný kapitál naraz": ", erfordert jedoch das volle Eigenkapital auf einmal",
    ", no s 2-ročným čakaním a administratívou": ", jedoch mit 2 Jahren Wartezeit und Verwaltungsaufwand",
    " — preto odporúčame vyváženejší model vyššie.": " — deshalb empfehlen wir das ausgewogenere Modell oben.",
    "Ktorá voľba je pre vás?": "Welche Option passt zu Ihnen?",
    "Máte voľný kapitál": "Sie haben freies Kapital",
    "Vlastná kúpa": "Kauf aus Eigenmitteln",
    "Najvyšší celkový zisk a vlastníctvo od prvého dňa. Žiadny dlh, žiadne úroky.":
        "Höchster Gesamtgewinn und Eigentum ab dem ersten Tag. Keine Schulden, keine Zinsen.",
    "Chcete si nechať hotovosť": "Sie möchten Ihre Liquidität behalten",
    "SIH zelený úver": "SIH-Grünkredit",
    "0 € z vrecka, vlastníctvo od začiatku, splátka sa platí z úspor. Kapitál ostáva vo firme.":
        "0 € aus eigener Tasche, Eigentum von Anfang an, die Rate wird aus den Einsparungen bezahlt. Das Kapital "
        "bleibt im Unternehmen.",
    "Nechcete vklad ani záväzok": "Keine Anzahlung, keine Verpflichtung",
    "Elektráreň postaví a vlastní ENERY, vy platíte len nižšiu cenu za prúd. Po zmluve prejde zadarmo na vás.":
        "ENERY baut und besitzt die Anlage, Sie zahlen nur einen niedrigeren Strompreis. Nach Vertragsende geht "
        "sie kostenlos auf Sie über.",
    "Fixná cena energie 20+ rokov": "Fester Energiepreis für 20+ Jahre",
    "— hedge proti rastu cien.": "— Absicherung gegen steigende Preise.",
    "Daňová výhoda": "Steuervorteil",
    "— odpis aj splátky znižujú základ dane.": "— Abschreibung und Raten mindern die Steuerbemessungsgrundlage.",
    "— doložiteľné zníženie CO₂.": "— nachweisbare CO₂-Reduktion.",
    "Jeden partner": "Ein Partner",
    "— projekt, realizácia, revízie aj servis.": "— Planung, Umsetzung, Revisionen und Service.",
    "Model": "Modell",
    "NPV (20 r)": "NPV (20 J.)",
    "Kumulatív 20r": "Kumuliert 20 J.",
    "Počiatočný výdaj": "Anfangsausgabe",
    "{n} r": "{n} J.",
    "okamžite": "sofort",
    # graf + insights
    "Kumulatívny tok a čo z neho vyplýva": "Kumulierter Cashflow und was er zeigt",
    "20-ročný horizont · Scenár: {s}": "20-Jahres-Horizont · Szenario: {s}",
    "Kumulatívny cashflow za 20 rokov (€)": "Kumulierter Cashflow über 20 Jahre (€)",
    "bod zvratu (0 €)": "Gewinnschwelle (0 €)",
    "r{n}": "J{n}",
    "Čo z grafu vyplýva": "Was das Diagramm zeigt",
    "Najvyššia hodnota": "Höchster Wert",
    "NPV 20 r": "NPV 20 J.",
    "Najrýchlejšie v pluse": "Am schnellsten im Plus",
    "rok {n}": "Jahr {n}",
    "od začiatku": "von Anfang an",
    "Najnižší vstup": "Geringste Anfangsausgabe",
    "Naše odporúčanie": "Unsere Empfehlung",
    "Bod zvratu — odkedy je model v pluse": "Gewinnschwelle — ab wann das Modell im Plus ist",
    "nad 20 r": "über 20 J.",
    "Ako čítať graf:": "So lesen Sie das Diagramm:",
    "každá krivka je kumulatívny čistý tok daného modelu. Kde pretne prerušovanú čiaru „bod zvratu (0 €)\", "
    "odvtedy vám elektráreň zarába. Čím vyššie krivka končí v roku 20, tým väčší je celkový prínos. Modely bez "
    "vstupného vkladu (PPA, SIH) sú v pluse prakticky od začiatku; vlastná kúpa nabieha pomalšie, no končí "
    "najvyššie.":
        "Jede Kurve zeigt den kumulierten Netto-Cashflow des jeweiligen Modells. Ab dem Punkt, an dem sie die "
        "gestrichelte Linie „Gewinnschwelle (0 €)“ schneidet, verdient die Anlage für Sie. Je höher die Kurve im "
        "Jahr 20 endet, desto größer ist der Gesamtnutzen. Modelle ohne Anzahlung (PPA, SIH) sind praktisch von "
        "Anfang an im Plus; der Kauf aus Eigenmitteln läuft langsamer an, endet aber am höchsten.",
    "elektráreň vyrobí za 20 rokov približne {mwh} MWh čistej energie — doložiteľné zníženie uhlíkovej stopy a "
    "nezávislosť od rastu cien elektriny.":
        "Die Anlage erzeugt in 20 Jahren rund {mwh} MWh saubere Energie — nachweisbare Verringerung des "
        "CO₂-Fußabdrucks und Unabhängigkeit von steigenden Strompreisen.",
    # detail variantu
    "Model {i} z {n} · Detailná analýza · {s} scenár": "Modell {i} von {n} · Detailanalyse · Szenario {s}",
    "{kwh} kWh batéria": "{kwh} kWh Batterie",
    "Čistá súčasná hodnota (NPV)": "Kapitalwert (NPV)",
    "diskontovaný prínos 20r": "abgezinster Nutzen über 20 J.",
    "Celkový zisk 20 rokov": "Gesamtgewinn über 20 Jahre",
    "kumulatívny cashflow": "kumulierter Cashflow",
    "Prevod FVE (zadarmo)": "Übertragung der PV-Anlage (kostenlos)",
    "Bod zvratu": "Gewinnschwelle",
    "po 10 rokoch": "nach 10 Jahren",
    "po 15 rokoch": "nach 15 Jahren",
    "bezplatný prevod po zmluve": "kostenlose Übertragung nach Vertragsende",
    "investícia splatená": "Investition zurückgezahlt",
    "Vnútorná výnosnosť (IRR)": "Interner Zinsfuß (IRR)",
    "N/A — nulová vstupná investícia": "N/A — keine Anfangsinvestition",
    "ročná výnosnosť projektu": "jährliche Projektrendite",
    "Rok po roku — cashflow ({s} scenár)": "Jahr für Jahr — Cashflow (Szenario {s})",
    "Rok": "Jahr",
    "Prínos FVE": "Nutzen PV-Anlage",
    "Splátka": "Rate",
    "Čistý CF": "Netto-CF",
    "Kumulatív": "Kumuliert",
    "1 ½r": "1 ½ J.",
    "Spolu": "Summe",
    "Kumulatívny vývoj (20 rokov)": "Kumulierte Entwicklung (20 Jahre)",
    "Parametre & podmienky": "Parameter & Bedingungen",
    "Výhody": "Vorteile",
    "Nevýhody": "Nachteile",
    # záver
    "Záverečné porovnanie — kumulatívny tok (20 rokov)": "Abschließender Vergleich — kumulierter Cashflow (20 Jahre)",
    "Kumulatív v míľnikových rokoch · Scenár: {s}": "Kumuliert in Meilensteinjahren · Szenario: {s}",
    "Rok / ukazovateľ": "Jahr / Kennzahl",
    "Vklad dnes": "Einlage heute",
    "1. rok": "1. Jahr",
    "Rok {n}": "Jahr {n}",
    "NPV 20r": "NPV 20 J.",
    "Kumulatívny čistý tok = súčet ročných prínosov mínus splátky, vrátane vstupného vkladu. Roky 10/15/20/25 sú "
    "zvýraznené. Zelená = v pluse, červená = ešte v mínuse.":
        "Kumulierter Netto-Cashflow = Summe der jährlichen Nutzen abzüglich Raten, einschließlich der Einlage. "
        "Die Jahre 10/15/20/25 sind hervorgehoben. Grün = im Plus, Rot = noch im Minus.",
    "Záver — odporúčanie Energovision": "Fazit — Empfehlung von Energovision",
    "Najvyššiu čistú súčasnú hodnotu za 20 rokov dosahuje {label} ({npv}) — bez vstupného kapitálu, s kladným "
    "tokom od začiatku.":
        "Den höchsten Kapitalwert über 20 Jahre erreicht {label} ({npv}) — ohne Eigenkapitaleinsatz, mit "
        "positivem Cashflow von Anfang an.",
    "Najvyššiu čistú súčasnú hodnotu za 20 rokov dosahuje {label} ({npv}); vyžaduje však jednorazový vstupný "
    "kapitál {capital}.":
        "Den höchsten Kapitalwert über 20 Jahre erreicht {label} ({npv}); dies erfordert jedoch einen einmaligen "
        "Kapitaleinsatz von {capital}.",
    "Najnižší vstupný náklad ({upfront}) a kladný tok od začiatku majú modely bez vstupného vkladu (PPA, SIH) — "
    "pozitívny cashflow od prvého mesiaca.":
        "Die geringsten Anfangskosten ({upfront}) und einen positiven Cashflow von Anfang an bieten die Modelle "
        "ohne Anzahlung (PPA, SIH) — positiver Cashflow ab dem ersten Monat.",
    "Ako vyváženú voľbu odporúčame {label}: nulový vstup, vlastníctvo elektrárne od prvého dňa, fixná sadzba a "
    "kladný mesačný tok už počas splácania. Vlastná kúpa dáva najvyšší absolútny zisk za 20 rokov, ak má firma "
    "voľný kapitál.":
        "Als ausgewogene Wahl empfehlen wir {label}: keine Anfangsausgabe, Eigentum an der Anlage ab dem ersten "
        "Tag, ein fester Satz und ein positiver monatlicher Cashflow bereits während der Rückzahlung. Der Kauf "
        "aus Eigenmitteln bringt den höchsten absoluten Gewinn über 20 Jahre, wenn das Unternehmen über freies "
        "Kapital verfügt.",
    "IČO": "Firmen-ID",
    "Správa vygenerovaná {d} · Hodnoty sú orientačné a slúžia pre rozhodovaciu podporu.":
        "Bericht erstellt am {d} · Die Werte sind Richtwerte und dienen der Entscheidungsunterstützung.",
}
