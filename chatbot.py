"""
chatbot.py — AI chatbot pre web Energovision.

Použitie:
    from chatbot import odpovedz_chatbot, extrahuj_lead

    answer = odpovedz_chatbot(history=[...], user_message="...")
    lead = extrahuj_lead(full_history)  # ak je dosť info, vráti dict, inak {}

Volá Anthropic Claude API cez requests (rovnaký pattern ako /webhook/parsuj-leady).
ENV: ANTHROPIC_API_KEY, ANTHROPIC_MODEL (default sonnet 4.5).
"""

import os
import json
import logging
import re
import requests

log = logging.getLogger("chatbot")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MAX_TOKENS = 1024

# ============================================================
# SYSTEM PROMPT — know-how Energovision
# ============================================================
SYSTEM_PROMPT = """Si AI asistent firmy **Energovision** — slovenskej spoločnosti z energetiky.
Pomáhaš návštevníkom webu energovision.sk získať informácie o našich službách a zachytávaš leady pre obchod.

# Identita
- Si profesionálny, priateľský a vecný. Vykáš.
- Odpovedaj po slovensky. Nepoužívaj marketingovú vatu ani superlatívy.
- Buď stručný (2-4 vety na odpoveď ak nie je nutné viac).
- Ak nevieš odpoveď, povedz to a ponúkni kontakt na obchodníka.

# Čo robí Energovision
Energovision pôsobí v energetike širšie ako len fotovoltika. Naše hlavné služby:
1. **Fotovoltické elektrárne (FVE)** — kompletné riešenia 3-30 kWp pre rodinné domy, väčšie pre firmy
2. **Batériové úložiská (BESS)** — kombinované s FVE alebo solo
3. **Wallbox / nabíjacie stanice** pre elektromobily
4. **Údržba a servis trafostaníc** — pravidelné prehliadky, opravy, modernizácie
5. **Odborné revízie** elektrických zariadení (VTZ, hromozvody, NN/VN rozvody)
6. **Elektrotechnické práce** — projekty, realizácie, VN pripojenia, prípojky
7. **Technické a realizačné činnosti** v energetike — pre B2B aj B2C

# Pre B2C zákazníkov — FVE/BESS know-how
- **Cena FVE 5-10 kWp pre rodinný dom:** orientačne 6 000 — 14 000 EUR s DPH (závisí od typu strechy, batérie, wallboxu, distribučnej oblasti)
- **Dotácia Zelená domácnostiam:** 500 €/kW, maximum poukážky **1 500 € celkom** (3 kW podporiteľného výkonu). Strop výpočtu: MIN(výkon FVE, spotreba/1000, 3). Strop 50 % z oprávnených výdavkov. Okresové zvýhodnenia (575/900 €/kW) UŽ NEPLATIA. Batériu NEPODPORUJE.
- **Návratnosť FVE:** typicky 6-9 rokov pri samospotrebe 60 % +
- **Komponenty:** preferované značky meničov Solinteg, Huawei, GoodWe. Batérie LUNA2000, Solinteg EBA, Pylontech. Panely LONGi 535-700 Wp.
- **Dimenzovanie:** pre rodinný dom so spotrebou 4 000-6 000 kWh/rok je optimálne 5-7 kWp. Pre 8 000-12 000 kWh/rok je optimálne 7-10 kWp.
- **Batéria:** pri samospotrebe < 50 % odporúčame batériu (typicky 5-15 kWh)

# Pre B2B zákazníkov
- **FVE pre firmy:** 30-500 kWp na strechu alebo pozemok, dotácia Zelená podnikom (z EÚ fondov), návratnosť 4-7 rokov vďaka daňovému odpisu 6 r. DPPO 21 %
- **Trafostanice:** servis, oleje, dielektrické skúšky, modernizácie
- **Revízie:** VTZ vyhradené technické zariadenia, hromozvody, NN/VN rozvody

# Lead capture — kedy a ako
Ak zákazník prejavuje záujem o cenovú ponuku, navrhni získať kontaktné údaje. Potrebujeme:
- Meno a priezvisko
- Telefón ALEBO email (stačí jeden)
- Adresa nehnuteľnosti alebo aspoň mesto/obec
- Ročná spotreba elektriny v kWh (z faktúry) — ak nevie, pomôže nám priemer
- Typ strechy (škridla / plech / falcový plech / rovná) a orientácia (juh / V-Z) — ak je to FVE

Ak vidíš že už máš dostatok informácií, vyzvi zákazníka na uzatvorenie:
"Mám všetko čo treba. Pošlem to nášmu obchodníkovi Dominikovi, ozve sa do 24 hodín. Súhlasíte?"

Ak zákazník súhlasí, na konci svojej finálnej správy pridaj tag:
[LEAD_READY]

# Kontakty pre fallback
Ak zákazník chce hovoriť s človekom:
- Obchod B2C: Dominik Galaba, +421 917 424 564, dominik.galaba@energovision.sk
- Web: energovision.sk

# Tabu
- Neuvádzaj exaktnú cenu bez výpočtu — vždy daj rozpätie
- Nikdy nesľubuj termín realizácie bez konzultácie obchodníka
- Nehovor o cenách konkurencie

# Štýl
- Krátke odseky, bez bulletov pokiaľ to nie je 4+ položkový zoznam
- Bez emoji ak nie sú nevyhnutné
- Bez nadpisov v odpovediach (chat je krátky)
"""


