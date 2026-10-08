"""Nočná synchronizácia cenníka Raynet -> B2B kalkulačka (Fáza 2, agent D).

Čo robí
-------
Stiahne z Raynet API v2 (len GET, do Raynetu sa NIČ nezapisuje — rozhodnutie D5) cenníky, ich položky a produkty,
vyráta "efektívnu" cenu a nákup každého produktu a porovná ich s tromi cieľmi v Supabase:

  * ``b2b_calc_rules``    — mapovanie podľa ``raynet_product_id`` alebo kódu v ``notes`` ("... kód XYZ; ...")
                            -> ``cost_per_unit`` (nákup) a ``price_per_unit`` (cenník)
  * ``b2b_vendor_stacks`` — prvky JSON stĺpcov s poľom ``code`` (panely majú ``sku``) -> ``cost`` a ``price``
                            (+ zrkadlá ``price_per_unit`` / ``price_per_panel`` pre staré jadro)
  * ``products``          — ``sku`` = kód Raynetu -> ``purchase_price`` a ``sale_price``

Endpoint
--------
``POST /cron/raynet-price-sync`` (za ``require_secret``). Parametre (query / form / JSON):

  * ``dry_run=1`` (predvolené) — nič nezapíše (ani ``raynet_raw_products``), vráti len plán a report.
  * ``apply=1``               — zapíše. ``apply`` spolu s ``dry_run=1`` = HTTP 400; ``dry_run=0`` bez ``apply=1`` = HTTP 400
                                (zápis sa zapína výlučne ``apply=1``).
  * ``align=1``               — prijme hodnotu z Raynetu aj tam, kde sa cieľ líši od posledného stavu katalógu
                                (kuratované hodnoty) a tam, kde pôvodná hodnota chýba. Stále platí limit 30 %, zámky
                                a kontroly. S ``dry_run`` ide o simuláciu ("čo by align zmenil"); zápis až s ``apply=1``.
  * ``targets=rules,stacks,products`` — podmnožina cieľov (predvolené všetky tri; env RAYNET_SYNC_TARGETS).
  * ``max_changes=N``         — poistka: ak plán obsahuje viac riadkov na zmenu, nezapíše sa nič (HTTP 409).

Env: ``RAYNET_INSTANCE``, ``RAYNET_USER`` (alebo ``RAYNET_USERNAME``), ``RAYNET_API_KEY`` (alebo ``RAYNET_KEY``),
``SUPABASE_SERVICE_ROLE_KEY`` (+ voliteľne ``SUPABASE_URL``), ``WEBHOOK_SECRET``. Bez Raynet premenných endpoint vráti
503 so správou, čo nastaviť. Odpoveď obsahuje nákupné ceny, preto bez ``WEBHOOK_SECRET`` vráti 503 aj dry-run (nezávisle
od toho, či je ``require_secret`` fail-open). Tajomstvá sa nikdy nevypisujú (ani do logu, ani do odpovede).

Pravidlá zápisu (apply)
-----------------------
1. Zmení sa len ``cost`` a ``price`` a len tam, kde je relatívna zmena <= 30 % (väčšie sa len nahlásia).
2. Trojcestné zlučovanie: zapíše sa len tam, kde sa cieľ ešte rovná POSLEDNÉMU stavu katalógu
   (``raynet_raw_products`` pred týmto behom). Ručne kuratované hodnoty (F0: ceny z ponúk 10/2026, LUNA241 40 000 €,
   Sungrow V21 ...) by sa inak prepísali staršími cenami z katalógu. Rozdiely sa nahlásia (``diverged``).
3. Nová hodnota musí byť > 0 (nulový nákup v Raynete = "nezadané"), výsledná cena musí byť > nákup,
   jednotky (ks / kWp / kpl ...) sa musia zhodovať, neplatný produkt (``validTill`` v minulosti) sa nepíše.
4. Zámky: prvok stacku s ``"sync": false`` a pravidlo s ``[sync:off]`` v ``notes`` sa nikdy nemenia.
   Prvky ``legacy`` sa preskakujú, rovnako neaktívne produkty a produkty s ``b2c_visible`` (B2C ceny sa spravujú zvlášť).
5. Nič sa nemaže. Zápis cieľa je podmienený pôvodnou hodnotou (ak sa medzitým zmenila, preskočí sa).
   Pri prvku stacku sa po zmene nákupu/ceny odstráni zodpovedajúca poznámka (``cost_note`` / ``price_note``) a
   ``source`` sa prepíše na "Raynet sync <dátum> (<cenník>)". ``notes`` pravidiel sa nemení (je kľúčom mapovania).
6. Záznam behu: JSON odpoveď, Python log a riadok v ``raynet_import_log`` (``entity_types = ['price_sync']``)
   so zoznamom zmien (stará -> nová hodnota; z neho sa dá zmena ručne vrátiť).

Cenník, ktorý má prednosť
-------------------------
Raynet API nemá pravidlo prednosti cenníkov — cenník sa vyberá pri tvorbe ponuky a produkt nesie hodnoty primárneho
cenníka (``primaryPriceListItem``, "Výchozí"). V ponukách 10/2026 je cenníková cena rovnaká v LZ-HU aj Výchozí
(104 kódov, jediný rozdiel bol zaokrúhlenie 3034,625 / 3034,63), líši sa len nákup, ktorý obchodník prepisuje na riadku
ponuky. Preto: efektívna hodnota = prvá kladná hodnota v poradí ``RAYNET_PRICELIST_ORDER`` (predvolene
``LZ-HU,Výchozí``), potom hodnoty samotného produktu. Rozdiely medzi cenníkmi sa uvádzajú v ``conflicts``.
Pri apply sa beh zastaví, ak sa prvý preferovaný cenník v Raynete nenájde (aby sa nepoužilo zlé poradie).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import requests

log = logging.getLogger("raynet_price_sync")

RAYNET_BASE = "https://app.raynet.cz/api/v2"
ROUTE = "/cron/raynet-price-sync"

MAX_CHANGE_PCT = Decimal("30")          # horný limit relatívnej zmeny pri apply
TOL = Decimal("0.005")                   # tolerancia porovnania cien (polovica centu)
PAGE_SIZE = 1000                         # Raynet: maximálna veľkosť stránky
MAX_PAGES = 60                           # poistka proti nekonečnému stránkovaniu
MAX_API_CALLS = 80                       # poistka proti zbehnutiu denného limitu (24 000)
RUN_BUDGET_S = 100.0                     # gunicorn timeout je 120 s
DEFAULT_PRICELISTS = ("LZ-HU", "Výchozí")
DEFAULT_MAX_CHANGES = 120
ALL_TARGETS = ("rules", "stacks", "products")
REPORT_CAP = 300                         # max položiek v jednotlivých zoznamoch odpovede

STACK_LIST_COLS = ("preferred_panels", "inverters", "optimizers", "batteries", "wallboxes", "accessories")
STACK_DICT_COLS = ("smart_manager", "smart_manager_large", "smart_meter")
MIRROR_KEYS = ("price_per_unit", "price_per_panel")   # zrkadlá `price` pre staré jadro (F0-diff §4)

TARGET_ALIASES = {
    "rules": "rules", "b2b_calc_rules": "rules", "pravidla": "rules",
    "stacks": "stacks", "b2b_vendor_stacks": "stacks", "stacky": "stacks",
    "products": "products", "produkty": "products",
}
TABLE_OF_NAME = {"rules": "b2b_calc_rules", "stacks": "b2b_vendor_stacks", "products": "products"}
COLUMNS = {
    "rules": {"cost": "cost_per_unit", "price": "price_per_unit"},
    "products": {"cost": "purchase_price", "price": "sale_price"},
}

ENV_NAMES = {
    "instance": ("RAYNET_INSTANCE",),
    "user": ("RAYNET_USER", "RAYNET_USERNAME"),
    "key": ("RAYNET_API_KEY", "RAYNET_KEY"),
}

UNIT_SYNONYMS = {"kus": "ks", "kusov": "ks", "komplet": "kpl", "kmpl": "kpl"}

# ID cenníkov v ponukách 10/2026 (PON-26-1404: LZ-HU = 10, Výchozí = 1). Len na kontrolu výberu — pri inom ID príde varovanie.
EXPECTED_PRICELIST_IDS = {"lz-hu": 10, "vychozi": 1}

REASON_TEXT = {
    "over_limit": "zmena presahuje limit 30 % — len nahlásené, nezapísané",
    "diverged": "hodnota v cieli sa líši od posledného stavu katalógu (ručne upravená / kuratovaná) — nezapísané; po kontrole použi align=1",
    "no_base": "pre kód nie je predošlý stav katalógu (nový produkt) — zapíše sa len s align=1",
    "missing_current": "v cieli chýba pôvodná hodnota (doplnenie) — zapíše sa len s align=1",
    "price_not_above_cost": "výsledná cena by nebola vyššia než nákup — riadok sa nemení",
    "unit_mismatch": "jednotka v cieli sa nezhoduje s jednotkou v Raynete — ceny nie sú porovnateľné",
    "product_invalid": "produkt je v Raynete neplatný — nezapísané",
    "ambiguous_code": "kód nie je jednoznačný (viac produktov v Raynete) — nezapísané",
    "changed_meanwhile": "cieľ sa medzitým zmenil — preskočené, opraví ďalší beh",
    "target_not_found": "cieľový prvok sa pri zápise nenašiel — preskočené",
}

_RUN_LOCK = threading.Lock()
_SB_CACHE: dict = {}


# ---------------------------------------------------------------------------------------------------------------
# Výnimky
# ---------------------------------------------------------------------------------------------------------------
class ConfigError(Exception):
    """Chýba konfigurácia (env). `missing` = názvy premenných, ktoré treba nastaviť (nikdy hodnoty)."""

    def __init__(self, missing, message):
        super().__init__(message)
        self.missing = list(missing)


class RaynetError(Exception):
    """Zlyhanie komunikácie s Raynetom; správa neobsahuje tajomstvá."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AbortRun(Exception):
    """Riadené zastavenie behu (poistka); nesie HTTP kód a čiastočný report."""

    def __init__(self, status, message, report=None):
        super().__init__(message)
        self.status = status
        self.report = report or {}


