"""
generate_from_notion.py — generuje multi-variantne ponuky z Notion DB záznamu.

Vstup: Notion page properties (dict)
Výstup: 1-3 ponuky (Variant A/B/C) v projektovom folderi + interná kalkulácia

Použitie: import a volanie generate_for_record(notion_record_dict)
"""

import json, os, sys, datetime, re, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_cp_html import main as generate_cp_html_main, vyrob_html_pdf, vyrob_eml_v2
from generate_cp import (
    load_cennik, vyrataj_konfig, vyrataj_ceny, vyrataj_navratnost,
    vyrob_grafy, vyrob_internu_kalkulaciu, DEFAULTS
)

# Mapovanie Notion select hodnôt → kódy v Cenníku
PANEL_MAP = {
    "LONGi 470 Wp": "PAN-001",
    "LONGi 535 Wp": "PAN-002",  # PAN-002 je v cenniku 535-545 Wp panel
    "LONGi 540 Wp": "PAN-002",
}
INVERTOR_MAP = {
    "Solinteg MHT-10K-25": "INV-001",
    "Huawei SUN2000-5K": "INV-002",
    "Huawei SUN2000-6K": "INV-003",
    "Huawei SUN2000-8K": "INV-004",
    "Huawei SUN2000-10K": "INV-005",
    "GoodWe GW6000-ET": "INV-006",
    "GoodWe GW8000-ET": "INV-007",
    "GoodWe GW10K-ET": "INV-008",
}
KONSTRUKCIA_MAP = {
    "Škridla": "KON-001",
    "Plech kombivrut": "KON-002",
    "Falcový plech": "KON-003",
    "Plochá strecha — J 13°": "KON-004",
    "Plochá strecha — V/Z 10°": "KON-005",
}
BATERIA_MAP = {
    "Pylontech Force H3 — 5.12 kWh": "BAT-001",
    "Solinteg EBA B5K1 — 5.12 kWh": "BAT-003",
    "Solinteg EBA B5K1 — 10.24 kWh": "BAT-004",
    "Huawei LUNA2000 — 5 kWh": "BAT-005",
    "Huawei LUNA2000 — 7 kWh": "BAT-006",
}
WALLBOX_MAP = {
    "Solinteg 7 kW (1F)": "WBX-001",
    "Solinteg 11 kW (3F)": "WBX-002",
    "Huawei AC Smart 22 kW": "WBX-003",
    "Huawei AC Smart 7 kW": "WBX-004",
    "GoodWe 11 kW": "WBX-005",
    "GoodWe 22 kW": "WBX-006",
}

DISTRIBUCKA_DEFAULT = "ZSD"

# Cieľový folder kam ukladáme finálne ponuky pre zákazníkov
_OBCHOD_ROOT = str(Path(__file__).resolve().parent.parent)  # parent of /sablony/
ZAKAZNICI_DIR = _OBCHOD_ROOT + "/zakaznici"


# === Kompatibilita meničov a batérií ===
# Huawei → iba Huawei LUNA
# Solinteg → Solinteg (preferované) ALEBO Pylontech
# GoodWe → iba Pylontech
INVERTOR_BATTERY_COMPAT = {
    "INV-001": ["BAT-003", "BAT-004", "BAT-001"],  # Solinteg → Solinteg pref, Pylontech ok
    "INV-002": ["BAT-005", "BAT-006"],              # Huawei 5K → LUNA only
    "INV-003": ["BAT-005", "BAT-006"],              # Huawei 6K → LUNA only
    "INV-004": ["BAT-005", "BAT-006"],              # Huawei 8K → LUNA only
    "INV-005": ["BAT-005", "BAT-006"],              # Huawei 10K → LUNA only
    "INV-006": ["BAT-001"],                         # GoodWe 6K → Pylontech only
    "INV-007": ["BAT-001"],                         # GoodWe 8K → Pylontech only
    "INV-008": ["BAT-001"],                         # GoodWe 10K → Pylontech only
}


def check_compatibility(invertor_kod, bateria_kod):
    """Overí kompatibilitu meniča a batérie. Vráti (ok: bool, odporucenie: str)."""
    if not bateria_kod:
        return True, ""
    allowed = INVERTOR_BATTERY_COMPAT.get(invertor_kod, [])
    if bateria_kod in allowed:
        return True, ""
    # Auto-fix odporúčanie
    if allowed:
        return False, f"Menič {invertor_kod} nie je kompatibilný s {bateria_kod}. Použiť: {allowed[0]}"
    return False, f"Neznámy menič {invertor_kod}"


def predpocitaj_ceny_pre_record(notion_props, variants_filter=None):
    """Pre Notion záznam vyrátaj ceny pre A/B/C/D BEZ generovania PDF.
    Vráti dict {variant: {cena_s_dph, nakupna, zisk, marza_pct}}.

    variants_filter: ak None, vyrátá vsetky kde su data. Ak list ["A", "B"], iba tieto.
    """
    cennik = load_cennik()
    out = {}
    iter_variants = variants_filter if variants_filter else ("A", "B", "C", "D")
    for variant in iter_variants:
        # Variant B/C potrebujú batériu, C/D aj wallbox — ak chýba, preskoč
        if variant in ("B", "C") and not notion_props.get("Batéria (typ)"):
            continue
        if variant in ("C", "D") and not notion_props.get("Wallbox (typ)"):
            continue
        try:
            lead = lead_from_notion(notion_props, variant)

            # Kompatibilita check (iba ak ma bateriu)
            if variant in ("B", "C"):
                ok, msg = check_compatibility(lead["invertor_kod"], lead.get("bateria_kod"))
                if not ok:
                    out[variant] = {"error": msg}
                    continue

            konfig = vyrataj_konfig(lead, cennik)
            ceny = vyrataj_ceny(konfig, lead)
            out[variant] = {
                "cena_s_dph": ceny["cena_s_dph"],
                "cena_finalna": ceny["cena_finalna"],
                "nakupna": ceny["nakupna_spolu"],
                "zisk": ceny["zisk"],
                "marza_pct": ceny["marza_pct"],
                "vykon_kwp": konfig["vykon_kwp"],
            }
        except Exception as e:
            out[variant] = {"error": str(e)}
    return out


