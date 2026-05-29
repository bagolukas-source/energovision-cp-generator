"""
Raynet CRM import — stiahne firmy, osoby, leady, obchodné prípady,
ponuky, faktúry a aktivity z Raynetu cez REST API v2 a uloží do Supabase.

Spustenie:
    python raynet_import.py [--dry-run] [--entity company,lead,...]

ENV vars (musia byť nastavené na Renderi):
    RAYNET_INSTANCE — napr. "energovision"
    RAYNET_USER     — email užívateľa
    RAYNET_KEY      — API kľúč

Idempotentné — používa external_source='raynet' + external_id (raynet ID).
Pri opakovanom behu sa záznamy aktualizujú (UPSERT).
"""
import os
import sys
import json
import time
import logging
import argparse
import base64
from typing import Iterator, Optional
import requests

log = logging.getLogger("raynet")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

RAYNET_INSTANCE = os.environ.get("RAYNET_INSTANCE", "energovision")
RAYNET_USER = os.environ.get("RAYNET_USER", "")
RAYNET_KEY = os.environ.get("RAYNET_KEY", "")
RAYNET_BASE = f"https://app.raynet.cz/api/v2"

SUPABASE_URL = os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not (SUPABASE_URL and SUPABASE_KEY):
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in ENV")


def raynet_get(endpoint: str, params: dict = None) -> dict:
    """GET requst na Raynet API s basic auth + instance header."""
    if not (RAYNET_USER and RAYNET_KEY):
        raise RuntimeError("Missing RAYNET_USER or RAYNET_KEY in ENV")
    headers = {
        "X-Instance-Name": RAYNET_INSTANCE,
        "Accept": "application/json",
    }
    r = requests.get(
        f"{RAYNET_BASE}/{endpoint.lstrip('/')}",
        auth=(RAYNET_USER, RAYNET_KEY),
        headers=headers,
        params=params or {},
        timeout=60,
    )
    if not r.ok:
        log.error("Raynet API error %s: %s", r.status_code, r.text[:300])
        r.raise_for_status()
    return r.json()


def raynet_iter(endpoint: str, page_size: int = 100, max_pages: int = 1000) -> Iterator[dict]:
    """Paginuje cez Raynet endpoint, yield-uje jednotlivé záznamy."""
    offset = 0
    pages = 0
    while pages < max_pages:
        data = raynet_get(endpoint, {"limit": page_size, "offset": offset})
        items = data.get("data") or data.get("items") or []
        if not items:
            return
        for item in items:
            yield item
        if len(items) < page_size:
            return
        offset += page_size
        pages += 1
        time.sleep(0.1)


def sb_upsert(table: str, rows: list, conflict_col: str = "external_id") -> int:
    """Upsert do Supabase cez REST. Vracia počet vložených/aktualizovaných."""
    if not rows:
        return 0
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": f"resolution=merge-duplicates,return=minimal",
    }
    # Bulk upsert v batchoch po 100
    total = 0
    BATCH = 100
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i+BATCH]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict=external_source,external_id",
            headers=headers,
            json=batch,
            timeout=60,
        )
        if not r.ok:
            log.error("Supabase upsert %s failed: %s %s", table, r.status_code, r.text[:500])
            r.raise_for_status()
        total += len(batch)
    return total


# ============================================================
# MAPPERS — Raynet → Supabase
# ============================================================

def raynet_company_to_customer(c: dict) -> dict:
    """Mapuje Raynet company → customers row."""
    primary_addr = (c.get("primaryAddress") or {})
    return {
        "external_source": "raynet",
        "external_id": str(c.get("id")),
        "external_synced_at": "now()",
        "company_name": c.get("name"),
        "ico": c.get("regNumber"),
        "dic": c.get("taxNumber"),
        "address": primary_addr.get("address"),
        "city": primary_addr.get("city"),
        "psc": primary_addr.get("zipCode"),
        "email": (c.get("emails") or [{}])[0].get("contact") if c.get("emails") else None,
        "phone": (c.get("phones") or [{}])[0].get("contact") if c.get("phones") else None,
        "type": "company",
        "notes": c.get("notice"),
    }


def raynet_person_to_customer(p: dict) -> dict:
    """Raynet person → customers row (B2C)."""
    primary_addr = (p.get("primaryAddress") or {})
    name_parts = (p.get("name") or "").split(" ", 1)
    first = name_parts[0] if name_parts else ""
    last = name_parts[1] if len(name_parts) > 1 else ""
    return {
        "external_source": "raynet",
        "external_id": f"person_{p.get('id')}",
        "external_synced_at": "now()",
        "first_name": p.get("firstName") or first,
        "last_name": p.get("lastName") or last,
        "address": primary_addr.get("address"),
        "city": primary_addr.get("city"),
        "psc": primary_addr.get("zipCode"),
        "email": (p.get("emails") or [{}])[0].get("contact") if p.get("emails") else None,
        "phone": (p.get("phones") or [{}])[0].get("contact") if p.get("phones") else None,
        "type": "person",
        "notes": p.get("notice"),
    }


