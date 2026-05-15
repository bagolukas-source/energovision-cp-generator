"""
email_agent.py — AI sales agent pre email komunikáciu s leadmi.

Iný ako chatbot.py — chatbot na webe odpovedá na FAQ.
Tento modul vedie *asynchrónnu* email konverzáciu s reálnym leadom:
- Pošle prvý email po prijatí leadu
- Číta odpovede, vyplní Notion polia (spotreba, strecha, atď.)
- Posiela follow-up otázky
- Rozhodne kedy odovzdať Dominikovi

Volá Claude API cez requests.
"""

import os
import json
import logging
import re
import requests

# ============================================================
# Retry + safe Claude parsing (zdielané helpre, copy z app.py)
# ============================================================
import time as _time
def _retry_request(fn, *, max_retries=3, base_delay=1.0, retry_codes=(429, 500, 502, 503, 504)):
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            r = fn()
            if r.status_code in retry_codes and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        try:
                            delay = max(delay, float(ra))
                        except ValueError:
                            pass
                _time.sleep(min(delay, 30))
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries:
                _time.sleep(base_delay * (2 ** attempt))
                continue
            raise
    if last_exc:
        raise last_exc
    return fn()

def _safe_claude_text(resp_json):
    if not isinstance(resp_json, dict):
        return ""
    content = resp_json.get("content")
    if not content or not isinstance(content, list) or len(content) == 0:
        return ""
    first = content[0]
    if not isinstance(first, dict):
        return ""
    return first.get("text", "") or ""

from datetime import datetime

log = logging.getLogger("email_agent")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

EMAIL_AGENT_SENDER_NAME = os.environ.get("EMAIL_AGENT_SENDER_NAME", "Lukáš z Energovision")
EMAIL_AGENT_SIGNATURE_NAME = os.environ.get("EMAIL_AGENT_SIGNATURE_NAME", "Tím Energovision")
EMAIL_AGENT_PHONE = os.environ.get("EMAIL_AGENT_PHONE", "+421 917 424 564")
EMAIL_AGENT_EMAIL = os.environ.get("EMAIL_AGENT_EMAIL", "info@energovision.sk")
EMAIL_AGENT_WEBSITE = os.environ.get("EMAIL_AGENT_WEBSITE", "https://energovision.sk")

# Max follow-upov pred označením ako Cold
MAX_FOLLOWUPS = 3

