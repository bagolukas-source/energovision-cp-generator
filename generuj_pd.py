"""
Generuj projektovú dokumentáciu (PD) pre malé zdroje FVE do 10 kW.

Používa Lukášove templaty z `Projekcia/` priečinka (Jinja2 {{placeholder}} syntax)
a vyplňuje ich cez docxtpl. Zachová celú štruktúru, logo, fonty, layout.

Templaty:
- Kryc.docx — krycí list
- Tit.docx — titulný list
- Zoz.docx — zoznam dokumentácie
- POUVV.docx — protokol o určení vonkajších vplyvov
- suhr_technicka_sprava.docx — súhrnná technická správa
- technicka_sprava.docx (resp. SSD/VSD/ZSDIS varianty)

Komisia (per Lukášovo požiadavku):
- Vypracoval: Lukáš Bago
- Kontroloval: Matej Horváth
- Zodpovedný projektant: Ing. Pavol Kaprál
"""
import os
import re
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger("generuj_pd")

# ============================================================
# PATH KU TEMPLATOM
# ============================================================
# Render: templaty sú v repo `Projekcia/` na úrovni vyššie alebo v `templates_projekcia/`
HERE = Path(__file__).resolve().parent
# Možné cesty (skúsi v poradí)
PROJEKCIA_DIRS = [
    HERE / "templates_projekcia",                 # primary — pri pushi
    HERE.parent.parent / "Projekcia",             # local dev
    Path("/sessions/magical-eager-gates/mnt/Obchod/2026-05-03_B2C_cenove_ponuky_generator/Projekcia"),  # sandbox
]


def _find_template(filename):
    for d in PROJEKCIA_DIRS:
        p = d / filename
        if p.exists():
            return p
    raise FileNotFoundError(f"Template {filename} nenajdený v žiadnom z {PROJEKCIA_DIRS}")


# ============================================================
# KOMISIA — fixná pre Energovision B2C
# ============================================================
KOMISIA = {
    "vypracoval": "Lukáš Bago",
    "vypracovalsk": "Lukáš Bago",
    "kontroloval": "Matej Horváth",
    "zodpovedny_projektant": "Ing. Pavol Kaprál",
    "zodpovedny_projektantsk": "Ing. Pavol Kaprál",
}


# ============================================================
# DISTRIBUČNÉ SPOLOČNOSTI
# ============================================================
DIS_FULL = {
    "SSD": "Stredoslovenská distribučná, a.s., Pri Rajčianke 2927/8, 010 47 Žilina",
    "VSD": "Východoslovenská distribučná, a.s., Mlynská 31, 042 91 Košice",
    "ZSDIS": "Západoslovenská distribučná, a.s., Čulenova 6, 816 47 Bratislava",
}


def _resolve_dis_from_psc(psc):
    if not psc:
        return ""
    digits = re.sub(r'\D', '', str(psc))
    if not digits:
        return ""
    p = int(digits[:2]) if len(digits) >= 2 else 0
    if p in (1, 2, 3, 96, 97):
        return "SSD"
    if p in (4, 5, 6, 7, 8):
        return "VSD"
    if p in (81, 82, 83, 84, 85, 90, 91, 92, 93, 94, 95):
        return "ZSDIS"
    return ""