# ---------------------------------------------------------------------------------------------------------------
# Čísla, texty
# ---------------------------------------------------------------------------------------------------------------
def to_dec(v):
    """Číslo/reťazec -> Decimal; None pre prázdne, nečíselné a nekonečné hodnoty."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def money(v):
    """Zaokrúhli na centy "half up" (3034,625 -> 3034,63, nie banker's rounding)."""
    d = to_dec(v)
    return None if d is None else d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def positive(v):
    """Kladná hodnota v centoch alebo None (0 / záporné / chýba = "nezadané")."""
    d = money(v)
    return d if d is not None and d > 0 else None


def same(a, b):
    return a is not None and b is not None and abs(a - b) <= TOL


def same_or_both_none(a, b):
    return (a is None and b is None) or same(a, b)


def num_json(d):
    """Decimal -> JSON číslo (celé ako int, inak float), aby uložený JSON ostal čitateľný (40000, nie 40000.0)."""
    if d is None:
        return None
    return int(d) if d == d.to_integral_value() else float(d)


def pct_of(old, new):
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old * Decimal(100)


def _pct_json(p):
    return None if p is None else round(float(p), 2)


def norm(s):
    """Porovnávací tvar: bez diakritiky, malé písmená, zlúčené medzery."""
    s = unicodedata.normalize("NFKD", str(s or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def norm_unit(u):
    u = norm(u).replace(".", "").replace(" ", "")
    return UNIT_SYNONYMS.get(u, u)


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "ano", "áno")


_CODE_RE = re.compile(r"k[óo]d\s+([^\s;,()]+)", re.IGNORECASE)


def code_from_notes(notes):
    """'Raynet LZ-HU 10/2026, kód R50; pásmo ...' -> 'R50'."""
    m = _CODE_RE.search(notes or "")
    return m.group(1).strip(".") if m else None


def _json_text(obj):
    """Rovnaký tvar ako doterajší import: {"id": 170, "value": "Komponenty"} ako JSON text (ensure_ascii)."""
    if obj is None or obj == "":
        return None
    return json.dumps(obj) if isinstance(obj, (dict, list)) else str(obj)


def _scrub(text, secrets):
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Bratislava")).date()
    except Exception:  # pragma: no cover - chýba tzdata
        return datetime.now(timezone.utc).date()


def _parse_date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------------------
# Konfigurácia
# ---------------------------------------------------------------------------------------------------------------
def load_config(env):
    """Raynet prístup z env; chýbajúce premenné -> ConfigError (503) s návodom, čo nastaviť."""
    vals, missing = {}, []
    for key, names in ENV_NAMES.items():
        val = ""
        for n in names:
            raw = env.get(n)
            if raw is not None and str(raw).strip():
                val = str(raw).strip()
                break
        vals[key] = val
        if not val:
            missing.append(names[0])
    if missing:
        raise ConfigError(
            missing,
            "Chýba konfigurácia Raynetu: nastav na Render službe energovision-cp-generator premenné "
            + ", ".join(missing)
            + " (akceptuje sa aj RAYNET_USERNAME a RAYNET_KEY). Hodnoty sa nikde nevypisujú.",
        )
    return vals


def _default_sb(env):
    if "client" in _SB_CACHE:
        return _SB_CACHE["client"]
    url = env.get("SUPABASE_URL") or "https://uzwajrpebblafuhrtuwn.supabase.co"
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_SERVICE_KEY")
    if not key:
        raise ConfigError(["SUPABASE_SERVICE_ROLE_KEY"], "Chýba SUPABASE_SERVICE_ROLE_KEY — synchronizácia nemá kam zapisovať.")
    from supabase import create_client

    _SB_CACHE["client"] = create_client(url, key)
    return _SB_CACHE["client"]


def parse_targets(raw):
    if raw is None or str(raw).strip() == "":
        return ALL_TARGETS
    out = []
    for part in re.split(r"[,\s]+", str(raw).strip()):
        if not part:
            continue
        t = TARGET_ALIASES.get(part.lower())
        if t is None:
            raise ValueError(f"neznámy cieľ '{part}' (povolené: rules, stacks, products)")
        if t not in out:
            out.append(t)
    return tuple(out) or ALL_TARGETS


# ---------------------------------------------------------------------------------------------------------------
# Raynet klient (len GET)
# ---------------------------------------------------------------------------------------------------------------
class RaynetClient:
    """Minimálny klient Raynet API v2: HTTP Basic (user + API kľúč) + hlavička X-Instance-Name.
    Zámerne obsahuje iba `get` / `pages` — do Raynetu sa nedá nič zapísať."""

    def __init__(self, cfg, session=None, sleep=time.sleep, base=RAYNET_BASE, timeout=25, budget_s=RUN_BUDGET_S):
        self.user, self.key, self.instance = cfg["user"], cfg["key"], cfg["instance"]
        self.base = base.rstrip("/")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.timeout = timeout
        self.deadline = time.monotonic() + budget_s
        self.calls = 0
        self.rate_remaining = None

    def _secrets(self):
        return (self.key, self.user)

    def get(self, path, params=None):
        url = f"{self.base}/{path.strip('/')}/"
        headers = {"X-Instance-Name": self.instance, "Accept": "application/json"}
        for attempt in range(1, 4):
            if time.monotonic() > self.deadline:
                raise RaynetError("Prekročený časový rozpočet behu pri čítaní Raynetu.")
            if self.calls >= MAX_API_CALLS:
                raise RaynetError("Prekročený počet volaní Raynet API v jednom behu (poistka).")
            try:
                r = self.session.get(url, params=params or {}, headers=headers, auth=(self.user, self.key), timeout=self.timeout)
            except requests.RequestException as e:
                if attempt < 3:
                    self.sleep(1.5 * attempt)
                    continue
                raise RaynetError(f"Raynet nedostupný ({type(e).__name__}).")
            self.calls += 1
            rem = r.headers.get("X-Ratelimit-Remaining") if getattr(r, "headers", None) else None
            if rem is not None:
                self.rate_remaining = to_int(rem)
            code = r.status_code
            if code == 429:
                if attempt < 2:
                    self.sleep(2.0)
                    continue
                raise RaynetError("Raynet: vyčerpaný denný limit API požiadaviek (HTTP 429).", status=429)
            if code in (401, 403):
                raise RaynetError(
                    f"Raynet odmietol prístup (HTTP {code}) — skontroluj RAYNET_USER, RAYNET_API_KEY a RAYNET_INSTANCE.", status=code)
            if 500 <= code < 600 and attempt < 3:
                self.sleep(1.5 * attempt)
                continue
            if not (200 <= code < 300):
                snippet = _scrub((getattr(r, "text", "") or "")[:200], self._secrets())
                raise RaynetError(f"Raynet HTTP {code} na {path}: {snippet}", status=code)
            try:
                data = r.json()
            except ValueError:
                raise RaynetError(f"Raynet vrátil neplatný JSON na {path}.")
            if not isinstance(data, dict):
                raise RaynetError(f"Neočakávaná odpoveď Raynetu na {path}.")
            if data.get("success") is False:
                raise RaynetError(f"Raynet vrátil chybu na {path}: {_scrub(str(data.get('message') or data)[:200], self._secrets())}")
            return data
        raise RaynetError(f"Raynet neodpovedal na {path}.")  # pragma: no cover

    def pages(self, path, page_size=PAGE_SIZE, params=None):
        """Všetky záznamy zoznamu (offset/limit, max 1000 na stranu)."""
        out, offset = [], 0
        for _ in range(MAX_PAGES):
            q = dict(params or {})
            q.update({"offset": offset, "limit": page_size})
            data = self.get(path, q)
            rows = data.get("data")
            if not isinstance(rows, list):
                raise RaynetError(f"Neočakávaný tvar odpovede Raynetu na {path} (chýba 'data').")
            out.extend(rows)
            total = to_int(data.get("totalCount"))
            offset += len(rows)
            if not rows or len(rows) < page_size or (total is not None and offset >= total):
                return out
        raise RaynetError(f"Stránkovanie {path} nekončí (poistka {MAX_PAGES} strán).")


# ---------------------------------------------------------------------------------------------------------------
# Cenníky a katalóg
# ---------------------------------------------------------------------------------------------------------------
def _is_eur(cur):
    if cur is None or cur == "":
        return True
    if isinstance(cur, dict):
        cur = cur.get("value") or cur.get("code") or cur.get("name")
    return norm(cur) in ("eur", "€", "euro", "")


def resolve_pricelists(lists, order):
    """Z `GET /priceList/` vyberie cenníky v poradí preferencie podľa kódu alebo názvu (bez diakritiky).
    Token 'Výchozí' sa pri nenájdení priradí k primárnemu cenníku (`primary: true`). Cenník v inej mene než EUR
    sa vynechá. Vráti (vybrané, varovania); `label` vybraného cenníka je token z `order`."""
    selected, warnings, used = [], [], set()
    for want in order:
        w = norm(want)
        hit = None
        for l in lists:
            if l.get("id") not in used and (norm(l.get("code")) == w or norm(l.get("name")) == w):
                hit = l
                break
        if hit is None:
            pat = re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])")
            for l in lists:
                if l.get("id") not in used and pat.search(norm(l.get("name")) + " " + norm(l.get("code"))):
                    hit = l
                    break
        if hit is None and w in ("vychozi", "vychozi cenik", "default", "primary", "@primary"):
            hit = next((l for l in lists if l.get("id") not in used and l.get("primary")), None)
        if hit is None:
            warnings.append(f"Cenník '{want}' sa v Raynete nenašiel (podľa kódu ani názvu).")
            continue
        used.add(hit.get("id"))
        if not _is_eur(hit.get("currency")):
            warnings.append(f"Cenník '{want}' je v inej mene než EUR — vynechaný.")
            continue
        selected.append({"id": hit.get("id"), "label": str(want), "code": hit.get("code"), "name": hit.get("name"),
                         "primary": bool(hit.get("primary"))})
        exp = EXPECTED_PRICELIST_IDS.get(w)
        if exp is not None and to_int(hit.get("id")) != exp:
            warnings.append(f"Cenník '{want}' má v Raynete id {hit.get('id')}, v ponukách 10/2026 mal id {exp} — over, že ide o správny cenník.")
    return selected, warnings


