"""Kanonické preklady kľúčov, ktoré viac oblastí preložilo rôzne (modul sa načíta posledný → vyhráva).
Test test_aom_i18n_catalog.test_no_conflicting_translations vyžaduje, aby tu bol každý taký kľúč."""
EN = {
    "FVE": "PV",
    "Priamo do odberu": "Direct use",
    "Špecifický výnos FVE": "Specific PV yield",
    "Max 15-min odber": "Max. 15-min demand",
    "MRK / RK": "MRK / RK (reserved capacity)",
    "Inštalovaný výkon FVE": "Installed PV capacity",
    "Batériové úložisko": "BESS (battery storage)",
    "Predpokladaná ročná výroba": "Expected annual generation",
    "Ročná výroba FVE": "Annual PV generation",
    "Silová zložka": "Energy component",
    "VSD tarif": "VSD tariff (regional distribution system operator)",
    "TPS + systémové služby": "TPS (system operation tariff) + system services",
    "Koľko stojí ponechať súčasný stav": "What it costs to keep the status quo",
    "Záruky a istoty": "Guarantees and assurances",
    "Realizácia, revízie a uvedenie do prevádzky pod jednou zodpovednosťou Energovision.":
        "Installation, inspections and commissioning under the single responsibility of Energovision.",
    "WACC / životnosť / zákon": "WACC / lifetime / statutory",
}
DE = {
    "FVE": "PV",
    "Priamo do odberu": "Direktverbrauch",
    "Cez batériu": "Über Batterie",
    "Špecifický výnos FVE": "Spezifischer PV-Ertrag",
    "EIC OM / č. miesta": "EIC der Abnahmestelle / Nr.",
    "Distribučná oblasť": "Verteilnetzgebiet",
    "Sadzba / tarif": "Tarifstufe / Tarif",
    "MRK / RK": "MRK / RK (Reservekapazität)",
    "Inštalovaný výkon FVE": "Installierte PV-Leistung",
    "Batériové úložisko": "BESS (Batteriespeicher)",
    "Pokrytie spotreby OM": "Verbrauchsdeckung der Abnahmestelle",
    "Silová zložka": "Energiepreis",
    "VSD tarif": "VSD-Tarif (regionaler Verteilnetzbetreiber)",
    "TPS + systémové služby": "TPS (Systembetriebstarif) + Systemdienstleistungen",
    "pásmo 2": "Stufe 2",
    "Pevná zložka distribúcie": "Fester Netzentgeltanteil",
    "Koľko stojí ponechať súčasný stav": "Was es kostet, alles beim Alten zu lassen",
    "Realizácia, revízie a uvedenie do prevádzky pod jednou zodpovednosťou Energovision.":
        "Ausführung, Prüfungen und Inbetriebnahme in der alleinigen Verantwortung von Energovision.",
    "orientačné": "Richtwert",
}