def safe_filename(s):
    """Pre file system — strip diakritiky + nepovolené znaky → '_' (ASCII-safe)."""
    import unicodedata
    if not s:
        return ""
    # NFD dekompozícia + drop combining marks (diakritika)
    nfd = unicodedata.normalize('NFD', s)
    ascii_str = ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')
    # Iba ASCII alfanumerické + dash + underscore
    return re.sub(r'[^A-Za-z0-9\-]', '_', ascii_str)


def detekuj_vek_leadu(notion_props):
    """
    Z poznámky vytiahne dátum vzniku leadu (formát 'Zdroj: Superponuky YYYY-MM-DD'
    alebo 'spotreba: ... datum: YYYY-MM-DD'). Vráti počet dní.
    Ak nenájde, vráti 0 (čerstvý).
    """
    pozn = notion_props.get("Poznámky") or ""
    # Hľadaj 'Superponuky 2026-01-13' alebo 'datum: 2026-01-13'
    m = re.search(r"(\d{4}-\d{2}-\d{2})", pozn)
    if not m:
        return 0
    try:
        lead_date = datetime.date.fromisoformat(m.group(1))
        return (datetime.date.today() - lead_date).days
    except Exception:
        return 0


def vek_leadu_kategoria(dni):
    """Vráti ('label', 'tone') pre úvod emailu/PDF podľa veku leadu."""
    if dni < 14:
        return ("čerstvý", "fresh")          # do 2 týždňov
    elif dni < 60:
        return ("stredný", "mid")            # 2-8 týždňov
    else:
        return ("starý", "old")              # 60+ dní


