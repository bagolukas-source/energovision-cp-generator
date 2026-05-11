"""
generuj_po.py — Material Purchase Order generator pre Energovision FVE/BESS projekty.

Vstup: lead_data dict (rovnaký formát ako pri generuj_dokumenty)
Výstup: list[dict] s položkami { polozka, mnozstvo, jednotka, dodavatel, cena_ks, spolu, kategoria, poznamka }

Zdroje cien:
- Cennik_v2.xlsx (predajné ceny → použijeme len ako referenčné)
- Hagard feed XML (35% zľava Energovision)
- ZeroDivisionError-safe defaulty pre chýbajúce hodnoty

BOM rozsah pre štandardnú FVE inštaláciu:
1. Panely (počet z konfigu)
2. Menič (1× podľa značky/výkonu)
3. Batéria (ak je)
4. Wallbox (ak je)
5. Konštrukcia (počet × typ strechy)
6. DC kábel solárny (paušál podľa kWp)
7. AC kábel CYKY-J 5x4 (15m default)
8. MC4 konektory (počet panelov × stringov)
9. DC rozvádzač s ochranami
10. AC rozvádzač podľa distribučky
11. Smartmeter 3F
12. SPD typ 2 1100VDC
13. Uzemnenie ZT 1,5m FeZn
14. Drobný materiál (pauš.)
"""

import re
from pathlib import Path

