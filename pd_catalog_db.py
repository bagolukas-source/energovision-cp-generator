"""
Loader PD katalógu komponentov z dedikovanej Supabase DB (pd_panely, pd_striedace).
Toto je samostatná, detailná databáza pre projektovú dokumentáciu (oddelená od products).
Pri nedostupnosti DB padá späť na statický pd_catalog.py (commitnutý snapshot).

get_catalog() -> (PANELY, STRIEDACE, ALIAS_PANEL, ALIAS_STRIEDAC)
  PANELY/STRIEDACE: dict {model: specs_dict} v tvare, ktorý očakáva generuj_pd._build_ctx
  ALIAS_*: dict {alias_nazov: model} poskladaný z DB stĺpca aliases
"""
import os
import json
import logging
import urllib.request

log = logging.getLogger("pd_catalog_db")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uzwajrpebblafuhrtuwn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_KEY", "")

_CACHE = None  # (PANELY, STRIEDACE, ALIAS_PANEL, ALIAS_STRIEDAC)


def _fetch(table):
    url = f"{SUPABASE_URL}/rest/v1/{table}?select=model,aliases,specs&is_active=eq.true&limit=1000"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode())


def _load_from_db():
    panely, striedace, ap, as_ = {}, {}, {}, {}
    prows = _fetch("pd_panely")
    srows = _fetch("pd_striedace")
    for row in prows:
        m = row.get("model")
        if not m:
            continue
        panely[m] = row.get("specs") or {}
        for a in (row.get("aliases") or []):
            ap[a] = m
    for row in srows:
        m = row.get("model")
        if not m:
            continue
        striedace[m] = row.get("specs") or {}
        for a in (row.get("aliases") or []):
            as_[a] = m
    if not panely or not striedace:
        raise RuntimeError("prázdny PD katalóg z DB")
    return panely, striedace, ap, as_


def get_catalog(force=False):
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    try:
        if not SUPABASE_KEY:
            raise RuntimeError("chýba SUPABASE_SERVICE_ROLE_KEY")
        _CACHE = _load_from_db()
        log.info("[pd_catalog_db] z DB: %d panelov, %d meničov", len(_CACHE[0]), len(_CACHE[1]))
    except Exception as e:
        from pd_catalog import PANELY as P, STRIEDACE as S
        log.warning("[pd_catalog_db] fallback na statický pd_catalog.py (%s)", e)
        _CACHE = (dict(P), dict(S), {}, {})
    return _CACHE
