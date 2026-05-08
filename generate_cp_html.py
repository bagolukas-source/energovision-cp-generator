"""
generate_cp_html.py — Energovision B2C CP generator (HTML→PDF verzia)

Vstup: lead.json
Výstupy: CP_*.pdf (krásne renderované cez WeasyPrint), CP_*.eml, kalkulacia_*.xlsx
"""

import json, sys, os, datetime, subprocess, re
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate

# Re-použijeme logiku z generate_cp.py
sys.path.insert(0, str(Path(__file__).parent))
from generate_cp import (
    load_cennik, vyrataj_konfig, vyrataj_ceny, vyrataj_navratnost,
    vyrob_grafy, vyrob_internu_kalkulaciu, ZHOTOVITEL, DEFAULTS
)

_SD = os.path.dirname(os.path.abspath(__file__))
BRAND_HEADER = os.path.join(_SD, "energovision_header.png")
TEMPLATE = os.path.join(_SD, "cp_template.html")


def fmt_eur(x):
    """123456.78 → '123 456,78 €' (slovensky)"""
    s = f"{x:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", " ")
    return s + " €"


def short(name, max_len=42):
    if len(name) <= max_len: return name
    return name[:max_len-1].rsplit(" ", 1)[0] + "…"


def shorten_konstrukcia(name):
    """'Rovná strecha Juh — balastová konštrukcia 13°' → 'Plochá strecha (J, 13°)'"""
    if "Rovná" in name and "Juh" in name: return "Plochá strecha (J, 13°)"
    if "Rovná" in name: return "Plochá strecha (V/Z, 10°)"
    if "Škridla" in name: return "Šikmá — háky na škridle"
    if "Plech" in name and "Kombi" in name: return "Šikmá — kombivrut na plech"
    if "Falc" in name: return "Šikmá — falcový úchyt"
    return name


def shorten_menic(name):
    """'Solinteg MHT-10K-25 hybridný 10 kW 3F' → 'Solinteg MHT-10K-25'"""
    if " hybridný " in name: return name.split(" hybridný ")[0]
    return name


def shorten_panel(name):
    """'LONGi Hi-MO X10 LR7-60HVH 535-545 Wp čierny rám' → 'LONGi Hi-MO X10 540 Wp'"""
    if "LONGi" in name:
        if "545" in name or "540" in name: return "LONGi Hi-MO X10 540 Wp"
        if "470" in name: return "LONGi Hi-MO X10 470 Wp"
    return short(name, 35)


def _sk_int(x):
    """123456 → '123 456' (slovenský formát)"""
    return f"{int(round(x)):,}".replace(",", " ")

def _sk_dec(x, n=2):
    """12.345 → '12,35' (slovenská desatinná čiarka)"""
    return f"{x:.{n}f}".replace(".", ",")


