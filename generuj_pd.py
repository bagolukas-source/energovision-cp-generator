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

from pd_catalog_db import get_catalog  # PD katalóg z dedikovanej Supabase DB (fallback pd_catalog.py)
PANELY, STRIEDACE, _ALIAS_PANEL_DB, _ALIAS_STRIEDAC_DB = get_catalog()

# ============================================================
# PATH KU TEMPLATOM
# ============================================================
# Render: templaty sú v repo `Projekcia/` na úrovni vyššie alebo v `templates_projekcia/`
HERE = Path(__file__).resolve().parent
# Možné cesty (skúsi v poradí)
PROJEKCIA_DIRS = [
    HERE / "templates_projekcia",                 # primary — pri pushi
    HERE / "templates_admin",                     # admin/úradné dokumenty (sada per stupeň)
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
# PANELY — kompletný katalóg v pd_catalog.py (importované nižšie)

PANEL_ALIAS = {
    "LONGi 470 Wp": "LR7-54HVH-485M",
    "LONGi 535 Wp": "LR7-60HVH-535M",
    "LONGi 540 Wp": "LR7-60HVH-540M",
    "JA Solar 460 Wp": "JAM72S20 460MR",
}

# STRIEDACE — kompletný katalóg v pd_catalog.py (importované nižšie)

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

# zlúč aliasy z DB (ak nejaké) nad statické
PANEL_ALIAS.update(_ALIAS_PANEL_DB)
STRIEDAC_ALIAS.update(_ALIAS_STRIEDAC_DB)


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
    # výkonový token (…-150K-… / 10K): kandidát s rovnakým K-tokenom je lepší než slepý 10K default
    m = re.search(r'(\d{1,3}(?:[.,]\d)?)\s*K', typ_menica.upper())
    if m:
        tok = m.group(1).replace(",", ".")
        for k, v in STRIEDACE.items():
            if re.search(r'\b' + re.escape(tok) + r'\s*K', k.upper()) or str(v.get("PMAX", "")).strip() == tok:
                log.warning("[pd] menič '%s' nie je v katalógu — použitý výkonový ekvivalent '%s'", typ_menica, k)
                return v
    log.warning("[pd] menič '%s' nie je v katalógu — fallback MHT-10K-25 (SKONTROLUJ výkon AC!)", typ_menica)
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


def _ciarka_ak(hodnota, sep=", "):
    """Pomocný placeholder pre šablóny, ktoré majú medzi dvoma poľami PEVNÚ čiarku
    v XML (napr. '{{preulica_a_cislo}}, {{prepsc_mesto}}'). Vráti sep len keď je
    predchádzajúce pole neprázdne — inak prázdny reťazec (žiadna visiaca ', ')."""
    return sep if hodnota and str(hodnota).strip() else ""


# ============================================================
# BUILD KONTEXTU pre docxtpl
# ============================================================

def _stupen_skratka(stupen):
    """Skratka stupňa PD (Make: Stupenskr) — RP/PSO/PSZaPS/RP ASDR/DSV."""
    s = (stupen or "").upper()
    if "ASDR" in s: return "RP ASDR"
    if "PSZ" in s: return "PSZaPS"
    if "PSO" in s: return "PSO"
    if "DSV" in s or "SKUTO" in s: return "DSV"
    return "RP"


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

    # Meniče (1-3) ako v Make: typ_menic1/2/3 + pocet_menic1/2/3 → Výkon AC = Σ PMAX×počet
    def _numpd(v):
        try:
            return float(str(v).replace(",", ".").strip())
        except Exception:
            return 0.0
    _inverters = []
    for _i in (1, 2, 3):
        _typ = lead_data.get('typ_menic%d' % _i)
        if _typ:
            _cnt = int(_numpd(lead_data.get('pocet_menic%d' % _i)) or 1)
            _inverters.append((_resolve_striedac(_typ), max(_cnt, 1)))
    _has_multi = len(_inverters) > 0
    if not _inverters:
        _inverters = [(striedac, 1)]
    striedac = _inverters[0][0]  # primárny menič = prvý (detailné tabuľky/ISC)
    _vykon_ac_calc = round(sum(_numpd(s.get("PMAX")) * c for s, c in _inverters), 1)
    _pocet_menic_total = sum(c for _, c in _inverters)
    _oznacenie_menic = " + ".join(
        ((str(c) + "× ") if c > 1 else "") + _safe(s.get("Type"), "menič") for s, c in _inverters
    )
    # Rozbalený zoznam JEDNOTLIVÝCH meničov (fyzické obvody FA1/FA2/... v revíznej správe) —
    # napr. 2× SUN2000-50KTL → ["SUN2000-50KTL", "SUN2000-50KTL"]. Použité pre per-obvod placeholdery.
    _menice_jednotlivo = []
    for _s, _c in _inverters:
        for _ in range(_c):
            _menice_jednotlivo.append(_safe(_s.get("Type"), "menič"))

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
        # Štatutárny orgán (mená a priezviská) — LEN z výslovného payload poľa `statutar`.
        # Žiadny fallback na meno_zak/priez_zak: pre B2B firmu by to split-lo obchodné meno
        # (napr. "DEMO-A2 s.r.o." → meno_zak="DEMO-A2", priez_zak="s.r.o.") a dalo nezmyselný
        # výsledok do úradného tlačiva URSO. Prázdne pole je čestné — projektant ho ručne doplní.
        "statutar": _safe(lead_data.get('statutar')),
        "mail_zak": _safe(lead_data.get('email')),
        "email_obchodnik": _safe(lead_data.get('email_obchodnik')),
        "tel_zak": _safe(lead_data.get('telefon')),
        "ico_zak": _safe(lead_data.get('ico_zak')),
        "dic_zak": _safe(lead_data.get('dic_zak')),
        "icdph_zak": _safe(lead_data.get('icdph_zak')),
        "ulica_a_cislo": ulica,
        "psc": psc,
        "mesto": mesto,
        "psc_mesto": f"{psc} {mesto}".strip(),
        "mesto_zak": mesto,
        "cena": _safe(lead_data.get('cena')),
        # Čiarka medzi ulicou a mestom v šablónach, kde je natvrdo v XML ('{{ulica_a_cislo}}, {{psc_mesto}}') —
        # zabráni visiacej ", " keď ulica/adresa chýba (napr. B2B projekt bez vyplnenej ulice).
        "ciarka_ulica": _ciarka_ak(ulica),

        # Prevádzka / Miesto stavby
        "prevadzka": prevadzka,
        "preulica_a_cislo": preulica,
        "prepsc_mesto": prepsc_mesto,
        "ciarka_preulica": _ciarka_ak(preulica),
        "parcely": _safe(lead_data.get('parcelne_cisla')),

        # Projekt
        "typ": typ,
        "SO01": so01,
        "stupen_projektu": _safe(lead_data.get("stupen_projektu"), "DPP — Dokumentácia pre pripojenie"),
        "STUPEN_PROJEKTU": _safe(lead_data.get("stupen_projektu"), "DPP — Dokumentácia pre pripojenie").upper(),
        "OZN": _stupen_skratka(lead_data.get("stupen_projektu")),
        "cislo_zakazky": ev_id,
        "datum": _safe(lead_data.get('datum_dnes'), datetime.now().strftime("%d.%m.%Y")),
        "dis": dis,

        # Komisia
        **KOMISIA,

        # Technické údaje
        "vykon_ac": (_sk(_vykon_ac_calc, 1).replace(",0", "") if _has_multi else _sk(vykon_kwp, 1).replace(",0", "")),
        "vykon_dc": _sk(vykon_kwp),
        "pocet_panel": str(pocet_panelov),
        "vykon_panel": _safe(panel.get("PMPP"), "535"),
        "pocet_menic": (str(_pocet_menic_total) if _has_multi else "1"),
        "oznacenie_menic": (_oznacenie_menic if _has_multi else _safe(striedac.get("Type"), "MHT-10K-25")),
        "oznacenie_RDC": "RDC1",
        "bateria": _safe(lead_data.get("bateria_typ")) or (_sk(bateria_kwh) + " kWh" if ma_bateriu else "—"),
        "ma_bateriu": ma_bateriu,
        "typ_panel": f"{panel.get('Manufacturer', 'LONGi')} {panel.get('Type', 'LR7-60HVH-535M')}",
        "typ_konstrukcia": konstrukcia,
        "EIC": _safe(lead_data.get('eic')),
        "EIC1": _safe(lead_data.get('eic_dodavka')),
        "ISC": _safe(panel.get("ISC"), "15,15"),  # Make: 644.ISC = panel
        # Trafostanica — voliteľné pole z CRM (zatiaľ nie je v customers/projects schéme);
        # keď chýba, neutrálny text namiesto natvrdo vpísanej TS z cudzieho referenčného projektu.
        "trafostanica": _safe(lead_data.get('trafostanica'), "TS podľa vyjadrenia PDS"),

        # Per-obvod menič (revízna správa — tabuľka FA1/FA2/…): jednotlivé kusy, nie skupiny.
        # Fallback na primárny menič, keď je obvodov viac než reálnych meničov (radšej správny typ než "—").
        "oznacenie_menic_fa1": _menice_jednotlivo[0] if len(_menice_jednotlivo) > 0 else _safe(striedac.get("Type"), "menič"),
        "oznacenie_menic_fa2": _menice_jednotlivo[1] if len(_menice_jednotlivo) > 1 else (_menice_jednotlivo[0] if _menice_jednotlivo else _safe(striedac.get("Type"), "menič")),
        "oznacenie_menic_fa3": _menice_jednotlivo[2] if len(_menice_jednotlivo) > 2 else "",
        "oznacenie_menic_fa4": _menice_jednotlivo[3] if len(_menice_jednotlivo) > 3 else "",
    }

    # ============================================================
    # Tabuľky technickej správy — číslovanie PODĽA ŠABLÓN (overené z DOCX):
    #   tab1 = FV PANEL (19 polí), tab2/tab3/tab4 = MENIČ 1/2/3 (27 polí + 28=ks, 29=výkon)
    # Jednotky (kg/V/A/%/mm²/„Typ ") sú V ŠABLÓNE — sem idú len čisté hodnoty.
    # ============================================================

    # ===== tab1 — FV panel =====
    ctx.update({
        "tab1_1": panel.get("Manufacturer", "LONGi"),
        "tab1_2": panel.get("Type", "LR7-60HVH-535M"),
        "tab1_3": panel.get("Dimensions", "1990x1134x30mm"),
        "tab1_4": panel.get("Weight", "25"),
        "tab1_5": panel.get("IP", "IP68"),
        "tab1_6": panel.get("Temp", "-40÷85°C"),
        "tab1_7": panel.get("Class", "Trieda II"),
        "tab1_8": panel.get("Cell", "6x20 monokryštál"),
        "tab1_9": panel.get("DesignLoad", "5400Pa"),
        "tab1_10": panel.get("DesignPull", "2400Pa"),
        "tab1_11": panel.get("UN_MAX", "1500"),
        "tab1_12": panel.get("IREV_MAX", "25"),
        "tab1_13": panel.get("Cable", "MC4"),
        "tab1_14": panel.get("PMPP", "535"),
        "tab1_15": panel.get("ISC", "15,15"),
        "tab1_16": panel.get("UOC", "44,78"),
        "tab1_17": panel.get("IMPP", "14,46"),
        "tab1_18": panel.get("UMPP", "37,01"),
        "tab1_19": panel.get("Efficiency", "23,7"),
    })

    # ===== tab2/tab3/tab4 — meniče 1..3 =====
    def _striedac_tab(s, count):
        return {
            "1": s.get("Manufacturer", ""),
            "2": s.get("Type", ""),
            "3": s.get("Dimensions", ""),
            "4": s.get("Weight", ""),
            "5": s.get("IP", ""),
            "6": s.get("Temp", ""),
            "7": s.get("Humidity", ""),
            "8": s.get("Noise", ""),
            "9": s.get("Efficiency", ""),
            "10": s.get("SPD_DC", "2"),
            "11": str(s.get("MPPT", "")),
            "12": str(s.get("Strings_per_MPPT", "")),
            "13": s.get("UPV_MIN", ""),
            "14": s.get("UMPP", ""),
            "15": s.get("UMPP_MAX", ""),
            "16": s.get("IMPP", ""),
            "17": s.get("ISC", ""),
            "18": s.get("Cable_DC", ""),
            "19": s.get("SPD_AC", "2"),
            "20": s.get("UN", "400"),
            "21": s.get("UN_MIN", ""),
            "22": s.get("UN_MAX", ""),
            "23": s.get("THD", ""),
            "24": s.get("PF", ""),
            "25": s.get("PMAX", ""),
            "26": s.get("I_MAX", ""),
            "27": s.get("Cable_AC", ""),
            "28": str(count),
            "29": (s.get("PMAX", "") + "kW") if s.get("PMAX") else "",
        }

    for _idx in range(3):
        _key = "tab%d" % (_idx + 2)
        if _idx < len(_inverters):
            _s, _c = _inverters[_idx]
            _rows = _striedac_tab(_s, _c)
        else:
            _rows = {str(_i): "—" for _i in range(1, 30)}
        for _k, _v in _rows.items():
            ctx["%s_%s" % (_key, _k)] = _v

    return ctx