# ============================================================
# TECHNICKÉ CENNÍKY — synced z Make Data Store
# ============================================================
PANELY = {
    "JAM72S20 460MR": {"Manufacturer": "JA Solar", "Type": "JAM72S20 460MR", "Dimensions": "2120x1052x40mm",
        "Weight": "25", "IP": "IP68", "Temp": "-40÷85°C", "Class": "Trieda II", "Cell": "6x24 mono",
        "DesignLoad": "3600Pa", "DesignPull": "1600Pa", "Cable": "MC4 4 mm² 2x0,3m", "UN_MAX": "1500",
        "IREV_MAX": "20", "PMPP": "460", "ISC": "11,45", "UOC": "50,01",
        "IMPP": "10,92", "UMPP": "42,13", "Efficiency": "20,6"},
    "LR7-60HVH-535M": {"Manufacturer": "LONGi", "Type": "LR7-60HVH-535M", "Dimensions": "1990x1134x30mm",
        "Weight": "25", "IP": "IP68", "Temp": "-40÷85°C", "Class": "Trieda II", "Cell": "6x20 mono",
        "DesignLoad": "5400Pa", "DesignPull": "2400Pa", "Cable": "MC4 4 mm² 2x1,4m", "UN_MAX": "1500",
        "IREV_MAX": "25", "PMPP": "535", "ISC": "15,15", "UOC": "44,78",
        "IMPP": "14,46", "UMPP": "37,01", "Efficiency": "23,7"},
    "LR7-60HVH-540M": {"Manufacturer": "LONGi", "Type": "LR7-60HVH-540M", "Dimensions": "1990x1134x30mm",
        "Weight": "25", "IP": "IP68", "Temp": "-40÷85°C", "Class": "Trieda II", "Cell": "6x20 mono",
        "DesignLoad": "5400Pa", "DesignPull": "2400Pa", "Cable": "MC4 4 mm² 2x1,4m", "UN_MAX": "1500",
        "IREV_MAX": "25", "PMPP": "540", "ISC": "15,25", "UOC": "44,88",
        "IMPP": "14,55", "UMPP": "37,11", "Efficiency": "23,9"},
    "LR7-54HVH-485M": {"Manufacturer": "LONGi", "Type": "LR7-54HVH-485M", "Dimensions": "1800x1134x30mm",
        "Weight": "21,6", "IP": "IP68", "Temp": "-40÷85°C", "Class": "Trieda II", "Cell": "6x18 mono",
        "DesignLoad": "5400Pa", "DesignPull": "2400Pa", "Cable": "MC4 4 mm² 2x1,2m", "UN_MAX": "1500",
        "IREV_MAX": "25", "PMPP": "485", "ISC": "15,23", "UOC": "40,4",
        "IMPP": "14,53", "UMPP": "33,4", "Efficiency": "23,8"},
}

PANEL_ALIAS = {
    "LONGi 470 Wp": "LR7-54HVH-485M",
    "LONGi 535 Wp": "LR7-60HVH-535M",
    "LONGi 540 Wp": "LR7-60HVH-540M",
    "JA Solar 460 Wp": "JAM72S20 460MR",
}