def raynet_lead_to_lead(l: dict, customer_id_map: dict) -> Optional[dict]:
    """Raynet lead → leads row. Vyžaduje existujúce customer_id v map."""
    cust_ext = l.get("company") or l.get("person")
    if not cust_ext:
        return None
    cust_key = f"raynet:{cust_ext.get('id')}"
    cust_id = customer_id_map.get(cust_key)
    if not cust_id:
        log.warning("Lead %s: customer %s nenájdený v mape", l.get("id"), cust_key)
        return None
    return {
        "external_source": "raynet",
        "external_id": str(l.get("id")),
        "external_synced_at": "now()",
        "customer_id": cust_id,
        "status": "dosly_lead",  # mapujeme všetko ako nový lead, manuálne sa potom presunie
        "estimated_value": l.get("amount", {}).get("amount") if isinstance(l.get("amount"), dict) else None,
        "source": "raynet_import",
        "notes": l.get("notice") or l.get("name"),
        "workspace": "b2c" if l.get("person") else "b2b",
    }


# ============================================================
# IMPORT FUNCTIONS
# ============================================================

def import_companies(dry_run: bool = False) -> dict:
    log.info("Stahujem companies z Raynetu...")
    rows = []
    for c in raynet_iter("company/"):
        rows.append(raynet_company_to_customer(c))
    log.info("Načítaných %d companies", len(rows))
    if dry_run:
        return {"fetched": len(rows), "upserted": 0}
    n = sb_upsert("customers", rows)
    return {"fetched": len(rows), "upserted": n}


def import_persons(dry_run: bool = False) -> dict:
    log.info("Stahujem persons z Raynetu...")
    rows = []
    for p in raynet_iter("person/"):
        rows.append(raynet_person_to_customer(p))
    log.info("Načítaných %d persons", len(rows))
    if dry_run:
        return {"fetched": len(rows), "upserted": 0}
    n = sb_upsert("customers", rows)
    return {"fetched": len(rows), "upserted": n}


def import_leads(dry_run: bool = False) -> dict:
    log.info("Stahujem leads z Raynetu...")
    # Najprv načítaj mapu customers (external_id → id)
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    cust_map = {}
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/customers",
            headers={**headers, "Range": f"{offset}-{offset+999}"},
            params={"select": "id,external_id,external_source", "external_source": "eq.raynet"},
            timeout=30,
        )
        data = r.json()
        if not data:
            break
        for c in data:
            cust_map[f"raynet:{c['external_id'].replace('person_', '')}"] = c["id"]
        if len(data) < 1000:
            break
        offset += 1000

    log.info("Customer map: %d entries", len(cust_map))

    rows = []
    skipped = 0
    for l in raynet_iter("lead/"):
        mapped = raynet_lead_to_lead(l, cust_map)
        if mapped:
            rows.append(mapped)
        else:
            skipped += 1

    log.info("Načítaných %d leads (skipped %d bez customera)", len(rows), skipped)
    if dry_run:
        return {"fetched": len(rows), "skipped": skipped, "upserted": 0}
    n = sb_upsert("leads", rows)
    return {"fetched": len(rows), "skipped": skipped, "upserted": n}


def _customer_map(workspace: Optional[str] = None) -> dict:
    """Mapa raynet_id → supabase customer_id."""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    cust_map = {}
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/customers",
            headers={**headers, "Range": f"{offset}-{offset+999}"},
            params={"select": "id,external_id,external_source", "external_source": "eq.raynet"},
            timeout=30,
        )
        data = r.json()
        if not data:
            break
        for c in data:
            raw = c["external_id"].replace("person_", "")
            cust_map[raw] = c["id"]
        if len(data) < 1000:
            break
        offset += 1000
    return cust_map