# ============================================================
# GENEROVANIE — fill Lukášovych templatov cez docxtpl
# ============================================================

def _render_template(template_name, ctx, output_path, prefer_subdir=None):
    """Načítaj template, vyplň cez Jinja2, ulož.
    prefer_subdir: skús najprv variant v podadresári (napr. 'revpro' — sada
    so zákazníckou hlavičkou); keď tam nie je, použi štandardnú šablónu."""
    from docxtpl import DocxTemplate
    template_path = None
    if prefer_subdir:
        try:
            template_path = _find_template(str(Path(prefer_subdir) / template_name))
        except FileNotFoundError:
            template_path = None
    if template_path is None:
        template_path = _find_template(template_name)
    doc = DocxTemplate(str(template_path))
    doc.render(ctx, autoescape=True)
    doc.save(str(output_path))
    return output_path


def _je_revpro(lead_data):
    """Hlavička PD ≠ Energovision → sada šablón so zákazníckou hlavičkou (revpro/)."""
    h = (lead_data.get("hlavicka_pd") or "energovision").strip().lower()
    return h not in ("", "energovision", "ev")


def _vykon_ac_kw(lead_data):
    """Σ PMAX × počet cez typ_menic1-3 (ako Make modul 602). Fallback: 0."""
    def _num(v):
        try:
            return float(str(v).replace(",", ".").strip())
        except Exception:
            return 0.0
    total = 0.0
    for i in (1, 2, 3):
        typ = lead_data.get("typ_menic%d" % i)
        if typ:
            s = _resolve_striedac(typ)
            total += _num(s.get("PMAX")) * max(int(_num(lead_data.get("pocet_menic%d" % i)) or 1), 1)
    if not total and lead_data.get("menic"):
        total = _num(_resolve_striedac(lead_data.get("menic")).get("PMAX"))
    return round(total, 1)


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