STRIEDACE = {
    "GW5K-ET": {"Manufacturer": "GoodWe", "Type": "GW5K-ET", "Grid": "Hybrid", "Dimensions": "415x516x180mm",
        "Weight": "24", "IP": "IP66", "Temp": "-35÷60°C", "Humidity": "0÷95%", "Noise": "<30dB",
        "Efficiency": "97,2", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "180",
        "UMPP": "620", "UMPP_MAX": "1000", "IMPP": "12,5", "ISC": "15,2", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "312", "UN_MAX": "528", "PMAX": "5",
        "I_MAX": "8,5", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "16",
        "Cable_AC": "6"},
    "MHT-5K-25": {"Manufacturer": "Solinteg", "Type": "MHT-5K-25", "Grid": "Hybrid", "Dimensions": "534x418x210mm",
        "Weight": "26", "IP": "IP65", "Temp": "-30÷60°C", "Humidity": "0÷100%", "Noise": "<25dB",
        "Efficiency": "98,1", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "135",
        "UMPP": "120", "UMPP_MAX": "950", "IMPP": "15", "ISC": "20", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "380", "UN_MAX": "415", "PMAX": "5",
        "I_MAX": "8,3", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "16",
        "Cable_AC": "6"},
    "MHT-6K-25": {"Manufacturer": "Solinteg", "Type": "MHT-6K-25", "Grid": "Hybrid", "Dimensions": "534x418x210mm",
        "Weight": "26", "IP": "IP65", "Temp": "-30÷60°C", "Humidity": "0÷100%", "Noise": "<25dB",
        "Efficiency": "98,1", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "135",
        "UMPP": "120", "UMPP_MAX": "950", "IMPP": "15", "ISC": "20", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "380", "UN_MAX": "415", "PMAX": "6",
        "I_MAX": "10", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "20",
        "Cable_AC": "6"},
    "MHT-8K-25": {"Manufacturer": "Solinteg", "Type": "MHT-8K-25", "Grid": "Hybrid", "Dimensions": "534x418x210mm",
        "Weight": "26", "IP": "IP65", "Temp": "-30÷60°C", "Humidity": "0÷100%", "Noise": "<25dB",
        "Efficiency": "98,2", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "135",
        "UMPP": "200", "UMPP_MAX": "950", "IMPP": "15", "ISC": "20", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "380", "UN_MAX": "415", "PMAX": "8",
        "I_MAX": "13,3", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "25",
        "Cable_AC": "6"},
    "MHT-10K-25": {"Manufacturer": "Solinteg", "Type": "MHT-10K-25", "Grid": "Hybrid", "Dimensions": "534x418x210mm",
        "Weight": "26", "IP": "IP65", "Temp": "-30÷60°C", "Humidity": "0÷100%", "Noise": "<25dB",
        "Efficiency": "98,2", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "135",
        "UMPP": "200", "UMPP_MAX": "950", "IMPP": "15", "ISC": "20", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "380", "UN_MAX": "415", "PMAX": "10",
        "I_MAX": "16,5", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "32",
        "Cable_AC": "6"},
    "SUN2000-5K": {"Manufacturer": "Huawei", "Type": "SUN2000-5KTL", "Grid": "Hybrid", "Dimensions": "525x470x146,5mm",
        "Weight": "17", "IP": "IP65", "Temp": "-25÷60°C", "Humidity": "0÷100%", "Noise": "<29dB",
        "Efficiency": "97,5", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "200",
        "UMPP": "600", "UMPP_MAX": "1100", "IMPP": "11", "ISC": "15", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "311", "UN_MAX": "478", "PMAX": "5",
        "I_MAX": "8,5", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "16",
        "Cable_AC": "6"},
    "SUN2000-8K": {"Manufacturer": "Huawei", "Type": "SUN2000-8KTL", "Grid": "Hybrid", "Dimensions": "525x470x146,5mm",
        "Weight": "17", "IP": "IP65", "Temp": "-25÷60°C", "Humidity": "0÷100%", "Noise": "<29dB",
        "Efficiency": "98", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "200",
        "UMPP": "600", "UMPP_MAX": "1080", "IMPP": "11", "ISC": "15", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "311", "UN_MAX": "478", "PMAX": "8",
        "I_MAX": "13,5", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "25",
        "Cable_AC": "16"},
    "SUN2000-10K": {"Manufacturer": "Huawei", "Type": "SUN2000-10KTL", "Grid": "Hybrid", "Dimensions": "525x470x146,5mm",
        "Weight": "17", "IP": "IP65", "Temp": "-25÷60°C", "Humidity": "0÷100%", "Noise": "<29dB",
        "Efficiency": "98", "MPPT": "2", "Strings_per_MPPT": "1", "UPV_MIN": "200",
        "UMPP": "600", "UMPP_MAX": "1080", "IMPP": "11", "ISC": "15", "SPD_DC": "Type II",
        "Cable_DC": "6", "UN": "400", "UN_MIN": "311", "UN_MAX": "478", "PMAX": "10",
        "I_MAX": "16,9", "THD": "<3%", "PF": "0,99", "SPD_AC": "Type II", "Protection": "32",
        "Cable_AC": "16"},
}

