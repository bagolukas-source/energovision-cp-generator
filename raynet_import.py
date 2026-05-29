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


def import_business_cases(dry_run: bool = False) -> dict:
    """Business cases v Raynete = obchodné prípady (môže byť ekvivalent leadov v pokrocilom stave)."""
    log.info("Stahujem business cases z Raynetu...")
    rows = []
    count = 0
    for bc in raynet_iter("businessCase/"):
        count += 1
    log.info("Načítaných %d business cases (zatiaľ len count, nemapujem)", count)
    return {"fetched": count, "upserted": 0, "note": "business_cases zatiaľ neimportujem — počkať na rozhodnutie kde ich dať"}


def import_offers(dry_run: bool = False) -> dict:
    log.info("Stahujem offers z Raynetu...")
    count = 0
    for o in raynet_iter("offer/"):
        count += 1
    log.info("Načítaných %d offers (počítam len)", count)
    return {"fetched": count, "upserted": 0, "note": "offers zatiaľ len count"}


def import_invoices(dry_run: bool = False) -> dict:
    log.info("Stahujem invoices z Raynetu...")
    count = 0
    for i in raynet_iter("invoice/"):
        count += 1
    log.info("Načítaných %d invoices (počítam len)", count)
    return {"fetched": count, "upserted": 0, "note": "invoices zatiaľ len count"}


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