def raynet_business_case_to_lead(bc: dict, cust_map: dict) -> Optional[dict]:
    """Raynet business case → leads row (otvorené obchodné prípady)."""
    cust_ext = bc.get("company") or bc.get("person") or bc.get("primaryRole", {}).get("company")
    if not cust_ext:
        return None
    cust_id = cust_map.get(str(cust_ext.get("id")))
    if not cust_id:
        return None
    state = (bc.get("state") or "").lower()
    # Mapovanie raynet state → naša pipeline
    status_map = {
        "open": "obhliadka",
        "won": "vyhra",
        "lost": "zamietnute",
        "deferred": "neaktualne",
    }
    status = status_map.get(state, "dosly_lead")
    amount = bc.get("amount") or {}
    return {
        "external_source": "raynet",
        "external_id": f"bc_{bc.get('id')}",
        "external_synced_at": "now()",
        "customer_id": cust_id,
        "status": status,
        "estimated_value": amount.get("amount") if isinstance(amount, dict) else None,
        "source": "raynet_business_case",
        "notes": bc.get("title") or bc.get("notice") or "",
        "workspace": "b2c" if bc.get("person") else "b2b",
    }


def import_business_cases(dry_run: bool = False) -> dict:
    log.info("Stahujem business cases z Raynetu...")
    cust_map = _customer_map()
    rows = []
    skipped = 0
    for bc in raynet_iter("businessCase/"):
        mapped = raynet_business_case_to_lead(bc, cust_map)
        if mapped:
            rows.append(mapped)
        else:
            skipped += 1
    log.info("Načítaných %d business cases (skipped %d bez customera)", len(rows), skipped)
    if dry_run:
        return {"fetched": len(rows), "skipped": skipped, "upserted": 0}
    # Upsert na external_source + external_id (s prefixom bc_)
    n = sb_upsert("leads", rows)
    return {"fetched": len(rows), "skipped": skipped, "upserted": n}


def raynet_offer_to_imported_quote(o: dict, cust_map: dict) -> dict:
    """Raynet offer → imported_quotes row."""
    cust_ext = o.get("company") or o.get("person") or {}
    cust_id = cust_map.get(str(cust_ext.get("id"))) if cust_ext else None
    amount = o.get("totalAmount") or {}
    if not isinstance(amount, dict):
        amount = {"amount": amount}
    return {
        "external_source": "raynet",
        "external_id": str(o.get("id")),
        "customer_id": cust_id,
        "quote_number": o.get("code") or o.get("offerNumber"),
        "title": o.get("title") or o.get("name"),
        "status": o.get("rowState") or o.get("state"),
        "rowState": o.get("rowState"),
        "total_no_vat": amount.get("amount"),
        "total_with_vat": (o.get("totalAmountWithVat") or {}).get("amount") if isinstance(o.get("totalAmountWithVat"), dict) else None,
        "currency": amount.get("currency", "EUR"),
        "issued_at": o.get("creationDate") or o.get("createdAt"),
        "valid_until": o.get("validUntil") or o.get("expirationDate"),
        "owner_name": (o.get("owner") or {}).get("fullName") if isinstance(o.get("owner"), dict) else None,
        "business_case_id": str((o.get("businessCase") or {}).get("id")) if o.get("businessCase") else None,
        "raw_data": o,
    }


def raynet_offer_item_to_row(item: dict, quote_id: str, position: int) -> dict:
    """Raynet offer item → imported_quote_items row."""
    return {
        "quote_id": quote_id,
        "external_id": str(item.get("id")) if item.get("id") else None,
        "position": position,
        "product_code": (item.get("product") or {}).get("code") if isinstance(item.get("product"), dict) else item.get("code"),
        "product_name": (item.get("product") or {}).get("name") if isinstance(item.get("product"), dict) else item.get("name"),
        "description": item.get("description") or item.get("name"),
        "quantity": item.get("count") or item.get("quantity"),
        "unit": item.get("unitName") or item.get("unit"),
        "unit_price": item.get("price"),
        "vat_rate": item.get("vatRate") or item.get("vat"),
        "total_no_vat": item.get("totalPrice") or item.get("priceTotal"),
        "discount_pct": item.get("discount") or item.get("discountPercent"),
        "raw_data": item,
    }