STRIEDAC_ALIAS = {
    "Solinteg MHT-10K-25": "MHT-10K-25",
    "Solinteg MHT-8K-25": "MHT-8K-25",
    "Solinteg MHT-6K-25": "MHT-6K-25",
    "Solinteg MHT-5K-25": "MHT-5K-25",
    "Huawei SUN2000-5K": "SUN2000-5K",
    "Huawei SUN2000-8K": "SUN2000-8K",
    "Huawei SUN2000-10K": "SUN2000-10K",
    "GoodWe GW5K-ET": "GW5K-ET",
}


def _resolve_panel(typ_panela):
    if not typ_panela:
        return PANELY["LR7-60HVH-535M"]
    if typ_panela in PANELY:
        return PANELY[typ_panela]
    if typ_panela in PANEL_ALIAS:
        return PANELY[PANEL_ALIAS[typ_panela]]
    m = re.search(r'(\d{3})\s*W', typ_panela)
    if m:
        wp = m.group(1)
        for k, v in PANELY.items():
            if v.get("PMPP") == wp:
                return v
    return PANELY["LR7-60HVH-535M"]


def _resolve_striedac(typ_menica):
    if not typ_menica:
        return STRIEDACE["MHT-10K-25"]
    if typ_menica in STRIEDACE:
        return STRIEDACE[typ_menica]
    if typ_menica in STRIEDAC_ALIAS:
        return STRIEDACE[STRIEDAC_ALIAS[typ_menica]]
    for k in STRIEDACE.keys():
        if k.lower() in typ_menica.lower() or typ_menica.lower() in k.lower():
            return STRIEDACE[k]
    return STRIEDACE["MHT-10K-25"]


# ============================================================
# HELPERS
# ============================================================
def _safe(v, fallback=""):
    if v is None or v == "":
        return fallback
    return str(v)


def _sk(value, decimals=2):
    try:
        return f"{float(value):.{decimals}f}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


# ============================================================
# BUILD KONTEXTU pre docxtpl
# ============================================================

