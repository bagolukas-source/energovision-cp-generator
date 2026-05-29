"""
Eva learning v2 — parse local folder (.eml, .msg, .mbox).

Nie M365 Graph (žiaden Azure setup), namiesto toho:
- Lukáš nadropne emaily do /Eva_Training_Emails/_lukas/, _dominik/, atď.
- Skript prejde priečinok rekurzívne
- Pre každý súbor: parse → classify (Claude) → embed (Voyage) → save do Supabase
- Po spracovaní presunie do /_spracovane/

Použitie:
    python eva_email_parse_folder.py [--root /cesta/k/Eva_Training_Emails]
    python eva_email_parse_folder.py --dry-run
"""
import os
import sys
import json
import time
import shutil
import logging
import argparse
import email
import email.policy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List
import requests

log = logging.getLogger("eva-folder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL_LEARNING", "claude-sonnet-4-5-20250929")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def parse_eml(filepath: Path) -> Optional[Dict]:
    """Parse .eml súbor (RFC822)."""
    try:
        with open(filepath, "rb") as f:
            msg = email.message_from_bytes(f.read(), policy=email.policy.default)
        # Body — prefer text/plain
        body_text = ""
        body_html = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                try:
                    if ct == "text/plain" and not body_text:
                        body_text = part.get_content()
                    elif ct == "text/html" and not body_html:
                        body_html = part.get_content()
                except Exception:
                    pass
        else:
            try:
                body_text = msg.get_content()
            except Exception:
                pass

        snippet = (body_text or body_html)[:500]
        return {
            "subject": str(msg.get("Subject") or ""),
            "from_address": _parse_email_addr(msg.get("From")),
            "to_addresses": _parse_email_list(msg.get("To")),
            "cc_addresses": _parse_email_list(msg.get("Cc")),
            "received_at": _parse_date(msg.get("Date")),
            "body_text": body_text[:10000],
            "body_html": body_html[:10000],
            "snippet": snippet,
            "thread_id": str(msg.get("Message-ID") or "")[:200],
            "has_attachments": any(p.get_filename() for p in msg.walk()) if msg.is_multipart() else False,
        }
    except Exception as e:
        log.warning("parse_eml %s: %s", filepath.name, e)
        return None


def parse_msg(filepath: Path) -> Optional[Dict]:
    """Parse .msg súbor (Outlook native) cez extract-msg."""
    try:
        import extract_msg
    except ImportError:
        log.error("extract_msg nie je nainštalovaný — pridajte do requirements.txt")
        return None
    try:
        m = extract_msg.Message(str(filepath))
        body_text = m.body or ""
        body_html = m.htmlBody or ""
        if isinstance(body_html, bytes):
            body_html = body_html.decode("utf-8", errors="ignore")
        snippet = (body_text or body_html)[:500]
        return {
            "subject": m.subject or "",
            "from_address": m.sender or "",
            "to_addresses": [t.strip() for t in (m.to or "").split(";") if t.strip()],
            "cc_addresses": [c.strip() for c in (m.cc or "").split(";") if c.strip()] if m.cc else [],
            "received_at": m.date.isoformat() if m.date else None,
            "body_text": body_text[:10000],
            "body_html": body_html[:10000] if isinstance(body_html, str) else "",
            "snippet": snippet,
            "thread_id": (m.messageId or "")[:200],
            "has_attachments": len(m.attachments) > 0,
        }
    except Exception as e:
        log.warning("parse_msg %s: %s", filepath.name, e)
        return None