class Entry:
    """Jeden produkt Raynetu s efektívnou cenou/nákupom a hodnotami z jednotlivých cenníkov."""

    __slots__ = ("raynet_id", "code", "name", "unit", "valid_from", "valid_till", "invalid_reason", "product",
                 "lists", "price", "cost", "price_src", "cost_src")

    def __init__(self, product):
        self.product = product
        self.raynet_id = to_int(product.get("id"))
        self.code = str(product.get("code") or "").strip()
        self.name = product.get("name")
        self.unit = product.get("unit")
        self.valid_from = product.get("validFrom")
        self.valid_till = product.get("validTill")
        self.invalid_reason = None
        self.lists = {}
        self.price = self.cost = None
        self.price_src = self.cost_src = None

    def resolve(self, order_labels):
        for label in order_labels:
            it = self.lists.get(label)
            if not it:
                continue
            if self.price is None and it["price"] is not None:
                self.price, self.price_src = it["price"], label
            if self.cost is None and it["cost"] is not None:
                self.cost, self.cost_src = it["cost"], label
        own_price, own_cost = positive(self.product.get("price")), positive(self.product.get("cost"))
        if self.price is None and own_price is not None:
            self.price, self.price_src = own_price, "produkt"
        if self.cost is None and own_cost is not None:
            self.cost, self.cost_src = own_cost, "produkt"

    def conflicts(self):
        """Rozdiely medzi cenníkmi pre ten istý produkt (informatívne)."""
        out = {}
        for field in ("price", "cost"):
            vals = {lb: it[field] for lb, it in self.lists.items() if it[field] is not None}
            if len(vals) > 1 and any(not same(a, b) for a in vals.values() for b in vals.values()):
                out[field] = {lb: num_json(v) for lb, v in vals.items()}
        return out