def vyrob_kombinovany_command(lead, results, out_command_path, ev_id, vek_dni=0):
    """
    Vytvorí .command Mac executable súbor ktorý cez AppleScript otvorí
    Outlook for Mac s draft emailom obsahujúcim VŠETKY varianty (A+B alebo A+B+C)
    a VŠETKY PDF prílohy naraz.

    `results` = list dictov (cena_s_dph, cena_finalna, navratnost_rokov, pdf, ma_bateriu, ma_wallbox)
    `vek_dni` = vek leadu v dňoch (pre age-aware úvod)
    """
    obch = lead.get("obchodnik", DEFAULT_OBCHODNIK)
    priezvisko = lead["meno"].split()[-1]
    mesto = lead["mesto"]

    def n(x, dec=0):
        s = f"{x:,.{dec}f}"
        return s.replace(",", " ").replace(".", ",")

    # Slovník popisov variantov — viac marketingovo
    variant_label = {
        "A": "Variant A — Fotovoltika",
        "B": "Variant B — Fotovoltika + batéria",
        "C": "Variant C — Fotovoltika + batéria + wallbox pre EV",
    }
    variant_pitch = {
        "A": "Najnižšia investícia. Prebytky predávate do siete za výkupnú cenu. Vhodné ak ste cez deň doma a stíhate spotrebovať vyrobenú energiu.",
        "B": "Vyššia samospotreba (~90 %) — energiu zo slnka využijete aj večer a v noci. Menej závislý od cien zo siete a od pravidiel pre prebytky.",
        "C": "Kompletný balík — výroba, ukladanie aj nabíjanie elektromobilu zo slnka. Pre rodiny s EV alebo plánom kúpiť ho v najbližších rokoch.",
    }

    # === AGE-AWARE ÚVOD ===
    label, tone = vek_leadu_kategoria(vek_dni)
    if tone == "fresh":
        intro = (
            f"Dobrý deň, pán {priezvisko},\n\n"
            f"ďakujem za Váš záujem o fotovoltickú elektráreň pre Váš dom v {mesto}. "
            f"V prílohe Vám posielam {'dve varianty' if len(results) == 2 else f'{len(results)} varianty'} "
            f"cenovej ponuky, aby ste mali možnosť porovnať a vybrať si to, čo Vám najviac vyhovuje.\n"
        )
    elif tone == "mid":
        intro = (
            f"Dobrý deň, pán {priezvisko},\n\n"
            f"ozývam sa s prísľubenou cenovou ponukou na fotovoltickú elektráreň pre Váš dom v {mesto}. "
            f"Vďaka za Vašu trpezlivosť — pripravil som pre Vás {'dve varianty' if len(results) == 2 else f'{len(results)} varianty'}, "
            f"aby ste mali jasný obraz o nákladoch aj o návratnosti.\n"
        )
    else:  # old
        # Mesiace
        mesiace = vek_dni // 30
        intro = (
            f"Dobrý deň, pán {priezvisko},\n\n"
            f"ospravedlňujem sa za neskorú reakciu na Váš dopyt na fotovoltickú elektráreň pre Váš dom v {mesto} "
            f"(zaregistrovaný pred ~{mesiace} mesiacmi). "
            f"Téma sa medzitým posunula — vyšla nová výzva Zelená domácnostiam 2026 s aktualizovanými podmienkami "
            f"a ceny elektriny pokračovali v raste. Ak je u Vás projekt FVE stále aktuálny, "
            f"pripravil som pre Vás {'dve aktuálne varianty' if len(results) == 2 else f'{len(results)} aktuálne varianty'} "
            f"cenovej ponuky — všetko prepočítané pre dnešné podmienky.\n\n"
            f"Ak už nie je téma aktuálna, dajte mi krátko vedieť — uložím dopyt do archívu a nebudeme Vás ďalej kontaktovať.\n"
        )

    body_variants = ""
    for r in results:
        v = r["variant"]
        body_variants += (
            f"\n📄 {variant_label[v]}\n"
            f"   Cena s DPH:        {n(r['cena_s_dph'], 2)} EUR\n"
            f"   Po dotácii:        {n(r['cena_finalna'], 2)} EUR\n"
            f"   Návratnosť:        cca {n(r['navratnost_rokov'], 1)} rokov\n"
            f"   {variant_pitch[v]}\n"
        )

    # CTA — silnejšie a konkrétne
    if tone == "old":
        cta = (
            f"\nNavrhujem 2 možnosti ako pokračovať:\n"
            f"  1. Zavolajte mi na {obch['tel']} a dohodneme bezplatnú obhliadku (pripravím termín na tento týždeň).\n"
            f"  2. Odpíšte na tento email — radi prediskutujeme detaily aj cez video hovor.\n\n"
            f"Aktuálne podmienky dotácie môžu byť výhodnejšie ako pred pár mesiacmi. "
            f"Ak chcete, prejdem to s Vami osobne.\n"
        )
    else:
        cta = (
            f"\nAko ďalší krok navrhujem bezplatnú obhliadku, kde upresníme technické detaily, "
            f"zameriame strechu a finalizujeme cenu. Stačí odpísať alebo zavolať — termín radi "
            f"prispôsobíme Vašim možnostiam (aj víkendy, večerné hodiny).\n"
        )

    outro = (
        f"\nPonuky sú platné {lead.get('platnost_dni', 30)} dní od dnes.\n\n"
        f"S pozdravom,\n{obch['meno']}\n"
        f"{obch.get('funkcia', 'CEO')}, Energovision, s.r.o.\n"
        f"{obch['tel']}  |  {obch['email']}\n"
        f"www.energovision.sk\n"
    )

    # === HTML BODY (Outlook for Mac plne podporuje) ===
    # Variant labels — HTML formátované
    variant_label_html = {
        "A": "Variant A — Fotovoltika",
        "B": "Variant B — Fotovoltika + batéria",
        "C": "Variant C — Fotovoltika + batéria + wallbox pre EV",
    }

    # HTML intro (3 verzie podľa veku)
    if tone == "fresh":
        html_intro = (
            f"<p>Dobrý deň, pán {priezvisko},</p>"
            f"<p>ďakujem za Váš záujem o fotovoltickú elektráreň pre Váš dom v {mesto}. "
            f"V prílohe Vám posielam {'<strong>dve varianty</strong>' if len(results) == 2 else f'<strong>{len(results)} varianty</strong>'} "
            f"cenovej ponuky, aby ste mali možnosť porovnať a vybrať si to, čo Vám najviac vyhovuje.</p>"
        )
    elif tone == "mid":
        html_intro = (
            f"<p>Dobrý deň, pán {priezvisko},</p>"
            f"<p>ozývam sa s prísľúbenou cenovou ponukou na fotovoltickú elektráreň pre Váš dom v {mesto}. "
            f"Vďaka za Vašu trpezlivosť — pripravil som pre Vás "
            f"{'<strong>dve varianty</strong>' if len(results) == 2 else f'<strong>{len(results)} varianty</strong>'}, "
            f"aby ste mali jasný obraz o nákladoch aj o návratnosti.</p>"
        )
    else:
        mesiace = vek_dni // 30
        html_intro = (
            f"<p>Dobrý deň, pán {priezvisko},</p>"
            f"<p>ospravedlňujem sa za neskorú reakciu na Váš dopyt na fotovoltickú elektráreň pre Váš dom v {mesto} "
            f"(zaregistrovaný pred ~{mesiace} mesiacmi). Téma sa medzitým posunula — vyšla "
            f"<strong>nová výzva Zelená domácnostiam 2026</strong> s aktualizovanými podmienkami "
            f"a ceny elektriny pokračovali v raste. Ak je u Vás projekt FVE stále aktuálny, pripravil som pre Vás "
            f"{'<strong>dve aktuálne varianty</strong>' if len(results) == 2 else f'<strong>{len(results)} aktuálne varianty</strong>'} "
            f"cenovej ponuky — všetko prepočítané pre dnešné podmienky.</p>"
            f"<p><em>Ak už nie je téma aktuálna, dajte mi krátko vedieť — uložím dopyt do archívu "
            f"a nebudeme Vás ďalej kontaktovať.</em></p>"
        )

    # HTML body — varianty ako pekná tabuľka
    html_variants = '<table cellspacing="0" cellpadding="10" style="border-collapse:collapse; width:100%; margin:16px 0; font-family:Helvetica,Arial,sans-serif;">'
    for r in results:
        v = r["variant"]
        html_variants += (
            '<tr style="background:#F5FBEC; border-top:2px solid #92D050;">'
            f'<td style="padding:12px;">'
            f'<div style="font-size:14px; font-weight:700; color:#1a1a1a; margin-bottom:6px;">'
            f'📄 {variant_label_html[v]}</div>'
            f'<div style="font-size:13px; color:#444; line-height:1.55;">{variant_pitch[v]}</div>'
            f'</td>'
            f'<td style="padding:12px; text-align:right; min-width:180px; vertical-align:top;">'
            f'<div style="color:#666; font-size:11px; text-transform:uppercase; letter-spacing:1px;">Cena s DPH</div>'
            f'<div style="font-size:14px; font-weight:600; color:#1a1a1a;">{n(r["cena_s_dph"], 2)} €</div>'
            f'<div style="color:#666; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin-top:6px;">Po dotácii</div>'
            f'<div style="font-size:18px; font-weight:800; color:#6FB022;">{n(r["cena_finalna"], 2)} €</div>'
            f'<div style="color:#666; font-size:11px; margin-top:4px;">Návratnosť ~{n(r["navratnost_rokov"], 1)} rokov</div>'
            f'</td>'
            f'</tr>'
        )
    html_variants += '</table>'

    # CTA
    if tone == "old":
        html_cta = (
            f'<p>Navrhujem 2 možnosti ako pokračovať:</p>'
            f'<ol>'
            f'<li>Zavolajte mi na <strong>{obch["tel"]}</strong> a dohodneme bezplatnú obhliadku '
            f'(termín pripravím tento týždeň).</li>'
            f'<li>Odpíšte na tento email — radi prediskutujeme detaily aj cez video hovor.</li>'
            f'</ol>'
            f'<p><strong>Aktuálne podmienky dotácie môžu byť výhodnejšie ako pred pár mesiacmi.</strong> '
            f'Ak chcete, prejdem to s Vami osobne.</p>'
        )
    else:
        html_cta = (
            f'<p>Ako ďalší krok navrhujem <strong>bezplatnú obhliadku</strong>, kde upresníme technické '
            f'detaily, zameriame strechu a finalizujeme cenu. Stačí odpísať alebo zavolať — termín radi '
            f'prispôsobíme Vašim možnostiam (aj víkendy alebo večerné hodiny).</p>'
        )

    html_outro = (
        f'<p style="color:#666; font-size:12px; margin-top:16px;">'
        f'Ponuky sú platné <strong>{lead.get("platnost_dni", 30)} dní</strong> od dnešného dátumu.<br>'
        f'Ak máte akékoľvek otázky, som Vám k dispozícii.'
        f'</p>'
    )

    # Plné HTML telo (bez signatúry — Outlook ju pridá automaticky)
    html_body = (
        '<div style="font-family:Helvetica,Arial,sans-serif; font-size:13.5px; color:#2c2c2c; line-height:1.55;">'
        f'{html_intro}'
        f'{html_variants}'
        f'{html_cta}'
        f'{html_outro}'
        '</div>'
    )

    # Subject — age-aware
    var_letters = " + ".join(r["variant"] for r in results)
    if tone == "old":
        subject = f"Cenová ponuka {ev_id} pre Váš dom — aktualizovaná na 2026 (varianty {var_letters})"
    else:
        subject = f"Cenová ponuka {ev_id} — Fotovoltika pre Váš dom — {len(results)} varianty na výber ({var_letters})"
    if not ev_id:
        subject = f"Cenové ponuky FVE — {len(results)} varianty na výber"

    to_addr = lead.get("email", "")

    # AppleScript escape — pre html content stačí escape backslash a quote
    def osa_escape(s):
        return s.replace('\\', '\\\\').replace('"', '\\"')

    # Single-line HTML aby AppleScript nemal problém s newlines
    html_body_oneline = re.sub(r'\s+', ' ', html_body).strip()

    # Build attachment lines
    attachment_lines = []
    for r in results:
        pdf_mac = r["pdf"].replace(
            _OBCHOD_ROOT,
            "/Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod"
        )
        attachment_lines.append(
            f'    make new attachment at newMessage with properties {{file:POSIX file "{pdf_mac}"}}'
        )
    attachments_block = "\n".join(attachment_lines)

    recipient_line = (
        f'make new recipient at newMessage with properties {{email address:{{address:"{to_addr}"}}}}'
        if to_addr else ''
    )

    script = f'''#!/bin/bash
# Otvorí Outlook for Mac s draft emailom obsahujúcim VŠETKY varianty ({var_letters})
# ako prílohy v jednej správe. Stačí dvojklik.

osascript <<'APPLESCRIPT'
tell application "Microsoft Outlook"
    activate
    set htmlBody to "{osa_escape(html_body_oneline)}"
    set emailSubject to "{osa_escape(subject)}"
    set newMessage to make new outgoing message with properties {{subject:emailSubject, content:htmlBody}}
    set content type of newMessage to HTML
    {recipient_line}
{attachments_block}
    open newMessage
end tell
APPLESCRIPT
'''

    with open(out_command_path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(out_command_path, 0o755)


def vyrob_outlook_draft_command(lead, konfig, ceny, navratnost, pdf_path, out_command_path, vek_dni=0):
    """
    Vytvorí .command Mac executable súbor ktorý cez AppleScript otvorí
    Outlook for Mac s pre-vyplneným draft emailom + PDF prílohou.

    Lukas dvojklikne na .command → Outlook otvorí draft → klik Send.

    Mac path zodpovedajúca lokálne (treba zameniť /sessions/... → /Users/lukasbago/...).
    """
    obch = lead.get("obchodnik", DEFAULT_OBCHODNIK)
    priezvisko = lead["meno"].split()[-1]
    mesto = lead["mesto"]
    rocna_uspora = navratnost.get("rocne_uspora_eur", 0)
    rocna_vyroba = navratnost.get("rocna_vyroba_kwh", 0)
    nav_rokov = navratnost.get("navratnost_rokov", 0)

    def n(x, dec=0):
        s = f"{x:,.{dec}f}"
        return s.replace(",", " ").replace(".", ",")

    label, tone = vek_leadu_kategoria(vek_dni)

    if tone == "fresh":
        intro_html = (
            f"<p>Dobrý deň, pán {priezvisko},</p>"
            f"<p>ďakujem za Váš záujem o fotovoltickú elektráreň pre Váš dom v {mesto}. "
            f"V prílohe Vám posielam cenovú ponuku spracovanú na základe údajov, ktoré ste mi poskytli.</p>"
        )
    elif tone == "mid":
        intro_html = (
            f"<p>Dobrý deň, pán {priezvisko},</p>"
            f"<p>ozývam sa s prísľúbenou cenovou ponukou na fotovoltickú elektráreň pre Váš dom v {mesto}. "
            f"Vďaka za Vašu trpezlivosť — v prílohe nájdete kompletnú ponuku.</p>"
        )
    else:
        mesiace = vek_dni // 30
        intro_html = (
            f"<p>Dobrý deň, pán {priezvisko},</p>"
            f"<p>ospravedlňujem sa za neskorú reakciu na Váš dopyt (zaregistrovaný pred ~{mesiace} mesiacmi). "
            f"Téma sa medzitým posunula — vyšla <strong>nová výzva Zelená domácnostiam 2026</strong> "
            f"a ceny elektriny pokračovali v raste. Ak je u Vás projekt FVE stále aktuálny, "
            f"v prílohe Vám posielam aktualizovanú cenovú ponuku.</p>"
            f"<p><em>Ak už nie je téma aktuálna, dajte mi krátko vedieť — uložím dopyt do archívu.</em></p>"
        )

    html_body = (
        '<div style="font-family:Helvetica,Arial,sans-serif; font-size:13.5px; color:#2c2c2c; line-height:1.55;">'
        f'{intro_html}'
        f'<table cellspacing="0" cellpadding="6" style="border-collapse:collapse; margin:14px 0; '
        f'background:#F5FBEC; border-left:3px solid #92D050;">'
        f'<tr><td style="padding:6px 12px; color:#666;">Výkon FVE</td>'
        f'<td style="padding:6px 12px;"><strong>{n(konfig["vykon_kwp"], 2)} kWp</strong> '
        f'({konfig["pocet_panelov"]} ks panelov)</td></tr>'
        f'<tr><td style="padding:6px 12px; color:#666;">Predpokladaná výroba</td>'
        f'<td style="padding:6px 12px;"><strong>{n(rocna_vyroba)} kWh / rok</strong></td></tr>'
        f'<tr><td style="padding:6px 12px; color:#666;">Predpokladaná úspora</td>'
        f'<td style="padding:6px 12px;"><strong>{n(rocna_uspora)} € / rok</strong></td></tr>'
        f'<tr><td style="padding:6px 12px; color:#666;">Cena s DPH</td>'
        f'<td style="padding:6px 12px;">{n(ceny["cena_s_dph"], 2)} €</td></tr>'
        f'<tr><td style="padding:6px 12px; color:#666;">Po dotácii</td>'
        f'<td style="padding:6px 12px; font-size:16px; color:#6FB022;">'
        f'<strong>{n(ceny["cena_finalna"], 2)} €</strong></td></tr>'
        f'<tr><td style="padding:6px 12px; color:#666;">Návratnosť</td>'
        f'<td style="padding:6px 12px;">cca <strong>{n(nav_rokov, 1)} rokov</strong></td></tr>'
        f'</table>'
        f'<p>Ako ďalší krok navrhujem <strong>bezplatnú obhliadku</strong>, kde upresníme technické '
        f'detaily a finalizujeme cenu. Stačí odpísať alebo zavolať.</p>'
        f'<p style="color:#666; font-size:12px;">Ponuka je platná '
        f'<strong>{lead.get("platnost_dni", 30)} dní</strong> od dnes.</p>'
        '</div>'
    )

    cislo_pon = lead.get("cislo_ponuky", "")
    if tone == "old":
        subject = f"Cenová ponuka {cislo_pon} pre Váš dom — aktualizovaná na 2026" if cislo_pon else "Cenová ponuka FVE — aktualizovaná na 2026"
    else:
        subject = f"Cenová ponuka {cislo_pon} — Fotovoltika pre Váš dom" if cislo_pon else "Cenová ponuka FVE pre Váš dom"

    to_addr = lead.get("email", "")
    pdf_mac_path = pdf_path.replace(
        _OBCHOD_ROOT,
        "/Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod"
    )

    def osa_escape(s):
        return s.replace('\\', '\\\\').replace('"', '\\"')

    html_body_oneline = re.sub(r'\s+', ' ', html_body).strip()

    recipient_line = (
        f'make new recipient at newMessage with properties {{email address:{{address:"{to_addr}"}}}}'
        if to_addr else ''
    )

    script = f'''#!/bin/bash
# Otvorí Outlook for Mac s pre-vyplneným HTML draft emailom + PDF prílohou.

osascript <<'APPLESCRIPT'
tell application "Microsoft Outlook"
    activate
    set htmlBody to "{osa_escape(html_body_oneline)}"
    set emailSubject to "{osa_escape(subject)}"
    set newMessage to make new outgoing message with properties {{subject:emailSubject, content:htmlBody}}
    set content type of newMessage to HTML
    {recipient_line}
    make new attachment at newMessage with properties {{file:POSIX file "{pdf_mac_path}"}}
    open newMessage
end tell
APPLESCRIPT
'''

    with open(out_command_path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(out_command_path, 0o755)


# ============================================================
# OBCHODNÍCI — mapovanie Notion select → kontaktné údaje
# ============================================================
OBCHODNICI = {
    "Dominik Galaba": {
        "meno": "Dominik Galaba",
        "funkcia": "Office & Administration Manager",
        "tel": "+421 917 424 564",
        "email": "dominik.galaba@energovision.sk",
    },
    "Pavol Kaprál": {
        "meno": "Ing. Pavol Kaprál",
        "funkcia": "Sale & Project Manager",
        "tel": "+421 911 700 727",
        "email": "pavol.kapral@energovision.sk",
    },
    "Andrej Herman": {
        "meno": "Andrej Herman",
        "funkcia": "Sales Manager",
        "tel": "+421 948 887 979",
        "email": "andrej.herman@energovision.sk",
    },
    "Lukáš Bago": {
        "meno": "Lukáš Bago",
        "funkcia": "Konateľ spoločnosti",
        "tel": "+421 918 187 762",
        "email": "lukas.bago@energovision.sk",
    },
}
DEFAULT_OBCHODNIK = OBCHODNICI["Dominik Galaba"]


def lead_from_notion(notion_props, variant):
    """
    Z Notion properties zostaví lead.json pre konkrétny variant.
    variant: "A" (FVE), "B" (FVE+BESS), "C" (FVE+BESS+Wallbox)
    """
    title = notion_props.get("Zákazník", "Zákazník").strip()
    # Title moze byt: "Meno Priezvisko" (novy format) alebo "Priezvisko, Mesto" (stary format)
    if "," in title:
        # Stary format "Priezvisko, Mesto" - cela prva cast je priezvisko (mozno aj viacslovne)
        full_name = title.split(",", 1)[0].strip()
        priezvisko = full_name
    else:
        # Novy format "Meno Priezvisko" - cele to ide do PDF, priezvisko vyextrahujeme
        full_name = title
        if " " in title:
            priezvisko = title.rsplit(" ", 1)[-1].strip()
        else:
            priezvisko = title.strip()

    # Mesto property môže obsahovať aj ulicu — formát "Ulica číslo, Mesto"
    # Ak je čiarka v Mesto property, prvá časť je ulica, druhá mesto
    # Inak sa fallback na druhú časť title alebo iba mesto
    mesto_full = (notion_props.get("Mesto") or "").strip()
    ulica_from_mesto = ""
    if "," in mesto_full:
        _parts = [p.strip() for p in mesto_full.split(",", 1)]
        ulica_from_mesto = _parts[0]
        mesto = _parts[1]
    elif mesto_full:
        mesto = mesto_full
    elif "," in title:
        mesto = title.split(",", 1)[1].strip()
    else:
        mesto = ""

    panel_kod = PANEL_MAP.get(notion_props.get("Panel"), "PAN-002")
    inv_kod = INVERTOR_MAP.get(notion_props.get("Menič"), "INV-001")
    kon_kod = KONSTRUKCIA_MAP.get(notion_props.get("Konštrukcia (typ)"), "KON-004")
    # Počet panelov — akceptuje Number (int/float) aj Select label ("12", "12 ks", "12 panelov")
    _val = notion_props.get("Počet panelov")
    if _val is None or _val == "":
        pocet_panelov = 24
    else:
        try:
            pocet_panelov = int(_val)
        except (TypeError, ValueError):
            _m = re.search(r"\d+", str(_val))
            pocet_panelov = int(_m.group(0)) if _m else 24

    # Wp panela na výpočet kWp
    panel_str = (notion_props.get("Panel") or "")
    if "540" in panel_str:
        wp = 540
    elif "535" in panel_str:
        wp = 535
    elif "470" in panel_str:
        wp = 470
    else:
        wp = 535  # default novy LONGi 535 Wp
    vykon_kwp = round(pocet_panelov * wp / 1000, 2)

    # Poznamky text - pouzity pre Spotreba fallback aj pre adresu nizsie
    pozn = notion_props.get("Poznámky") or ""

    # Spotreba — najprv skus novu Spotreba property (NUMBER), potom Poznamky, potom default
    spotreba_raw = notion_props.get("Spotreba")
    spotreba = None
    if spotreba_raw is not None:
        try:
            spotreba = int(float(spotreba_raw))
        except (TypeError, ValueError):
            spotreba = None
    if spotreba is None:
        # Backward compat: skus z Poznamok formatu "spotreba: X kWh"
        m = re.search(r"spotreba[\s:]+(\d+(?:\s?\d+)?)\s*kWh", pozn, re.I)
        if m:
            spotreba = int(m.group(1).replace(" ", ""))
    if spotreba is None or spotreba <= 0:
        # Univerzalny default 8000 kWh (priemer SK domacnosti)
        spotreba = 8000

    # Komponenty — podľa variantu
    bateria_kwh = 0
    bateria_kod = None
    wallbox = False
    wallbox_kod = None

    if variant in ("B", "C"):
        bateria_typ = notion_props.get("Batéria (typ)") or ""
        bateria_kod = BATERIA_MAP.get(bateria_typ, "BAT-001")
        # Extrahuj per-modul kWh zo select labelu, napr. "Huawei LUNA2000 — 5 kWh" → 5.0
        m_modul = re.search(r"(\d+(?:[.,]\d+)?)\s*kWh", bateria_typ)
        per_modul_kwh = float(m_modul.group(1).replace(",", ".")) if m_modul else 5.0
        # Spočítaj kapacitu: počet × per-modul kWh; fallback na staré "Batéria (kWh)" ako absolútna hodnota
        pocet_raw = notion_props.get("Batéria počet")
        try:
            pocet = int(pocet_raw) if pocet_raw not in (None, "") else None
        except (TypeError, ValueError):
            pocet = None
        if pocet is not None and pocet > 0:
            bateria_kwh = pocet * per_modul_kwh
        else:
            # Backward compat: ak je "Batéria (kWh)" zadané ako absolútna hodnota (zo starých záznamov)
            bateria_kwh = float(notion_props.get("Batéria (kWh)") or 10)

    if variant in ("C", "D"):
        wallbox = True
        wallbox_kod = WALLBOX_MAP.get(notion_props.get("Wallbox (typ)"), "WBX-002")

    # ID ponuky — formát EV-26-XXX-{Variant}
    # Akceptuje: number, "150", "EV-150", "B2C-150", atď. — extrahuje prvé číslo
    id_p = notion_props.get("ID ponuky")
    if id_p is None or id_p == "":
        id_int = 0
    else:
        try:
            id_int = int(id_p)
        except (TypeError, ValueError):
            _m_id = re.search(r"\d+", str(id_p))
            id_int = int(_m_id.group(0)) if _m_id else 0
    cislo_ponuky = f"EV-26-{id_int:03d}-{variant}" if id_int else f"EV-26-{variant}"

    # Telefón / email z Notion
    telefon = notion_props.get("Telefón") or ""
    email = notion_props.get("Email") or ""

    # Ulica + PSČ — najprv skús z Poznámok regex "adresa: ulica, 12345"
    # Ak nenájde, použij ulica_from_mesto (vyextrahované z Mesto property vyššie)
    ulica = ""
    psc = ""
    m_addr = re.search(r"adresa[\s:]+([^,]+),\s*(\d{3}\s?\d{2})", pozn, re.I)
    if m_addr:
        ulica = m_addr.group(1).strip()
        psc = m_addr.group(2)
    elif ulica_from_mesto:
        ulica = ulica_from_mesto

    # Per-variant marža s fallbackom na centrálnu "Marža %"
    # POZOR: nepouzivaj `or 30` — keby uzivatel nastavil 0% maržu, fallbackoval by na 30%!
    # Marža je teraz SELECT v Notione (string "0", "5", "10", ..., "100") — nie number.
    # Treba handlovat None aj prazdny string ("") aj numeric value (legacy).
    def _marza_or(val, fallback):
        if val is None or val == "":
            return fallback
        try:
            return int(val)
        except (ValueError, TypeError):
            # ak je to napr. "30 %" alebo neparsovatelne, fallback
            import re as _re
            _m = _re.search(r"\d+", str(val))
            return int(_m.group(0)) if _m else fallback

    marza_central = _marza_or(notion_props.get("Marža %"), 30)
    marza_per_variant = {
        "A": _marza_or(notion_props.get("Marža A %"), marza_central),
        "B": _marza_or(notion_props.get("Marža B %"), marza_central),
        "C": _marza_or(notion_props.get("Marža C %"), marza_central),
        "D": _marza_or(notion_props.get("Marža D %"), marza_central),
    }
    marza_pct = int(marza_per_variant.get(variant, marza_central))

    # Vek leadu — pre age-aware texty v PDF
    vek_dni = detekuj_vek_leadu(notion_props)

    return {
        "meno": full_name,  # cele meno (Meno Priezvisko) - pouzite v PDF "PRE" a titulke
        "priezvisko": priezvisko,  # iba priezvisko - pre filename, folder, salutacie
        "mesto": mesto,
        "psc": psc,
        "ulica": ulica,
        "telefon": telefon,
        "email": email,
        "vek_dni": vek_dni,
        "rocna_spotreba_kwh": spotreba,
        "cena_el_eur_kwh": 0.16,
        "distribucka": DISTRIBUCKA_DEFAULT,
        "vykon_kwp": vykon_kwp,
        "panel_kod": panel_kod,
        "invertor_kod": inv_kod,
        "konstrukcia_kod": kon_kod,
        "bateria_kwh": bateria_kwh,
        "bateria_kod": bateria_kod,
        "wallbox": wallbox,
        "wallbox_kod": wallbox_kod,
        "doprava_km": 100,
        "marza_pct": marza_pct,
        "rezerva_pct": 5,
        "zlava_eur": 0,
        "platnost_dni": 30,
        "dotacia": True,
        "platby": "60 % zálohová faktúra vopred  ·  30 % po nainštalovaní elektrárne  ·  10 % po protokolárnom odovzdaní",
        "cislo_ponuky": cislo_ponuky,
        "obchodnik": OBCHODNICI.get(notion_props.get("Obchodník") or notion_props.get("Obchodnik") or "", DEFAULT_OBCHODNIK),
    }


def generate_for_record(notion_props, variants_to_run=None, notion_page_id=None):
    """
    Vygeneruje ponuky pre zákazníka z Notion záznamu.
    notion_props: dict s Notion properties
    variants_to_run: list ["A", "B", "C"] alebo None (auto-detect z checkboxes)

    Vráti dict {"variant": "A", "pdf": path, "eml": path, "xlsx": path, "ceny": {...}}[]
    """
    if variants_to_run is None:
        variants_to_run = []
        if notion_props.get("Variant A — FVE") == "__YES__" or notion_props.get("Variant A — FVE") is True:
            variants_to_run.append("A")
        if notion_props.get("Variant B — FVE + BESS") == "__YES__" or notion_props.get("Variant B — FVE + BESS") is True:
            variants_to_run.append("B")
        if notion_props.get("Variant C — FVE + BESS + Wallbox") == "__YES__" or notion_props.get("Variant C — FVE + BESS + Wallbox") is True:
            variants_to_run.append("C")

    if not variants_to_run:
        print("⚠️  Žiaden variant nie je zaškrtnutý — preskakujem.")
        return []

    title = notion_props.get("Zákazník", "Zákazník")
    if "," in title:
        priezvisko = title.split(",")[0].strip()
        mesto = title.split(",")[1].strip()
    else:
        priezvisko = title.split()[-1]
        mesto = notion_props.get("Mesto", "")

    # Folder s prefixom EV-26-XXX
    id_p = notion_props.get("ID ponuky")
    try:
        id_int = int(id_p)
    except (TypeError, ValueError):
        id_int = 0
    prefix = f"EV-26-{id_int:03d}_" if id_int else ""
    folder_name = f"{prefix}{safe_filename(priezvisko)}_{safe_filename(mesto)}"
    out_dir = f"{ZAKAZNICI_DIR}/{folder_name}"
    os.makedirs(out_dir, exist_ok=True)

    cennik = load_cennik()
    results = []

    # Vek leadu — pre age-aware úvody
    vek_dni = detekuj_vek_leadu(notion_props)

    for variant in variants_to_run:
        variant_name = {"A": "A_FVE", "B": "B_FVE_BESS", "C": "C_FVE_BESS_Wallbox"}[variant]
        variant_dir = f"{out_dir}/{variant_name}"
        os.makedirs(variant_dir, exist_ok=True)

        print(f"  📦 Variant {variant} → {variant_dir}")

        lead = lead_from_notion(notion_props, variant)
        lead["out_dir"] = variant_dir

        konfig = vyrataj_konfig(lead, cennik)
        ceny = vyrataj_ceny(konfig, lead)
        navratnost = vyrataj_navratnost(konfig, ceny, lead)

        ev_num = f"EV-26-{id_int:03d}-{variant}" if id_int else f"EV-26-{variant}"
        base = f"{ev_num}_{safe_filename(priezvisko)}"
        interna = f"{ev_num}_kalkulacia_{safe_filename(priezvisko)}"

        # Grafy
        grafy = vyrob_grafy(navratnost, lead, variant_dir, base)

        # PDF cez HTML
        pdf_path = f"{variant_dir}/{base}.pdf"
        vyrob_html_pdf(lead, konfig, ceny, navratnost, grafy, pdf_path)

        # Interná kalkulácia
        vyrob_internu_kalkulaciu(lead, konfig, ceny, navratnost, f"{variant_dir}/{interna}.xlsx")

        # EML (záloha — pre Outlook Win)
        eml_path = f"{variant_dir}/{base}.eml"
        vyrob_eml_v2(lead, konfig, ceny, navratnost, pdf_path, eml_path)

        # .command (Mac-native — odporúčané pre Outlook for Mac)
        command_path = f"{variant_dir}/POSLI_EMAIL_{base}.command"
        vyrob_outlook_draft_command(lead, konfig, ceny, navratnost, pdf_path, command_path, vek_dni=vek_dni)

        # Lead JSON pre referenciu
        with open(f"{variant_dir}/lead.json", "w", encoding="utf-8") as f:
            json.dump(lead, f, ensure_ascii=False, indent=2)

        results.append({
            "variant": variant,
            "variant_name": variant_name,
            "pdf": pdf_path,
            "eml": eml_path,
            "command": command_path,
            "xlsx": f"{variant_dir}/{interna}.xlsx",
            "cena_s_dph": ceny["cena_s_dph"],
            "cena_finalna": ceny["cena_finalna"],
            "navratnost_rokov": navratnost["navratnost_rokov"],
            "vykon_kwp": konfig["vykon_kwp"],
            "ma_bateriu": konfig["ma_bateriu"],
            "ma_wallbox": konfig["ma_wallbox"],
            "nakupna_cena": ceny["nakupna_spolu"],
            "marza_eur": ceny["marza_eur"],
            "rezerva_eur": ceny["rezerva_eur"],
            "zisk": ceny["zisk"],
            "marza_pct_pouzita": ceny["marza_pct"],
        })

    # Kombinovaný .command — jeden email so všetkými variantmi
    if results:
        # Lead pre kombinovaný email — beriem prvý variant ako bázu (meno, mesto, email)
        last_lead = lead_from_notion(notion_props, results[0]["variant"])
        ev_label = f"EV-26-{id_int:03d}" if id_int else ""
        kombi_command = f"{out_dir}/POSLI_VSETKY_VARIANTY_{ev_label}_{safe_filename(priezvisko)}.command"
        vyrob_kombinovany_command(last_lead, results, kombi_command, ev_label, vek_dni=vek_dni)
        print(f"  📧 Kombinovaný email (všetky varianty) → {Path(kombi_command).name}")

    # Súhrn všetkých variantov — vylepšený pre rýchle rozhodnutie
    suhrn_path = f"{out_dir}/SUHRN.md"
    notion_url = notion_props.get("url", "")
    if not notion_url and notion_page_id:
        clean_id = notion_page_id.replace("-", "")
        notion_url = f"https://www.notion.so/{clean_id}"
    ev_label = f"EV-26-{id_int:03d}" if id_int else ""
    vek_kategoria_label, _ = vek_leadu_kategoria(vek_dni)

    with open(suhrn_path, "w", encoding="utf-8") as f:
        # === HEADER ===
        f.write(f"# 📋 Ponuka {ev_label} — {title}\n\n")

        if notion_url:
            f.write(f"🔗 [**Otvoriť záznam v Notion**]({notion_url}) — vyklikať varianty, sledovať status\n\n")

        f.write(f"_Vygenerované: {datetime.datetime.now():%d. %m. %Y %H:%M}_  ·  ")
        if vek_dni > 0:
            f.write(f"_Vek leadu: {vek_dni} dní ({vek_kategoria_label})_\n\n")
        else:
            f.write(f"_Lead: čerstvý_\n\n")

        # === ČO ROBIŤ TERAZ ===
        f.write("## 🚀 Čo teraz?\n\n")
        if len(results) > 1:
            f.write(f"1. **Pošli zákazníkovi všetky {len(results)} varianty naraz** — dvojklik na "
                    f"`POSLI_VSETKY_VARIANTY_{ev_label}_*.command` v tomto folderi\n")
            f.write("2. Alebo **vyber jeden variant** a pošli len ten — dvojklik na `POSLI_EMAIL_*.command` v príslušnej podzložke\n")
            f.write("3. Po odoslaní v Notion zaškrtni **Email odoslaný** + dnešný dátum → spustí sa D+ tracking\n\n")
        else:
            f.write(f"1. **Pošli zákazníkovi** — dvojklik na `POSLI_EMAIL_*.command` v podzložke\n")
            f.write("2. Po odoslaní v Notion zaškrtni **Email odoslaný** + dnešný dátum\n\n")

        f.write("> ℹ️ Pri prvom spustení `.command` Mac varuje „súbor z internetu“ — povol cez **Pravý klik → Otvoriť**. Druhýkrát už dvojklik.\n\n")

        # === PRE ZÁKAZNÍKA ===
        f.write("## 💰 Pre zákazníka (verejné ceny)\n\n")
        f.write("| Variant | Konfigurácia | Cena s DPH | Po dotácii | Návratnosť |\n")
        f.write("|---------|-------------|-----------:|-----------:|-----------:|\n")
        for r in results:
            konfig_str = f"{r['vykon_kwp']:.2f} kWp"
            if r["ma_bateriu"]: konfig_str += " + batéria"
            if r["ma_wallbox"]: konfig_str += " + wallbox"
            f.write(
                f"| **{r['variant']}** | {konfig_str} | "
                f"{r['cena_s_dph']:,.2f} € | **{r['cena_finalna']:,.2f} €** | {r['navratnost_rokov']:.1f} r |\n"
            )

        # === INTERNÁ EKONOMIKA ===
        f.write("\n## 🔒 Interná ekonomika (NEPOSIELAŤ ZÁKAZNÍKOVI!)\n\n")
        f.write("| Variant | Nákupná cena | Marža (€) | **Zisk** | Marža % | Použitá marža % |\n")
        f.write("|---------|-------------:|----------:|---------:|--------:|----------------:|\n")
        for r in results:
            cena_bez_dph = r['cena_s_dph'] / 1.23
            marza_pct_z_ceny = r['marza_eur'] / cena_bez_dph * 100 if cena_bez_dph else 0
            f.write(
                f"| **{r['variant']}** | {r['nakupna_cena']:,.2f} € | "
                f"{r['marza_eur']:,.2f} € | **{r['zisk']:,.2f} €** | "
                f"{marza_pct_z_ceny:.1f} % | {r.get('marza_pct_pouzita', 30)} % |\n"
            )

        # === SÚBORY ===
        f.write("\n## 📁 Súbory v tomto folderi\n\n")

        # Hlavná akcia
        if len(results) > 1:
            f.write(f"**📧 [POSLI_VSETKY_VARIANTY_{ev_label}_*.command]** — kombinovaný email s {len(results)} PDF prílohami (odporúčané)\n\n")

        for r in results:
            f.write(f"### Variant {r['variant']} ({r['variant_name']})\n")
            f.write(f"- 📧 [Pošli email]({r['variant_name']}/{Path(r['command']).name}) — dvojklik\n")
            f.write(f"- 📄 [PDF (zákaznícky)]({r['variant_name']}/{Path(r['pdf']).name})\n")
            f.write(f"- 📨 EML (záloha pre Win Outlook): `{r['variant_name']}/{Path(r['eml']).name}`\n")
            f.write(f"- 🔒 **Interná kalkulácia (NEPOSIELAJ!):** `{r['variant_name']}/{Path(r['xlsx']).name}`\n\n")

        # === FOLLOW-UP REMINDER ===
        f.write("\n## 📅 Follow-up logika (po odoslaní emailu)\n\n")
        f.write("| Po | Akcia |\n|----|-------|\n")
        f.write("| **D+2** dni | Krátky kontrolný email (ranný brief generuje draft) |\n")
        f.write("| **D+5** dní | Telefonát (bullet points v briefs folderi) |\n")
        f.write("| **D+10** dní | Posledný follow-up s ponukou schôdzky |\n")
        f.write("| **D+14** dní | Manuálne presuň do statusu **Archivované** |\n\n")
        f.write("Ranný brief o **07:00** automaticky updatuje fázu FU + generuje drafty.\n")

    print(f"\n✅ Vygenerované {len(results)} variantov pre {title}")
    print(f"   Folder: {out_dir}")
    print(f"   Súhrn:  {suhrn_path}")

    return {
        "folder": out_dir,
        "folder_url": f"file://{out_dir}",
        "variants": results,
        "suhrn": suhrn_path,
    }


if __name__ == "__main__":
    # Test: simulujem Sedlárov záznam
    test_record = {
        "Zákazník": "Sedlár, Janíkovce",
        "Mesto": "Janíkovce",
        "Telefón": "+421 908 385 802",
        "Email": "stanislav.sedlar@gmail.com",
        "Poznámky": "spotreba: 10000 kWh, adresa: Jána Gustíniho 1064/377, 949 07 Janíkovce",
        "Panel": "LONGi 540 Wp",
        "Menič": "Solinteg MHT-10K-25",
        "Počet panelov": 24,
        "Konštrukcia (typ)": "Plochá strecha — J 13°",
        "Batéria (typ)": "Pylontech Force H3 — 5.12 kWh",
        "Batéria (kWh)": 10.24,
        "Wallbox (typ)": "Solinteg 11 kW (3F)",
        "Marža %": 30,
        "Variant A — FVE": "__YES__",
        "Variant B — FVE + BESS": "__YES__",
        "Variant C — FVE + BESS + Wallbox": "__YES__",
        "ID ponuky": 1,
    }
    result = generate_for_record(test_record)
    print("\n🎉 Hotovo!")
    print(f"   {result['folder']}")