def gen_copywriting(lead, konfig, ceny, navratnost):
    """Vygeneruje personalizované copywritingové texty na základe leadu."""
    priezvisko = lead["meno"].split()[-1]
    spotreba = lead["rocna_spotreba_kwh"]
    cena_el = lead.get("cena_el_eur_kwh", 0.16)
    rocna_faktura = spotreba * cena_el
    vykon = konfig["vykon_kwp"]
    vyroba = navratnost["rocna_vyroba_kwh"]
    pokrytie = min(100, vyroba / spotreba * 100)
    nadvyroba_pct = (vyroba / spotreba * 100) - 100 if vyroba > spotreba else 0

    # Vek leadu — pre age-aware úvodný pozdrav
    vek_dni = lead.get("vek_dni", 0)

    # Typ domácnosti podľa spotreby
    if spotreba < 3500: typ = "menšia domácnosť (1–2 osoby alebo úsporná prevádzka)"
    elif spotreba < 5500: typ = "štandardná rodina (2–3 osoby)"
    elif spotreba < 8500: typ = "väčšia rodina (3–4 osoby)"
    else: typ = "vysoká spotreba (väčšia rodina alebo dom s tepelným čerpadlom či elektromobilom)"

    # === ÚVODNÝ POZDRAV (titulka) — age-aware ===
    if vek_dni < 14:
        # Čerstvý lead — štandardný úvod
        uvodny_pozdrav = (
            f"Pán {priezvisko}, ďakujem za prejavený záujem. "
            f"Pripravil som pre Vás návrh, ktorý vychádza z údajov, čo ste nám poskytli — "
            f"a z toho, ako u Vás reálne fungujú spotreba aj strecha. Verím, že Vás zaujme."
        )
    elif vek_dni < 60:
        # Stredný lead — vďačnosť za trpezlivosť
        uvodny_pozdrav = (
            f"Pán {priezvisko}, ďakujem za Vašu trpezlivosť. "
            f"Pripravil som pre Vás návrh prispôsobený údajom, ktoré ste nám poskytli pri prvotnom dopyte — "
            f"prepočítaný na aktuálne ceny a podmienky."
        )
    else:
        # Starý lead (60+ dní) — ospravedlnenie + dôvod prečo to ešte stojí
        mesiace = vek_dni // 30
        uvodny_pozdrav = (
            f"Pán {priezvisko}, ospravedlňujem sa za neskorú reakciu na Váš dopyt z pred ~{mesiace} mesiacmi. "
            f"Téma sa medzitým posunula — vyšla nová výzva Zelená domácnostiam 2026 a ceny elektriny "
            f"pokračovali v raste. Pripravil som pre Vás aktualizovanú ponuku — verím, že je u Vás projekt "
            f"FVE stále aktuálny a táto verzia Vás zaujme viac."
        )

    # === POCHOPENIE POTRIEB ===
    p1 = (
        f"Vaša ročná spotreba <strong>{_sk_int(spotreba)} kWh</strong> zodpovedá charakteristike: "
        f"<strong>{typ}</strong>. Pri aktuálnej cene cca {_sk_dec(cena_el)} €/kWh to znamená "
        f"<strong>~ {_sk_int(rocna_faktura)} € ročne</strong> len za samotnú elektrinu. "
        f"A keďže ceny v posledných rokoch rástli o 3–8 % ročne, o päť rokov to môže byť výrazne viac."
    )

    p2_parts = []
    kon = konfig["konstrukcia"]
    if "Rovná" in kon and "Juh" in kon:
        p2_parts.append("Vaša rovná strecha s J orientáciou je pre fotovoltiku ideálna — vieme z nej získať blízke maximum.")
    elif "Rovná" in kon:
        p2_parts.append("Vaša rovná strecha umožňuje optimálne nasmerovanie panelov balastovou konštrukciou.")
    elif "Škridla" in kon:
        p2_parts.append("Vaša šikmá strecha so škridlou je štandardný case, s ktorým máme bohaté skúsenosti.")
    elif "Plech" in kon or "Falc" in kon:
        p2_parts.append("Plechová strecha je pre montáž rýchla a čistá — bez prierazov do krytiny.")
    else:
        p2_parts.append("Pre Vašu konštrukciu strechy máme overené riešenie.")

    if konfig["ma_bateriu"]:
        p2_parts.append("Súčasťou návrhu je aj batériové úložisko, takže časť vyrobenej energie použijete aj večer alebo cez víkend.")
    else:
        p2_parts.append("Pre začiatok Vám neodporúčame batériu — pridať ju budete môcť kedykoľvek neskôr (menič je pripravený).")

    if konfig["ma_wallbox"]:
        p2_parts.append("Pridali sme aj wallbox pre nabíjanie elektromobilu priamo zo slnka.")

    p2 = " ".join(p2_parts)

    # Záver
    if nadvyroba_pct > 20:
        zaver = (
            f"Navrhujeme výkon <strong>{_sk_dec(vykon)} kWp</strong>, ktorý pokryje Vašu spotrebu "
            f"na 100 % a vyrobí ešte {nadvyroba_pct:.0f} % naviac. Prebytky predáme do siete za "
            f"výkupnú cenu, alebo ich neskôr viete uložiť do prípadnej batérie."
        )
    elif pokrytie >= 95:
        zaver = (
            f"Navrhujeme výkon <strong>{_sk_dec(vykon)} kWp</strong>, ktorý pokryje takmer celú Vašu spotrebu. "
            f"S batériou alebo posunom spotreby do dňa (pranie, varenie, nabíjanie) sa dostanete blízko energetickej sebestačnosti."
        )
    else:
        zaver = (
            f"Navrhujeme výkon <strong>{_sk_dec(vykon)} kWp</strong>, ktorý pokryje cca "
            f"<strong>{pokrytie:.0f} %</strong> Vašej spotreby. Zvyšok dokúpite ako doteraz, "
            f"ale za výrazne nižší ročný účet."
        )

    # === RIEŠENIE INTRO ===
    rieseni_intro = (
        f"Zostava postavená tak, aby vyrobila ~{_sk_int(vyroba)} kWh ročne a pokryla "
        f"{pokrytie:.0f} % Vašej spotreby. Komponenty sú overené značky, montáž robíme vlastným tímom."
    )

    # === BENEFITY ===
    rocna_uspora = navratnost["rocne_uspora_eur"]
    nav_rokov = navratnost["navratnost_rokov"]
    benefit_uspora = (
        f"Ročná úspora <strong>~{_sk_int(rocna_uspora)} €</strong> pri dnešných cenách. "
        f"Keďže ceny elektriny rastú a panely majú degradáciu len ~0,5 % ročne, úspora sa s každým rokom zvyšuje. "
        f"Investícia sa Vám vráti za <strong>~{_sk_dec(nav_rokov, 1)} rokov</strong> — a potom ďalších 15+ rokov vyrábate prakticky zadarmo."
    )

    benefit_nezavislost = (
        "Distribučky každý rok upravujú ceny — a smerujú nahor. S vlastnou FVE zafixujete cenu časti svojej "
        "spotreby na 25+ rokov dopredu. Žiadne nemilé prekvapenia keď príde nová tarifa."
    )

    if konfig["ma_bateriu"]:
        benefit_bateria = (
            f"Batéria s kapacitou {_sk_dec(konfig['bateria_kwh'], 1)} kWh uloží to, čo cez deň nestihnete spotrebovať. "
            "Večer a v noci čerpáte vlastnú elektrinu zo slnka, nie zo siete. Samospotreba sa tak zvyšuje "
            "z bežných ~70 % až na 90 %+."
        )
    else:
        benefit_bateria = (
            "Hybridný menič v tejto zostave je pripravený na neskoršie pripojenie batérie. "
            "Keď sa rozhodnete (typicky po prvej zime, keď uvidíte reálne čísla), pridáme ju "
            "bez väčších úprav. Žiadny duplicitný menič, žiadne prerábanie."
        )

    # === NÁVRATNOSŤ TEXT ===
    navratnost_text = (
        f"Pri dnešných cenách elektriny ({_sk_dec(cena_el)} €/kWh) sa investícia po dotácii "
        f"<strong>{_sk_int(ceny['cena_finalna'])} €</strong> vráti za "
        f"<strong>~{_sk_dec(nav_rokov, 1)} rokov</strong>. "
        f"S rastom cien elektriny (3 % ročne) bude návratnosť reálne kratšia. "
        f"Za 25 rokov životnosti panelov ušetríte <strong>~{_sk_int(navratnost['uspora_25_rokov'])} €</strong>."
    )

    # === UZATVORENIE ===
    uzatvorenie_p1 = (
        f"Pán {priezvisko}, fotovoltika je rozhodnutie na 25+ rokov. "
        f"Nie je to o tom, kto má najlacnejšiu ponuku v tomto týždni — je to o tom, "
        f"komu zveríte strechu svojho domu a kto Vám zdvihne telefón, keď budete potrebovať servis o päť rokov."
    )
    uzatvorenie_p2 = (
        f"Vašu ponuku mám rezervovanú do <strong>{(datetime.date.today() + datetime.timedelta(days=lead.get('platnost_dni',30))).strftime('%d. %m. %Y')}</strong>. "
        f"Ak budete mať akúkoľvek otázku — od technického detailu po platobné podmienky — "
        f"som Vám k dispozícii telefonicky aj e-mailom. Nečakajte, ozvite sa."
    )

    return {
        "uvodny_pozdrav": uvodny_pozdrav,
        "pochopenie_p1": p1,
        "pochopenie_p2": p2,
        "pochopenie_zaver": zaver,
        "rieseni_intro": rieseni_intro,
        "benefit_uspora": benefit_uspora,
        "benefit_nezavislost": benefit_nezavislost,
        "benefit_batería": benefit_bateria,
        "navratnost_text": navratnost_text,
        "uzatvorenie_p1": uzatvorenie_p1,
        "uzatvorenie_p2": uzatvorenie_p2,
    }


