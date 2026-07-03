"""
Vypĺňanie AcroForm PDF formulárov (RDC schémy, protokoly) — ekvivalent Make pdf-co.
Field mapping je prebraté z Make blueprintu (parameters.fields). pypdf zapíše hodnoty
do form polí + nastaví NeedAppearances, aby boli viditeľné.
"""
import io
import logging
from pathlib import Path
from pypdf import PdfReader, PdfWriter

log = logging.getLogger("pdf_forms")
HERE = Path(__file__).resolve().parent
PDF_DIRS = [HERE / "templates_pdf"]


def _find(rel):
    for d in PDF_DIRS:
        p = d / rel
        if p.exists():
            return p
    raise FileNotFoundError(f"PDF šablóna {rel} nenájdená")


def fill_pdf(template_rel, values):
    """Vyplní AcroForm polia v PDF šablóne. template_rel napr. 'rdc/2x2.pdf'. Vráti bytes.
    Robustné: zapíše /V priamo na widget annotations + NeedAppearances (zvládne formy bez /AP)."""
    from pypdf.generic import NameObject, TextStringObject
    src = _find(template_rel)
    reader = PdfReader(str(src))
    writer = PdfWriter()
    writer.append(reader)
    vals = {k: ("" if v is None else str(v)) for k, v in values.items()}
    from pypdf.generic import NameObject, TextStringObject
    root = writer._root_object
    acro = root.get("/AcroForm")
    def _walk(flds):
        for ref in flds:
            obj = ref.get_object()
            nm = obj.get("/T")
            if nm is not None and str(nm) in vals:
                obj[NameObject("/V")] = TextStringObject(vals[str(nm)])
                if "/AP" in obj:
                    del obj[NameObject("/AP")]
                for k in (obj.get("/Kids") or []):
                    ko = k.get_object()
                    if "/AP" in ko:
                        del ko[NameObject("/AP")]
            if "/Kids" in obj and (nm is None or str(nm) not in vals):
                _walk(obj.get("/Kids") or [])
    if acro is not None:
        ao = acro.get_object()
        if "/Fields" in ao:
            _walk(ao.get("/Fields") or [])
    try:
        writer.set_need_appearances_writer(True)
    except Exception:
        pass
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def rdc_field_values(ctx, i, stupen_full=None, stupen_skr=None):
    """26 polí RDC schémy (Text1..Text26) podľa Make mapovania."""
    sf = stupen_full or ctx.get("stupen_projektu", "")
    ss = stupen_skr or (sf.split()[0] if sf else "")
    sidlo = f'{ctx.get("ulica_a_cislo","")}, {ctx.get("psc_mesto","")}'.strip(", ")
    prev = f'{ctx.get("preulica_a_cislo","")}'.strip(", ")
    ozn = ctx.get("oznacenie") or ctx.get("OZN", "")
    parc = f'Parcela {ctx.get("parcely","")}'
    return {
        "Text1": ctx.get("nazov_zakaznika", ""), "Text19": ctx.get("nazov_zakaznika", ""),
        "Text2": sidlo, "Text3": ozn, "Text18": ozn,
        "Text4": parc, "Text21": parc,
        "Text5": sf, "Text12": ss, "Text23": ss,
        "Text7": ctx.get("cislo_zakazky", ""), "Text22": ctx.get("cislo_zakazky", ""),
        "Text8": ctx.get("datum", ""), "Text14": ctx.get("datum", ""), "Text15": ctx.get("datum", ""),
        "Text9": ctx.get("vypracoval", ""),
        "Text10": ctx.get("kontroloval", ""), "Text17": ctx.get("kontroloval", ""),
        "Text11": ctx.get("zodpovedny_projektant", ""),
        "Text13": ctx.get("vypracovalsk", ""), "Text16": ctx.get("vypracovalsk", ""),
        "Text20": prev,
        "Text6": f"05- Schéma zapojenia- RDC{i}",
        "Text24": f"+RDC{i}", "Text25": f"Rozvádzač +RDC{i}", "Text26": f"+FG{i}",
    }


def vyplň_rdc(ctx, config, i, stupen_full=None, stupen_skr=None):
    """Vyplní jednu RDC schému. config = názov base šablóny bez .pdf (napr. '2x2')."""
    return fill_pdf(f"rdc/{config}.pdf", rdc_field_values(ctx, i, stupen_full, stupen_skr))


def rfv_field_values(ctx, stupen_full=None, stupen_skr=None):
    """RFV schéma zapojenia (S1/S2/M1–M4) — rovnaká rohová pečiatka ako RDC (Text1–Text23),
    Text6 je konštanta bez RDC čísla; Text24–26 (+RDC/+FG označenia) RFV nemá."""
    vals = rdc_field_values(ctx, 0, stupen_full, stupen_skr)
    vals["Text6"] = "05- Schéma zapojenia"
    for k in ("Text24", "Text25", "Text26"):
        vals.pop(k, None)
    return vals


def vyplň_rfv(ctx, name, stupen_full=None, stupen_skr=None):
    """Vyplní RFV schému zapojenia. name = S1/S2/M1..M4 (bez .pdf)."""
    return fill_pdf(f"rfv/{name}.pdf", rfv_field_values(ctx, stupen_full, stupen_skr))