# ============================================================
# B2B PD (firma) — nové šablóny z kitu „Generovanie PD"
# Jadro PD = Tit_Zoz_POUVV + technicka_sprava_b2b (DIS ako text {{dis}}).
# stupen_projektu prichádza z CRM (odvodené z process_templates: RP/PSO/PSZaPS).
# Tech-listy (datasheety) prikladá volajúci (webhook) po vygenerovaní.
# ============================================================

def gen_tit_zoz_pouvv_b2b(lead_data, output_path):
    return _render_template("Tit_Zoz_POUVV.docx", _build_ctx(lead_data), output_path)


def gen_technicka_sprava_b2b(lead_data, output_path):
    return _render_template("technicka_sprava_b2b.docx", _build_ctx(lead_data), output_path)


def vygeneruj_pd_b2b(lead_data, out_dir):
    """B2B PD jadro: Titul+Zoznam+PoUVV + Technická správa. Vráti {kluc: path}."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    priezvisko = lead_data.get('meno_priezvisko', 'Klient').split()[-1] if lead_data.get('meno_priezvisko') else 'Klient'
    base = re.sub(r'[^A-Za-zÁ-ž0-9]+', '_', priezvisko).strip('_') or 'Klient'
    ev_id = lead_data.get('ev_id', 'EV-XX')
    if not lead_data.get('dis'):
        g = _resolve_dis_from_psc(lead_data.get('psc'))
        if g:
            lead_data['dis'] = g
    out = {}
    out['titul_zoznam_pouvv'] = gen_tit_zoz_pouvv_b2b(lead_data, out_dir / f"{ev_id}_PD_01_Titul_Zoznam_PoUVV_{base}.docx")
    out['technicka'] = gen_technicka_sprava_b2b(lead_data, out_dir / f"{ev_id}_PD_02_Technicka_sprava_{base}.docx")
    log.info("[pd-b2b] %d dokumentov pre %s (stupeň=%s, dis=%s)", len(out), priezvisko,
             lead_data.get('stupen_projektu'), lead_data.get('dis'))
    return out


def vygeneruj_pd_dsv(lead_data, out_dir):
    """DSV — Dokumentácia skutočného vyhotovenia. Texty v stavovom/minulom čase
    (šablóny *_DSV), stupeň DSV, + preberacie protokoly. Vráti {kluc: path}."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    lead = dict(lead_data)
    lead["stupen_projektu"] = "DSV (Dokumentácia skutočného vyhotovenia)"
    if not lead.get('dis'):
        g = _resolve_dis_from_psc(lead.get('psc'))
        if g:
            lead['dis'] = g
    priezvisko = lead.get('meno_priezvisko', 'Klient').split()[-1] if lead.get('meno_priezvisko') else 'Klient'
    base = re.sub(r'[^A-Za-zÁ-ž0-9]+', '_', priezvisko).strip('_') or 'Klient'
    ev_id = lead.get('ev_id', 'EV-XX')
    ctx = _build_ctx(lead)
    out = {}
    out['titul_zoznam_pouvv'] = _render_template("Tit_Zoz_POUVV.docx", ctx, out_dir / f"{ev_id}_DSV_01_Titul_Zoznam_PoUVV_{base}.docx")
    out['technicka'] = _render_template("technicka_sprava_b2b_DSV.docx", ctx, out_dir / f"{ev_id}_DSV_02_Technicka_sprava_{base}.docx")
    out['suhrnna'] = _render_template("suhr_technicka_sprava_DSV.docx", ctx, out_dir / f"{ev_id}_DSV_03_Suhrnna_technicka_sprava_{base}.docx")
    try:
        out['preberaci_komponenty'] = _render_template("Preberaci_protokol_komponenty.docx", ctx, out_dir / f"{ev_id}_DSV_04_Preberaci_protokol_komponenty_{base}.docx")
        out['preberaci_final'] = _render_template("Preberaci_protokol_final.docx", ctx, out_dir / f"{ev_id}_DSV_05_Preberaci_protokol_final_{base}.docx")
    except Exception as e:
        log.warning("[pd-dsv] preberacie protokoly zlyhali: %s", e)
    log.info("[pd-dsv] %d dokumentov pre %s", len(out), priezvisko)
    return out


