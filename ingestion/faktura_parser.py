# -*- coding: utf-8 -*-
"""Faktúra → rozpad tarifu (AI-asistovaná extrakcia). Zvláda PDF aj XLS/XLSX/CSV
od rôznych dodávateľov (SSE/ZSE/VSD/Energetika Slovensko...) cez Claude.
Výstup: tarif_* polia pre analyza_om."""
import io, json, logging
log = logging.getLogger(__name__)


def extract_text(file_bytes: bytes, filename: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".pdf"):
        try:
            import pdfplumber
            out = []
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for pg in pdf.pages[:8]:
                    out.append(pg.extract_text() or "")
            return "\n".join(out)
        except Exception as e:
            log.error("pdf extract failed: %s", e)
            return ""
    if fn.endswith((".xls", ".xlsx", ".xlsm")):
        try:
            import pandas as pd
            xls = pd.ExcelFile(io.BytesIO(file_bytes))
            out = []
            for sh in xls.sheet_names[:6]:
                df = xls.parse(sh, header=None, dtype=str)
                out.append(f"# Hárok: {sh}\n" + df.fillna("").astype(str).to_csv(index=False))
            return "\n".join(out)[:60000]
        except Exception as e:
            log.error("xls extract failed: %s", e)
            return ""
    try:
        return file_bytes.decode("utf-8", "replace")[:60000]
    except Exception:
        return ""


def ai_extract_tarif(text: str) -> dict:
    """Claude vytiahne zložky tarifu z textu faktúry → tarif_* schéma (€/MWh)."""
    if not text.strip():
        return {}
    sysp = (
        "Si parser faktúr za elektrinu pre SK B2B odberné miesta. Z textu faktúry vytiahni zložky ceny a údaje OM.\n"
        "Ceny za kWh preveď na €/MWh (× 1000). Pevnú zložku distribúcie nechaj v €/kW/mes.\n"
        "Vráť STRIKTNÝ JSON (žiadny markdown), čísla ako number, chýbajúce ako null:\n"
        "{\n"
        '  "tarif_silova_eur_mwh": <silová/komoditná zložka dodávky elektriny €/MWh>,\n'
        '  "tarif_distribucia_eur_mwh": <variabilná zložka tarify za distribúciu (vrát. prenosu) €/MWh>,\n'
        '  "tarif_tps_eur_mwh": <platba za prevádzkovanie systému / TPS €/MWh>,\n'
        '  "tarif_oze_eur_mwh": <platba za systémové služby (SO) €/MWh>,\n'
        '  "tarif_ostatne_eur_mwh": <súčet: distribučné straty + odvod do jadrového fondu + spotrebná daň €/MWh>,\n'
        '  "tarif_fix_mes_eur": <pevná zložka tarify za distribúciu €/kW/mes>,\n'
        '  "eic_om": <EIC kód OM ak je>, "cislo_om": <číslo miesta spotreby>,\n'
        '  "om_sadzba": <distribučná sadzba napr. FirmaDvojtarif X2 (VN)>,\n'
        '  "dodavatel": <názov dodávateľa>, "obdobie": <fakturačné obdobie>,\n'
        '  "vt_kwh": <odber VT kWh>, "nt_kwh": <odber NT kWh>,\n'
        '  "om_mrk_kw": <MRK kW ak je>, "om_rk_kw": <rezervovaná kapacita / pevná zložka × kW>\n'
        "}\n"
        "Ber jednotkové ceny (€/mer.j.), NIE celkové sumy. Ak je viac období, ber posledné/reprezentatívne."
    )
    try:
        from anthropic import Anthropic
        msg = Anthropic().messages.create(model="claude-sonnet-4-5-20250929", max_tokens=1200, temperature=0,
            system=sysp, messages=[{"role": "user", "content": "Text faktúry:\n" + text[:40000]}])
        t = msg.content[0].text.strip()
        if t.startswith("```"): t = t.split("\n", 1)[1].rsplit("```", 1)[0]
        if t.lstrip().startswith("json"): t = t.lstrip()[4:]
        return json.loads(t)
    except Exception as e:
        log.error("ai_extract_tarif failed: %s", e)
        return {}


def parse_faktura(file_bytes: bytes, filename: str) -> dict:
    text = extract_text(file_bytes, filename)
    data = ai_extract_tarif(text)
    # odvodiť VT/ST/NT % ak sú kWh
    vt = data.get("vt_kwh"); nt = data.get("nt_kwh")
    if vt and nt:
        tot = float(vt) + float(nt)
        if tot > 0:
            data["vt_pct"] = round(float(vt) / tot * 100)
            data["nt_pct"] = round(float(nt) / tot * 100)
    data["_text_len"] = len(text)
    return data