class Catalog:
    def __init__(self):
        self.by_id, self.by_code, self.by_ci = {}, {}, {}
        self.ambiguous = set()
        self.order_labels = []
        self.items_ignored = 0

    def lookup(self, code):
        """(entry, dôvod). Presná zhoda kódu, inak jednoznačná zhoda bez ohľadu na veľkosť písmen."""
        if not code:
            return None, None
        code = str(code).strip()
        if code in self.ambiguous:
            return None, "ambiguous_code"
        e = self.by_code.get(code)
        if e is not None:
            return e, None
        cands = self.by_ci.get(code.casefold(), [])
        if len(cands) == 1:
            return cands[0], None
        return None, ("ambiguous_code" if len(cands) > 1 else None)


def build_catalog(products, selected_lists, items_by_label, today=None):
    cat = Catalog()
    cat.order_labels = [l["label"] for l in selected_lists]
    today = today or _today()
    seen = {}
    for p in products:
        e = Entry(p)
        if not e.code or e.raynet_id is None:
            continue
        seen.setdefault(e.code, []).append(e)
    for code, es in seen.items():
        if len(es) > 1:
            cat.ambiguous.add(code)
            continue
        e = es[0]
        cat.by_code[code] = e
        cat.by_id[e.raynet_id] = e
        cat.by_ci.setdefault(code.casefold(), []).append(e)
        vt, vf = _parse_date(e.valid_till), _parse_date(e.valid_from)
        if vt is not None and vt < today:
            e.invalid_reason = f"produkt je v Raynete neplatný od {vt.isoformat()}"
        elif vf is not None and vf > today:
            e.invalid_reason = f"produkt platí až od {vf.isoformat()}"
    for label, items in items_by_label.items():
        for it in items:
            pr = it.get("product") or {}
            e = cat.by_id.get(to_int(pr.get("id")))
            if e is None and pr.get("code"):
                e, _ = cat.lookup(pr.get("code"))
            if e is None:
                cat.items_ignored += 1
                continue
            e.lists[label] = {"price": positive(it.get("price")), "cost": positive(it.get("cost")), "item_id": it.get("id")}
    for e in cat.by_id.values():
        e.resolve(cat.order_labels)
    return cat


def fetch_catalog(client, order, page_size=PAGE_SIZE, strict_first=False):
    """Stiahne cenníky, ich položky a produkty. strict_first: ak sa prvý preferovaný cenník nenájde, chyba."""
    lists = client.pages("priceList", page_size)
    selected, warnings = resolve_pricelists(lists, order)
    if strict_first and (not selected or norm(selected[0]["label"]) != norm(order[0])):
        names = ", ".join(str(l.get("code") or l.get("name")) for l in lists)[:300]
        raise RaynetError(
            f"Preferovaný cenník '{order[0]}' sa v Raynete nenašiel — zápis zastavený, aby sa nepoužilo zlé poradie. "
            f"Nájdené cenníky: {names}.")
    items = {l["label"]: client.pages(f"priceList/{l['id']}/items", page_size) for l in selected}
    products = client.pages("product", page_size)
    return build_catalog(products, selected, items), selected, warnings, len(lists)


