"""
migrate_notion_b2b.py
=====================
Helper modul pre migráciu Notion B2B Dashboard PDF príloh do Supabase Storage.

Importovaný v app.py — exponuje 2 funkcie:
  - build_mapping()                       -> List[ProjectMatch]
  - migrate_one(notion_page_id, supabase_project_id, ds, dry_run)  -> dict
"""
from __future__ import annotations
import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

log = logging.getLogger("migrate_notion_b2b")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
DASHBOARD_DB_ID = os.environ.get("NOTION_B2B_DASHBOARD_DB_ID", "2671b0e51aa3803b9ee2dde6da0fb130")

NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

BUCKET = "b2b-documents"

# Mapping Notion property -> (storage subfolder template, project_documents.kind)
# Folder paths sú relatívne k {project_id}/
# "__DIS__" sa rozbalí podľa projects.ds
PROPERTY_MAPPING: Dict[str, Tuple[str, str]] = {
    "ZoD - súbor":                  ("02_Administratíva/03_Zmluva o dielo/02_Zmluva s IFT", "zod_signed"),
    "Plnomocenstvo - súbor":        ("02_Administratíva/02_Dokumenty 01",                    "splnomocnenie_signed"),
    "Dotazník 01 - súbor":          ("02_Administratíva/02_Dokumenty 01",                    "dotaznik"),
    "LV pdf":                       ("01_Podklady/03_Podklady od zákazníka",                  "lv"),
    "Zmluva o pripojení":           ("__DIS__",                                               "zop_signed"),
    "Zmluva o prístupe ":           ("__DIS__",                                               "zopad_signed"),
    "Stanovisko k ž. o pripojenie": ("__DIS__",                                               "stanovisko_zop"),
    "Stanovisko k RP ":             ("__DIS__",                                               "stanovisko_rp"),
    "OPaOS":                        ("04_Realizácia/05_Revízie",                              "opaos"),
}

DS_TO_FOLDER: Dict[str, str] = {
    "Stredoslovenská distribučná a.s.":  "05_DIS-SSD",
    "Stredoslovenská distribučná, a.s.": "05_DIS-SSD",
    "Východoslovenská distribučná, a.s.": "05_DIS-VSD",
    "Východoslovenská distribučná a.s.":  "05_DIS-VSD",
    "Západoslovenská distribučná, a.s.":  "05_DIS-ZSDIS",
    "Západoslovenská distribučná a.s.":   "05_DIS-ZSDIS",
    "ZSDIS":  "05_DIS-ZSDIS",
    "SSD":    "05_DIS-SSD",
    "VSD":    "05_DIS-VSD",
}


# ------------------- Supabase REST helpers -------------------

def _sb_headers() -> Dict[str, str]:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def sb_query(table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_sb_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_insert(table: str, rows: List[Dict[str, Any]], on_conflict: Optional[str] = None) -> Any:
    headers = {**_sb_headers(), "Content-Type": "application/json",
               "Prefer": "return=minimal,resolution=ignore-duplicates"}
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    r = requests.post(url, headers=headers, json=rows, timeout=30)
    if r.status_code >= 400:
        log.warning("Supabase insert %s failed: %s %s", table, r.status_code, r.text[:300])


def storage_exists(path: str) -> bool:
    encoded = quote(path, safe="/")
    r = requests.head(f"{SUPABASE_URL}/storage/v1/object/info/{BUCKET}/{encoded}",
                      headers=_sb_headers(), timeout=15)
    return r.status_code == 200


def storage_upload(path: str, content: bytes, content_type: str) -> Tuple[bool, str]:
    encoded = quote(path, safe="/")
    headers = {**_sb_headers(), "Content-Type": content_type, "x-upsert": "true"}
    r = requests.post(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{encoded}",
                      headers=headers, data=content, timeout=120)
    if r.status_code in (200, 201):
        return True, ""
    return False, f"{r.status_code}: {r.text[:200]}"


# ------------------- Notion API helpers -------------------

def notion_query_db_all(db_id: str) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        body: Dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"{NOTION_API}/databases/{db_id}/query",
                          headers=NOTION_HEADERS, json=body, timeout=30)
        r.raise_for_status()
        d = r.json()
        pages.extend(d.get("results", []))
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")
    return pages