def _build_ctx(lead_data):
    """
    Z lead_data zostav celý Jinja2 kontext pre Lukášove projekčné templaty.
    Pokrýva všetkých 146 placeholderov (vrátane tab1-4 pre technické tabuľky).
    """
    # Doplň DIS ak chýba
    dis = _safe(lead_data.get('dis'))
    if not dis:
        dis = _resolve_dis_from_psc(lead_data.get('psc'))

    panel = _resolve_panel(lead_data.get('panel_typ'))
    striedac = _resolve_striedac(lead_data.get('menic'))

    meno = _safe(lead_data.get('meno_priezvisko'))
    # Split meno na (prvé, posledné)
    parts = meno.split()
    meno_zak = parts[0] if parts else ""
    priez_zak = parts[-1] if len(parts) > 1 else ""

    vykon_kwp = lead_data.get('vykon_kwp', 0)
    bateria_kwh = lead_data.get('bateria_kwh', 0)
    pocet_panelov = lead_data.get('pocet_panelov', 0)
    ma_bateriu = (lead_data.get('pocet_baterii') or 0) > 0
    ma_wallbox = lead_data.get('ma_wallbox', False)
    konstrukcia = _safe(lead_data.get('konstrukcia'), "Šikmá strecha (škridla)")

    # Typ (variant + sumár)
    variant = _safe(lead_data.get('variant'), "B")
    typ = f"FVE {_sk(vykon_kwp)} kWp"
    if ma_bateriu:
        typ += f" + BESS {_sk(bateria_kwh)} kWh"
    if ma_wallbox:
        typ += " + Wallbox"

    # SO01
    so01 = f"SO01 — Fotovoltická elektráreň {_sk(vykon_kwp)} kWp"

    # ev_id / číslo zákazky
    ev_id = _safe(lead_data.get('ev_id'), "EV-26-XXX")

    # Adresa klienta
    ulica = _safe(lead_data.get('ulica_cislo'))
    mesto = _safe(lead_data.get('mesto'))
    psc = _safe(lead_data.get('psc'))

    # Prevádzka (default = "Rodinný dom" + adresa klienta)
    prevadzka = _safe(lead_data.get('prevadzka'), "Rodinný dom")
    preulica = _safe(lead_data.get('preulica_a_cislo'), ulica)
    prepsc_mesto = _safe(lead_data.get('prepsc_mesto'), f"{psc} {mesto}".strip())

    ctx = {
        # Zákazník
        "nazov_zakaznika": meno,
        "meno_zak": meno_zak,
        "priez_zak": priez_zak,
        "mail_zak": _safe(lead_data.get('email')),
        "tel_zak": _safe(lead_data.get('telefon')),
        "ico_zak": _safe(lead_data.get('ico_zak')),
        "dic_zak": _safe(lead_data.get('dic_zak')),
        "icdph_zak": _safe(lead_data.get('icdph_zak')),
        "ulica_a_cislo": ulica,
        "psc": psc,
        "mesto": mesto,

        # Prevádzka / Miesto stavby
        "prevadzka": prevadzka,
        "preulica_a_cislo": preulica,
        "prepsc_mesto": prepsc_mesto,
        "parcely": _safe(lead_data.get('parcelne_cisla')),

        # Projekt
        "typ": typ,
        "SO01": so01,
        "stupen_projektu": "DPP — Dokumentácia pre pripojenie",
        "STUPEN_PROJEKTU": "DPP — DOKUMENTÁCIA PRE PRIPOJENIE",
        "OZN": "FVE",
        "cislo_zakazky": ev_id,
        "datum": _safe(lead_data.get('datum_dnes'), datetime.now().strftime("%d.%m.%Y")),
        "dis": dis,

        # Komisia
        **KOMISIA,

        # Technické údaje
        "vykon_ac": _sk(vykon_kwp, 1).replace(",0", ""),  # 10 (zaokrúhlené)
        "vykon_dc": _sk(vykon_kwp),
        "pocet_panel": str(pocet_panelov),
        "vykon_panel": _safe(panel.get("PMPP"), "535"),
        "pocet_menic": "1",
        "oznacenie_menic": _safe(striedac.get("Type"), "MHT-10K-25"),
        "oznacenie_RDC": "RDC1",
        "bateria": _sk(bateria_kwh) if ma_bateriu else "0",
        "typ_panel": f"{panel.get('Manufacturer', 'LONGi')} {panel.get('Type', 'LR7-60HVH-535M')}",
        "typ_konstrukcia": konstrukcia,
        "EIC": _safe(lead_data.get('eic')),
        "EIC1": _safe(lead_data.get('eic_dodavka')),
        "ISC": _safe(striedac.get("ISC"), "15"),
    }

    # ===== Tabuľka 1 — Distribúcia / meranie (technická správa) =====
    ctx.update({
        "tab1_1": dis,
        "tab1_2": DIS_FULL.get(dis, ""),
        "tab1_3": _safe(lead_data.get('eic'), "—"),
        "tab1_4": _safe(lead_data.get('cislo_obch_partnera'), "—"),
        "tab1_5": _safe(lead_data.get('hlavny_istic'), "3x25A"),
        "tab1_6": _safe(lead_data.get('predajca_energii'), "—"),
        "tab1_7": "3+N+PE 400/230V~50Hz TN-C-S",
        "tab1_8": "AC",
        "tab1_9": "Trafostanica VSD",
        "tab1_10": "Existujúca",
        "tab1_11": _safe(lead_data.get('spotreba'), "—") + " kWh/rok",
        "tab1_12": "—",
        "tab1_13": "—",
        "tab1_14": "—",
        "tab1_15": "—",
        "tab1_16": "—",
        "tab1_17": "—",
        "tab1_18": "—",
        "tab1_19": "—",
    })

    # ===== Tabuľka 2 — FV panel parametre =====
    ctx.update({
        "tab2_1": panel.get("Manufacturer", "LONGi"),
        "tab2_2": panel.get("Type", "LR7-60HVH-535M"),
        "tab2_3": panel.get("Cell", "6x20 mono"),
        "tab2_4": panel.get("Dimensions", "1990x1134x30mm"),
        "tab2_5": panel.get("Weight", "25") + " kg",
        "tab2_6": panel.get("IP", "IP68"),
        "tab2_7": panel.get("Temp", "-40÷85°C"),
        "tab2_8": panel.get("Class", "Trieda II"),
        "tab2_9": panel.get("DesignLoad", "5400Pa"),
        "tab2_10": panel.get("DesignPull", "2400Pa"),
        "tab2_11": panel.get("Cable", "MC4 4 mm²"),
        "tab2_12": panel.get("UN_MAX", "1500") + " V",
        "tab2_13": panel.get("IREV_MAX", "25") + " A",
        "tab2_14": panel.get("PMPP", "535") + " Wp",
        "tab2_15": panel.get("UMPP", "37,01") + " V",
        "tab2_16": panel.get("IMPP", "14,46") + " A",
        "tab2_17": panel.get("UOC", "44,78") + " V",
        "tab2_18": panel.get("ISC", "15,15") + " A",
        "tab2_19": panel.get("Efficiency", "23,7") + " %",
        "tab2_20": "—",
        "tab2_21": "—",
        "tab2_22": "—",
        "tab2_23": "—",
        "tab2_24": "—",
        "tab2_25": "—",
        "tab2_26": "—",
        "tab2_27": "—",
        "tab2_28": "—",
        "tab2_29": "—",
    })

    # ===== Tabuľka 3 — Striedač parametre =====
    def _striedac_rows(s):
        return {
            "1": s.get("Manufacturer", ""),
            "2": s.get("Type", ""),
            "3": s.get("Grid", "Hybrid"),
            "4": s.get("Dimensions", ""),
            "5": s.get("Weight", "") + " kg",
            "6": s.get("IP", ""),
            "7": s.get("Temp", ""),
            "8": s.get("Humidity", ""),
            "9": s.get("Noise", ""),
            "10": s.get("Efficiency", "") + " %",
            "11": str(s.get("MPPT", "")),
            "12": str(s.get("Strings_per_MPPT", "")),
            "13": s.get("UPV_MIN", "") + " V",
            "14": s.get("UMPP", "") + " V",
            "15": s.get("UMPP_MAX", "") + " V",
            "16": s.get("IMPP", "") + " A",
            "17": s.get("ISC", "") + " A",
            "18": s.get("SPD_DC", ""),
            "19": s.get("Cable_DC", "") + " mm²",
            "20": s.get("UN", "") + " V",
            "21": s.get("UN_MIN", "") + " V",
            "22": s.get("UN_MAX", "") + " V",
            "23": s.get("PMAX", "") + " kW",
            "24": s.get("I_MAX", "") + " A",
            "25": s.get("THD", ""),
            "26": s.get("PF", ""),
            "27": s.get("SPD_AC", ""),
            "28": s.get("Protection", "") + " A",
            "29": s.get("Cable_AC", "") + " mm²",
        }

    striedac_rows = _striedac_rows(striedac)
    for k, v in striedac_rows.items():
        ctx[f"tab3_{k}"] = v
        ctx[f"tab4_{k}"] = v  # Pre 2. striedač (zatiaľ rovnaký) — neskôr ak je 2. striedač iný, treba rozlíšiť

    return ctx