# ---------------------------------------------------------------------------------------------------------------
# Plánovanie zmien
# ---------------------------------------------------------------------------------------------------------------
class Planner:
    """Porovná ciele s katalógom a pripraví akcie (apply) a zoznam zablokovaných/informatívnych rozdielov."""

    def __init__(self, catalog, base_by_id, base_by_code, align=False, max_pct=MAX_CHANGE_PCT):
        self.cat, self.base_by_id, self.base_by_code = catalog, base_by_id, base_by_code
        self.align, self.max_pct = align, max_pct
        self.actions = []      # akcie pripravené na zápis
        self.entries = []      # report položiek (apply / blocked)
        self.unmapped = []
        self.stats = {t: Counter() for t in ALL_TARGETS}

    def _base(self, e):
        return self.base_by_id.get(e.raynet_id) or self.base_by_code.get(e.code) or {}

    def _field(self, cur, new, base):
        """(rozhodnutie, kód dôvodu, pct) pre jedno pole: apply | blocked | same | skip."""
        if new is None:
            return "skip", "raynet_missing", None
        if cur is None or cur <= 0:
            return ("apply", None, None) if self.align else ("blocked", "missing_current", None)
        if same(cur, new):
            return "same", None, None
        p = pct_of(cur, new)
        if abs(p) > self.max_pct:
            return "blocked", "over_limit", p
        if not self.align:
            if base is None:
                return "blocked", "no_base", p
            if not same(cur, base):
                return "blocked", "diverged", p
        return "apply", None, p

    def consider(self, kind, ref, entry, cur_cost, cur_price, *, unit=None, check_unit=False, loc=None):
        """Rozhodne o jednom cieli. Vráti True, ak sa pripravila akcia. `loc` = údaje na nájdenie riadku pri zápise."""
        st = self.stats[kind]
        st["mapped"] += 1
        base = self._base(entry)
        res = {"cost": self._field(cur_cost, entry.cost, base.get("cost")),
               "price": self._field(cur_price, entry.price, base.get("price"))}
        decisions = [r[0] for r in res.values()]
        if all(d == "skip" for d in decisions):
            st["no_raynet_value"] += 1
            return False
        if all(d in ("same", "skip") for d in decisions):
            st["in_sync"] += 1
            return False
        row_block = None
        if entry.invalid_reason:
            row_block = "product_invalid"
        elif check_unit and unit and entry.unit and norm_unit(unit) != norm_unit(entry.unit):
            row_block = "unit_mismatch"
        elif "apply" in decisions:
            r_cost = entry.cost if res["cost"][0] == "apply" else cur_cost
            r_price = entry.price if res["price"][0] == "apply" else cur_price
            if r_cost is not None and r_price is not None and r_cost > 0 and r_price <= r_cost:
                row_block = "price_not_above_cost"
        fields = {}
        for field, (dec, why, p) in res.items():
            if dec in ("same", "skip"):
                continue
            if dec == "apply" and row_block:
                dec, why = "blocked", row_block
            cur = cur_cost if field == "cost" else cur_price
            new = entry.cost if field == "cost" else entry.price
            src = entry.cost_src if field == "cost" else entry.price_src
            fields[field] = (dec, why, p, cur, new, src)
        applied = {f: v for f, v in fields.items() if v[0] == "apply"}
        if applied:
            st["to_apply"] += 1
            self.actions.append({"table": TABLE_OF_NAME[kind], "kind": kind, "ref": ref, "code": entry.code, "loc": loc or {},
                                 "updates": {f: v[4] for f, v in applied.items()},
                                 "olds": {f: v[3] for f, v in applied.items()},
                                 "srcs": sorted({v[5] for v in applied.values() if v[5]})})
        else:
            st["blocked_rows"] += 1
        for field, (dec, why, p, cur, new, src) in fields.items():
            self.entries.append({
                "table": TABLE_OF_NAME[kind], "ref": ref, "code": entry.code, "field": field,
                "current": num_json(cur), "raynet": num_json(new), "change_pct": _pct_json(p),
                "decision": "apply" if dec == "apply" else "blocked", "code_reason": why or "ok",
                "reason": REASON_TEXT.get(why, "") if dec != "apply" else "", "source": src,
            })
        return bool(applied)

    def _unmapped(self, kind, ref, code, why):
        self.stats[kind]["unmapped"] += 1
        self.unmapped.append({"table": TABLE_OF_NAME[kind], "ref": ref, "code": code,
                              "reason": REASON_TEXT["ambiguous_code"] if why else "kód sa v Raynete nenašiel"})

    # -- b2b_calc_rules ----------------------------------------------------------------------------------------
    def plan_rules(self, rules):
        st = self.stats["rules"]
        for r in rules:
            if not r.get("active", True):
                st["inactive"] += 1
                continue
            notes = r.get("notes") or ""
            ref = f"{r.get('rule_type')}/{r.get('rule_key')}"
            pid = to_int(r.get("raynet_product_id"))
            if pid is not None:
                entry, why, code = self.cat.by_id.get(pid), None, f"id:{pid}"
            else:
                code = code_from_notes(notes)
                if not code:
                    st["no_code"] += 1
                    continue
                entry, why = self.cat.lookup(code)
            if entry is None:
                self._unmapped("rules", ref, code, why)
                continue
            if "[sync:off]" in notes.lower():
                st["locked"] += 1
                continue
            self.consider("rules", ref, entry, to_dec(r.get("cost_per_unit")), to_dec(r.get("price_per_unit")),
                          unit=r.get("unit"), check_unit=True, loc={"id": r.get("id")})

    # -- b2b_vendor_stacks -------------------------------------------------------------------------------------
    def plan_stacks(self, stacks):
        for s in stacks:
            vendor = s.get("vendor_key")
            for col in STACK_LIST_COLS:
                arr = s.get(col)
                if isinstance(arr, list):
                    for idx, el in enumerate(arr):
                        self._stack_element(s, vendor, col, idx, el)
            for col in STACK_DICT_COLS:
                el = s.get(col)
                if isinstance(el, dict):
                    self._stack_element(s, vendor, col, None, el)

    def _stack_element(self, stack, vendor, col, idx, el):
        st = self.stats["stacks"]
        if not isinstance(el, dict):
            return
        code = str(el.get("code") or (el.get("sku") if col == "preferred_panels" else "") or "").strip()
        if not code:
            st["no_code"] += 1
            return
        ident = el.get("key") or el.get("sku") or (idx if idx is not None else col)
        ref = f"{vendor}.{col}.{ident}"
        if el.get("legacy"):
            st["legacy_skipped"] += 1
            return
        if el.get("sync") is False or str(el.get("sync")).strip().lower() in ("false", "0", "off"):
            st["locked"] += 1
            return
        entry, why = self.cat.lookup(code)
        if entry is None:
            self._unmapped("stacks", ref, code, why)
            return
        self.consider("stacks", ref, entry, to_dec(el.get("cost")), to_dec(el.get("price")),
                      loc={"stack_id": stack.get("id"), "col": col, "key": el.get("key"), "sku": el.get("sku"), "code": code})

    # -- products ----------------------------------------------------------------------------------------------
    def plan_products(self, products):
        st = self.stats["products"]
        for p in products:
            if not p.get("is_active", True):
                st["inactive"] += 1
                continue
            sku = str(p.get("sku") or "").strip()
            if not sku:
                continue
            if p.get("b2c_visible"):       # B2C ceny sa spravujú zvlášť — nesiahame na ne
                st["b2c_skipped"] += 1
                continue
            entry, why = self.cat.lookup(sku)
            if entry is None:
                if why:
                    self._unmapped("products", f"sku:{sku}", sku, why)
                else:
                    st["not_in_raynet"] += 1
                continue
            self.consider("products", f"sku:{sku}", entry, to_dec(p.get("purchase_price")), to_dec(p.get("sale_price")),
                          unit=p.get("unit"), check_unit=True, loc={"id": p.get("id")})