# ============================================================
# SADA PD per stupeň (RP/PSO/PSZaPS/RP ASDR/DSV) — jadro + admin/úradné dokumenty
# Rozcestník zodpovedá Make (process_templates): nad 100 kW = URSO + IFT dátové prenosy.
# ============================================================

_POTVRDENIE_OCHRANA = {
    "SSD": "Potvrdenie_ochrana_SSD.docx",
    "VSD": "Potvrdenie_ochrana_VSD.docx",
    "ZSDIS": "Potvrdenie_ochrana_ZSDIS.docx",
}


def _protokol_ochrany_pdf(dis, ctx):
    """(template_rel, fields) pre AcroForm protokol ochrany per DIS — mapovanie z Make blueprintu.
    SSD je XFA formulár (nevyplniteľný pypdf) → None; namiesto neho ide editable DOCX."""
    sidlo = f'{ctx.get("ulica_a_cislo","")}, {ctx.get("psc_mesto","")}'.strip(", ")
    prev = f'{ctx.get("preulica_a_cislo","")}, {ctx.get("prepsc_mesto","")}, {ctx.get("parcely","")}'.strip(", ")
    if dis == "ZSDIS":
        return "protokoly/ZSD_protokol_ochrany.pdf", {
            "Telefon_1": ctx.get("tel_zak", ""), "Email_1": ctx.get("mail_zak", ""),
            "ICO_1": ctx.get("ico_zak", ""), "EIC_2": ctx.get("EIC", ""),
            "Obchodne_meno_meno_priezvisko_1": ctx.get("nazov_zakaznika", ""),
            "Sidlo_1": sidlo, "Adresa_2": prev,
        }
    if dis == "VSD":
        return "protokoly/VSD_protokol_ochrany.pdf", {
            "Text Field 2": ctx.get("nazov_zakaznika", ""),
            "Text Field 21": ctx.get("tel_zak", ""), "Text Field 20": ctx.get("mail_zak", ""),
            "Text Field 22": prev, "Text Field 18": sidlo,
            "Text Field 19": ctx.get("ico_zak", ""), "Text Field 23": ctx.get("EIC", ""),
        }
    return None, None