def parse_mbox(filepath: Path) -> List[Dict]:
    """Parse .mbox súbor (Unix mailbox — viacero emailov)."""
    try:
        import mailbox
    except ImportError:
        return []
    out = []
    try:
        mb = mailbox.mbox(str(filepath))
        for msg in mb:
            try:
                body_text = ""
                body_html = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        try:
                            ct = part.get_content_type()
                            payload = part.get_payload(decode=True)
                            if payload:
                                txt = payload.decode("utf-8", errors="ignore")
                                if ct == "text/plain" and not body_text:
                                    body_text = txt
                                elif ct == "text/html" and not body_html:
                                    body_html = txt
                        except Exception:
                            pass
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body_text = payload.decode("utf-8", errors="ignore")
                snippet = (body_text or body_html)[:500]
                out.append({
                    "subject": str(msg.get("Subject") or ""),
                    "from_address": _parse_email_addr(msg.get("From")),
                    "to_addresses": _parse_email_list(msg.get("To")),
                    "cc_addresses": _parse_email_list(msg.get("Cc")),
                    "received_at": _parse_date(msg.get("Date")),
                    "body_text": body_text[:10000],
                    "body_html": body_html[:10000],
                    "snippet": snippet,
                    "thread_id": str(msg.get("Message-ID") or "")[:200],
                    "has_attachments": False,
                })
            except Exception:
                continue
    except Exception as e:
        log.warning("parse_mbox %s: %s", filepath.name, e)
    return out


def _parse_email_addr(s: Optional[str]) -> str:
    if not s:
        return ""
    s = str(s)
    if "<" in s and ">" in s:
        return s.split("<", 1)[1].split(">", 1)[0].strip().lower()
    return s.strip().lower()


def _parse_email_list(s: Optional[str]) -> List[str]:
    if not s:
        return []
    s = str(s)
    parts = s.split(",")
    return [_parse_email_addr(p) for p in parts if p.strip()]


def _parse_date(s) -> Optional[str]:
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(s)).isoformat()
    except Exception:
        return None


def classify_email(subject: str, snippet: str, from_addr: str) -> Dict:
    """Claude Sonnet — klasifikácia + extrakcia (z eva_email_learning.py)."""
    prompt = f"""Klasifikuj tento email do jednej kategórie a extrahuj kľúčové info. Odpovedz LEN JSON, nič iné.

Email subject: {subject}
From: {from_addr}
Body (prvých 800 znakov): {snippet[:800]}

Vráť JSON:
{{
  "category": "sales|admin|support|internal|marketing|other",
  "topics": ["napr. 'cenovka', 'reklamacia', 'splnomocnenie', 'revizia', 'faktura'"],
  "customer_mention": "meno klienta/firmy alebo null",
  "customer_ico": "IČO ak je v emaili alebo null",
  "is_template_worthy": true/false,
  "use_case": "stručný opis use case (3-6 slov)",
  "brand_voice_notes": "stručný popis tonality a štýlu (1 veta)"
}}"""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if not r.ok:
            return {"category": "other"}
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        return {"category": "other", "error": str(e)}


def embed_voyage(text: str) -> Optional[List[float]]:
    if not VOYAGE_API_KEY or not text:
        return None
    try:
        r = requests.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {VOYAGE_API_KEY}"},
            json={"input": text[:8000], "model": "voyage-3-lite"},
            timeout=30,
        )
        if r.ok:
            return r.json()["data"][0]["embedding"]
    except Exception:
        pass
    return None