# ============================================================
# CENNÍK — referenčné ceny (predaj bez DPH)
# Hagard feed dáva nákupné ceny so zľavou 35% Energovision.
# Pre PO je relevantná NÁKUPNÁ cena (po zľave) → použijeme približne 65% feed.
# ============================================================
CENY = {
    # Panely
    "panel_470Wp": {"nazov": "LONGi Hi-MO X10 LR7-54HVB 470 Wp celočierny", "cena": 77.10, "dod": "Hagard"},
    "panel_535Wp": {"nazov": "LONGi Hi-MO X10 LR7-60HVH 535 Wp", "cena": 79.80, "dod": "Hagard"},
    "panel_600Wp": {"nazov": "LONGi Hi-MO 600 Wp", "cena": 95.00, "dod": "Hagard"},
    "panel_700Wp": {"nazov": "LONGi Hi-MO 700 Wp", "cena": 110.00, "dod": "Hagard"},

    # Meniče
    "inv_solinteg_5K": {"nazov": "Solinteg MHT-5K-25 hybridný 5 kW 3F", "cena": 720, "dod": "Solinteg SK"},
    "inv_solinteg_6K": {"nazov": "Solinteg MHT-6K-25 hybridný 6 kW 3F", "cena": 820, "dod": "Solinteg SK"},
    "inv_solinteg_8K": {"nazov": "Solinteg MHT-8K-25 hybridný 8 kW 3F", "cena": 920, "dod": "Solinteg SK"},
    "inv_solinteg_10K": {"nazov": "Solinteg MHT-10K-25 hybridný 10 kW 3F", "cena": 1042, "dod": "Solinteg SK"},
    "inv_huawei_5K": {"nazov": "Huawei SUN2000-5K hybridný 5 kW", "cena": 844, "dod": "Hagard"},
    "inv_huawei_6K": {"nazov": "Huawei SUN2000-6K hybridný 6 kW", "cena": 944, "dod": "Hagard"},
    "inv_huawei_8K": {"nazov": "Huawei SUN2000-8K hybridný 8 kW", "cena": 1119, "dod": "Hagard"},
    "inv_huawei_10K": {"nazov": "Huawei SUN2000-10K hybridný 10 kW", "cena": 1257, "dod": "Hagard"},
    "inv_goodwe_6K": {"nazov": "GoodWe GW6000-ET-20 hybridný 6 kW", "cena": 970, "dod": "Hagard"},
    "inv_goodwe_8K": {"nazov": "GoodWe GW8000-ET-20 hybridný 8 kW", "cena": 997, "dod": "Hagard"},
    "inv_goodwe_10K": {"nazov": "GoodWe GW10K-ET-20 hybridný 10 kW", "cena": 1025, "dod": "Hagard"},

    # Batérie
    "bat_solinteg_5K": {"nazov": "Solinteg EBA B5K1 5,12 kWh modul", "cena": 755, "dod": "Solinteg SK"},
    "bat_solinteg_10K": {"nazov": "Solinteg EBA B5K1 10,24 kWh modul", "cena": 1405, "dod": "Solinteg SK"},
    "bat_huawei_5K": {"nazov": "Huawei LUNA2000-5-E1 5 kWh modul", "cena": 1585, "dod": "Hagard"},
    "bat_huawei_7K": {"nazov": "Huawei LUNA2000-7-E1 7 kWh modul", "cena": 2250, "dod": "Hagard"},
    "bat_huawei_bms": {"nazov": "Huawei LUNA2000-10KW-C1 BMS riadiaca jednotka", "cena": 800, "dod": "Hagard"},
    "bat_pylon_5K": {"nazov": "Pylontech Force H3 modul 5,12 kWh", "cena": 770, "dod": "Hagard"},
    "bat_pylon_bms": {"nazov": "Pylontech Force H3 BMS riadiaca jednotka", "cena": 440, "dod": "Hagard"},

    # Wallboxy
    "wbx_solinteg_7K": {"nazov": "Solinteg Charger ECA-S07K-BS0 7 kW 1F", "cena": 425, "dod": "Solinteg SK"},
    "wbx_solinteg_11K": {"nazov": "Solinteg Charger ECA-S11K-BS0 11 kW 3F", "cena": 475, "dod": "Solinteg SK"},
    "wbx_huawei_7K": {"nazov": "Huawei AC Smart 7 kW/32A 1F", "cena": 310, "dod": "Hagard"},
    "wbx_huawei_22K": {"nazov": "Huawei AC Smart 22 kW/32A 3F", "cena": 340, "dod": "Hagard"},
    "wbx_goodwe_11K": {"nazov": "GoodWe EV Charger 11 kW", "cena": 425, "dod": "Hagard"},
    "wbx_goodwe_22K": {"nazov": "GoodWe EV Charger 22 kW", "cena": 475, "dod": "Hagard"},

    # Konštrukcia (cena na modul vrátane materiálu)
    "kon_skridla": {"nazov": "Konštrukcia Škridla — háky + profil", "cena": 30, "dod": "Hagard"},
    "kon_plech": {"nazov": "Konštrukcia Plech — kombivrut + profil", "cena": 20, "dod": "Hagard"},
    "kon_falc": {"nazov": "Konštrukcia Falcový plech — falcový úchyt + profil", "cena": 30, "dod": "Hagard"},
    "kon_rovna_juh": {"nazov": "Rovná strecha Juh — balastová konštrukcia 13°", "cena": 50, "dod": "Hagard"},
    "kon_rovna_vz": {"nazov": "Rovná strecha V/Z — balastová konštrukcia 10°", "cena": 40, "dod": "Hagard"},

    # Materiál
    "kab_solar_cierny": {"nazov": "FVE solárny kábel H1Z2Z2-K 6 mm² čierny", "cena": 0.88, "dod": "Hagard"},  # feed × 0.65
    "kab_solar_cerveny": {"nazov": "FVE solárny kábel H1Z2Z2-K 6 mm² červený", "cena": 0.88, "dod": "Hagard"},
    "kab_cyky_5x4": {"nazov": "Kábel CYKY-J 5×4 mm²", "cena": 2.75, "dod": "Hagard"},  # feed 4.23 × 0.65
    "kab_cyky_5x6": {"nazov": "Kábel CYKY-J 5×6 mm²", "cena": 4.20, "dod": "Hagard"},
    "mc4_samec": {"nazov": "MC4 Stäubli konektor samec", "cena": 0.98, "dod": "Hagard"},  # feed 1.50 × 0.65
    "mc4_samica": {"nazov": "MC4 Stäubli konektor samica", "cena": 1.30, "dod": "Hagard"},  # feed 2.00 × 0.65
    "spd_dc": {"nazov": "SPD typ 2 1100 VDC pre FV", "cena": 40.30, "dod": "Hagard"},  # feed 62 × 0.65
    "uzemnenie_zt": {"nazov": "ZT 1,5 m FeZn uzemňovací bod", "cena": 13.30, "dod": "Hagard"},  # feed 20.45 × 0.65

    # Rozvádzače
    "rvz_dc": {"nazov": "DC rozvádzač s ochranami a vyhlásením o zhode", "cena": 200, "dod": "Hagard"},
    "rvz_ac_zsdis": {"nazov": "AC rozvádzač pre lokalitu ZSDIS", "cena": 100, "dod": "Hagard"},
    "rvz_ac_ssd": {"nazov": "AC rozvádzač pre lokalitu SSD", "cena": 100, "dod": "Hagard"},
    "rvz_ac_vsd": {"nazov": "AC rozvádzač pre lokalitu VSD", "cena": 100, "dod": "Hagard"},
    "rvz_12mod": {"nazov": "Rozvádzač nástenný 12-modulový", "cena": 14, "dod": "Hagard"},
    "rvz_24mod": {"nazov": "Rozvádzač nástenný 24-modulový", "cena": 24, "dod": "Hagard"},

    # Smartmeter
    "smt_3f": {"nazov": "Smartmeter 3-fázový", "cena": 132, "dod": "Hagard"},

    # Drobný materiál (paušál)
    "drobny_material": {"nazov": "Drobný materiál (skrutky, lepidlá, koncovky, pásky, vázacie káble)", "cena": 50, "dod": "Hagard"},
}