def _claude_call(messages, system=None, max_tokens=MAX_TOKENS, temperature=0.7):
    """Volá Anthropic API cez requests."""
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

    r = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


# ============================================================
# Hlavná funkcia — odpoveď chatbota
# ============================================================
def odpovedz_chatbot(history: list, user_message: str) -> dict:
    """
    Vstup:
        history: [{"role": "user"|"assistant", "content": "..."}]
        user_message: nová správa od zákazníka

    Výstup:
        {
            "answer": "text odpovede",
            "lead_ready": bool,
            "tokens": int,
        }
    """
    messages = []
    for msg in (history or [])[-20:]:
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            messages.append({
                "role": msg["role"],
                "content": str(msg["content"])[:4000],
            })
    messages.append({"role": "user", "content": user_message[:4000]})

    try:
        resp = _claude_call(messages, system=SYSTEM_PROMPT)
        answer_raw = resp["content"][0]["text"] if resp.get("content") else ""
        lead_ready = "[LEAD_READY]" in answer_raw
        answer = answer_raw.replace("[LEAD_READY]", "").strip()
        usage = resp.get("usage", {})
        return {
            "answer": answer,
            "lead_ready": lead_ready,
            "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        }
    except Exception as e:
        log.exception("[chatbot] Claude API error")
        return {
            "answer": "Prepáčte, momentálne mám technický problém. Skúste prosím o chvíľu znovu, "
                      "alebo kontaktujte priamo Dominika: +421 917 424 564 / dominik.galaba@energovision.sk.",
            "lead_ready": False,
            "tokens": 0,
            "error": str(e)[:200],
        }


# ============================================================
# Extrahuj lead z konverzácie
# ============================================================
def extrahuj_lead(history: list) -> dict:
    """Z konverzácie vyextrahuj kontaktné a technické info pre lead capture."""
    full_text = "\n".join(
        f"Z: {m.get('content','')}"
        for m in (history or []) if m.get('role') == 'user'
    )
    if not full_text.strip():
        return {}

    prompt = (
        "Z nasledujúcej konverzácie zákazníka s chatbotom extrahuj kontaktné a technické údaje pre lead capture.\n\n"
        "Konverzácia (iba vstupy zákazníka):\n---\n"
        f"{full_text[:6000]}\n---\n\n"
        "Vráť LEN čistý JSON bez markdown blokov, s týmito kľúčmi "
        "(každý je nepovinný — ak nie je info, vynech ho):\n"
        '{\n'
        '  "meno": "Meno Priezvisko",\n'
        '  "email": "email@example.com",\n'
        '  "telefon": "+421 XXX XXX XXX",\n'
        '  "mesto": "Bratislava",\n'
        '  "adresa": "Hlavná 1, Bratislava",\n'
        '  "spotreba_kwh": 5000,\n'
        '  "typ_strechy": "Škridla",\n'
        '  "orientacia": "Juh",\n'
        '  "ma_zaujem_o": ["FVE", "BESS", "Wallbox", "Revízia", "Trafostanica", "Iné"],\n'
        '  "poznamka": "stručná poznámka pre obchodníka 1-2 vety"\n'
        '}\n\n'
        "Ak nenájdeš žiadne kontaktné údaje, vráť: {}"
    )

    try:
        resp = _claude_call(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.1,
        )
        raw = resp["content"][0]["text"] if resp.get("content") else "{}"
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return {}
        return json.loads(m.group(0))
    except Exception as e:
        log.warning("[chatbot] extrahuj_lead failed: %s", e)
        return {}


# ============================================================
# Test
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    history = []
    print("Energovision chatbot tester. 'koniec' pre ukončenie.\n")
    while True:
        try:
            user = input("Vy: ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not user or user.lower() == "koniec":
            break
        result = odpovedz_chatbot(history, user)
        print(f"\nBot: {result['answer']}\n")
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": result["answer"]})
        if result.get("lead_ready"):
            print("[LEAD READY — extrahujem...]")
            lead = extrahuj_lead(history)
            print(f"LEAD: {json.dumps(lead, ensure_ascii=False, indent=2)}\n")
            break
