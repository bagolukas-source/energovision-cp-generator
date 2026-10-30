"""Notion Dashboard -> projects: zrkadlo stavu, To do a report rozdielov.

Čisté funkcie bez siete, volá ich /webhook/notion-sync-zakazky v app.py.

Pravidlá:
- do projects ide len stav_celkovy_notion, todo_notion a notion_synced_at,
- vlastné polia CRM (POLIA_CRM) sa nezapisujú vôbec: ZoD a suma spúšťajú lifecycle, bránu
  realizácie, W4, províziu aj návrh faktúry. Prázdne idú do reportu doplnenia, rôzne do
  reportu rozdiely a ako udalosť notion_rozdiel,
- párovanie len cez notion_page_id, pri neistej väzbe sa nezapisuje a nahlási sa,
- archivované projekty sa preskakujú, nič sa nezakladá ani nemaže,
- computed_phase sa nemení, nesúlad so stavom ide do reportu rozpor_stavu.

Hodnoty sa čítajú priamo z Notion properties: notion_props_to_flat nepozná typy
status a date, stav a dátumy by zahodil.
"""
import difflib
import hmac
import re
import unicodedata
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bratislava")
except Exception:  # chýba tzdata, modul sa importuje v app.py a nesmie zhodiť službu
    TZ = timezone.utc

P_STAV = "STAV celkový"
P_TODO = "To do / na čo čakáme ?"
P_SUMA = "Cena diela"
P_ZOD = "ZoD - dátum podpisu"
P_FS = "Dátum funkčnej skúšky"
P_TERMIN = "Termín podľa ZoD"
P_OBCHODNIK = "Obchodník"
VSETKY_P = (P_STAV, P_TODO, P_SUMA, P_ZOD, P_FS, P_TERMIN, P_OBCHODNIK)

# Predvolená hodnota status property: Notion ju ukazuje aj pri riadku bez hodnoty.
STAV_PREDVOLENY = "Administratíva"
STAV_POZASTAVENE = "Pozastavené"

ZRKADLO = {"stav": "stav_celkovy_notion", "todo": "todo_notion"}
ZAPIS_STLPCE = frozenset(ZRKADLO.values()) | {"notion_synced_at"}
POLIA_CRM = {
    "suma": "contract_value_no_vat",
    "zod": "contract_signed_at",
    "fs": "fs_termin",
    "termin": "expected_end_date",
    "obchodnik": "obchodnik",
}
POPIS = {"suma": "Suma", "zod": "ZoD", "fs": "Funkčná skúška", "termin": "Termín", "obchodnik": "Obchodník"}

# computed_phase -> stavy Notionu, ktoré s ňou nie sú v rozpore. Pozastavené sa neporovnáva.
STAV_FAZY = {
    "bez_zod": ("Obchod", "Administratíva"),
    "vystavit_fa1": ("Administratíva",),
    "urgovat_fa1": ("Administratíva",),
    "projekcia": ("Administratíva", "V realizácii"),
    "realizacia": ("V realizácii",),
    "funkcna_skuska": ("V realizácii", "Realizácia dokončená"),
    "porealizacna": ("Realizácia dokončená", "Porealizačná fáza", "Zákazka dokončená"),
    "dokoncene": ("Porealizačná fáza", "Zákazka dokončená"),
}

_STOP = {"sro", "spol", "spolocnost", "ing", "kwp", "kwh", "fve", "bess", "bue"}
_ROZLISOVACE = (
    r"\bom\s*-?\s*(\d+)\b",
    r"\b(\d+)\s*\.?\s*etap",
    r"\betap[a-z]*\s*(\d+)\b",
    r"(\d+(?:[.,]\d+)?)\s*(?:kwp|kwh|kw|mw)\b",
)


def zapis_povoleny(dry_run, env):
    """Poistka: cron volá dry_run=0 a Render nasadzuje merge hneď, zápis zapína až env."""
    return not dry_run and env.get("NOTION_ZRKADLO_ZAPIS") == "1"


def tajomstvo_ok(prijate, tajomstvo):
    """Prázdne tajomstvo na serveri znamená odmietnuť, nie pustiť všetkých."""
    return bool(tajomstvo) and hmac.compare_digest((prijate or "").encode(), tajomstvo.encode())