def notion_get_page(page_id: str) -> Dict[str, Any]:
    r = requests.get(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


# ------------------- Public functions -------------------

def build_mapping() -> Dict[str, Any]:
    """
    Vyrobí mapping Supabase project_id -> Notion page_id na základe P-XX-XXXX prefixu v názve.
    """
    sb_projects = sb_query("projects", {
        "workspace": "eq.b2b",
        "select": "id,project_code,name,ds",
        "limit": "500",
    })

    notion_pages = notion_query_db_all(DASHBOARD_DB_ID)

    notion_index: Dict[str, str] = {}
    for p in notion_pages:
        title_prop = p.get("properties", {}).get("Zákazka", {})
        title_blocks = title_prop.get("title", []) if title_prop else []
        if not title_blocks:
            continue
        title = "".join(b.get("plain_text", "") for b in title_blocks).strip()
        for token in title.split():
            if token.startswith("P-") and "-" in token[2:]:
                notion_index[token] = p["id"]
                break

    matched: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    for sb in sb_projects:
        name = sb.get("name") or ""
        token = None
        for t in name.split():
            if t.startswith("P-") and "-" in t[2:]:
                token = t
                break
        if not token or token not in notion_index:
            unmatched.append({"name": name, "id": sb["id"]})
            continue
        matched.append({
            "supabase_project_id": sb["id"],
            "project_code": token,
            "name": name,
            "ds": sb.get("ds"),
            "notion_page_id": notion_index[token],
        })

    return {
        "supabase_total": len(sb_projects),
        "notion_total":   len(notion_pages),
        "notion_indexed": len(notion_index),
        "matched":   len(matched),
        "unmatched": len(unmatched),
        "projects":  matched,
        "unmatched_list": unmatched[:20],
    }


def migrate_one(notion_page_id: str, supabase_project_id: str, ds: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, Any]:
    """Migruje 1 projekt — všetky file properties."""
    try:
        page = notion_get_page(notion_page_id)
    except Exception as e:
        return {"ok": False, "error": f"notion_fetch: {e}", "files": []}

    props = page.get("properties", {})
    results: List[Dict[str, Any]] = []
    total_bytes = 0

    for prop_name, (folder_template, kind) in PROPERTY_MAPPING.items():
        prop = props.get(prop_name)
        if not prop or prop.get("type") != "files":
            continue
        files = prop.get("files", [])
        if not files:
            continue

        if folder_template == "__DIS__":
            ds_folder = DS_TO_FOLDER.get(ds or "", "05_DIS-DS")
            folder = f"02_Administratíva/{ds_folder}"
        else:
            folder = folder_template

        for f in files:
            ftype = f.get("type")
            if ftype == "file":
                file_url = f.get("file", {}).get("url", "")
            elif ftype == "external":
                file_url = f.get("external", {}).get("url", "")
            else:
                continue
            if not file_url:
                continue
            filename = (f.get("name") or "untitled.pdf").replace("/", "_").replace("\\", "_")
            storage_path = f"{supabase_project_id}/{folder}/{filename}"

            entry = {
                "property": prop_name,
                "filename": filename,
                "kind": kind,
                "storage_path": storage_path,
                "size_bytes": None,
                "status": "pending",
                "error": None,
            }

            if dry_run:
                entry["status"] = "dry-run"
                results.append(entry)
                continue

            if storage_exists(storage_path):
                entry["status"] = "skipped"
                results.append(entry)
                continue

            try:
                rr = requests.get(file_url, timeout=120)
                if rr.status_code != 200:
                    entry["status"] = "failed"
                    entry["error"] = f"notion_download {rr.status_code}"
                    results.append(entry)
                    continue
                content = rr.content
                entry["size_bytes"] = len(content)
                total_bytes += len(content)
            except Exception as e:
                entry["status"] = "failed"
                entry["error"] = f"download_exc: {e}"
                results.append(entry)
                continue

            ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
            ctype = {
                "pdf":  "application/pdf",
                "png":  "image/png",
                "jpg":  "image/jpeg",
                "jpeg": "image/jpeg",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "zip":  "application/zip",
                "rar":  "application/x-rar-compressed",
            }.get(ext, "application/octet-stream")

            ok, err = storage_upload(storage_path, content, ctype)
            if not ok:
                entry["status"] = "failed"
                entry["error"] = f"upload: {err}"
                results.append(entry)
                continue

            entry["status"] = "uploaded"
            # Optional DB log (best-effort)
            try:
                sb_insert("project_documents", [{
                    "project_id": supabase_project_id,
                    "kind": kind,
                    "display_name": filename,
                    "current_state": "signed",
                    "notes": f"Migrated from Notion {notion_page_id} ({prop_name}). Storage: {storage_path}",
                    "state_changed_at": datetime.utcnow().isoformat() + "Z",
                    "workspace": "b2b",
                }])
            except Exception as e:
                entry["error"] = f"db_insert_warn: {e}"
            results.append(entry)

    counts = {"uploaded": 0, "skipped": 0, "failed": 0, "dry-run": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "ok": True,
        "project_id": supabase_project_id,
        "notion_page_id": notion_page_id,
        "counts": counts,
        "total_bytes": total_bytes,
        "files": results,
    }