# ---------------------------------------------------------------------------------------------------------------
# Čítanie a zápis Supabase
# ---------------------------------------------------------------------------------------------------------------
def select_all(sb, table, cols="*", order="id", **eq):
    out, start = [], 0
    while True:
        q = sb.table(table).select(cols)
        for k, v in eq.items():
            q = q.eq(k, v)
        data = q.order(order).range(start, start + 999).execute().data or []
        out.extend(data)
        if len(data) < 1000:
            return out
        start += 1000


def _effective_of(row):
    """Posledný známy stav katalógu pre riadok raynet_raw_products: `_sync.effective` z predošlého behu, inak stĺpce."""
    rj = row.get("raw_json")
    eff = (rj.get("_sync") or {}).get("effective") if isinstance(rj, dict) else None
    if isinstance(eff, dict):
        return {"price": to_dec(eff.get("price")), "cost": to_dec(eff.get("cost"))}
    return {"price": positive(row.get("price")), "cost": positive(row.get("cost"))}


def load_base(raw_rows):
    by_id, by_code = {}, {}
    for r in raw_rows:
        b = _effective_of(r)
        if r.get("raynet_id") is not None:
            by_id[to_int(r.get("raynet_id"))] = b
        if r.get("code"):
            by_code[str(r["code"]).strip()] = b
    return by_id, by_code


def raw_row(entry, now_iso):
    p = entry.product
    pli = p.get("primaryPriceListItem") or {}
    cur = (pli.get("priceList") or {}).get("currency") if isinstance(pli, dict) else None
    if isinstance(cur, dict):
        cur = cur.get("value") or cur.get("code")
    payload = dict(p)
    payload["_sync"] = {
        "v": 1, "synced_at": now_iso,
        "effective": {"price": num_json(entry.price), "cost": num_json(entry.cost),
                      "price_src": entry.price_src, "cost_src": entry.cost_src},
        "pricelists": {lb: {"price": num_json(it["price"]), "cost": num_json(it["cost"]), "item_id": it.get("item_id")}
                       for lb, it in entry.lists.items()},
    }
    return {
        "raynet_id": entry.raynet_id, "code": entry.code, "name": p.get("name"),
        "category": _json_text(p.get("category")), "product_line": _json_text(p.get("productLine")),
        "unit": p.get("unit"), "price": p.get("price"), "price_incl_vat": None, "cost": p.get("cost"),
        "currency": cur, "vat_rate": p.get("taxRate"), "manufacturer": None, "type": None, "power_capacity": None,
        "efficiency": None, "warranty": None, "in_catalog": None, "hybrid_inverter": None,
        "raw_json": payload, "fetched_at": now_iso,
    }


def raw_diff(catalog, old_rows):
    """Čo sa v katalógu Raynetu zmenilo oproti poslednému uloženému stavu (informatívne)."""
    old = {to_int(r.get("raynet_id")): r for r in old_rows}
    new_cnt = changed = unchanged = 0
    changes = []
    for e in catalog.by_id.values():
        o = old.get(e.raynet_id)
        if o is None:
            new_cnt += 1
            changes.append({"code": e.code, "change": "nový produkt", "price": num_json(e.price), "cost": num_json(e.cost)})
            continue
        b = _effective_of(o)
        if same_or_both_none(b["price"], e.price) and same_or_both_none(b["cost"], e.cost) and o.get("name") == e.name:
            unchanged += 1
        else:
            changed += 1
            changes.append({"code": e.code, "price": [num_json(b["price"]), num_json(e.price)],
                            "cost": [num_json(b["cost"]), num_json(e.cost)]})
    missing = [r.get("code") for rid, r in old.items() if rid not in catalog.by_id]
    return {"new": new_cnt, "changed": changed, "unchanged": unchanged, "missing_in_raynet": len(missing),
            "missing_codes": missing[:50], "changes": changes[:REPORT_CAP]}


def _cas_update(sb, table, row_id, updates, olds):
    """UPDATE podmienený ID a pôvodnými hodnotami; vráti True, ak sa riadok zmenil."""
    q = sb.table(table).update(updates).eq("id", row_id)
    for col, old in olds.items():
        q = q.is_(col, "null") if old is None else q.eq(col, str(old))
    return bool(q.execute().data)


def _find_element(container, loc):
    """Nájde prvok stacku podľa key / sku / code (zoznam) alebo samotný objekt (smart_*)."""
    if isinstance(container, dict):
        return container
    if not isinstance(container, list):
        return None
    for key_field in ("key", "sku"):
        want = loc.get(key_field)
        if want is not None:
            for el in container:
                if isinstance(el, dict) and el.get(key_field) == want:
                    return el
    if loc.get("code"):
        for el in container:
            if isinstance(el, dict) and str(el.get("code") or "") == str(loc["code"]):
                return el
    return None


def apply_actions(sb, actions, today_iso, now_iso):
    """Zapíše akcie. Vráti (applied, skipped, errors). Nič nemaže."""
    applied, skipped, errors = [], [], []

    def _skip(a, code):
        skipped.append({"table": a["table"], "ref": a["ref"], "code_reason": code, "reason": REASON_TEXT[code]})

    def _err(a, e):
        errors.append({"table": a["table"], "ref": a["ref"], "code_reason": "write_error",
                       "reason": f"zápis zlyhal: {type(e).__name__}: {str(e)[:160]}"})

    for a in actions:      # pravidlá a produkty: jedna podmienená UPDATE na riadok
        if a["kind"] not in ("rules", "products"):
            continue
        cols = COLUMNS[a["kind"]]
        upd = {cols[f]: num_json(v) for f, v in a["updates"].items()}
        olds = {cols[f]: v for f, v in a["olds"].items()}
        if a["kind"] == "rules":
            upd["updated_at"] = now_iso
        try:
            if _cas_update(sb, a["table"], a["loc"]["id"], upd, olds):
                applied.append(a)
            else:
                _skip(a, "changed_meanwhile")
        except Exception as e:  # noqa: BLE001
            _err(a, e)

    by_stack = {}
    for a in actions:      # stacky: čerstvý riadok -> úprava prvkov -> zápis dotknutých stĺpcov
        if a["kind"] == "stacks":
            by_stack.setdefault(a["loc"]["stack_id"], []).append(a)
    for stack_id, acts in by_stack.items():
        try:
            fresh = sb.table("b2b_vendor_stacks").select("*").eq("id", stack_id).execute().data or []
            if not fresh:
                for a in acts:
                    _skip(a, "target_not_found")
                continue
            row, payload, done = fresh[0], {}, []
            for a in acts:
                col = a["loc"]["col"]
                el = _find_element(row.get(col), a["loc"])
                if el is None:
                    _skip(a, "target_not_found")
                    continue
                if any(not same_or_both_none(to_dec(el.get(f)), a["olds"][f]) for f in a["updates"]):
                    _skip(a, "changed_meanwhile")
                    continue
                old_price = to_dec(el.get("price"))
                for f, v in a["updates"].items():
                    el[f] = num_json(v)
                    el.pop(f"{f}_note", None)               # cost_note / price_note už neplatia
                if "price" in a["updates"]:
                    for mk in MIRROR_KEYS:
                        if mk in el and same(to_dec(el.get(mk)), old_price):
                            el[mk] = num_json(a["updates"]["price"])
                el["source"] = f"Raynet sync {today_iso} ({', '.join(a['srcs'])})" if a["srcs"] else f"Raynet sync {today_iso}"
                payload[col] = row[col]
                done.append(a)
            if payload:
                sb.table("b2b_vendor_stacks").update(payload).eq("id", stack_id).execute()
                applied.extend(done)
        except Exception as e:  # noqa: BLE001
            for a in acts:
                _err(a, e)
    return applied, skipped, errors