def vyrob_html_pdf(lead, konfig, ceny, navratnost, grafy, out_pdf):
    from jinja2 import Environment, FileSystemLoader
    from weasyprint import HTML, CSS

    today = datetime.date.today()
    platnost = today + datetime.timedelta(days=lead.get("platnost_dni", DEFAULTS["platnost_dni"]))

    obch = lead.get("obchodnik", DEFAULTS["obchodnik"])

    # extrahnúť Wp panela z názvu
    panel_n = konfig["panel"]
    wp_match = re.search(r"(\d{3})\s*Wp", panel_n.replace("-", " "))
    wp = int(wp_match.group(1)) if wp_match else int(round(konfig["vykon_kwp"] * 1000 / konfig["pocet_panelov"]))
    if 535 <= wp <= 545: wp = 540

    distribucka_short = lead.get("distribucka", "ZSD")
    distribucka_full = {
        "ZSD": "Západoslovenskou distribučnou (ZSD)",
        "SSD": "Stredoslovenskou distribučnou (SSD)",
        "VSD": "Východoslovenskou distribučnou (VSD)",
    }.get(distribucka_short, "distribučnou spoločnosťou")

    # orientácia
    orientacia = lead.get("orientacia", "J")
    orientacia_text = {
        "J": "južná orientácia — najproduktívnejšia",
        "JV": "juhovýchodná — výborná, prevažujúce ranné slnko",
        "JZ": "juhozápadná — výborná, prevažujúce popoludňajšie slnko",
        "V": "východná — solídna ranná produkcia",
        "Z": "západná — solídna popoludňajšia produkcia",
    }.get(orientacia, "orientácia podľa zamerania")

    cw = gen_copywriting(lead, konfig, ceny, navratnost)

    ctx = {
        "lead": lead,
        "obch": obch,
        "today": today.strftime("%d. %m. %Y"),
        "platnost_do": platnost.strftime("%d. %m. %Y"),
        "cislo_ponuky": lead.get("cislo_ponuky", f"PON-{today:%Y-%m%d}"),
        "header_img": f"file://{BRAND_HEADER}",
        "vykon_kwp_sk": f"{konfig['vykon_kwp']:.2f}".replace(".", ","),
        "pocet_panelov": konfig["pocet_panelov"],
        "panel_short": shorten_panel(konfig["panel"]),
        "menic_short": shorten_menic(konfig["menic"]),
        "konstrukcia_short": shorten_konstrukcia(konfig["konstrukcia"]),
        "wp_panel": wp,
        "rocna_vyroba": navratnost["rocna_vyroba_kwh"],
        "rocne_uspora": navratnost["rocne_uspora_eur"],
        "navratnost_rokov_sk": f"{navratnost['navratnost_rokov']:.1f}".replace(".", ","),
        "uspora_25_rokov": navratnost["uspora_25_rokov"],
        "kg_co2_rok": navratnost["kg_co2_rok"],
        "pokrytie_pct": min(100, navratnost["rocna_vyroba_kwh"] / lead["rocna_spotreba_kwh"] * 100),
        "ma_bateriu": konfig["ma_bateriu"],
        "ma_wallbox": konfig["ma_wallbox"],
        "bateria_kwh_sk": f"{konfig['bateria_kwh']:.2f}".replace(".", ",") if konfig["ma_bateriu"] else "",
        "wallbox_short": "11 kW 3F" if konfig["ma_wallbox"] else "",
        "cena_bez_dph_eur": fmt_eur(ceny["cena_bez_dph"]),
        "dph_eur": fmt_eur(ceny["cena_s_dph"] - ceny["cena_bez_dph"]),
        "cena_s_dph_eur": fmt_eur(ceny["cena_s_dph"]),
        "dotacia_eur": fmt_eur(ceny["dotacia"]),
        "zlava_eur": fmt_eur(ceny["zlava_eur"]),
        "cena_finalna_eur": fmt_eur(ceny["cena_finalna"]),
        "dotacia": ceny["dotacia"],
        "zlava": ceny["zlava_eur"],
        "chart_uspora": f"file://{grafy['uspora']}",
        "chart_vyroba": f"file://{grafy['vyroba']}",
        "chart_porovnanie": f"file://{grafy['porovnanie']}",
        "platby": lead.get("platby", "60 % zálohová faktúra vopred  ·  30 % po nainštalovaní elektrárne  ·  10 % po protokolárnom odovzdaní"),
        "distribucka_full": distribucka_full,
        "distribucka_short": distribucka_short,
        "orientacia_text": orientacia_text,
        "rocna_faktura_eur": f"{lead['rocna_spotreba_kwh'] * lead.get('cena_el_eur_kwh', 0.16):,.0f}".replace(",", " "),
        "cena_el_sk": f"{lead.get('cena_el_eur_kwh', 0.16):.2f}".replace(".", ","),
        **cw,
    }

    env = Environment(loader=FileSystemLoader(str(Path(TEMPLATE).parent)))
    tmpl = env.get_template(Path(TEMPLATE).name)
    html_str = tmpl.render(**ctx)

    # uložím aj HTML pre debug
    html_path = out_pdf.replace(".pdf", ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_str)

    HTML(string=html_str, base_url=str(Path(TEMPLATE).parent)).write_pdf(out_pdf)
    return out_pdf