# ============================================================
# MAPOVANIE značky/výkonu meniča
# ============================================================
def vyber_invertor(menic_str: str, vykon_kwp: float) -> str:
    """Vyber kód meniča z CENY podľa textu z Notion."""
    if not menic_str:
        return "inv_solinteg_10K"

    menic_lower = menic_str.lower()
    # Detekuj výkon z textu
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*kW", menic_str, re.IGNORECASE)
    kw = float(m.group(1).replace(",", ".")) if m else round(vykon_kwp)
    kw_int = max(5, min(10, round(kw)))

    if "solinteg" in menic_lower:
        if kw_int <= 5:
            return "inv_solinteg_5K"
        if kw_int <= 6:
            return "inv_solinteg_6K"
        if kw_int <= 8:
            return "inv_solinteg_8K"
        return "inv_solinteg_10K"
    if "huawei" in menic_lower:
        if kw_int <= 5:
            return "inv_huawei_5K"
        if kw_int <= 6:
            return "inv_huawei_6K"
        if kw_int <= 8:
            return "inv_huawei_8K"
        return "inv_huawei_10K"
    if "goodwe" in menic_lower:
        if kw_int <= 6:
            return "inv_goodwe_6K"
        if kw_int <= 8:
            return "inv_goodwe_8K"
        return "inv_goodwe_10K"

    # Fallback
    return "inv_solinteg_10K"


def vyber_baterie(bateria_typ: str, pocet_baterii: int, menic_kod: str):
    """Vráti list batériových položiek (modul × N + BMS ak treba)."""
    if not bateria_typ or pocet_baterii <= 0:
        return []

    bat_lower = bateria_typ.lower()
    items = []

    if "huawei" in bat_lower or "luna" in bat_lower:
        # Huawei LUNA — kapacita 5 alebo 7 kWh
        if "7" in bat_lower:
            kod = "bat_huawei_7K"
        else:
            kod = "bat_huawei_5K"
        items.append((kod, pocet_baterii))
        items.append(("bat_huawei_bms", 1))  # 1 BMS na batériový stack
    elif "solinteg" in bat_lower or "eba" in bat_lower:
        if "10" in bat_lower:
            kod = "bat_solinteg_10K"
        else:
            kod = "bat_solinteg_5K"
        items.append((kod, pocet_baterii))
    elif "pylon" in bat_lower or "force" in bat_lower:
        items.append(("bat_pylon_5K", max(2, pocet_baterii)))  # min 2 moduly
        items.append(("bat_pylon_bms", 1))
    else:
        # Fallback — Solinteg
        items.append(("bat_solinteg_5K", pocet_baterii))

    return items


def vyber_wallbox(wallbox_typ: str, menic_kod: str):
    """Vráti kód wallboxu zo CENY, alebo None."""
    if not wallbox_typ:
        return None

    wb_lower = wallbox_typ.lower()

    if "solinteg" in wb_lower:
        return "wbx_solinteg_11K" if "11" in wb_lower or "3f" in wb_lower else "wbx_solinteg_7K"
    if "huawei" in wb_lower:
        return "wbx_huawei_22K" if "22" in wb_lower or "3f" in wb_lower else "wbx_huawei_7K"
    if "goodwe" in wb_lower:
        return "wbx_goodwe_22K" if "22" in wb_lower or "3f" in wb_lower else "wbx_goodwe_11K"

    # Default — značka podľa meniča
    if "solinteg" in menic_kod:
        return "wbx_solinteg_11K"
    if "huawei" in menic_kod:
        return "wbx_huawei_22K"
    return "wbx_goodwe_11K"


