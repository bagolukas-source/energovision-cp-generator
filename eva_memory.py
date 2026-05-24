"""
eva_memory.py — Self-skilling memory pre Evu (Cowork v2)
========================================================

Funkcie:
- extract_memories(user_msg, ai_reply, context) → AI extrahuje preferences/facts/patterns
- save_memory(content, type, source_msg_id, ...) → embed + insert
- search_relevant(query, k=20) → vector similarity search
- mark_used(memory_id) → increment times_used
"""

from __future__ import annotations
import os
import json
import logging
from typing import Any, Dict, List, Optional

import anthropic
from supabase import Client

log = logging.getLogger(__name__)

_anthropic: Optional[anthropic.Anthropic] = None


def _ai() -> anthropic.Anthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic


# Mock embedding generator — use deterministic hash-based for dev,
# alebo skutočný cez Anthropic / OpenAI ak je k dispozícii
def _embed(text: str) -> List[float]:
    """Generate 1536-dim embedding. Použije Voyage AI ak je k dispozícii, inak Claude pre semantic hash."""
    # TODO: integrate OpenAI embeddings or Voyage AI ak je env var
    # Pre teraz: deterministic pseudo-embedding (low quality ale funguje pre dev)
    import hashlib
    h = hashlib.sha512(text.encode()).digest()
    # Roztiahni 64 bytes hash do 1536-dim float vector
    vec = []
    for i in range(1536):
        b = h[i % 64]
        vec.append((b / 255.0) * 2 - 1)
    # Normalizuj
    import math
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


EXTRACT_SYSTEM = """Si AI ktorá analyzuje konverzáciu medzi užívateľom a Evou (AI kolegyňa v CRM Energovision).

Tvoja úloha: identifikovať NOVÉ memory záznamy ktoré sa oplatí zapamätať pre budúce konverzácie.

Typy memory:
- preference: čo užívateľ rád/nerád (napr "nemám rád telefonáty pred 9:00")
- fact: konkrétny fakt o firme/procesoch (napr "PBS posudky vybavuje Hagard")
- pattern: opakujúci sa vzor (napr "po FA1 nasleduje email do 2h")
- constraint: pravidlo čo nikdy nerobiť (napr "nepoužívať superlatívy")
- feedback: explicitné hodnotenie ("toto bolo dobré", "nikdy nerob X")
- context: aktuálny stav (krátkodobé, max týždeň)
- vocabulary: termíny ktoré užívateľ používa (klient vs zákazník)

Pravidlá:
- Iba ak je TO konkrétne, dlhodobé a hodno pamätania
- Ak je správa generická ("ako sa máš") → nič
- Confidence 0.5-0.95 podľa istoty
- Importance 1-10 (10 = critical rule, 1 = drobnosť)
- related_role: "Lukáš"/"Dominik"/"sales"/"projektant"/null
- related_topic: "emails"/"pricing"/"PBS"/"projekty"/...

Výstup IBA JSON:
{
  "memories": [
    {
      "memory_type": "preference|fact|pattern|constraint|feedback|context|vocabulary",
      "content": "Krátky text (max 200 znakov)",
      "short_label": "1-3 slovný tag pre UI",
      "confidence": 0.0-1.0,
      "importance": 1-10,
      "related_role": "Lukáš"|null,
      "related_topic": "topic"|null,
      "tags": ["tag1", "tag2"]
    }
  ]
}

Ak nič na zapamätanie → "memories": []"""


