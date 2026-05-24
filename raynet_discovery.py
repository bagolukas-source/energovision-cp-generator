"""
Raynet CRM discovery — sťahuje quotations, business cases, products, companies
zo živého Raynet inštance Energovision (app.raynet.cz/energovision).

Cieľ: NIE migrácia. Cieľ = analýza ako Energovision REÁLNE robí cenovky:
- aké položky idú do akej ponuky
- ako sa škálujú s kWp / typom strechy / batériou / wallboxom
- aké default počty / dĺžky / násobky používajú

Dáta sa ukladajú do staging tabuliek `raynet_raw_*` v Supabase. Sú read-only,
nezasahujú do produkčnej DB.

Env vars (na Render):
  RAYNET_USERNAME, RAYNET_API_KEY, RAYNET_INSTANCE (default: energovision)

Endpoint Raynet: https://app.raynet.cz/api/v2/{resource}/
Auth: HTTP Basic + X-Instance-Name header
"""
import os
import time
import logging
import requests
from typing import Iterable

log = logging.getLogger(__name__)
RAYNET_BASE = "https://app.raynet.cz/api/v2"


_RUNTIME_CREDS = {}  # set per-request via set_creds(user, key, inst)


def set_creds(user: str, key: str, inst: str = "energovision"):
    """Override env vars for single request (used by webhook body)."""
    _RUNTIME_CREDS["user"] = user
    _RUNTIME_CREDS["key"] = key
    _RUNTIME_CREDS["inst"] = inst or "energovision"


def _creds():
    user = _RUNTIME_CREDS.get("user") or os.environ.get("RAYNET_USERNAME", "")
    key = _RUNTIME_CREDS.get("key") or os.environ.get("RAYNET_API_KEY", "")
    inst = _RUNTIME_CREDS.get("inst") or os.environ.get("RAYNET_INSTANCE", "energovision")
    if not user or not key:
        raise RuntimeError("Missing RAYNET_USERNAME / RAYNET_API_KEY (env or body)")
    return user, key, inst


