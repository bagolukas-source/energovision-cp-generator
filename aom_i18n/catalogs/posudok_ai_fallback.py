"""Deterministický fallback AI sekcií posudku (analyza_om_v2._generate_chocosuc_ai._fallback)."""
EN = {
    "Ročná úspora {a} € (báza) až {b} € (plný scenár), NPV 20 r. {npv} € a IRR {irr} %. "
    "Pravdepodobnosť kladného NPV (Monte Carlo) {p} %.":
        "Annual savings of {a} € (base case) up to {b} € (full system PV + BESS), 20-year NPV of {npv} € and IRR of {irr} %. "
        "Probability of a positive NPV (Monte Carlo): {p} %.",
    "Z technického hľadiska je navrhnutý variant {kwp} kWp primeraný profilu OM — pri samospotrebe "
    "{sam} % sa vyrobená energia využije takmer celá. Rozhodujúce predpoklady sú špecifický výnos "
    "{vyn} kWh/kWp (PVGIS) a stabilita odberového profilu.":
        "From a technical point of view, the proposed {kwp} kWp option suits the supply point's load profile — with "
        "self-consumption of {sam} %, almost all generated energy is used on site. The key assumptions are a specific yield "
        "of {vyn} kWh/kWp (PVGIS) and a stable consumption profile.",
    "Citlivosť: medzi defenzívnym a optimistickým scenárom je rozpätie NPV {lo} – {hi} €. "
    "Pred realizáciou odporúčam overiť finálnu tarifu (faktúra), podmienky pripojenia u "
    "prevádzkovateľa distribučnej sústavy a presnú konštrukciu z cenovej ponuky.":
        "Sensitivity: between the defensive and the optimistic scenario, NPV ranges from {lo} to {hi} €. "
        "Before implementation we recommend verifying the final tariff (invoice), the grid connection conditions with the "
        "distribution system operator and the exact mounting structure from the price quote.",
    "Odporúčanie k navrhnutému variantu": "Recommendation for the proposed option",
    "Doplniť faktúru za elektrinu": "Provide an electricity invoice",
    "Spresní silovú aj distribučnú cenu — posudok prejde z orientačného na presný.":
        "It refines both the energy and the distribution price — the assessment moves from indicative to exact.",
    "Preveriť variant s batériou": "Assess an option with a battery",
    "Batéria posúva výrobu do špičiek a zvyšuje samospotrebu — vhodné preveriť ako ďalší variant.":
        "A battery shifts generation into peak hours and increases self-consumption — worth assessing as a further option.",
    "Preveriť zníženie RK": "Assess reducing the reserved capacity (RK)",
    "Po inštalácii batérie klesá špičkové zaťaženie zo siete — možnosť znížiť rezervovanú kapacitu.":
        "With a battery installed, peak grid demand falls — the reserved capacity can potentially be reduced.",
    "Technická obhliadka OM": "Technical site survey",
    "Overenie strechy/plochy, statiky a bodu pripojenia pred finálnym projektom.":
        "Verification of the roof/area, structural capacity and connection point before the final design.",
}
DE = {
    "Ročná úspora {a} € (báza) až {b} € (plný scenár), NPV 20 r. {npv} € a IRR {irr} %. "
    "Pravdepodobnosť kladného NPV (Monte Carlo) {p} %.":
        "Jährliche Einsparung von {a} € (Basisfall) bis {b} € (Gesamtsystem PV + BESS), NPV über 20 Jahre {npv} € und "
        "IRR {irr} %. Wahrscheinlichkeit eines positiven NPV (Monte-Carlo): {p} %.",
    "Z technického hľadiska je navrhnutý variant {kwp} kWp primeraný profilu OM — pri samospotrebe "
    "{sam} % sa vyrobená energia využije takmer celá. Rozhodujúce predpoklady sú špecifický výnos "
    "{vyn} kWh/kWp (PVGIS) a stabilita odberového profilu.":
        "Aus technischer Sicht ist die vorgeschlagene Variante mit {kwp} kWp dem Lastprofil der Abnahmestelle angemessen — "
        "bei einem Eigenverbrauch von {sam} % wird die erzeugte Energie nahezu vollständig vor Ort genutzt. Maßgebliche "
        "Annahmen sind ein spezifischer Ertrag von {vyn} kWh/kWp (PVGIS) und ein stabiles Verbrauchsprofil.",
    "Citlivosť: medzi defenzívnym a optimistickým scenárom je rozpätie NPV {lo} – {hi} €. "
    "Pred realizáciou odporúčam overiť finálnu tarifu (faktúra), podmienky pripojenia u "
    "prevádzkovateľa distribučnej sústavy a presnú konštrukciu z cenovej ponuky.":
        "Sensitivität: Zwischen dem defensiven und dem optimistischen Szenario liegt der NPV zwischen {lo} und {hi} €. "
        "Vor der Umsetzung empfehlen wir, den endgültigen Tarif (Rechnung), die Anschlussbedingungen beim "
        "Verteilnetzbetreiber und die genaue Unterkonstruktion laut Angebot zu prüfen.",
    "Odporúčanie k navrhnutému variantu": "Empfehlung zur vorgeschlagenen Variante",
    "Doplniť faktúru za elektrinu": "Stromrechnung nachreichen",
    "Spresní silovú aj distribučnú cenu — posudok prejde z orientačného na presný.":
        "Sie präzisiert Energie- und Netzentgelt — das Gutachten wird von einer Richtwert- zu einer exakten Berechnung.",
    "Preveriť variant s batériou": "Variante mit Batterie prüfen",
    "Batéria posúva výrobu do špičiek a zvyšuje samospotrebu — vhodné preveriť ako ďalší variant.":
        "Eine Batterie verlagert die Erzeugung in die Spitzenzeiten und erhöht den Eigenverbrauch — als weitere Variante "
        "prüfenswert.",
    "Preveriť zníženie RK": "Senkung der reservierten Leistung (RK) prüfen",
    "Po inštalácii batérie klesá špičkové zaťaženie zo siete — možnosť znížiť rezervovanú kapacitu.":
        "Nach der Installation der Batterie sinkt die Spitzenlast aus dem Netz — die reservierte Leistung kann ggf. gesenkt "
        "werden.",
    "Technická obhliadka OM": "Technische Begehung der Abnahmestelle",
    "Overenie strechy/plochy, statiky a bodu pripojenia pred finálnym projektom.":
        "Prüfung von Dach/Fläche, Statik und Anschlusspunkt vor der endgültigen Planung.",
}