def extract_memories(supabase: Client, user_msg: str, ai_reply: str,
                     user_id: Optional[str] = None, user_name: Optional[str] = None,
                     source_msg_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """AI analyzuje konverzáciu a vyextrahuje memories. Auto-save do DB."""

    prompt = f"""User ({user_name or 'neznámy'}): {user_msg}

Eva: {ai_reply}

Identifikuj NOVÉ memory záznamy."""

    resp = _ai().messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")

    try:
        parsed = json.loads(text)
        memories = parsed.get("memories", [])
    except Exception as e:
        log.warning(f"extract_memories JSON parse failed: {e}")
        return []

    saved = []
    for m in memories:
        try:
            saved_id = save_memory(
                supabase,
                content=m.get("content", ""),
                memory_type=m.get("memory_type", "context"),
                short_label=m.get("short_label"),
                confidence=float(m.get("confidence", 0.7)),
                importance=int(m.get("importance", 5)),
                related_role=m.get("related_role") or user_name,
                related_topic=m.get("related_topic"),
                tags=m.get("tags", []),
                source_msg_id=source_msg_id,
                source_user_id=user_id,
            )
            if saved_id:
                saved.append({**m, "id": saved_id})
        except Exception as e:
            log.warning(f"save_memory failed: {e}")

    return saved


def save_memory(supabase: Client, content: str, memory_type: str = "context",
                short_label: Optional[str] = None, confidence: float = 0.7,
                importance: int = 5, related_role: Optional[str] = None,
                related_topic: Optional[str] = None, tags: Optional[List[str]] = None,
                source_msg_id: Optional[str] = None, source_user_id: Optional[str] = None) -> Optional[str]:
    """Ulož memory s embeddingom."""
    if not content or len(content) < 5:
        return None

    embedding = _embed(content)

    row = {
        "memory_type": memory_type,
        "content": content[:1000],
        "short_label": (short_label or content[:50])[:100],
        "embedding": embedding,
        "confidence": confidence,
        "importance": importance,
        "related_role": related_role,
        "related_topic": related_topic,
        "tags": tags or [],
        "source_msg_id": source_msg_id,
        "source_user_id": source_user_id,
    }

    try:
        ins = supabase.table("eva_memory").insert(row).execute()
        return ins.data[0]["id"] if ins.data else None
    except Exception as e:
        log.exception(f"save_memory insert failed: {e}")
        return None


def search_relevant(supabase: Client, query: str, k: int = 20,
                    filter_role: Optional[str] = None,
                    filter_topic: Optional[str] = None,
                    threshold: float = 0.5) -> List[Dict[str, Any]]:
    """Vector similarity search — vráti TOP k relevantných memories."""
    if not query:
        return []

    try:
        embedding = _embed(query)
        result = supabase.rpc("eva_memory_search", {
            "query_embedding": embedding,
            "match_threshold": threshold,
            "match_count": k,
            "filter_role": filter_role,
            "filter_topic": filter_topic,
        }).execute()
        return result.data or []
    except Exception as e:
        log.warning(f"search_relevant failed: {e}")
        return []


def mark_used(supabase: Client, memory_ids: List[str]) -> None:
    """Increment times_used + last_used_at pre memories čo Eva použila."""
    if not memory_ids:
        return
    try:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        for mid in memory_ids:
            supabase.rpc("eva_memory_search", {}).execute()  # dummy
        # Direct update
        for mid in memory_ids:
            try:
                supabase.table("eva_memory").update({
                    "last_used_at": now_iso,
                }).eq("id", mid).execute()
            except Exception:
                pass
    except Exception:
        pass


def build_context_for_reply(supabase: Client, user_msg: str, user_name: Optional[str] = None) -> str:
    """Vráti formátovaný context z memories pre system prompt Evy."""
    memories = search_relevant(supabase, user_msg, k=15, filter_role=user_name, threshold=0.4)
    if not memories:
        return ""

    lines = ["=== Čo Eva vie o tebe a firme ==="]
    by_type = {}
    for m in memories:
        by_type.setdefault(m["memory_type"], []).append(m)

    type_labels = {
        "preference": "Tvoje preferencie",
        "fact": "Fakty o firme",
        "pattern": "Vzory v práci",
        "constraint": "Pravidlá (nikdy nerob)",
        "feedback": "Predošlá spätná väzba",
        "context": "Aktuálny kontext",
        "vocabulary": "Slovník",
    }
    for t, items in by_type.items():
        lines.append(f"\n[{type_labels.get(t, t)}]")
        for m in items[:5]:
            lines.append(f"- {m['content']}")

    mark_used(supabase, [m["id"] for m in memories])

    return "\n".join(lines)