# ---------------------------------------------------------------------------------------------------------------
# Hlavný beh
# ---------------------------------------------------------------------------------------------------------------
def _cap(items):
    return items[:REPORT_CAP], max(0, len(items) - REPORT_CAP)


def run_sync(sb, client, *, apply=False, align=False, max_changes=DEFAULT_MAX_CHANGES, targets=ALL_TARGETS,
             order=DEFAULT_PRICELISTS, page_size=PAGE_SIZE, now=None):
    """Jeden beh synchronizácie. Pri apply=False nezapisuje NIČ. Vráti report (JSON-kompatibilný dict)."""
    t0 = time.monotonic()
    started = now or _now()
    now_iso = _iso(started)
    catalog, selected, pl_warnings, n_lists = fetch_catalog(client, order, page_size, strict_first=apply)

    raw_rows = select_all(sb, "raynet_raw_products", "raynet_id,code,name,unit,price,cost,raw_json", order="raynet_id")
    base_by_id, base_by_code = load_base(raw_rows)
    planner = Planner(catalog, base_by_id, base_by_code, align=align)
    if "rules" in targets:
        planner.plan_rules(select_all(sb, "b2b_calc_rules", "id,rule_type,rule_key,raynet_product_id,unit,cost_per_unit,price_per_unit,active,notes"))
    if "stacks" in targets:
        planner.plan_stacks(select_all(sb, "b2b_vendor_stacks", "*"))
    if "products" in targets:
        planner.plan_products(select_all(sb, "products", "id,sku,unit,purchase_price,sale_price,is_active,b2c_visible", is_active=True))

    to_apply = [e for e in planner.entries if e["decision"] == "apply"]
    blocked = [e for e in planner.entries if e["decision"] != "apply"]
    conflicts = [{"code": e.code, **c} for e in catalog.by_id.values() if (c := e.conflicts())]
    diff = raw_diff(catalog, raw_rows)

    report = {
        "ok": True, "mode": "apply" if apply else "dry_run", "align": bool(align), "targets": list(targets),
        "started_at": now_iso,
        "raynet": {"price_lists_total": n_lists,
                   "price_lists_used": [{k: l[k] for k in ("label", "id", "code", "name", "primary")} for l in selected],
                   "products": len(catalog.by_id), "ambiguous_codes": sorted(catalog.ambiguous)[:20],
                   "price_list_items_ignored": catalog.items_ignored, "api_calls": client.calls,
                   "rate_limit_remaining": client.rate_remaining, "warnings": pl_warnings},
        "catalog": {"with_price": sum(1 for e in catalog.by_id.values() if e.price is not None),
                    "with_cost": sum(1 for e in catalog.by_id.values() if e.cost is not None),
                    "invalid_products": sum(1 for e in catalog.by_id.values() if e.invalid_reason)},
        "raw_products": {**diff, "written": 0},
        "stats": {TABLE_OF_NAME[t]: dict(planner.stats[t]) for t in targets},
        "planned_rows": len(planner.actions), "planned_fields": len(to_apply),
        "blocked_total": len(blocked), "blocked_by_reason": dict(Counter(e["code_reason"] for e in blocked)),
        "unmapped_total": len(planner.unmapped), "conflicts_total": len(conflicts),
    }
    report["changes"], report["changes_truncated"] = _cap(to_apply)
    report["blocked"], report["blocked_truncated"] = _cap(blocked)
    report["unmapped"], report["unmapped_truncated"] = _cap(planner.unmapped)
    report["conflicts"], report["conflicts_truncated"] = _cap(conflicts)
    report["raw_products"]["changes"] = report["raw_products"]["changes"][:REPORT_CAP]

    if apply:
        if len(planner.actions) > max_changes:
            report.update({"ok": False, "status": "limit_zmien",
                           "message": f"Plán obsahuje {len(planner.actions)} riadkov na zmenu (limit {max_changes}) — nezapísané nič. "
                                      "Skontroluj dry-run a zopakuj s vyšším max_changes."})
            report["finished_at"] = _iso(_now())
            raise AbortRun(409, report["message"], report)
        applied, skipped, errors = apply_actions(sb, planner.actions, started.date().isoformat(), now_iso)
        report["applied"] = len(applied)
        report["applied_fields"] = sum(len(a["updates"]) for a in applied)
        report["skipped"] = skipped[:REPORT_CAP]
        report["errors"] = errors[:REPORT_CAP]
        applied_keys = {(a["table"], a["ref"]) for a in applied}
        for e in report["changes"]:
            e["applied"] = (e["table"], e["ref"]) in applied_keys
        if errors:
            # základ (raynet_raw_products) sa neposúva, aby ďalší beh zlyhané zmeny zopakoval
            report["ok"] = False
            report["raw_products"]["note"] = "chyby pri zápise cieľov — základ sa neposúva, ďalší beh zmeny zopakuje"
        else:
            try:        # báza (raynet_raw_products) sa posúva až po úspešnom zápise cieľov
                rows = [raw_row(e, now_iso) for e in catalog.by_id.values()]
                for i in range(0, len(rows), 200):
                    sb.table("raynet_raw_products").upsert(rows[i:i + 200], on_conflict="raynet_id").execute()
                report["raw_products"]["written"] = len(rows)
            except Exception as e:  # noqa: BLE001
                report["ok"] = False
                report["raw_products"]["error"] = f"zápis raynet_raw_products zlyhal: {type(e).__name__}: {str(e)[:160]}"
    else:
        report["applied"] = 0
        report["would_apply"] = len(planner.actions)

    report["finished_at"] = _iso(_now())
    report["duration_ms"] = int((time.monotonic() - t0) * 1000)
    report["summary"] = _summary(report)
    return report