def process_folder(root: Path, dry_run: bool = False, focus: List[str] = None) -> Dict:
    """Hlavná funkcia — prejde root rekurzívne, spracuje súbory."""
    if focus is None:
        focus = ["admin", "support", "sales"]

    spracovane_dir = root / "_spracovane"
    spracovane_dir.mkdir(exist_ok=True)

    stats = {"files": 0, "emails_parsed": 0, "processed": 0, "by_category": {}, "by_author": {}}
    rows_buffer: List[Dict] = []

    # Iter cez podpriečinky autora
    for author_dir in root.iterdir():
        if not author_dir.is_dir() or author_dir.name.startswith("_spracovane"):
            continue
        author = author_dir.name.lstrip("_").lower()
        author_name = author.capitalize()
        log.info("Spracovávam %s (autor: %s)", author_dir.name, author_name)

        # Skupiny podľa autora — počty
        if author not in stats["by_author"]:
            stats["by_author"][author] = 0

        # Prejdi všetky súbory
        for filepath in sorted(author_dir.rglob("*")):
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in [".eml", ".msg", ".mbox"]:
                continue
            stats["files"] += 1

            # Parse podľa extension
            parsed: List[Dict] = []
            if filepath.suffix.lower() == ".eml":
                p = parse_eml(filepath)
                if p:
                    parsed.append(p)
            elif filepath.suffix.lower() == ".msg":
                p = parse_msg(filepath)
                if p:
                    parsed.append(p)
            elif filepath.suffix.lower() == ".mbox":
                parsed.extend(parse_mbox(filepath))

            stats["emails_parsed"] += len(parsed)

            for p in parsed:
                # Klasifikuj
                cls = classify_email(p["subject"], p["snippet"], p["from_address"])
                category = cls.get("category", "other")

                stats["by_category"][category] = stats["by_category"].get(category, 0) + 1

                if dry_run:
                    continue

                embedding = None
                if category in focus:
                    text_for_embed = f"{p['subject']}\n\n{p['snippet']}\n\n{(p.get('body_text') or '')[:1500]}"
                    embedding = embed_voyage(text_for_embed)

                # Detekcia direction (z autor priečinka)
                direction = "out" if author in p["from_address"].lower() else "in"

                rows_buffer.append({
                    "source_type": "manual_upload",
                    "mailbox": f"{author}@energovision.sk",
                    "author_name": author_name,
                    "external_id": f"file_{filepath.name}",
                    "thread_id": p.get("thread_id"),
                    "subject": p["subject"][:500],
                    "snippet": p["snippet"][:500],
                    "body_html": (p.get("body_html") or p.get("body_text") or "")[:5000],
                    "from_address": p["from_address"],
                    "to_addresses": p["to_addresses"],
                    "cc_addresses": p["cc_addresses"],
                    "received_at": p["received_at"],
                    "direction": direction,
                    "has_attachments": p.get("has_attachments", False),
                    "category": category,
                    "topics": cls.get("topics", []),
                    "customer_mentions": [cls.get("customer_mention")] if cls.get("customer_mention") else [],
                    "extracted_facts": {
                        "use_case": cls.get("use_case"),
                        "brand_voice_notes": cls.get("brand_voice_notes"),
                        "is_template_worthy": cls.get("is_template_worthy"),
                        "source_file": str(filepath.relative_to(root)),
                    },
                    "embedding": embedding,
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                })

                stats["processed"] += 1
                stats["by_author"][author] += 1

                if len(rows_buffer) >= 30:
                    _bulk_upsert(rows_buffer)
                    rows_buffer = []

                time.sleep(0.2)  # Rate limit

            # Po spracovaní presun do _spracovane (nech sa znova nespustí)
            if not dry_run and parsed:
                dest = spracovane_dir / author_dir.name
                dest.mkdir(exist_ok=True)
                target = dest / filepath.name
                if target.exists():
                    # Prepis ak existuje
                    target.unlink()
                try:
                    shutil.move(str(filepath), str(target))
                except Exception as e:
                    log.warning("move %s zlyhal: %s", filepath, e)

    # Final batch
    if rows_buffer:
        _bulk_upsert(rows_buffer)

    return stats


def _bulk_upsert(rows: List[Dict]) -> int:
    if not rows:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/eva_training_sources?on_conflict=source_type,mailbox,external_id",
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows,
        timeout=60,
    )
    if not r.ok:
        log.error("Upsert failed: %s %s", r.status_code, r.text[:300])
        return 0
    log.info("Upsertnutých %d rows", len(rows))
    return len(rows)


def extract_templates_after(focus: List[str]) -> int:
    """Po spracovaní extrahuje konsolidované šablóny."""
    try:
        from eva_email_learning import extract_templates_from_batch
        return extract_templates_from_batch("", focus)
    except Exception as e:
        log.warning("template extraction zlyhalo: %s", e)
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/Users/lukasbago/Documents/Claude/Projects/Eva_Training_Emails")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--focus", default="admin,support")
    args = parser.parse_args()
    root = Path(args.root)
    if not root.exists():
        print(f"Root {root} neexistuje")
        sys.exit(1)
    focus = [s.strip() for s in args.focus.split(",")]

    stats = process_folder(root, args.dry_run, focus)
    if not args.dry_run:
        stats["templates_extracted"] = extract_templates_after(focus)
    print(json.dumps(stats, indent=2))