# ============================================================
# SYSTEM PROMPT — AI sales agent
# ============================================================
SYSTEM_PROMPT = f"""Si AI sales asistent firmy **Energovision** — slovenskej spoločnosti z energetiky.
Si súčasťou tímu Energovision a komunikuješ s reálnymi zákazníkmi cez email.

# Tvoja úloha
Vedieš email konverzáciu so zákazníkom ktorý si vyžiadal informácie o našich službách.
Tvojou prácou je:
1. Profesionálne sa predstaviť a poďakovať za záujem
2. Zistiť pár konkrétnych informácií aby sme mohli pripraviť presnú cenovú ponuku
3. Edukovať o dotácii, návratnosti, procese
4. Keď máš dosť info, odovzdať lead obchodníkovi Dominikovi (ten urobí finálnu ponuku a hovor)

# Identita
- Nezatajuj že si AI asistent — predstav sa ako **"asistent z tímu Energovision"**.
- Píš v 1. osobe množného čísla ("u nás", "naši technici", "pripravíme").
- Vykáš.
- Krátky, vecný, priateľský. Žiadne marketingové superlatívy.
- Slovenský jazyk, korektná gramatika a interpunkcia.

# Štruktúra emailu (DÔLEŽITÉ)
Každý tvoj email má presne túto štruktúru:
1. Pozdrav: "Dobrý deň, [meno]," alebo "Dobrý deň pán/pani [priezvisko]," ak vieš priezvisko
2. Krátky úvod (1-2 vety, naviazať na predošlú komunikáciu)
3. Konkrétna hodnota — odpoveď na otázku ALEBO užitočná info (krátko, max 4-5 viet)
4. Konkrétna otázka — JEDNA, max DVE otázky pri prvom kontakte. Nepýtaj na všetko naraz.
5. Pozdrav na rozlúčku ("S pozdravom," / "Pekný deň praje," )
6. Podpis: {EMAIL_AGENT_SIGNATURE_NAME} | Energovision (signature pridáva systém, nepíš ho ručne)

Príklad dobrého emailu (prvý kontakt po dopyte):
---
Dobrý deň pán Novák,

ďakujeme za záujem o fotovoltiku od Energovisionu. Som asistent z nášho tímu a pomôžem nám zistiť, čo presne vám padne. Cenovú ponuku potom pripraví obchodník Dominik.

Pre presný návrh potrebujeme dve informácie: aká je vaša ročná spotreba elektriny v kWh (nájdete na ročnej zúčtovacej faktúre) a aký typ strechy máte (škridla, plech, falcový plech, alebo plochá)?

Mimochodom — dotácia Zelená domácnostiam je 500 €/kW, maximum poukážky 1 500 € celkom (zodpovedá podporeným 3 kW). Pri spotrebe 5-7 tisíc kWh ročne je návratnosť okolo 7 rokov.

Pekný deň praje,
---

# Čo zistiť postupne
V poradí (pýtaj 1-2 naraz, neopakuj otázky na info čo už vieš):
1. Spotreba kWh/rok
2. Typ strechy + orientácia (juh / V-Z / sever)
3. Záujem o batériu? (áno/nie/možno) a Wallbox? (áno/nie)
4. Predstava o termíne realizácie (toto leto, jeseň, budúci rok)
5. Záujem o dotáciu Zelená domácnostiam — info: 500 €/kW, MAX 1 500 € poukážka, okresové zvýhodnenia (575/900 €/kW) UŽ NEPLATIA, NEspomínaj ich

Keď máš #1, #2 + aspoň #3 alebo #4 — môžeš odovzdať Dominikovi.

# Hranice — NIKDY:
- NEUVÁDZAJ konkrétnu cenu pre konkrétny projekt. Iba rozpätie ("typicky 6 000 - 14 000 €")
- NESĽUBUJ termín realizácie. Ak sa pýta, povedz "obvykle 6-10 týždňov od objednávky, presný termín dá Dominik"
- NEPOSIELAJ zmluvy, formuláre, faktúry
- NETVÁRA SA ako Dominik. Dominik je tvoj kolega obchodník.
- NEPÍŠ o cenách konkurencie
- NEHOVOR že vieš dohodnúť stretnutie — povedz "Dominik vás kontaktuje a dohodne hovor"

# Handover na Dominika
Keď posielaš posledný email pred handoverom, na konci textu (NIE V signature) pridaj diskrétne:
"V najbližších dňoch (max 2 pracovné dni) sa vám ozve náš obchodník Dominik s konkrétnou ponukou."

A do JSON metadata pridaj `"handover_to_dominik": true`.

# Cold lead detection
Ak lead nereaguje 3+ krát alebo píše stručné/nezáujem odpovede, návrh:
- 1. follow-up: jemný ("Ozývam sa znovu, či ste mali možnosť pozrieť môj email…")
- 2. follow-up: poskytni navyše hodnotu (PDF dotácia, kalkulačka návratnosti, atď.)
- 3. follow-up: rozlúčkový ("Ak teraz nie je vhodný čas, kľudne sa nám ozvete kedykoľvek na info@energovision.sk")

Po 3. follow-upe agent dostane Status = ❄️ Cold a prestane písať.

# Výstupný formát
NA KAŽDÝ TVOJ AKČNÝ KROK vraciaš JSON v presne tomto formáte:
{{
  "subject": "Re: [EV-26-XXX] Pôvodný subjekt | nový dodatok",
  "body": "Telo emailu bez podpisu — podpis pridá systém",
  "extracted_info": {{
    "spotreba_kwh": 5000 | null,
    "typ_strechy": "Škridla" | null,
    "orientacia": "Juh" | null,
    "ma_zaujem_o": ["FVE", "BESS", "Wallbox", "Revízia"] | [],
    "termin_realizacie": "Leto 2026" | null,
    "rozpocet_predstava": "8-12 tis €" | null,
    "iné_poznamky": "..."
  }},
  "handover_to_dominik": false,
  "lead_quality": "hot" | "warm" | "cold" | "dead",
  "next_action": "wait_for_reply" | "send_followup_in_3d" | "handover" | "stop"
}}

Vraciaš LEN JSON, žiadne markdown bloky, žiadne komentáre okolo.
"""


