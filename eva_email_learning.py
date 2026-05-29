"""
Eva learning pipeline — M365 emails + AI extraction.

Workflow:
1. Pull emaily z M365 Graph API (sent + received) za dané obdobie pre N mailboxov
2. Pre každý email:
   - Klasifikuj cez Claude (sales/admin/support/internal/marketing/other)
   - Ak je admin/support/sales — extrahuj insights (template patterns, facts, processes)
   - Generate embedding cez Voyage
   - Save do eva_training_sources
3. Po batchi: postupne agreguj rovnaké use_case-y → extracted_templates

Spustenie:
    POST /webhook/eva-learning-start
    { mailboxes: ["lukas.bago@energovision.sk", ...],
      months_back: 6,
      categories_focus: ["admin", "support"] }
"""
import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

log = logging.getLogger("eva-learning")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL_LEARNING", "claude-sonnet-4-5-20250929")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")

# M365 — používa client_credentials flow (potrebuje Mail.Read app permission)
M365_TENANT = os.environ.get("AZURE_TENANT_ID", "76e87fb6-78db-4090-aed2-25ad525e4f82")
M365_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
M365_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _get_m365_app_token() -> str:
    """Application-level token (Mail.Read scope, app permission)."""
    r = requests.post(
        f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/token",
        data={
            "client_id": M365_CLIENT_ID,
            "client_secret": M365_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_messages(mailbox: str, since_iso: str, max_messages: int = 1000) -> List[Dict]:
    """Pull-ne emaily z M365 Graph API pre konkrétny mailbox za posledné obdobie."""
    token = _get_m365_app_token()
    out: List[Dict] = []
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
    params = {
        "$top": "100",
        "$select": "id,subject,bodyPreview,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments,conversationId,body",
        "$filter": f"receivedDateTime ge {since_iso}",
        "$orderby": "receivedDateTime desc",
    }
    while url and len(out) < max_messages:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
        if not r.ok:
            log.error("Graph API error %s: %s", r.status_code, r.text[:300])
            break
        data = r.json()
        out.extend(data.get("value", []))
        # next page
        url = data.get("@odata.nextLink")
        params = None
    return out[:max_messages]


def classify_email(subject: str, snippet: str, from_addr: str) -> Dict:
    """Claude Sonnet — klasifikácia + extrakcia."""
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
            return {"category": "other", "error": f"claude {r.status_code}"}
        text = r.json()["content"][0]["text"].strip()
        # strip code fences ak sú
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        log.exception("classify_email failed")
        return {"category": "other", "error": str(e)}


def embed_voyage(text: str) -> Optional[List[float]]:
    """Vytvor embedding cez Voyage AI."""
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
        log.exception("voyage failed")
    return None


def process_mailbox(mailbox: str, months_back: int, categories_focus: List[str], batch_id: str) -> Dict:
    """Spracuje jeden mailbox: pull → klasifikuj → extrahuj → save."""
    since = (datetime.now(timezone.utc) - timedelta(days=months_back * 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("[%s] Pull-ujem od %s", mailbox, since)
    messages = fetch_messages(mailbox, since)
    log.info("[%s] Stiahnutých %d emailov", mailbox, len(messages))

    stats = {"fetched": len(messages), "processed": 0, "admin": 0, "support": 0, "skipped": 0}
    rows_to_insert = []

    for msg in messages:
        msg_id = msg.get("id")
        subject = msg.get("subject") or ""
        snippet = msg.get("bodyPreview") or ""
        body_html = (msg.get("body") or {}).get("content", "")
        from_addr = (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
        to_addrs = [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])]
        cc_addrs = [r.get("emailAddress", {}).get("address", "") for r in msg.get("ccRecipients", [])]
        received = msg.get("receivedDateTime")
        direction = "out" if from_addr.lower() == mailbox.lower() else "in"

        # Klasifikuj
        cls = classify_email(subject, snippet, from_addr)
        category = cls.get("category", "other")

        # Skip ak nie je v categories_focus
        if categories_focus and category not in categories_focus:
            stats["skipped"] += 1
            # Aj tak uložíme metadata bez extracted_facts/embedding
            embedding = None
            extracted_facts = {}
        else:
            stats[category] = stats.get(category, 0) + 1
            extracted_facts = {
                "use_case": cls.get("use_case"),
                "brand_voice_notes": cls.get("brand_voice_notes"),
                "is_template_worthy": cls.get("is_template_worthy"),
            }
            # Embed full snippet pre RAG
            embedding = embed_voyage(f"{subject}\n\n{snippet[:1500]}")

        rows_to_insert.append({
            "source_type": "email_m365",
            "mailbox": mailbox,
            "author_name": mailbox.split("@")[0].replace(".", " ").title(),
            "external_id": msg_id,
            "thread_id": msg.get("conversationId"),
            "subject": subject[:500],
            "snippet": snippet[:500],
            "body_html": body_html[:5000],  # cap pre size
            "from_address": from_addr,
            "to_addresses": to_addrs,
            "cc_addresses": cc_addrs,
            "received_at": received,
            "direction": direction,
            "has_attachments": msg.get("hasAttachments", False),
            "category": category,
            "topics": cls.get("topics", []),
            "customer_mentions": [cls.get("customer_mention")] if cls.get("customer_mention") else [],
            "extracted_facts": extracted_facts,
            "embedding": embedding,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        })
        stats["processed"] += 1

        # Bulk insert každých 50
        if len(rows_to_insert) >= 50:
            _bulk_upsert_sources(rows_to_insert)
            rows_to_insert = []

        # API rate limit (Claude má 50 req/min na free tier)
        time.sleep(0.2)

    # Final batch
    if rows_to_insert:
        _bulk_upsert_sources(rows_to_insert)

    return stats


def _bulk_upsert_sources(rows: List[Dict]) -> int:
    if not rows:
        return 0
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/eva_training_sources?on_conflict=source_type,mailbox,external_id",
        headers=headers,
        json=rows,
        timeout=60,
    )
    if not r.ok:
        log.error("Upsert sources failed: %s %s", r.status_code, r.text[:400])
        return 0
    return len(rows)


def extract_templates_from_batch(batch_id: str, categories: List[str]) -> int:
    """Po spracovaní batch-u prejde top use_case-mi a vyrobí konsolidované šablóny."""
    headers = _sb_headers()
    # Načítaj všetky 'template_worthy' emaily z aktuálneho batchu
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/eva_training_sources",
        headers=headers,
        params={
            "select": "id,subject,body_html,extracted_facts,category,mailbox,direction",
            "category": f"in.({','.join(categories)})",
            "direction": "eq.out",
            "extracted_facts->is_template_worthy": "eq.true",
            "limit": "500",
        },
        timeout=30,
    )
    if not r.ok:
        return 0

    emails = r.json()
    # Zoskupiť per use_case
    by_use_case = {}
    for e in emails:
        uc = (e.get("extracted_facts") or {}).get("use_case")
        if not uc:
            continue
        by_use_case.setdefault(uc, []).append(e)

    templates_created = 0
    for use_case, group in by_use_case.items():
        if len(group) < 2:
            continue  # Aspoň 2 emaily pre konsolidáciu

        # Claude vytvorí šablónu z top 5
        sample_emails = "\n\n---\n\n".join([
            f"Predmet: {e['subject']}\n\n{(e.get('body_html') or '')[:1500]}"
            for e in group[:5]
        ])

        prompt = f"""Z týchto {len(group)} reálnych emailov tímu Energovision (use case: '{use_case}') vytvor šablónu ktorú Eva môže používať pri generovaní podobných emailov.

UKÁŽKY:
{sample_emails}

Vytvor JSON:
{{
  "title": "Krátky názov šablóny (3-6 slov)",
  "description": "Kedy túto šablónu použiť (1 veta)",
  "template_body": "Šablóna v markdown so [[premennými]] na vyplnenie. Zachovaj tonality a štýl tímu. Použi <<klient>>, <<datum>>, <<suma>>, <<obchodnik>> ako placeholdery.",
  "example_input": "Príklad situácie kedy by Eva mala použiť (2-3 vety kontextu)",
  "confidence": 0.0-1.0 (ako konzistentný bol vzor v emailoch)
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
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )
            if not r.ok:
                continue
            text = r.json()["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            tmpl = json.loads(text)

            # Save
            category = group[0].get("category", "admin")
            embedding = embed_voyage(f"{tmpl['title']}\n{tmpl['description']}\n{tmpl['template_body'][:1000]}")
            requests.post(
                f"{SUPABASE_URL}/rest/v1/eva_extracted_templates",
                headers={**_sb_headers(), "Prefer": "return=minimal"},
                json={
                    "template_type": "email_template",
                    "use_case": use_case,
                    "category": category,
                    "title": tmpl["title"],
                    "description": tmpl["description"],
                    "template_body": tmpl["template_body"],
                    "example_input": tmpl.get("example_input", ""),
                    "source_email_ids": [e["id"] for e in group[:5]],
                    "confidence": tmpl.get("confidence", 0.5),
                    "embedding": embedding,
                    "active": True,
                },
                timeout=15,
            )
            templates_created += 1
        except Exception:
            log.exception("template extraction failed for use_case %s", use_case)

    return templates_created


def run_learning(mailboxes: List[str], months_back: int = 6, categories_focus: List[str] = None) -> Dict:
    """Hlavný entrypoint pre Render endpoint."""
    if categories_focus is None:
        categories_focus = ["admin", "support"]
    log.info("=== EVA LEARNING START mailboxes=%s months=%d focus=%s ===", mailboxes, months_back, categories_focus)

    # Vytvor batch log
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/eva_learning_batches",
        headers={**_sb_headers(), "Prefer": "return=representation"},
        json={
            "mailboxes": mailboxes,
            "date_from": (datetime.now(timezone.utc) - timedelta(days=months_back * 30)).date().isoformat(),
            "date_to": datetime.now(timezone.utc).date().isoformat(),
            "categories_focus": categories_focus,
            "status": "running",
        },
        timeout=10,
    )
    batch_id = r.json()[0]["id"] if r.ok else None

    all_stats = {}
    total_processed = 0
    for mb in mailboxes:
        try:
            s = process_mailbox(mb, months_back, categories_focus, batch_id)
            all_stats[mb] = s
            total_processed += s.get("processed", 0)
        except Exception as e:
            log.exception("mailbox %s failed", mb)
            all_stats[mb] = {"error": str(e)}

    # Extract konsolidované šablóny
    templates_count = 0
    if batch_id:
        try:
            templates_count = extract_templates_from_batch(batch_id, categories_focus)
        except Exception:
            log.exception("template extraction failed")

    summary = {
        "mailboxes": all_stats,
        "total_processed": total_processed,
        "templates_extracted": templates_count,
    }

    if batch_id:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/eva_learning_batches",
            headers=_sb_headers(),
            params={"id": f"eq.{batch_id}"},
            json={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "emails_processed": total_processed,
                "templates_extracted": templates_count,
                "status": "done",
                "summary": summary,
            },
            timeout=10,
        )

    log.info("=== EVA LEARNING DONE %s ===", json.dumps(summary)[:300])
    return {**summary, "batch_id": batch_id}