def hodnoty_stranky(pg):
    """Z Notion stránky vyberie sledované hodnoty. Chýbajúca property = kľúč chýba, pole sa nechá tak."""
    p = pg.get("properties") or {}
    out = {}
    if P_STAV in p:
        out["stav"] = ((p[P_STAV].get("status") or p[P_STAV].get("select") or {}).get("name")) or STAV_PREDVOLENY
    if P_TODO in p:
        out["todo"] = "".join(t.get("plain_text", "") for t in p[P_TODO].get("rich_text") or []).strip() or None
    if P_SUMA in p:
        out["suma"] = p[P_SUMA].get("number") or None  # 0 v Notione znamená nevyplnené
    for kluc, nazov in (("zod", P_ZOD), ("fs", P_FS), ("termin", P_TERMIN)):
        if nazov in p:
            start = (p[nazov].get("date") or {}).get("start")
            out[kluc] = (start if kluc == "fs" else start and start[:10]) or None
    if P_OBCHODNIK in p:
        out["obchodnik"] = ", ".join(s["name"] for s in p[P_OBCHODNIK].get("multi_select") or []) or None
    return out


def _prazdne(v):
    return v is None or str(v).strip() == ""


def _den(v):
    """Kalendárny deň v Bratislave; timestamptz 2026-05-21T22:00Z je 22. 5."""
    s = str(v)
    if len(s) <= 10:
        return s
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    return (d if d.tzinfo else d.replace(tzinfo=TZ)).astimezone(TZ).date().isoformat()


def _mena(v):
    return {m.strip().casefold() for m in re.split(r"[,;/]", str(v)) if m.strip()}


def _rovnake(kluc, crm, notion):
    if kluc == "suma":
        return abs(float(crm) - float(notion)) < 0.01
    if kluc == "obchodnik":
        return _mena(crm) <= _mena(notion)  # Notion je multi_select, poradie ani ďalší obchodník nie sú rozpor
    return _den(crm) == _den(notion)


def porovnaj(hodnoty, projekt):
    """Vráti (zmeny pre PATCH, doplnenia, rozdiely). Zmeny obsahujú len zrkadlové stĺpce."""
    zmeny, doplnenia, rozdiely = {}, [], []
    for kluc, stlpec in ZRKADLO.items():
        if kluc in hodnoty and (projekt.get(stlpec) or None) != hodnoty[kluc]:
            zmeny[stlpec] = hodnoty[kluc]
    for kluc, stlpec in POLIA_CRM.items():
        notion = hodnoty.get(kluc)
        if notion is None:
            continue
        crm = projekt.get(stlpec)
        if _prazdne(crm):
            doplnenia.append({"pole": kluc, "stlpec": stlpec, "notion": str(notion)})
        elif not _rovnake(kluc, crm, notion):
            rozdiely.append({"pole": kluc, "crm": str(crm), "notion": str(notion)})
    return zmeny, doplnenia, rozdiely


def rozpor_stavu(stav, faza):
    povolene = STAV_FAZY.get(faza)
    return bool(stav and povolene and stav != STAV_POZASTAVENE and stav not in povolene)


def _kod(t):
    m = re.search(r"P-\d{2}-\d+", (t or "").upper())
    return m.group(0) if m else None


def _ascii(t):
    return "".join(c for c in unicodedata.normalize("NFKD", (t or "").lower()) if not unicodedata.combining(c))


def _slova(t):
    t = re.sub(r"p-\d{2}-\d+", " ", _ascii(t)).replace("s.r.o", "sro").replace("s. r. o", "sro")
    return {w for w in re.findall(r"[a-z]{3,}", t) if w not in _STOP}


def _rozlisovace_sedia(a, b):
    """OM, etapa a výkon: ak ich uvádzajú obe strany, musia sa zhodovať."""
    a, b = _ascii(a), _ascii(b)
    for vzor in _ROZLISOVACE:
        ra = {x.replace(",", ".") for x in re.findall(vzor, a)}
        rb = {x.replace(",", ".") for x in re.findall(vzor, b)}
        if ra and rb and ra != rb:
            return False
    return True


def _podobne(slova, ine):
    return all(any(difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 for b in ine) for a in slova)


def vazba_ok(nazov_notion, nazov_crm):
    """Sedí stránka na projekt? Pri neistote False: nezapisuje sa a nahlási sa.
    KRUP: rovnaký kód, ale Baliareň v CRM a TRAFOSTANICA v Notione."""
    kn, kc = _kod(nazov_notion), _kod(nazov_crm)
    if kn and kc and kn != kc:
        return False
    if not _rozlisovace_sedia(nazov_notion, nazov_crm):
        return False
    a, b = _slova(nazov_notion), _slova(nazov_crm)
    if not a or not b:
        return bool(kn and kc)  # zhodný kód bez ďalšieho názvu
    if not (kn and kc):  # CRM názov môže byť voľný („FVE Radošinka 200kW“), stačí spoločné vlastné slovo
        return bool(a & b)
    return a <= b or b <= a or _podobne(a - b, b) or _podobne(b - a, a)