def import_offers(dry_run: bool = False) -> dict:
    log.info("Stahujem offers + items z Raynetu...")
    cust_map = _customer_map()
    quotes_to_upsert = []
    items_to_insert = []  # tieto vložíme až po upsert quotes (potrebujeme id)
    offer_items_raw = {}  # external_id → [items]

    for o in raynet_iter("offer/"):
        q_row = raynet_offer_to_imported_quote(o, cust_map)
        quotes_to_upsert.append(q_row)
        items = o.get("items") or o.get("offerItems") or []
        offer_items_raw[q_row["external_id"]] = items

    log.info("Načítaných %d offers", len(quotes_to_upsert))
    if dry_run:
        total_items = sum(len(v) for v in offer_items_raw.values())
        return {"fetched": len(quotes_to_upsert), "items_fetched": total_items, "upserted": 0}

    # Upsert quotes
    n_q = sb_upsert("imported_quotes", quotes_to_upsert)

    # Pre items potrebujeme získať quote_id (po upsert)
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    quote_id_map = {}
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/imported_quotes",
            headers={**headers, "Range": f"{offset}-{offset+999}"},
            params={"select": "id,external_id", "external_source": "eq.raynet"},
            timeout=30,
        )
        data = r.json()
        if not data:
            break
        for c in data:
            quote_id_map[c["external_id"]] = c["id"]
        if len(data) < 1000:
            break
        offset += 1000

    # Najprv zmaž staré položky pre tieto cenovky (idempotency)
    for ext_id in offer_items_raw.keys():
        qid = quote_id_map.get(ext_id)
        if qid:
            requests.delete(
                f"{SUPABASE_URL}/rest/v1/imported_quote_items",
                headers=headers,
                params={"quote_id": f"eq.{qid}"},
                timeout=10,
            )

    # Insert nové items
    items_to_insert = []
    for ext_id, items in offer_items_raw.items():
        qid = quote_id_map.get(ext_id)
        if not qid:
            continue
        for pos, item in enumerate(items, 1):
            items_to_insert.append(raynet_offer_item_to_row(item, qid, pos))

    n_i = 0
    if items_to_insert:
        BATCH = 100
        for i in range(0, len(items_to_insert), BATCH):
            batch = items_to_insert[i:i+BATCH]
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/imported_quote_items",
                headers={**headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
                json=batch,
                timeout=60,
            )
            if r.ok:
                n_i += len(batch)
    return {"fetched": len(quotes_to_upsert), "items_fetched": len(items_to_insert), "upserted": n_q, "items_inserted": n_i}


def raynet_invoice_to_imported(inv: dict, cust_map: dict) -> dict:
    cust_ext = inv.get("company") or inv.get("person") or {}
    cust_id = cust_map.get(str(cust_ext.get("id"))) if cust_ext else None
    amount = inv.get("totalAmount") or {}
    if not isinstance(amount, dict):
        amount = {"amount": amount}
    return {
        "external_source": "raynet",
        "external_id": str(inv.get("id")),
        "customer_id": cust_id,
        "invoice_number": inv.get("code") or inv.get("invoiceNumber"),
        "invoice_type": inv.get("documentType") or "faktura",
        "status": inv.get("rowState") or inv.get("state"),
        "total_no_vat": amount.get("amount"),
        "total_with_vat": (inv.get("totalAmountWithVat") or {}).get("amount") if isinstance(inv.get("totalAmountWithVat"), dict) else None,
        "currency": amount.get("currency", "EUR"),
        "issued_at": inv.get("issuedAt") or inv.get("issueDate"),
        "due_at": inv.get("dueDate") or inv.get("dueAt"),
        "paid_at": inv.get("paidAt"),
        "paid_amount": (inv.get("paidAmount") or {}).get("amount") if isinstance(inv.get("paidAmount"), dict) else inv.get("paidAmount"),
        "variable_symbol": inv.get("variableSymbol"),
        "raw_data": inv,
    }


def import_invoices(dry_run: bool = False) -> dict:
    log.info("Stahujem invoices z Raynetu...")
    cust_map = _customer_map()
    rows = []
    for inv in raynet_iter("invoice/"):
        rows.append(raynet_invoice_to_imported(inv, cust_map))
    log.info("Načítaných %d invoices", len(rows))
    if dry_run:
        return {"fetched": len(rows), "upserted": 0}
    n = sb_upsert("imported_invoices", rows)
    return {"fetched": len(rows), "upserted": n}


# ============================================================
# MAIN
# ============================================================
def run(entities: list, dry_run: bool = False) -> dict:
    log.info("=== RAYNET IMPORT START (dry_run=%s, entities=%s) ===", dry_run, entities)
    results = {}
    for ent in entities:
        try:
            if ent == "companies":
                results["companies"] = import_companies(dry_run)
            elif ent == "persons":
                results["persons"] = import_persons(dry_run)
            elif ent == "leads":
                results["leads"] = import_leads(dry_run)
            elif ent == "business_cases":
                results["business_cases"] = import_business_cases(dry_run)
            elif ent == "offers":
                results["offers"] = import_offers(dry_run)
            elif ent == "invoices":
                results["invoices"] = import_invoices(dry_run)
        except Exception as e:
            log.exception("Import %s zlyhal", ent)
            results[ent] = {"error": str(e)}
    log.info("=== DONE === %s", json.dumps(results))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--entity", default="companies,persons,leads", help="comma-separated: companies,persons,leads,business_cases,offers,invoices")
    args = parser.parse_args()
    entities = [e.strip() for e in args.entity.split(",") if e.strip()]
    print(json.dumps(run(entities, args.dry_run), indent=2))