def vyrob_eml_v2(lead, konfig, ceny, navratnost, pdf_path, out_path):
    obch = lead.get("obchodnik", DEFAULTS["obchodnik"])
    body = f"""Dobrý deň, pán {lead['meno'].split()[-1]},

ďakujem za Váš záujem o fotovoltickú elektráreň pre Váš dom v {lead['mesto']}.
V prílohe Vám posielam cenovú ponuku spracovanú na základe údajov, ktoré ste mi poskytli.

Krátko zhrnuté:
- Výkon FVE: {konfig['vykon_kwp']:.2f} kWp ({konfig['pocet_panelov']} ks panelov)
- Predpokladaná ročná výroba: {navratnost['rocna_vyroba_kwh']:,.0f} kWh
- Predpokladaná ročná úspora: {navratnost['rocne_uspora_eur']:,.0f} EUR
- Cena s DPH: {ceny['cena_s_dph']:,.2f} EUR
- Cena po dotácii Zelená domácnostiam: {ceny['cena_finalna']:,.2f} EUR
- Návratnosť: cca {navratnost['navratnost_rokov']:.1f} rokov

Ako ďalší krok navrhujem bezplatnú obhliadku, kde upresníme technické detaily
a finalizujeme cenu. Stačí zavolať alebo odpovedať na tento email.

Ponuka je platná {lead.get('platnost_dni', 30)} dní.

Ak máte akékoľvek otázky, som Vám k dispozícii.

S pozdravom,
{obch['meno']}
{obch['funkcia']}, {ZHOTOVITEL['nazov']}
{obch['tel']}  |  {obch['email']}
""".replace(",", " ")

    msg = MIMEMultipart()
    msg['From'] = ''
    msg['To'] = lead.get("email", "")
    msg['Subject'] = f"Cenová ponuka FVE pre Váš dom — Energovision"
    msg['Date'] = formatdate(localtime=True)
    msg['X-Unsent'] = '1'
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    if pdf_path and Path(pdf_path).exists():
        with open(pdf_path, 'rb') as f:
            part = MIMEBase('application', 'pdf')
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{Path(pdf_path).name}"')
            msg.attach(part)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(msg.as_string())