def _get(path: str, params: dict | None = None) -> dict:
    user, key, inst = _creds()
    url = f"{RAYNET_BASE}/{path.lstrip('/')}"
    headers = {"X-Instance-Name": inst, "Accept": "application/json"}
    r = requests.get(url, auth=(user, key), headers=headers, params=params or {}, timeout=30)
    if r.status_code == 429:
        time.sleep(2)
        r = requests.get(url, auth=(user, key), headers=headers, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def paginate(resource: str, limit: int = 50, max_pages: int = 200) -> Iterable[dict]:
    offset = 0
    pages = 0
    while pages < max_pages:
        d = _get(resource, {"offset": offset, "limit": limit})
        rows = d.get("data") or []
        if not rows:
            break
        for row in rows:
            yield row
        total = d.get("totalCount") or 0
        offset += limit
        pages += 1
        if offset >= total:
            break
        time.sleep(0.2)


def _sb_upsert(sb, table: str, rows: list, conflict_col: str = "raynet_id"):
    if not rows:
        return 0
    try:
        # chunky upsert (500 at a time)
        saved = 0
        for i in range(0, len(rows), 500):
            chunk = rows[i:i+500]
            res = sb.table(table).upsert(chunk, on_conflict=conflict_col).execute()
            saved += len(res.data) if res.data else len(chunk)
        return saved
    except Exception as e:
        log.exception(f"[raynet] upsert {table} failed: {e}")
        return 0


def _gid(obj, key="id"):
    """Extract nested id field (Raynet returns {id, name} or just id)."""
    if isinstance(obj, dict):
        return obj.get(key)
    return obj


def _gname(obj):
    if isinstance(obj, dict):
        return obj.get("fullName") or obj.get("name") or obj.get("title")
    return None


def discover_quotations(sb, max_records: int = 2000) -> dict:
    count = 0
    item_count = 0
    rows = []
    items = []
    for q in paginate("quotation", limit=50):
        count += 1
        q_items = q.get("quotationItems") or q.get("items") or []
        item_count += len(q_items)
        ri = q.get("rowInfo") or {}
        rows.append({
            "raynet_id": q.get("id"),
            "code": q.get("code"),
            "title": q.get("title") or q.get("name"),
            "company_id": _gid(q.get("company")),
            "business_case_id": _gid(q.get("businessCase")),
            "status": ri.get("rowState") or q.get("state"),
            "total_excl_vat": q.get("totalAmount") or q.get("totalAmountWithoutVat"),
            "total_incl_vat": q.get("totalAmountIncl") or q.get("totalAmountWithVat"),
            "currency": q.get("currency"),
            "valid_from": q.get("validFrom"),
            "valid_until": q.get("validTill") or q.get("validUntil"),
            "created_at_raynet": ri.get("createDate") or ri.get("createdAt"),
            "updated_at_raynet": ri.get("editDate") or ri.get("updatedAt"),
            "owner_name": _gname(q.get("owner")),
            "raw_json": q,
        })
        for it in q_items:
            prod = it.get("product") if isinstance(it.get("product"), dict) else None
            items.append({
                "raynet_id": it.get("id"),
                "quotation_raynet_id": q.get("id"),
                "product_id": _gid(it.get("product")),
                "product_code": (prod or {}).get("code"),
                "product_name": it.get("name") or (prod or {}).get("name"),
                "qty": it.get("count") or it.get("qty") or it.get("quantity"),
                "unit": it.get("unit"),
                "unit_price": it.get("price") or it.get("unitPrice"),
                "total_price": it.get("totalPrice") or it.get("priceTotal"),
                "vat_rate": it.get("vatRate"),
                "discount_pct": it.get("discountPercent"),
                "position": it.get("rowNumber") or it.get("position"),
                "raw_json": it,
            })
        if count >= max_records:
            break
    saved_q = _sb_upsert(sb, "raynet_raw_quotations", rows)
    saved_i = _sb_upsert(sb, "raynet_raw_quotation_items", items)
    return {"quotations_fetched": count, "quotations_saved": saved_q,
            "items_fetched": item_count, "items_saved": saved_i}


def discover_business_cases(sb, max_records: int = 2000) -> dict:
    count = 0
    rows = []
    for bc in paginate("businessCase", limit=50):
        count += 1
        rows.append({
            "raynet_id": bc.get("id"),
            "code": bc.get("code"),
            "name": bc.get("name") or bc.get("title"),
            "company_id": _gid(bc.get("company")),
            "owner_name": _gname(bc.get("owner")),
            "status": _gid(bc.get("businessCaseStatus"), "name") or bc.get("status"),
            "phase": _gid(bc.get("salesPhase"), "name") or bc.get("phase"),
            "currency": bc.get("primaryCurrency"),
            "amount": bc.get("primaryAmount") or bc.get("primaryAmountIncl"),
            "expected_close": bc.get("expectedCloseDate"),
            "actual_close": bc.get("closeDate"),
            "probability": bc.get("probability"),
            "raw_json": bc,
        })
        if count >= max_records:
            break
    saved = _sb_upsert(sb, "raynet_raw_business_cases", rows)
    return {"fetched": count, "saved": saved}


def discover_products(sb, max_records: int = 2000) -> dict:
    count = 0
    rows = []
    for p in paginate("product", limit=50):
        count += 1
        rows.append({
            "raynet_id": p.get("id"),
            "code": p.get("code"),
            "name": p.get("name"),
            "category": p.get("category") or _gid(p.get("category"), "name"),
            "product_line": p.get("productLine") or _gid(p.get("productLine"), "name") or p.get("productRow"),
            "unit": p.get("unit"),
            "price": p.get("price") or p.get("standardPrice"),
            "price_incl_vat": p.get("priceIncl") or p.get("standardPriceIncl"),
            "cost": p.get("cost"),
            "currency": p.get("currency"),
            "vat_rate": p.get("vatRate"),
            "manufacturer": p.get("manufacturer") or _gid(p.get("manufacturer"), "name"),
            "type": p.get("type"),
            "power_capacity": p.get("powerCapacity"),
            "efficiency": p.get("efficiency"),
            "warranty": p.get("warranty"),
            "in_catalog": p.get("inCatalog"),
            "hybrid_inverter": p.get("hybridInverter"),
            "raw_json": p,
        })
        if count >= max_records:
            break
    saved = _sb_upsert(sb, "raynet_raw_products", rows)
    return {"fetched": count, "saved": saved}


def discover_companies(sb, max_records: int = 5000) -> dict:
    count = 0
    rows = []
    for c in paginate("company", limit=50):
        count += 1
        addr = c.get("primaryAddress") or c.get("address") or {}
        if not isinstance(addr, dict):
            addr = {}
        rows.append({
            "raynet_id": c.get("id"),
            "code": c.get("code"),
            "name": c.get("name"),
            "regnumber": c.get("regNumber") or c.get("ico"),
            "tax_number": c.get("taxNumber") or c.get("dic"),
            "country": addr.get("country"),
            "city": addr.get("city"),
            "owner_name": _gname(c.get("owner")),
            "industry": c.get("industry"),
            "rating": c.get("rating"),
            "raw_json": c,
        })
        if count >= max_records:
            break
    saved = _sb_upsert(sb, "raynet_raw_companies", rows)
    return {"fetched": count, "saved": saved}


def discover_all(sb) -> dict:
    out = {}
    for name, fn in [("products", discover_products),
                     ("companies", discover_companies),
                     ("business_cases", discover_business_cases),
                     ("quotations", discover_quotations)]:
        try:
            out[name] = fn(sb)
        except Exception as e:
            log.exception(f"[raynet] {name} failed")
            out[name] = {"error": str(e)[:200]}
    return out


def whoami() -> dict:
    return _get("whoami")