def vyber_konstrukciu(konstrukcia_typ: str) -> str:
    """Vráti kód konštrukcie podľa typu strechy."""
    if not konstrukcia_typ:
        return "kon_skridla"

    k_lower = konstrukcia_typ.lower()
    if "škridl" in k_lower or "skridl" in k_lower:
        return "kon_skridla"
    if "falc" in k_lower:
        return "kon_falc"
    if "plech" in k_lower:
        return "kon_plech"
    if "rovn" in k_lower and ("v/z" in k_lower or "vz" in k_lower or "vých" in k_lower):
        return "kon_rovna_vz"
    if "rovn" in k_lower:
        return "kon_rovna_juh"
    return "kon_skridla"


def vyber_panel(pocet_panelov: int, panel_typ: str = "") -> tuple:
    """Vráti (kod_panela, Wp na panel)."""
    if panel_typ:
        p_lower = panel_typ.lower()
        if "470" in p_lower:
            return ("panel_470Wp", 470)
        if "600" in p_lower:
            return ("panel_600Wp", 600)
        if "700" in p_lower:
            return ("panel_700Wp", 700)
    # Default 535 Wp
    return ("panel_535Wp", 535)


def vyber_ac_rozvadzac(distribucka: str = "") -> str:
    """SSD / ZSDIS / VSD."""
    if not distribucka:
        return "rvz_ac_ssd"  # default najčastejšie SSD pre stred Slovenska
    d_lower = distribucka.lower()
    if "vsd" in d_lower or "východ" in d_lower:
        return "rvz_ac_vsd"
    if "zsdis" in d_lower or "západ" in d_lower or "zse" in d_lower:
        return "rvz_ac_zsdis"
    return "rvz_ac_ssd"


# ============================================================
# HLAVNÁ FUNKCIA — generuj BOM zo lead_data
# ============================================================
def generuj_bom(lead_data: dict) -> list:
    """
    Vstup: dict s kľúčmi
        - pocet_panelov (int)
        - vykon_kwp (float)
        - panel_typ (str, optional)
        - menic (str)
        - bateria_typ (str)
        - pocet_baterii (int)
        - wallbox_typ (str)
        - ma_wallbox (bool)
        - konstrukcia (str)
        - distribucka (str, optional)

    Výstup: list[dict] položiek BOM
    """
    pocet_panelov = int(lead_data.get("pocet_panelov") or 0)
    vykon_kwp = float(lead_data.get("vykon_kwp") or 0)
    panel_typ = lead_data.get("panel_typ", "")
    menic = lead_data.get("menic", "")
    bateria_typ = lead_data.get("bateria_typ", "")
    pocet_baterii = int(lead_data.get("pocet_baterii") or 0)
    wallbox_typ = lead_data.get("wallbox_typ", "")
    ma_wallbox = bool(lead_data.get("ma_wallbox") or wallbox_typ)
    konstrukcia = lead_data.get("konstrukcia", "")
    distribucka = lead_data.get("distribucka", "")

    bom = []

    def add(kod, mnozstvo, jednotka, kategoria, poznamka=""):
        if kod not in CENY:
            return
        c = CENY[kod]
        spolu = round(c["cena"] * mnozstvo, 2)
        bom.append({
            "polozka": c["nazov"],
            "kod": kod,
            "mnozstvo": mnozstvo,
            "jednotka": jednotka,
            "dodavatel": c["dod"],
            "cena_ks": c["cena"],
            "spolu": spolu,
            "kategoria": kategoria,
            "poznamka": poznamka,
            "stav": "Objednať",
        })

    # 1) PANELY
    if pocet_panelov > 0:
        kod_panel, wp = vyber_panel(pocet_panelov, panel_typ)
        add(kod_panel, pocet_panelov, "ks", "Panel")

    # 2) MENIČ
    inv_kod = vyber_invertor(menic, vykon_kwp)
    add(inv_kod, 1, "ks", "Menič")

    # 3) BATÉRIA
    for bat_kod, bat_qty in vyber_baterie(bateria_typ, pocet_baterii, inv_kod):
        add(bat_kod, bat_qty, "ks", "Batéria")

    # 4) WALLBOX
    if ma_wallbox:
        wb_kod = vyber_wallbox(wallbox_typ, inv_kod)
        if wb_kod:
            add(wb_kod, 1, "ks", "Wallbox")

    # 5) KONŠTRUKCIA
    if pocet_panelov > 0:
        kon_kod = vyber_konstrukciu(konstrukcia)
        add(kon_kod, pocet_panelov, "modul", "Konštrukcia",
            poznamka="Vrátane všetkých kotvení a profilov")

    # 6) DC SOLÁRNY KÁBEL
    # ~30m čierny + 30m červený na každú FVE 5-10 kWp
    dc_dlzka = max(30, int(vykon_kwp * 6))  # 6m/kWp obojstranne
    add("kab_solar_cierny", dc_dlzka, "m", "Materiál",
        poznamka="DC kabeláž medzi panelmi a meničom (čierny)")
    add("kab_solar_cerveny", dc_dlzka, "m", "Materiál",
        poznamka="DC kabeláž medzi panelmi a meničom (červený)")

    # 7) AC KÁBEL CYKY
    # 15m štandard pre stredne dlhú trasu (menič → rozvádzač)
    ac_dlzka = 15
    if vykon_kwp >= 8:
        add("kab_cyky_5x6", ac_dlzka, "m", "Materiál",
            poznamka="AC kabeláž menič → rozvádzač (vyšší výkon)")
    else:
        add("kab_cyky_5x4", ac_dlzka, "m", "Materiál",
            poznamka="AC kabeláž menič → rozvádzač")

    # 8) MC4 konektory
    # Default: 2 stringy → 4 páry = 4 samec + 4 samica
    pocet_stringov = 2 if pocet_panelov >= 10 else 1
    pocet_konektorov = pocet_stringov * 2
    add("mc4_samec", pocet_konektorov, "ks", "Materiál")
    add("mc4_samica", pocet_konektorov, "ks", "Materiál")

    # 9) DC ROZVÁDZAČ s ochranami (obsahuje aj SPD)
    add("rvz_dc", 1, "ks", "Rozvádzač",
        poznamka="DC rozvádzač má SPD typ 2 1100 VDC integrované")

    # 10) AC ROZVÁDZAČ podľa distribučky
    ac_rvz_kod = vyber_ac_rozvadzac(distribucka)
    add(ac_rvz_kod, 1, "ks", "Rozvádzač")

    # 11) SMARTMETER
    add("smt_3f", 1, "ks", "Smartmeter")

    # 12) UZEMNENIE
    add("uzemnenie_zt", 1, "ks", "Materiál",
        poznamka="ZT 1,5 m FeZn uzemnenie + práca v balíku PRC-008")

    # 13) DROBNÝ MATERIÁL
    add("drobny_material", 1, "kpl", "Materiál",
        poznamka="Skrutky, lepidlá, koncovky, pásky, viazacie pásky")

    return bom