def nazov_stranky(pg):
    for pr in (pg.get("properties") or {}).values():
        if pr.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in pr.get("title") or []).strip()
    return ""


def _id(v):
    return (v or "").replace("-", "")


def naplanuj(stranky, projekty, videne_rozdiely):
    """Čistý výpočet behu: čo zapísať a čo nahlásiť. videne_rozdiely = {(entity_id, pole, crm, notion)}."""
    podla_id = {}
    for p in projekty:
        if p.get("notion_page_id"):
            podla_id.setdefault(_id(p["notion_page_id"]), []).append(p)

    plan = {"zmeny": [], "udalosti": [], "ok_ids": []}
    rep = {
        "stranky": len(stranky), "sparovane": 0, "archivovane": 0, "projekty_so_zmenou": 0,
        "zmeny_polia": {}, "doplnenia_polia": {}, "rozdiely_polia": {}, "nove_rozdiely": 0,
        "zla_vazba": [], "bez_projektu": [], "chybajuce_properties": [],
        "rozpor_stavu": [], "rozdiely": [], "doplnenia": [],
    }
    videne_stranky = set()
    for pg in stranky:
        pid, nazov = _id(pg.get("id")), nazov_stranky(pg)
        videne_stranky.add(pid)
        vsetky = podla_id.get(pid, [])
        if not vsetky:
            rep["bez_projektu"].append({"notion": nazov, "notion_page_id": pid})
            continue
        zhody = [p for p in vsetky if not p.get("archived_at")]
        rep["archivovane"] += len(vsetky) - len(zhody)
        if not zhody:
            continue
        zle = [p for p in zhody if len(zhody) > 1 or not vazba_ok(nazov, p.get("name"))]
        if zle:
            rep["zla_vazba"] += [{"projekt": p.get("name"), "id": p["id"], "notion": nazov, "notion_page_id": pid} for p in zle]
            continue
        p = zhody[0]
        rep["sparovane"] += 1
        plan["ok_ids"].append(p["id"])
        hodnoty = hodnoty_stranky(pg)
        rep["chybajuce_properties"] = sorted(set(rep["chybajuce_properties"]) | {n for n in VSETKY_P if n not in (pg.get("properties") or {})})
        zmeny, doplnenia, rozdiely = porovnaj(hodnoty, p)
        if zmeny:
            plan["zmeny"].append((p["id"], zmeny))
            rep["projekty_so_zmenou"] += 1
            for s in zmeny:
                rep["zmeny_polia"][s] = rep["zmeny_polia"].get(s, 0) + 1
        for d in doplnenia:
            rep["doplnenia_polia"][d["pole"]] = rep["doplnenia_polia"].get(d["pole"], 0) + 1
            rep["doplnenia"].append({"projekt": p.get("name"), **d})
        if rozpor_stavu(hodnoty.get("stav"), p.get("computed_phase")):
            rep["rozpor_stavu"].append({"projekt": p.get("name"), "stav_notion": hodnoty["stav"], "computed_phase": p["computed_phase"], "suma": p.get("contract_value_no_vat")})
        for r in rozdiely:
            rep["rozdiely_polia"][r["pole"]] = rep["rozdiely_polia"].get(r["pole"], 0) + 1
            rep["rozdiely"].append({"projekt": p.get("name"), **r})
            if (p["id"], r["pole"], r["crm"], r["notion"]) in videne_rozdiely:
                continue
            rep["nove_rozdiely"] += 1
            plan["udalosti"].append({
                "verb": "notion_rozdiel", "source": "notion_sync_auto", "entity_type": "project",
                "entity_id": p["id"], "actor_type": "system",
                "payload": {**r, "notion_page_id": pid, "text": f"{POPIS[r['pole']]}: CRM {r['crm'][:40]}, Notion {r['notion'][:40]}"},
            })

    for pid, zhody in podla_id.items():
        if pid not in videne_stranky:
            rep["zla_vazba"] += [{"projekt": p.get("name"), "id": p["id"], "notion": None, "notion_page_id": pid} for p in zhody if not p.get("archived_at")]
    return plan, rep