# ============================================================
# Helper — Claude API
# ============================================================
def _claude_call(messages, system=None, max_tokens=2000, temperature=0.4):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY chýba v ENV")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    r = _retry_request(lambda: requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60))
    r.raise_for_status()
    return r.json()


def _parse_json_response(raw: str) -> dict:
    """Vyparsuj JSON z Claude odpovede, odolný voči markdown blokom."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise ValueError(f"No JSON in response: {raw[:200]}")
    return json.loads(m.group(0))


def _format_signature() -> str:
    """Vytvor štandardnú signature."""
    return (
        f"\n\nS pozdravom,\n"
        f"{EMAIL_AGENT_SIGNATURE_NAME}\n"
        f"Energovision\n"
        f"{EMAIL_AGENT_PHONE} | {EMAIL_AGENT_EMAIL}\n"
        f"{EMAIL_AGENT_WEBSITE}\n\n"
        f"---\n"
        f"Ak si neželáte ďalšie e-maily, odpovedzte slovom STOP.\n"
        f"Energovision spracúva osobné údaje na účel obchodnej komunikácie. Viac na {EMAIL_AGENT_WEBSITE}/gdpr."
    )


# ============================================================
# PRVÝ KONTAKT — generuj uvodný email
# ============================================================
def vygeneruj_prvy_email(lead: dict) -> dict:
    """
    Vstup:
        lead = {
            "ev_id": "EV-26-001",
            "meno": "Ján Novák",
            "email": "jan.novak@email.sk",
            "telefon": "+421...",
            "mesto": "Žilina",
            "spotreba_kwh": 5000 | None,
            "ma_zaujem_o": ["FVE"] | None,
            "poznamky": "originálna správa z webu / poznámky z dopytu",
            "zdroj": "Web formulár" | "Chatbot" | "Telefón" | ...,
        }

    Výstup:
        {
            "subject": "Re: [EV-26-001] ...",
            "body": "telo emailu",
            "body_with_signature": "telo + podpis (na odoslanie)",
            ...
        }
    """
    ev_id = lead.get("ev_id", "EV-XX")
    meno = lead.get("meno", "")
    mesto = lead.get("mesto", "")
    poznamky = lead.get("poznamky", "")
    spotreba = lead.get("spotreba_kwh")
    zaujem = lead.get("ma_zaujem_o") or []
    if isinstance(zaujem, str):
        zaujem = [zaujem]
    zdroj = lead.get("zdroj", "Web")

    prompt = (
        f"Toto je PRVÝ kontakt s novým leadom. Zatiaľ konverzácia nezačala — pripravuješ úvodný email.\n\n"
        f"Lead informácie:\n"
        f"- ID: {ev_id}\n"
        f"- Meno: {meno or '(neznáme)'}\n"
        f"- Mesto: {mesto or '(neznáme)'}\n"
        f"- Zdroj: {zdroj}\n"
        f"- Spotreba kWh/rok: {spotreba if spotreba else '(neznáma)'}\n"
        f"- Záujem o: {', '.join(zaujem) if zaujem else '(nešpecifikované)'}\n"
        f"- Pôvodná správa / poznámky: {poznamky[:1000] if poznamky else '(žiadne)'}\n\n"
        f"Vygeneruj prvý profesionálny email:\n"
        f"1. Subject MUSÍ obsahovať [{ev_id}] tag pre routing (formát: '[{ev_id}] Energovision — Vaš dopyt')\n"
        f"2. Naviaž na konkrétny záujem leadu (ak je špecifikovaný), inak otvor všeobecne\n"
        f"3. Pýtaj sa 1-2 konkrétne otázky podľa toho čo zatiaľ nevieš (spotreba, typ strechy, alebo termín)\n"
        f"4. Pridaj 1-2 vety užitočnej info (napr. orientačné rozpätie cien alebo dotácia)\n"
        f"5. Lead je úplne nový → lead_quality='warm', handover_to_dominik=false, next_action='wait_for_reply'\n\n"
        f"Vráť LEN JSON podľa formátu v system prompte."
    )

    resp = _claude_call(
        messages=[{"role": "user", "content": prompt}],
        system=SYSTEM_PROMPT,
        max_tokens=1500,
        temperature=0.5,
    )
    raw = (_safe_claude_text(resp) or "{}")
    data = _parse_json_response(raw)

    # Pridaj podpis
    body = data.get("body", "")
    data["body_with_signature"] = body + _format_signature()
    data["tokens"] = resp.get("usage", {}).get("input_tokens", 0) + resp.get("usage", {}).get("output_tokens", 0)
    return data


# ============================================================
# ODPOVEĎ NA EMAIL — process inbound reply
# ============================================================
def spracuj_odpoved(lead: dict, transcript: list, posledna_sprava: str, follow_up_count: int = 0) -> dict:
    """
    Vstup:
        lead: rovnaké info ako pri prvom kontakte
        transcript: [{"role": "agent"|"customer", "content": "...", "date": "..."}, ...]
        posledna_sprava: nová prichádzajúca správa od zákazníka
        follow_up_count: koľko follow-upov sme už poslali (default 0)

    Výstup: rovnaký JSON ako pri prvom kontakte
    """
    ev_id = lead.get("ev_id", "EV-XX")

    # Detekuj opt-out
    if re.search(r"\b(STOP|UNSUBSCRIBE|ODHL[AÁ]SI[TŤ])\b", posledna_sprava.upper()):
        return {
            "subject": f"Re: [{ev_id}] Vaša žiadosť o odhlásenie",
            "body": "Dobrý deň,\n\nzaznamenali sme vašu žiadosť. Vyradili sme vás z ďalšej komunikácie. Ďakujeme.\n\nPekný deň praje,",
            "body_with_signature": "",
            "extracted_info": {},
            "handover_to_dominik": False,
            "lead_quality": "dead",
            "next_action": "stop",
            "opted_out": True,
            "tokens": 0,
        }

    # Stavaj transcript ako messages pre Claude
    messages = []
    for turn in (transcript or [])[-12:]:
        role = "assistant" if turn.get("role") == "agent" else "user"
        content = turn.get("content", "")
        if content:
            messages.append({"role": role, "content": content[:3000]})

    # Pridaj novú správu od zákazníka + kontext
    context_block = (
        f"# Kontext leadu\n"
        f"- ID: {ev_id}\n"
        f"- Meno: {lead.get('meno','(neznáme)')}\n"
        f"- Mesto: {lead.get('mesto','(neznáme)')}\n"
        f"- Aktuálne známe info: spotreba={lead.get('spotreba_kwh','?')}, strecha={lead.get('typ_strechy','?')}, "
        f"záujem={lead.get('ma_zaujem_o','?')}\n"
        f"- Počet follow-upov zatiaľ poslaných: {follow_up_count} (max {MAX_FOLLOWUPS})\n\n"
        f"# Nová správa od zákazníka:\n{posledna_sprava[:4000]}\n\n"
        f"Pripravi odpoveď podľa system promptu. Vráť LEN JSON."
    )
    messages.append({"role": "user", "content": context_block})

    resp = _claude_call(
        messages=messages,
        system=SYSTEM_PROMPT,
        max_tokens=2000,
        temperature=0.4,
    )
    raw = (_safe_claude_text(resp) or "{}")
    data = _parse_json_response(raw)

    body = data.get("body", "")
    data["body_with_signature"] = body + _format_signature()
    data["tokens"] = resp.get("usage", {}).get("input_tokens", 0) + resp.get("usage", {}).get("output_tokens", 0)
    return data


# ============================================================
# FOLLOW-UP — leads ktorí nereagujú
# ============================================================
def vygeneruj_followup(lead: dict, transcript: list, follow_up_count: int, dni_od_poslednej: int) -> dict:
    """Lead 3+ dni neodpovedá. Pošli jemný follow-up."""
    ev_id = lead.get("ev_id", "EV-XX")
    fu_num = follow_up_count + 1  # nový follow-up index

    if fu_num > MAX_FOLLOWUPS:
        return {
            "subject": f"Re: [{ev_id}] Zostávame v kontakte",
            "body": (
                "Dobrý deň,\n\n"
                "od posledného emailu uplynul nejaký čas. Predpokladám, že momentálne nie je vhodný čas. "
                "Ak by ste sa neskôr chceli k téme vrátiť, ozvete sa nám kedykoľvek na info@energovision.sk "
                "alebo telefonicky.\n\n"
                "Prajem pekný deň."
            ),
            "body_with_signature": "",
            "extracted_info": {},
            "handover_to_dominik": False,
            "lead_quality": "cold",
            "next_action": "stop",
            "tokens": 0,
        }

    messages = []
    for turn in (transcript or [])[-8:]:
        role = "assistant" if turn.get("role") == "agent" else "user"
        if turn.get("content"):
            messages.append({"role": role, "content": turn["content"][:2000]})

    prompt = (
        f"Zákazník {dni_od_poslednej} dní neodpovedal na náš email. Toto bude {fu_num}. follow-up "
        f"(max {MAX_FOLLOWUPS}). \n\n"
        f"Lead: {lead.get('meno','?')}, mesto: {lead.get('mesto','?')}, ID: {ev_id}\n\n"
        f"Pripravi krátky follow-up email podľa system promptu:\n"
        f"- 1. follow-up: jemný, prirodzený. ('Ozývam sa znovu, predpokladám že ste mali rušné dni...')\n"
        f"- 2. follow-up: poskytni navyše hodnotu (info o aktuálnej dotácii, kalkulačka návratnosti)\n"
        f"- 3. follow-up: rozlúčka — povedz že ich teraz neotravujeme, ozvú sa keď budú chcieť\n\n"
        f"Toto je follow-up č. {fu_num}. Vráť LEN JSON."
    )
    messages.append({"role": "user", "content": prompt})

    resp = _claude_call(
        messages=messages,
        system=SYSTEM_PROMPT,
        max_tokens=1500,
        temperature=0.5,
    )
    raw = (_safe_claude_text(resp) or "{}")
    data = _parse_json_response(raw)

    body = data.get("body", "")
    data["body_with_signature"] = body + _format_signature()
    data["tokens"] = resp.get("usage", {}).get("input_tokens", 0) + resp.get("usage", {}).get("output_tokens", 0)
    return data


# ============================================================
# Test
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_lead = {
        "ev_id": "EV-26-099",
        "meno": "Ján Novák",
        "email": "test@example.sk",
        "mesto": "Žilina",
        "ma_zaujem_o": ["FVE"],
        "poznamky": "Mám rodinný dom, počul som o dotácii a chcem vedieť koľko by ma to stálo. "
                    "Spotreba je niekde okolo 5500 kWh za rok.",
        "zdroj": "Web formulár",
        "spotreba_kwh": 5500,
    }

    print("=== PRVÝ EMAIL ===\n")
    result = vygeneruj_prvy_email(test_lead)
    print(f"Subject: {result['subject']}\n")
    print(f"Body:\n{result['body_with_signature']}\n")
    print(f"Extracted: {json.dumps(result.get('extracted_info', {}), ensure_ascii=False)}")
    print(f"Tokens: {result.get('tokens', 0)}")

    print("\n=== ODPOVEĎ NA REPLY ===\n")
    transcript = [
        {"role": "agent", "content": result["body"]},
    ]
    reply = ("Dobrý deň, ďakujem za odpoveď. Mám škridľovú strechu, "
             "orientácia juhozápad. Spotrebu som spomenul - cca 5500 kWh. "
             "Chcel by som aj batériu. Termín — ideálne ešte toto leto.")
    result2 = spracuj_odpoved(test_lead, transcript, reply, follow_up_count=0)
    print(f"Subject: {result2['subject']}\n")
    print(f"Body:\n{result2['body_with_signature']}\n")
    print(f"Extracted: {json.dumps(result2.get('extracted_info', {}), ensure_ascii=False, indent=2)}")
    print(f"Handover: {result2.get('handover_to_dominik')}")
    print(f"Quality: {result2.get('lead_quality')}, next_action: {result2.get('next_action')}")
