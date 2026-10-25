"""Porovnávací súhrn ponúk (posudok_orkestra/templates/porovnanie.html, grafy charts.py, render_porovnanie, fallback).
„Návratnosť" je v common.py."""
EN = {
    # hlavička, meta
    "Porovnávací súhrn ponúk": "Offer comparison summary",
    "Porovnanie ponúk": "Offer comparison",
    "Klient": "Client",
    "Pripravené pre": "Prepared for",
    "Vystavené": "Issued",
    "Pripravil": "Prepared by",
    "Identifikátor": "Reference",
    "{n} porovnávaných ponúk · ročná spotreba {mwh} MWh · špička {peak} kW · MRK {mrk} kW":
        "{n} offers compared · annual consumption {mwh} MWh · peak demand {peak} kW · "
        "maximum reserved capacity (MRK) {mrk} kW",
    "<strong>K veci.</strong> Z {n} predložených ponúk je <strong>1 ponuka</strong> vo všetkých ukazovateľoch "
    "(CAPEX, NPV, IRR, návratnosť) horšia než iná ponuka v tomto porovnaní — reálna voľba je medzi zvyšnými {viable}.":
        "<strong>Bottom line.</strong> Of the {n} offers submitted, <strong>1 offer</strong> is worse on every metric "
        "(CAPEX, NPV, IRR, payback) than another offer in this comparison — the real choice is between the "
        "remaining {viable}.",
    "<strong>K veci.</strong> Z {n} predložených ponúk sú <strong>{k} ponuky</strong> vo všetkých ukazovateľoch "
    "(CAPEX, NPV, IRR, návratnosť) horšia než iná ponuka v tomto porovnaní — reálna voľba je medzi zvyšnými {viable}.":
        "<strong>Bottom line.</strong> Of the {n} offers submitted, <strong>{k} offers</strong> are worse on every "
        "metric (CAPEX, NPV, IRR, payback) than another offer in this comparison — the real choice is between the "
        "remaining {viable}.",
    # voľby, tabuľka
    "Zhrnutie situácie": "Situation summary",
    "Odporúčaná voľba": "Recommended choice",
    "Dve rozumné voľby": "Two sensible choices",
    "Najvyšší zisk (NPV)": "Highest profit (NPV)",
    "aktuálne vybrané": "currently selected",
    "Najrýchlejšia návratnosť": "Fastest payback",
    "Všetky porovnávané ponuky ({n})": "All offers compared ({n})",
    "Variant": "Option",
    "FVE (DC)": "PV plant (DC)",
    "NPV 20r": "NPV 20 yrs",
    "Voľba": "Choice",
    "VYBRANÉ": "SELECTED",
    'Horšie vo všetkom než „{name}"': "Worse on every metric than “{name}”",
    "nevráti sa": "no payback",
    # grafy
    "CAPEX vs. NPV — vstup oproti zisku": "CAPEX vs. NPV — investment versus profit",
    "Modrá = vstupná investícia, zelená = čistý zisk za 20 rokov (súčasná hodnota). Šedá = dominovaná ponuka.":
        "Blue = upfront investment, green = net profit over 20 years (present value). Grey = dominated offer.",
    "Návratnosť — od najrýchlejšej po najpomalšiu": "Payback — from fastest to slowest",
    "Zelená = odporúčaná ponuka (najvyššie NPV), šedá = dominovaná ponuka.":
        "Green = recommended offer (highest NPV), grey = dominated offer.",
    "CAPEX (vstup)": "CAPEX (investment)",
    "NPV 20 r (zisk)": "NPV 20 yrs (profit)",
    "Návratnosť (roky) — kratšia je lepšia": "Payback (years) — shorter is better",
    # hodnotenie ponúk
    "Hodnotenie jednotlivých ponúk": "Assessment of each offer",
    "dominovaná ponuka": "dominated offer",
    "Výhody": "Advantages",
    "Nevýhody": "Disadvantages",
    "Najvyšší čistý zisk za 20 rokov zo všetkých porovnávaných ponúk ({npv}).":
        "Highest net profit over 20 years of all offers compared ({npv}).",
    "Najrýchlejšia návratnosť v porovnaní ({pb} roka).": "Fastest payback in this comparison ({pb} years).",
    "Najnižšia vstupná investícia ({capex}).": "Lowest upfront investment ({capex}).",
    "Batéria {kwh} kWh chráni pred prekročením MRK {mrk} kW a umožňuje arbitráž na spotovom trhu.":
        "The {kwh} kWh battery protects against exceeding the MRK of {mrk} kW and enables arbitrage on the "
        "spot market.",
    "Vyššia vstupná investícia oproti variantu bez batérie a dodatočná technológia na údržbu.":
        "Higher upfront investment than the option without a battery, plus additional equipment to maintain.",
    "Jednoduchšie riešenie bez batériového úložiska — menej technológie, nižší CAPEX.":
        "Simpler solution without battery storage — less equipment, lower CAPEX.",
    "Bez ochrany pred prekročením MRK {mrk} kW a bez výnosu z arbitráže.":
        "No protection against exceeding the MRK of {mrk} kW and no arbitrage revenue.",
    "Vo všetkých ukazovateľoch horšia než „{name}“ — nemá zmysel ju vybrať.":
        "Worse than “{name}” on every metric — there is no reason to choose it.",
    "Najvyššie ročné zhodnotenie investície (IRR {irr} %).": "Highest annual return on investment (IRR {irr} %).",
    # odporúčanie
    "Odporúčanie": "Recommendation",
    "Odporúčanie Energovision": "Energovision recommendation",
    "Ponuka „{name}“ má súčasne najvyšší čistý zisk za 20 rokov ({npv}) aj najrýchlejšiu návratnosť ({pb}) — "
    "jednoznačná voľba v tomto porovnaní.":
        "The offer “{name}” delivers both the highest net profit over 20 years ({npv}) and the fastest payback "
        "({pb}) — the clear choice in this comparison.",
    "Ponuka „{name}“ prinesie za 20 rokov najvyšší čistý zisk ({npv} pri návratnosti {pb}). Ak preferujete nižšiu "
    "vstupnú investíciu a najrýchlejší návrat peňazí, zvoľte „{alt}“ ({alt_capex}, návratnosť {alt_pb}).":
        "The offer “{name}” delivers the highest net profit over 20 years ({npv} with a payback of {pb}). If you "
        "prefer a lower upfront investment and the fastest return of your money, choose “{alt}” ({alt_capex}, "
        "payback {alt_pb}).",
    # technické fakty
    "Technické fakty o odbernom mieste": "Technical facts about the supply point",
    "Ročná spotreba elektriny": "Annual electricity consumption",
    "Špička odberu (15-min maximum)": "Peak demand (15-min maximum)",
    "Maximálna rezervovaná kapacita (MRK)": "Maximum reserved capacity (MRK)",
    "<strong>⚠ Špička odberu prekračuje MRK</strong> — riziko pokút od distribútora; batéria ho vie znížiť "
    "(peak shaving)":
        "<strong>⚠ Peak demand exceeds the MRK</strong> — risk of penalties from the distribution system "
        "operator; a battery can reduce it (peak shaving)",
    "Cena elektriny — nákup": "Electricity price — purchase",
    "Cena elektriny — výkup prebytkov": "Electricity price — buy-back of surplus",
    "Ročná výroba FVE ({name})": "Annual PV plant generation ({name})",
    "Samospotreba vyrobenej elektriny": "Self-consumption of generated electricity",
    "Ročný export prebytkov do siete": "Annual export of surplus to the grid",
    "Čo si overiť pred rozhodnutím": "What to verify before deciding",
    # ako čítať
    "Ako čítať toto porovnanie": "How to read this comparison",
    "<strong>CAPEX</strong> — celková vstupná investícia bez DPH.":
        "<strong>CAPEX</strong> — total upfront investment excl. VAT.",
    "<strong>NPV (čistá súčasná hodnota)</strong> — čistý zisk za {years} rokov prevádzky, po odpočítaní "
    "investície, v dnešnej hodnote peňazí. Vyššie číslo = viac peňazí za 20 rokov.":
        "<strong>NPV (net present value)</strong> — net profit over {years} years of operation after deducting "
        "the investment, in today’s money. Higher figure = more money over 20 years.",
    "<strong>IRR (vnútorná miera výnosu)</strong> — ročné zhodnotenie investície v percentách; porovnaj s bankovým "
    "úrokom alebo alternatívnou investíciou.":
        "<strong>IRR (internal rate of return)</strong> — annual return on the investment in percent; compare it "
        "with bank interest or an alternative investment.",
    "<strong>Návratnosť</strong> — počet rokov, za ktoré sa investícia vráti z úspor na energii.":
        "<strong>Payback</strong> — number of years in which the investment is recovered from energy savings.",
    "<strong>BESS (batériové úložisko)</strong> — ochrana pred pokutou za prekročenie MRK a výnos z obchodovania "
    "so spotovou cenou elektriny (arbitráž).":
        "<strong>BESS (battery storage)</strong> — protection against penalties for exceeding the MRK and revenue "
        "from trading on the electricity spot price (arbitrage).",
    "<strong>Dominovaná ponuka</strong> — existuje iná porovnávaná ponuka, ktorá je súčasne lacnejšia aj "
    "výnosnejšia vo všetkých ukazovateľoch; nemá zmysel ju vybrať za žiadnych preferencií.":
        "<strong>Dominated offer</strong> — another offer in this comparison is both cheaper and more profitable "
        "on every metric; there is no preference under which choosing it makes sense.",
    # rozhodnutie, kontakt
    "Podpis zákazníka · vybraný variant": "Customer signature · selected option",
    "Dátum": "Date",
    "Kontakt": "Contact",
    "IČO": "Company ID",
    "Porovnávací súhrn nahrádza samostatné technické posudky priložené k jednotlivým ponukám. Podrobný technický "
    "posudok k vybranému variantu je na vyžiadanie k dispozícii v plnom znení. Generated {date}.":
        "This comparison summary replaces the separate techno-economic assessments attached to the individual "
        "offers. The detailed techno-economic assessment of the selected option is available in full on request. "
        "Generated {date}.",
}