def _summary(r):
    parts = [f"{'APPLY' if r['mode'] == 'apply' else 'DRY-RUN'}: Raynet {r['raynet']['products']} produktov, cenníky "
             + "/".join(l["label"] for l in r["raynet"]["price_lists_used"])]
    for name, s in r["stats"].items():
        parts.append(f"{name}: {s.get('in_sync', 0)} sedí, {s.get('to_apply', 0)} na zmenu, {s.get('blocked_rows', 0)} zablokovaných")
    parts.append(f"zapísané riadky: {r.get('applied', 0)}" if r["mode"] == "apply" else f"by sa zapísalo riadkov: {r.get('would_apply', 0)}")
    return "; ".join(parts)


def _write_run_log(sb, report, error=None):
    """Riadok v raynet_import_log (existujúca tabuľka importov). Zlyhanie logu beh neruší."""
    try:
        res = sb.table("raynet_import_log").insert({
            "started_at": report.get("started_at"), "finished_at": report.get("finished_at") or _iso(_now()),
            "dry_run": False, "entity_types": ["price_sync"], "result": report, "error": error,
        }).execute()
        return (res.data or [{}])[0].get("id")
    except Exception as e:  # noqa: BLE001
        log.warning("[raynet-price-sync] zápis do raynet_import_log zlyhal: %s", type(e).__name__)
        return None


# ---------------------------------------------------------------------------------------------------------------
# HTTP vrstva (bez Flasku, aby sa dala testovať bez závislostí)
# ---------------------------------------------------------------------------------------------------------------
def handle_request(args, *, env=None, get_sb=None, session=None, sleep=time.sleep, now=None, require_webhook_secret=True):
    """Spracuje parametre a vráti (http_status, body). Nezávisí od Flasku."""
    env = os.environ if env is None else env
    args = args or {}
    apply = truthy(args.get("apply"))
    dry = args.get("dry_run")
    if apply and dry is not None and truthy(dry):
        return 400, {"ok": False, "error": "dry_run=1 a apply=1 sa navzájom vylučujú."}
    if not apply and dry is not None and not truthy(dry):
        return 400, {"ok": False, "error": "Zápis sa zapína výlučne apply=1 (dry_run=0 samo o sebe nestačí)."}
    align = truthy(args.get("align"))
    try:
        targets = parse_targets(args.get("targets") or env.get("RAYNET_SYNC_TARGETS"))
        max_changes = int(args.get("max_changes") or env.get("RAYNET_SYNC_MAX_CHANGES") or DEFAULT_MAX_CHANGES)
        if not 0 < max_changes <= 1000:
            raise ValueError("max_changes musí byť 1–1000")
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    if require_webhook_secret and not str(env.get("WEBHOOK_SECRET") or "").strip():
        return 503, {"ok": False, "error": "WEBHOOK_SECRET nie je nastavený na službe — endpoint vracia nákupné ceny, preto je bez tajomstva zablokovaný."}
    try:
        cfg = load_config(env)
    except ConfigError as e:
        return 503, {"ok": False, "error": str(e), "missing_env": e.missing}
    order = tuple(x.strip() for x in str(env.get("RAYNET_PRICELIST_ORDER") or "").split(",") if x.strip()) or DEFAULT_PRICELISTS
    if not _RUN_LOCK.acquire(blocking=False):
        return 409, {"ok": False, "error": "Synchronizácia už beží."}
    try:
        try:
            sb = get_sb() if get_sb else _default_sb(env)
        except ConfigError as e:
            return 503, {"ok": False, "error": str(e), "missing_env": e.missing}
        client = RaynetClient(cfg, session=session, sleep=sleep)
        try:
            report = run_sync(sb, client, apply=apply, align=align, max_changes=max_changes, targets=targets, order=order,
                              page_size=PAGE_SIZE, now=now)
        except AbortRun as e:
            e.report["log_id"] = _write_run_log(sb, e.report, error=str(e)) if apply else None
            log.warning("[raynet-price-sync] zastavené: %s", e)
            return e.status, e.report
        except RaynetError as e:
            msg = _scrub(str(e), (cfg["key"], cfg["user"]))
            log.warning("[raynet-price-sync] Raynet chyba: %s", msg)
            return (429 if e.status == 429 else 502), {"ok": False, "error": msg}
        except Exception as e:  # noqa: BLE001
            msg = _scrub(f"{type(e).__name__}: {e}", (cfg["key"], cfg["user"]))
            log.exception("[raynet-price-sync] neočakávaná chyba")
            return 500, {"ok": False, "error": msg[:400]}
        if apply:
            report["log_id"] = _write_run_log(sb, report)
        log.info("[raynet-price-sync] %s", report["summary"])
        return (200 if report["ok"] else 500), report
    finally:
        _RUN_LOCK.release()


def register(app, require_secret, get_sb=None):
    """Zaregistruje POST /cron/raynet-price-sync. Volanie z app.py: `raynet_price_sync.register(app, require_secret)`."""
    from flask import jsonify, request

    def raynet_price_sync_view():
        args = request.args.to_dict()
        if request.form:
            args.update(request.form.to_dict())
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            args.update(body)
        status, payload = handle_request(args, get_sb=get_sb)
        resp = jsonify(payload)
        resp.status_code = status
        resp.headers["Cache-Control"] = "no-store"      # odpoveď obsahuje nákupné ceny
        return resp

    app.add_url_rule(ROUTE, endpoint="raynet_price_sync", view_func=require_secret(raynet_price_sync_view), methods=["POST"])
    return app


def main(argv=None):
    """Ručné spustenie: python raynet_price_sync.py [--apply] [--align] [--targets rules,stacks] [--max-changes N]."""
    import argparse

    ap = argparse.ArgumentParser(description="Synchronizácia cenníka Raynet -> B2B kalkulačka (predvolene dry-run)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--align", action="store_true")
    ap.add_argument("--targets")
    ap.add_argument("--max-changes", type=int)
    ns = ap.parse_args(argv)
    args = {"apply": "1" if ns.apply else None, "align": "1" if ns.align else None, "targets": ns.targets, "max_changes": ns.max_changes}
    status, body = handle_request({k: v for k, v in args.items() if v is not None}, require_webhook_secret=False)
    print(json.dumps(body, ensure_ascii=False, indent=2, default=str))
    return 0 if status == 200 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