# ============================================================
# GENEROVANIE — fill Lukášovych templatov cez docxtpl
# ============================================================

def _render_template(template_name, ctx, output_path):
    """Načítaj template, vyplň cez Jinja2, ulož."""
    from docxtpl import DocxTemplate
    template_path = _find_template(template_name)
    doc = DocxTemplate(str(template_path))
    doc.render(ctx)
    doc.save(str(output_path))
    return output_path


def gen_kryci_list(lead_data, output_path):
    ctx = _build_ctx(lead_data)
    return _render_template("Kryc.docx", ctx, output_path)


def gen_titulny_list(lead_data, output_path):
    ctx = _build_ctx(lead_data)
    return _render_template("Tit.docx", ctx, output_path)


def gen_zoznam_dokumentacie(lead_data, output_path):
    ctx = _build_ctx(lead_data)
    return _render_template("Zoz.docx", ctx, output_path)


def gen_pouvv(lead_data, output_path):
    ctx = _build_ctx(lead_data)
    return _render_template("POUVV.docx", ctx, output_path)


def gen_suhrnna_sprava(lead_data, output_path):
    ctx = _build_ctx(lead_data)
    return _render_template("suhr_technicka_sprava.docx", ctx, output_path)


def gen_technicka_sprava(lead_data, output_path):
    """Vyber správnu verziu podľa DIS."""
    ctx = _build_ctx(lead_data)
    dis = ctx.get("dis", "").upper()
    if dis == "SSD":
        template = "technicka_spravaSSD.docx"
    elif dis == "VSD":
        template = "technicka_spravaVSD.docx"
    elif dis == "ZSDIS":
        template = "technicka_spravaZSDIS.docx"
    else:
        template = "technicka_sprava.docx"
    return _render_template(template, ctx, output_path)


