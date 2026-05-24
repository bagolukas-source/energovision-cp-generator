"""
B2B PDF generator — klientske + interné PDF z b2b_quotes.
"""
import os
import io
import logging
import tempfile
from datetime import datetime

log = logging.getLogger(__name__)

STRECHA_LABELS = {
    "vychod_zapad": "Rovná strecha (Východ-Západ)",
    "trapez": "Trapézový plech",
    "skridla": "Škridla / šindeľ",
    "falcovany_plech": "Falcovaný plech",
    "plech_kombi_skrutka": "Plech (kombi skrutka)",
    "juzna": "Južná konštrukcia",
    "zemne_skrutky": "Pozemná (zemné skrutky)",
    "corab": "CORAB WS-024R (pozemná)",
}


def _render_pdf(template_path: str, data: dict, output_path: str):
    """Render Jinja template → HTML → WeasyPrint PDF."""
    from jinja2 import Environment, FileSystemLoader
    import weasyprint
    
    template_dir = os.path.dirname(template_path) or "."
    template_name = os.path.basename(template_path)
    env = Environment(loader=FileSystemLoader(template_dir))
    tpl = env.get_template(template_name)
    html_str = tpl.render(**data)
    
    weasyprint.HTML(string=html_str, base_url=template_dir).write_pdf(output_path)


def generate_quote_pdf(sb, quote_id: str, mode: str = "klient") -> dict:
    """Vygeneruje PDF pre quote_id. mode = 'klient' | 'internal'.
    Vráti {'pdf_url': ..., 'storage_path': ...}.
    """
    # Načítaj quote + items + customer
    quote_res = sb.table("b2b_quotes").select("*, customers(name, email, ico, dic)").eq("id", quote_id).single().execute()
    quote = quote_res.data
    if not quote:
        raise ValueError(f"Quote {quote_id} not found")
    
    items_res = sb.table("b2b_quote_items").select("*").eq("quote_id", quote_id).order("position").execute()
    items = items_res.data or []
    
    customer = quote.get("customers") or {}
    
    template_name = "b2b_quote_internal.html" if mode == "internal" else "b2b_quote_klient.html"
    template_path = os.path.join(os.path.dirname(__file__), template_name)
    
    data = {
        "quote": quote,
        "items": items,
        "customer": customer,
        "strecha_label": STRECHA_LABELS.get(quote.get("typ_strechy"), quote.get("typ_strechy")),
        "datum": datetime.now().strftime("%d.%m.%Y"),
    }
    
    # Render PDF
    tmp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_pdf.close()
    try:
        _render_pdf(template_path, data, tmp_pdf.name)
        with open(tmp_pdf.name, "rb") as f:
            pdf_bytes = f.read()
    finally:
        os.unlink(tmp_pdf.name)
    
    # Upload do Supabase Storage
    storage_path = f"b2b_quotes/{quote.get('code', quote_id)}/{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    sb.storage.from_("documents").upload(
        storage_path,
        pdf_bytes,
        {"content-type": "application/pdf", "upsert": "true"}
    )
    public_url = sb.storage.from_("documents").get_public_url(storage_path)
    
    # Update quote with PDF URL
    field = "pdf_url" if mode == "klient" else "internal_pdf_url"
    sb.table("b2b_quotes").update({field: public_url}).eq("id", quote_id).execute()
    
    return {
        "pdf_url": public_url,
        "storage_path": storage_path,
        "mode": mode,
        "quote_code": quote.get("code"),
    }