def main(lead_path):
    with open(lead_path, encoding="utf-8") as f:
        lead = json.load(f)

    print(f"📋 Lead: {lead['meno']} ({lead['mesto']})")
    cennik = load_cennik()
    konfig = vyrataj_konfig(lead, cennik)
    ceny = vyrataj_ceny(konfig, lead)
    navratnost = vyrataj_navratnost(konfig, ceny, lead)

    priezv = lead["meno"].split()[-1].replace(" ", "_")
    mesto = lead["mesto"].split(",")[0].replace(" ", "_")
    base = f"CP_{priezv}_{mesto}_v2"
    interna = f"kalkulacia_{priezv}_{mesto}_v2"
    out_dir = lead.get("out_dir", "/sessions/magical-eager-gates/mnt/outputs")

    print("📊 Generujem grafy...")
    grafy = vyrob_grafy(navratnost, lead, out_dir, base)

    print(f"🎨 Generujem krásne PDF cez HTML+CSS3 ...")
    pdf_path = f"{out_dir}/{base}.pdf"
    vyrob_html_pdf(lead, konfig, ceny, navratnost, grafy, pdf_path)

    print(f"💼 Generujem internú kalkuláciu ...")
    vyrob_internu_kalkulaciu(lead, konfig, ceny, navratnost, f"{out_dir}/{interna}.xlsx")

    print(f"✉️  Generujem .eml draft ...")
    vyrob_eml_v2(lead, konfig, ceny, navratnost, pdf_path, f"{out_dir}/{base}.eml")

    print(f"\n✅ Hotovo:")
    print(f"   {pdf_path}")
    print(f"   {out_dir}/{base}.eml")
    print(f"   {out_dir}/{interna}.xlsx")
    print(f"\n💰 Súhrn cien:")
    print(f"   Cena s DPH:        {ceny['cena_s_dph']:>12,.2f} €".replace(",", " "))
    print(f"   Dotácia:           {-ceny['dotacia']:>12,.0f} €".replace(",", " "))
    print(f"   Cena po dotácii:   {ceny['cena_po_dotacii']:>12,.2f} €".replace(",", " "))
    print(f"   Návratnosť:        {navratnost['navratnost_rokov']:>12.1f} rokov")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "lead_sedlar.json")
