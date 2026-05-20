"""SK lingvistický helper — pán/pani derivácia podľa mena/priezviska."""
import re

_MALE_NAMES_ENDING_A = {"luca", "nikita", "saša"}

def guess_gender(first_name: str = "", last_name: str = "") -> str:
    """Vráti 'male' / 'female' / 'unknown'."""
    fn = (first_name or "").strip().lower()
    ln = (last_name or "").strip().lower()

    # Najsilnejší signál — slovenské ženské priezvisko
    if ln.endswith("ová") or ln.endswith("eková") or ln.endswith("ská") or ln.endswith("cká") or ln.endswith("anská"):
        return "female"

    # Krstné meno končiace na -a/-á/-ia → typicky žena (s výnimkami)
    if fn and (fn.endswith("a") or fn.endswith("á") or fn.endswith("ia")):
        if fn not in _MALE_NAMES_ENDING_A:
            return "female"

    # Mužský pattern — končí na spoluhlásku alebo -o
    if fn and re.search(r"[bcdfghjklmnopqrstuvwxzčďňťľĺŕš]$", fn):
        return "male"
    if fn and fn.endswith("o"):
        return "male"

    return "unknown"


def oslovenie_pan_pani(first_name: str = "", last_name: str = "") -> str:
    """'pán' / 'pani' / 'pán/pani' (neutralne ak gender unknown)."""
    g = guess_gender(first_name, last_name)
    if g == "male": return "pán"
    if g == "female": return "pani"
    return "pán/pani"


def oslovenie_plne(first_name: str = "", last_name: str = "") -> str:
    """Napr. 'pán Bago', 'pani Baginská', 'pán/pani Bago'."""
    return f"{oslovenie_pan_pani(first_name, last_name)} {last_name or ''}".strip()