def vygeneruj_pd_sada(lead_data, out_dir):
    """Kompletná sada PD — parita s Make „Komplet automatizácia Firma":
    jadro (krycí + titul/zoznam/PoUVV + súhrnná + technická) + Pomocný word (master)
    + RDC schémy podľa fg_sum (DC rozvádzač) + úradné dokumenty podľa DIS a AC výkonu.
    hlavicka_pd ≠ energovision → sada šablón revpro/ (zákaznícka hlavička).
    Vráti {kluc: path}."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    if not lead_data.get('dis'):
        g = _resolve_dis_from_psc(lead_data.get('psc'))
        if g:
            lead_data['dis'] = g
    dis = (lead_data.get('dis') or "").upper()
    ac = _vykon_ac_kw(lead_data)
    revpro = _je_revpro(lead_data)
    sub = "revpro" if revpro else None
    ctx = _build_ctx(lead_data)

    priezvisko = lead_data.get('meno_priezvisko', 'Klient').split()[-1] if lead_data.get('meno_priezvisko') else 'Klient'
    base = re.sub(r'[^A-Za-zÁ-ž0-9]+', '_', priezvisko).strip('_') or 'Klient'
    ev_id = lead_data.get('ev_id', 'EV-XX')

    out = {}
    n = 0

    def add_docx(kluc, label, template, subdir=sub):
        nonlocal n
        n += 1
        try:
            path = out_dir / f"{ev_id}_PD_{n:02d}_{label}_{base}.docx"
            out[kluc] = _render_template(template, ctx, path, prefer_subdir=subdir)
        except Exception as e:
            log.warning("[pd-sada] %s (%s) zlyhal: %s", kluc, template, e)

    # ── jadro PD (Make: vždy) ──
    add_docx('kryci', 'Kryci_list', 'Kryc.docx')
    if revpro:
        # zákaznícka sada nemá zlúčený Tit_Zoz_POUVV — generujú sa samostatne ako v Make
        add_docx('titulny', 'Titulny_list', 'Tit.docx')
        add_docx('zoznam', 'Zoznam_dokumentacie', 'Zoz.docx')
        add_docx('pouvv', 'PoUVV', 'POUVV.docx')
    else:
        add_docx('titul_zoznam_pouvv', 'Titul_Zoznam_PoUVV', 'Tit_Zoz_POUVV.docx')
    add_docx('suhrnna', 'Suhrnna_sprava', 'suhr_technicka_sprava.docx')
    # technická správa: AC ≥ 100 kW → per-DIS šablóna (Make), inak jednotná b2b
    if ac >= 100 and dis in ("SSD", "VSD", "ZSDIS"):
        add_docx('technicka', 'Technicka_sprava', f'technicka_sprava{dis}.docx')
    else:
        add_docx('technicka', 'Technicka_sprava', 'technicka_sprava.docx' if revpro else 'technicka_sprava_b2b.docx')
    # Pomocný word — záloha všetkých vstupov (Make: vždy)
    add_docx('master', 'Pomocny_word', 'Pomocny word.docx', subdir=None)

    # ── schémy (PDF) ──
    def _n(v):
        try:
            return int(float(str(v).replace(",", ".")))
        except Exception:
            return 0
    fg = _n(lead_data.get('fg_sum')) or _n(ctx.get('pocet_menic')) or 1
    stupen_full = ctx.get('stupen_projektu', '')
    stupen_skr = ctx.get('OZN', '')

    # RDC schémy — DC rozvádzač=Áno → 1 schéma na menič (fg_sum), ako Make repeater
    if lead_data.get('dc_rozvadzac'):
        try:
            import pdf_forms as PF
            strings_per = _n(lead_data.get('stringov_na_rdc')) or 2
            avail = [1, 2, 4, 6, 10, 12]
            cfg = "2x%d" % min(avail, key=lambda x: abs(x - max(1, strings_per)))
            for i in range(1, min(fg, 12) + 1):
                n += 1
                pdf = PF.vyplň_rdc(ctx, cfg, i, stupen_full=stupen_full, stupen_skr=stupen_skr)
                p = out_dir / f"{ev_id}_PD_{n:02d}_Schema_RDC{i}_{base}.pdf"
                p.write_bytes(pdf)
                out['rdc_%d' % i] = p
        except Exception as e:
            log.warning("[pd-sada] RDC schémy zlyhali: %s", e)

    # RFV schéma zapojenia — Make tabuľka: AC≤50 → S1/S2 (FG 1/2), M3/M4 (FG 3/4);
    # 50<AC<100 → M1..M4 podľa FG (FG>4 → M4); AC≥100 → žiadna RFV.
    # (FG>4 pri AC≤50 nemal v Make route — dopĺňame M4, logujeme.)
    try:
        import pdf_forms as PF
        rfv = None
        if ac <= 50:
            rfv = {1: "S1", 2: "S2", 3: "M3"}.get(fg) or ("M4" if fg >= 4 else None)
            if fg > 4:
                log.warning("[pd-sada] AC≤50 s FG=%d nemal v Make vetvu — použitá M4", fg)
        elif ac < 100:
            rfv = {1: "M1", 2: "M2", 3: "M3"}.get(fg) or ("M4" if fg >= 4 else None)
        if rfv:
            n += 1
            pdf = PF.vyplň_rfv(ctx, rfv, stupen_full=stupen_full, stupen_skr=stupen_skr)
            p = out_dir / f"{ev_id}_PD_{n:02d}_Schema_zapojenia_RFV_{rfv}_{base}.pdf"
            p.write_bytes(pdf)
            out['rozvadzac'] = p
    except Exception as e:
        log.warning("[pd-sada] RFV schéma zlyhala: %s", e)

    # DWG podklady (DISP + JPS) — Make ich prikladal len pri zákazníckej hlavičke (revpro)
    if revpro:
        for kluc, fname in (("dwg_disp", "24-460 DISP.dwg"), ("dwg_jps", "24-460 JPS.dwg")):
            try:
                src = _find_template(str(Path("revpro") / fname))
                n += 1
                p = out_dir / f"{ev_id}_PD_{n:02d}_{fname.replace(' ', '_')}"
                p.write_bytes(Path(src).read_bytes())
                out[kluc] = p
            except Exception as e:
                log.warning("[pd-sada] DWG %s zlyhal: %s", fname, e)

    # ── úradné/admin dokumenty (templates_admin, bez revpro variantov) ──
    admin = []
    # potvrdenie ochrany per DIS — Make pásma: SSD vždy; ZSDIS/VSD len 10 < AC < 100
    if dis == "SSD" or (dis in ("VSD", "ZSDIS") and 10 < ac < 100):
        admin.append(('potvrdenie_ochrana', _POTVRDENIE_OCHRANA[dis], 'Potvrdenie_ochrana_' + dis))
    admin.append(('vyhlasenie_projektant', 'Vyhlasenie_projektant.docx', 'Vyhlasenie_projektant'))
    admin.append(('revizna_sprava', 'Revizna_sprava_FVZ.docx', 'Revizna_sprava_FVZ'))
    # URSO — Make: vždy (priečinok podľa AC rieši CRM register)
    admin.append(('urso_oznamovacia', 'URSO_oznamovacia_povinnost.docx', 'URSO_oznamovacia_povinnost'))
    admin.append(('urso_vyroba_lz', 'URSO_vyroba_LZ.docx', 'URSO_vyroba_LZ'))
    # IFT dátové prenosy — Make: len ZSDIS ∧ AC ≥ 100
    if dis == "ZSDIS" and ac >= 100:
        admin.append(('ift_zmluva', 'IFT_zmluva_datove_prenosy.docx', 'IFT_zmluva'))
        admin.append(('ift_priloha', 'IFT_priloha_1.docx', 'IFT_priloha_1'))
    # preberacie protokoly — Make: vždy (filtre 114/115 prázdne)
    admin.append(('preberaci_komponenty', 'Preberaci_protokol_komponenty.docx', 'Preberaci_protokol_komponenty'))
    admin.append(('preberaci_final', 'Preberaci_protokol_final.docx', 'Preberaci_protokol_final'))
    # SSD protokol funkčnej skúšky — Make mal XFA PDF (nevyplniteľné), tu editable DOCX
    if dis == "SSD":
        admin.append(('protokol_skuska', 'SSD_protokol_funkcna_skuska.docx', 'SSD_protokol_funkcna_skuska'))
    for kluc, fname, label in admin:
        add_docx(kluc, label, fname, subdir=None)

    # IFT cenník (statický XLSX — Príloha č. 2)
    if dis == "ZSDIS" and ac >= 100:
        try:
            n += 1
            src = _find_template('IFT_priloha_2_cennik.xlsx')
            p = out_dir / f"{ev_id}_PD_{n:02d}_IFT_priloha_2_cennik_{base}.xlsx"
            p.write_bytes(Path(src).read_bytes())
            out['ift_cennik'] = p
        except Exception as e:
            log.warning("[pd-sada] IFT cenník zlyhal: %s", e)

    # PDF protokol ochrany (AcroForm) — ZSDIS/VSD v pásme 10–100 kW
    if dis in ("ZSDIS", "VSD") and 10 < ac < 100:
        try:
            import pdf_forms as PF
            rel, fields = _protokol_ochrany_pdf(dis, ctx)
            if rel:
                n += 1
                p = out_dir / f"{ev_id}_PD_{n:02d}_Protokol_ochrany_{dis}_{base}.pdf"
                p.write_bytes(PF.fill_pdf(rel, fields))
                out['protokol_ochrany'] = p
        except Exception as e:
            log.warning("[pd-sada] protokol ochrany %s zlyhal: %s", dis, e)

    log.info("[pd-sada] %d dokumentov (stupeň=%s, dis=%s, AC=%.1f kW, revpro=%s)",
             len(out), lead_data.get('stupen_projektu'), dis, ac, revpro)
    return out