DE = {
    # hlavička, meta
    "Porovnávací súhrn ponúk": "Vergleichsübersicht der Angebote",
    "Porovnanie ponúk": "Angebotsvergleich",
    "Klient": "Kunde",
    "Pripravené pre": "Erstellt für",
    "Vystavené": "Ausgestellt am",
    "Pripravil": "Erstellt von",
    "Identifikátor": "Kennung",
    "{n} porovnávaných ponúk · ročná spotreba {mwh} MWh · špička {peak} kW · MRK {mrk} kW":
        "{n} verglichene Angebote · Jahresverbrauch {mwh} MWh · Lastspitze {peak} kW · "
        "maximale Reservekapazität (MRK) {mrk} kW",
    "<strong>K veci.</strong> Z {n} predložených ponúk je <strong>1 ponuka</strong> vo všetkých ukazovateľoch "
    "(CAPEX, NPV, IRR, návratnosť) horšia než iná ponuka v tomto porovnaní — reálna voľba je medzi zvyšnými {viable}.":
        "<strong>Kurz gesagt.</strong> Von den {n} vorgelegten Angeboten ist <strong>1 Angebot</strong> in allen "
        "Kennzahlen (CAPEX, NPV, IRR, Amortisation) schlechter als ein anderes Angebot in diesem Vergleich — die "
        "eigentliche Wahl besteht zwischen den verbleibenden {viable}.",
    "<strong>K veci.</strong> Z {n} predložených ponúk sú <strong>{k} ponuky</strong> vo všetkých ukazovateľoch "
    "(CAPEX, NPV, IRR, návratnosť) horšia než iná ponuka v tomto porovnaní — reálna voľba je medzi zvyšnými {viable}.":
        "<strong>Kurz gesagt.</strong> Von den {n} vorgelegten Angeboten sind <strong>{k} Angebote</strong> in allen "
        "Kennzahlen (CAPEX, NPV, IRR, Amortisation) schlechter als ein anderes Angebot in diesem Vergleich — die "
        "eigentliche Wahl besteht zwischen den verbleibenden {viable}.",
    # voľby, tabuľka
    "Zhrnutie situácie": "Zusammenfassung der Ausgangslage",
    "Odporúčaná voľba": "Empfohlene Wahl",
    "Dve rozumné voľby": "Zwei sinnvolle Optionen",
    "Najvyšší zisk (NPV)": "Höchster Gewinn (NPV)",
    "aktuálne vybrané": "derzeit ausgewählt",
    "Najrýchlejšia návratnosť": "Schnellste Amortisation",
    "Všetky porovnávané ponuky ({n})": "Alle verglichenen Angebote ({n})",
    "Variant": "Variante",
    "FVE (DC)": "PV-Anlage (DC)",
    "NPV 20r": "NPV 20 J.",
    "Voľba": "Auswahl",
    "VYBRANÉ": "AUSGEWÄHLT",
    'Horšie vo všetkom než „{name}"': "In allen Kennzahlen schlechter als „{name}“",
    "nevráti sa": "keine Amortisation",
    # grafy
    "CAPEX vs. NPV — vstup oproti zisku": "CAPEX vs. NPV — Investition im Vergleich zum Gewinn",
    "Modrá = vstupná investícia, zelená = čistý zisk za 20 rokov (súčasná hodnota). Šedá = dominovaná ponuka.":
        "Blau = Anfangsinvestition, Grün = Nettogewinn über 20 Jahre (Barwert). Grau = dominiertes Angebot.",
    "Návratnosť — od najrýchlejšej po najpomalšiu": "Amortisation — von der schnellsten zur langsamsten",
    "Zelená = odporúčaná ponuka (najvyššie NPV), šedá = dominovaná ponuka.":
        "Grün = empfohlenes Angebot (höchster NPV), Grau = dominiertes Angebot.",
    "CAPEX (vstup)": "CAPEX (Investition)",
    "NPV 20 r (zisk)": "NPV 20 J. (Gewinn)",
    "Návratnosť (roky) — kratšia je lepšia": "Amortisation (Jahre) — kürzer ist besser",
    # hodnotenie ponúk
    "Hodnotenie jednotlivých ponúk": "Bewertung der einzelnen Angebote",
    "dominovaná ponuka": "dominiertes Angebot",
    "Výhody": "Vorteile",
    "Nevýhody": "Nachteile",
    "Najvyšší čistý zisk za 20 rokov zo všetkých porovnávaných ponúk ({npv}).":
        "Höchster Nettogewinn über 20 Jahre unter allen verglichenen Angeboten ({npv}).",
    "Najrýchlejšia návratnosť v porovnaní ({pb} roka).": "Schnellste Amortisation im Vergleich ({pb} Jahre).",
    "Najnižšia vstupná investícia ({capex}).": "Niedrigste Anfangsinvestition ({capex}).",
    "Batéria {kwh} kWh chráni pred prekročením MRK {mrk} kW a umožňuje arbitráž na spotovom trhu.":
        "Die Batterie mit {kwh} kWh schützt vor einer Überschreitung der MRK von {mrk} kW und ermöglicht "
        "Arbitrage am Spotmarkt.",
    "Vyššia vstupná investícia oproti variantu bez batérie a dodatočná technológia na údržbu.":
        "Höhere Anfangsinvestition als die Variante ohne Batterie sowie zusätzliche Technik, die gewartet werden muss.",
    "Jednoduchšie riešenie bez batériového úložiska — menej technológie, nižší CAPEX.":
        "Einfachere Lösung ohne Batteriespeicher — weniger Technik, niedrigerer CAPEX.",
    "Bez ochrany pred prekročením MRK {mrk} kW a bez výnosu z arbitráže.":
        "Kein Schutz vor einer Überschreitung der MRK von {mrk} kW und keine Arbitrageerlöse.",
    "Vo všetkých ukazovateľoch horšia než „{name}“ — nemá zmysel ju vybrať.":
        "In allen Kennzahlen schlechter als „{name}“ — eine Wahl dieses Angebots ist nicht sinnvoll.",
    "Najvyššie ročné zhodnotenie investície (IRR {irr} %).": "Höchste jährliche Rendite der Investition (IRR {irr} %).",
    # odporúčanie
    "Odporúčanie": "Empfehlung",
    "Odporúčanie Energovision": "Empfehlung von Energovision",
    "Ponuka „{name}“ má súčasne najvyšší čistý zisk za 20 rokov ({npv}) aj najrýchlejšiu návratnosť ({pb}) — "
    "jednoznačná voľba v tomto porovnaní.":
        "Das Angebot „{name}“ bietet zugleich den höchsten Nettogewinn über 20 Jahre ({npv}) und die schnellste "
        "Amortisation ({pb}) — die eindeutige Wahl in diesem Vergleich.",
    "Ponuka „{name}“ prinesie za 20 rokov najvyšší čistý zisk ({npv} pri návratnosti {pb}). Ak preferujete nižšiu "
    "vstupnú investíciu a najrýchlejší návrat peňazí, zvoľte „{alt}“ ({alt_capex}, návratnosť {alt_pb}).":
        "Das Angebot „{name}“ erzielt über 20 Jahre den höchsten Nettogewinn ({npv} bei einer Amortisation von "
        "{pb}). Wenn Sie eine niedrigere Anfangsinvestition und den schnellsten Kapitalrückfluss bevorzugen, wählen "
        "Sie „{alt}“ ({alt_capex}, Amortisation {alt_pb}).",
    # technické fakty
    "Technické fakty o odbernom mieste": "Technische Fakten zur Abnahmestelle",
    "Ročná spotreba elektriny": "Jährlicher Stromverbrauch",
    "Špička odberu (15-min maximum)": "Lastspitze (15-Minuten-Maximum)",
    "Maximálna rezervovaná kapacita (MRK)": "Maximale Reservekapazität (MRK)",
    "<strong>⚠ Špička odberu prekračuje MRK</strong> — riziko pokút od distribútora; batéria ho vie znížiť "
    "(peak shaving)":
        "<strong>⚠ Die Lastspitze überschreitet die MRK</strong> — Risiko von Strafzahlungen an den "
        "Verteilnetzbetreiber; eine Batterie kann sie senken (Peak Shaving)",
    "Cena elektriny — nákup": "Strompreis — Bezug",
    "Cena elektriny — výkup prebytkov": "Strompreis — Abnahme von Überschüssen",
    "Ročná výroba FVE ({name})": "Jährliche Erzeugung der PV-Anlage ({name})",
    "Samospotreba vyrobenej elektriny": "Eigenverbrauch des erzeugten Stroms",
    "Ročný export prebytkov do siete": "Jährliche Einspeisung von Überschüssen ins Netz",
    "Čo si overiť pred rozhodnutím": "Was Sie vor der Entscheidung prüfen sollten",
    # ako čítať
    "Ako čítať toto porovnanie": "So lesen Sie diesen Vergleich",
    "<strong>CAPEX</strong> — celková vstupná investícia bez DPH.":
        "<strong>CAPEX</strong> — Gesamtinvestition ohne MwSt.",
    "<strong>NPV (čistá súčasná hodnota)</strong> — čistý zisk za {years} rokov prevádzky, po odpočítaní "
    "investície, v dnešnej hodnote peňazí. Vyššie číslo = viac peňazí za 20 rokov.":
        "<strong>NPV (Kapitalwert)</strong> — Nettogewinn über {years} Betriebsjahre nach Abzug der Investition, "
        "in heutigem Geldwert. Höherer Wert = mehr Geld über 20 Jahre.",
    "<strong>IRR (vnútorná miera výnosu)</strong> — ročné zhodnotenie investície v percentách; porovnaj s bankovým "
    "úrokom alebo alternatívnou investíciou.":
        "<strong>IRR (interner Zinsfuß)</strong> — jährliche Rendite der Investition in Prozent; vergleichen Sie sie "
        "mit dem Bankzins oder einer alternativen Anlage.",
    "<strong>Návratnosť</strong> — počet rokov, za ktoré sa investícia vráti z úspor na energii.":
        "<strong>Amortisation</strong> — Anzahl der Jahre, in denen sich die Investition aus den Energieeinsparungen "
        "zurückzahlt.",
    "<strong>BESS (batériové úložisko)</strong> — ochrana pred pokutou za prekročenie MRK a výnos z obchodovania "
    "so spotovou cenou elektriny (arbitráž).":
        "<strong>BESS (Batteriespeicher)</strong> — Schutz vor Strafzahlungen bei Überschreitung der MRK und Erlöse "
        "aus dem Handel zum Strom-Spotpreis (Arbitrage).",
    "<strong>Dominovaná ponuka</strong> — existuje iná porovnávaná ponuka, ktorá je súčasne lacnejšia aj "
    "výnosnejšia vo všetkých ukazovateľoch; nemá zmysel ju vybrať za žiadnych preferencií.":
        "<strong>Dominiertes Angebot</strong> — ein anderes Angebot in diesem Vergleich ist zugleich günstiger und "
        "in allen Kennzahlen rentabler; unter keiner Präferenz ist seine Wahl sinnvoll.",
    # rozhodnutie, kontakt
    "Podpis zákazníka · vybraný variant": "Unterschrift des Kunden · gewählte Variante",
    "Dátum": "Datum",
    "Kontakt": "Kontakt",
    "IČO": "Firmen-ID",
    "Porovnávací súhrn nahrádza samostatné technické posudky priložené k jednotlivým ponukám. Podrobný technický "
    "posudok k vybranému variantu je na vyžiadanie k dispozícii v plnom znení. Generated {date}.":
        "Diese Vergleichsübersicht ersetzt die separaten technisch-wirtschaftlichen Gutachten zu den einzelnen "
        "Angeboten. Das ausführliche technisch-wirtschaftliche Gutachten zur gewählten Variante stellen wir Ihnen "
        "auf Anfrage vollständig zur Verfügung. Erstellt am {date}.",
}
