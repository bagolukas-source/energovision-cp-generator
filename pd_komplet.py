"""
KOMPLETNÉ generovanie PD balíka (ekvivalent Make „Komplet automatizácia Firma" — časť projekcia).
Produkuje: PD jadro (docx) + RDC schémy (PDF per string) + protokol ochrany per DIS (PDF) + admin docx.
Vracia list {kluc, filename, bytes}.
"""
import io
import re
import logging
import tempfile
from pathlib import Path

import generuj_pd as G
import pdf_forms as PF

log = logging.getLogger("pd_komplet")
HERE = Path(__file__).resolve().parent


def _docx_bytes(render_fn, lead_data):
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
        p = tf.name
    render_fn(lead_data, p)
    b = open(p, "rb").read()
    try:
        Path(p).unlink()
    except Exception:
        pass
    return b


def _base(meno):
    pz = (meno or "Klient").split()[-1] if meno else "Klient"
    return re.sub(r"[^A-Za-zÁ-ž0-9]+", "_", pz).strip("_") or "Klient"


# ── protokol ochrany per DIS — field maps z Make (resolved na ctx) ──
def _protokol_fields(dis, ctx):
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
    return None, None  # SSD = XFA, zatiaľ vynechané


def _rdc_config(strings_per_rdc):
    """Vyber base RDC schému 2xN podľa počtu stringov."""
    avail = [1, 2, 4, 6, 10, 12]
    n = min(avail, key=lambda x: abs(x - max(1, strings_per_rdc)))
    return f"2x{n}"


def vygeneruj_pd_komplet(lead_data):
    """Komplet PD balík. lead_data má navyše: pocet_menicov, stringov_na_rdc, stupen_skr, hlavicka_pd, je_dsv."""
    ctx = G._build_ctx(lead_data)
    if not lead_data.get("dis"):
        g = G._resolve_dis_from_psc(lead_data.get("psc"))
        if g:
            lead_data["dis"] = g
            ctx = G._build_ctx(lead_data)
    dis = (ctx.get("dis") or "").upper()
    base = _base(lead_data.get("meno_priezvisko"))
    ev = lead_data.get("ev_id", "EV-XX")
    je_dsv = bool(lead_data.get("je_dsv"))
    stupen_full = "Dokumentácia skutočného vyhotovenia" if je_dsv else ctx.get("stupen_projektu", "")
    stupen_skr = "DSV" if je_dsv else (lead_data.get("stupen_skr") or (stupen_full.split()[0] if stupen_full else ""))
    pref = "DSV" if je_dsv else "PD"

    out = []

    def add(kluc, fname, data):
        out.append({"kluc": kluc, "filename": fname, "data": data})

    # 1) PD jadro (docx)
    add("kryci", f"{ev}_{pref}_00_Kryci_list_{base}.docx", _docx_bytes(G.gen_kryci_list, lead_data))
    add("titul_zoznam_pouvv", f"{ev}_{pref}_01_Titul_Zoznam_PoUVV_{base}.docx", _docx_bytes(G.gen_tit_zoz_pouvv_b2b, lead_data))
    add("suhrnna", f"{ev}_{pref}_02_Suhrnna_sprava_{base}.docx", _docx_bytes(G.gen_suhrnna_sprava, lead_data))
    add("technicka", f"{ev}_{pref}_03_Technicka_sprava_{base}.docx", _docx_bytes(G.gen_technicka_sprava, lead_data))

    # 2) RDC schémy (PDF) — jedna na menič (počet meničov), config podľa stringov
    n_rdc = int(lead_data.get("pocet_menicov") or 1)
    strings_per = int(lead_data.get("stringov_na_rdc") or 2)
    cfg = _rdc_config(strings_per)
    for i in range(1, n_rdc + 1):
        try:
            pdf = PF.vyplň_rdc(ctx, cfg, i, stupen_full=stupen_full, stupen_skr=stupen_skr)
            add(f"rdc_{i}", f"{ev}_{pref}_05_Schema_RDC{i}_{base}.pdf", pdf)
        except Exception as e:
            log.warning("[komplet] RDC%d zlyhal: %s", i, e)

    # 3) Protokol ochrany per DIS (PDF)
    rel, fields = _protokol_fields(dis, ctx)
    if rel:
        try:
            add("protokol_ochrany", f"{ev}_Protokol_ochrany_{dis}_{base}.pdf", PF.fill_pdf(rel, fields))
        except Exception as e:
            log.warning("[komplet] protokol %s zlyhal: %s", dis, e)

    # 4) Admin docx (Revízna, Vyhlásenie, Preberacie)
    admin = [
        ("revizna", "Revizna_sprava_FVZ.docx", "Revizna_sprava_FVZ"),
        ("vyhlasenie", "Vyhlasenie_projektant.docx", "Vyhlasenie_projektant"),
        ("preberaci_komponenty", "Preberaci_protokol_komponenty.docx", "Preberaci_protokol_komponenty"),
        ("preberaci_final", "Preberaci_protokol_final.docx", "Preberaci_protokol_final"),
    ]
    for kluc, tpl, fn in admin:
        try:
            b = _render_admin(tpl, ctx)
            add(kluc, f"{ev}_{fn}_{base}.docx", b)
        except Exception as e:
            log.warning("[komplet] admin %s zlyhal: %s", tpl, e)

    log.info("[komplet] %s: %d dokumentov (dis=%s, rdc=%d, dsv=%s)", base, len(out), dis, n_rdc, je_dsv)
    return out


def _render_admin(tpl_name, ctx):
    """Render admin docx (templates_admin/) cez docxtpl s ctx."""
    from docxtpl import DocxTemplate
    src = HERE / "templates_admin" / tpl_name
    doc = DocxTemplate(str(src))
    doc.render(ctx)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