# ============================================================
# MASTER ENTRY POINT
# ============================================================

def vygeneruj_projektovu_dokumentaciu(lead_data, out_dir, solaredge_pdf_bytes=None):
    """
    Vyrobí kompletný balík PD pre malý zdroj do 10 kW.
    Vyplní Lukášove templaty z `Projekcia/` cez docxtpl.

    Returns: dict {kluc: path} s 6-7 dokumentmi (5-6 docx + výkres).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    priezvisko = lead_data.get('meno_priezvisko', 'Klient').split()[-1] if lead_data.get('meno_priezvisko') else 'Klient'
    base = re.sub(r'[^A-Za-zÁ-ž0-9]+', '_', priezvisko).strip('_') or 'Klient'
    ev_id = lead_data.get('ev_id', 'EV-XX')

    # Doplniť DIS ak chýba
    if not lead_data.get('dis'):
        psc_guess = _resolve_dis_from_psc(lead_data.get('psc'))
        if psc_guess:
            lead_data['dis'] = psc_guess

    out = {}
    try:
        out['kryci'] = gen_kryci_list(lead_data, out_dir / f"{ev_id}_PD_01_Kryci_list_{base}.docx")
        out['titulny'] = gen_titulny_list(lead_data, out_dir / f"{ev_id}_PD_02_Titulny_list_{base}.docx")
        out['zoznam'] = gen_zoznam_dokumentacie(lead_data, out_dir / f"{ev_id}_PD_03_Zoznam_dokumentacie_{base}.docx")
        out['pouvv'] = gen_pouvv(lead_data, out_dir / f"{ev_id}_PD_04_PoUVV_{base}.docx")
        out['suhrnna'] = gen_suhrnna_sprava(lead_data, out_dir / f"{ev_id}_PD_05_Suhrnna_sprava_{base}.docx")
        out['technicka'] = gen_technicka_sprava(lead_data, out_dir / f"{ev_id}_PD_06_Technicka_sprava_{base}.docx")
    except Exception as e:
        log.exception("[pd] template fill zlyhal: %s", e)
        raise

    # 7. Technický výkres (ak je SolarEdge PDF)
    if solaredge_pdf_bytes:
        try:
            from solar_vykres import vyrob_z_bytes
            vykres_path = out_dir / f"{ev_id}_PD_07_Vykres_FVE_{base}.pdf"
            vyrob_z_bytes(solaredge_pdf_bytes, lead_data, vykres_path)
            out['vykres'] = vykres_path
            log.info("[pd] technický výkres pridaný")
        except Exception as e:
            log.warning("[pd] technický výkres zlyhal: %s", e)

    log.info("[pd] vygenerovaných %d dokumentov pre %s", len(out), priezvisko)
    return out