def bom_total(bom: list) -> float:
    """Sumár nákupných cien materiálu."""
    return round(sum(item["spolu"] for item in bom), 2)


def bom_summary(bom: list) -> dict:
    """Sumár podľa kategórie."""
    by_cat = {}
    for item in bom:
        cat = item["kategoria"]
        by_cat[cat] = by_cat.get(cat, 0) + item["spolu"]
    return {k: round(v, 2) for k, v in by_cat.items()}


# ============================================================
# TEST
# ============================================================
if __name__ == "__main__":
    test_lead = {
        "pocet_panelov": 14,
        "vykon_kwp": 7.49,
        "panel_typ": "LONGi 535 Wp",
        "menic": "Solinteg MHT-8K-25 hybridný 8 kW",
        "bateria_typ": "Solinteg EBA B5K1 5,12 kWh",
        "pocet_baterii": 2,
        "wallbox_typ": "Solinteg Charger 11 kW",
        "ma_wallbox": True,
        "konstrukcia": "Škridla",
        "distribucka": "SSD",
    }

    bom = generuj_bom(test_lead)
    print(f"BOM ({len(bom)} položiek):\n")
    for i, item in enumerate(bom, 1):
        print(f"{i:2d}. {item['mnozstvo']:>4} {item['jednotka']:<5} | "
              f"{item['polozka'][:55]:<55} | "
              f"{item['cena_ks']:>8.2f} EUR | "
              f"{item['spolu']:>9.2f} EUR | {item['dodavatel']}")

    print(f"\nSumár: {bom_total(bom)} EUR (nákupne, bez DPH)")
    print("\nPo kategóriách:")
    for kat, suma in bom_summary(bom).items():
        print(f"  {kat:<15} {suma:>9.2f} EUR")
