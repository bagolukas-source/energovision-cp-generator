# -*- coding: utf-8 -*-
"""Intake orchestrátor — chat-first: roztriedi nahrané súbory, prečistí, uloží ako
pomenované podklady (analyza_om_podklady), extrahuje parametre, vráti AI zhrnutie."""
import os, json, logging
log = logging.getLogger("intake")

CONS_EXT = (".xls", ".xlsx", ".csv", ".tsv")


def classify_file(filename: str) -> tuple:
    """Vráti (kind, label, role). kind: 15min|faktura|opis|pvgis|ine."""
    fn = (filename or "").lower()
    ext = os.path.splitext(fn)[1]
    if any(k in fn for k in ("faktur", "invoice", "vyuctovanie", "vyúčtovanie")):
        return ("faktura", "Faktúra za elektrinu", None)
    if "pvgis" in fn:
        return ("pvgis", "PVGIS report", None)
    if ext in (".txt", ".md") or any(k in fn for k in ("opis", "popis", "zadanie", "poznamk")):
        return ("opis", "Opis projektu", None)
    if ext in CONS_EXT or any(k in fn for k in ("sse", "zsdis", "vsd", "15min", "profil", "odber", "spotreb")):
        # rola
        if any(k in fn for k in ("vykur", "heating", "teplo", "tc")):
            role = "vykurovací"
        elif any(k in fn for k in ("dodav", "dodávk", "vyrob", "výrob", "do_siete", "do siete")):
            role = "dodávka/výroba"
        else:
            role = "hlavný odber"
        return ("15min", f"15-min spotreba ({role})", role)
    if ext == ".pdf":
        return ("faktura", "Faktúra / dokument (PDF)", None)  # PDF default faktúra (skúsi parse)
    return ("ine", "Iný podklad", None)


def _download(sb, bucket, path):
    return sb.storage.from_(bucket).download(path)


def run_intake(sb, analyza_id: str, files: list, bucket: str = "analyza-om") -> dict:
    """files: [{storage_path, filename}]. Roztriedi, parsuj, ulož podklady, extrahuj parametre."""
    from ingestion.faktura_parser import parse_faktura
    podklady, extracted, warnings = [], {}, []
    cons_paths, cons_files = [], []

    for f in files:
        path = f.get("storage_path"); fn = f.get("filename") or (path or "").split("/")[-1]
        if not path:
            continue
        kind, label, role = classify_file(fn)
        rec = {"analyza_id": analyza_id, "kind": kind, "label": label,
               "original_filename": fn, "source_path": path, "storage_path": path}

        if kind == "15min":
            cons_paths.append(path); cons_files.append((fn, role)); rec["extracted"] = {"role": role}
        elif kind == "faktura":
            try:
                raw = _download(sb, bucket, path)
                fak = parse_faktura(raw, fn) or {}
                rec["extracted"] = fak
                # extrahuj parametre do analyza_om
                for k in ("tarif_silova_eur_mwh", "tarif_distribucia_eur_mwh", "tarif_tps_eur_mwh",
                          "tarif_oze_eur_mwh", "tarif_ostatne_eur_mwh", "tarif_fix_mes_eur"):
                    if fak.get(k) is not None:
                        extracted[k] = fak[k]
                for fk in ("om_mrk_kw", "om_rk_kw", "om_sadzba", "eic_om", "cislo_om"):
                    if fak.get(fk) is not None: extracted[fk] = fak[fk]
                if any(fak.get(k) for k in ("tarif_silova_eur_mwh",)):
                    extracted["tarif_source"] = "faktúra"
                rec["label"] = "Faktúra za elektrinu" + (f" ({fak.get('obdobie')})" if fak.get("obdobie") else "")
            except Exception as e:
                warnings.append(f"Faktúra '{fn}' sa nepodarilo prečítať: {str(e)[:120]}")
                rec["extracted"] = {"error": str(e)[:200]}
        elif kind == "opis":
            try:
                txt = _download(sb, bucket, path).decode("utf-8", "replace")[:8000]
                rec["extracted"] = {"text": txt}
                extracted["_customer_request"] = (extracted.get("_customer_request", "") + "\n" + txt).strip()
            except Exception as e:
                warnings.append(f"Opis '{fn}': {str(e)[:120]}")
        # pvgis / ine — len uložiť referenciu
        podklady.append(rec)

    # consumption — jeden parse cez engine
    cons_summary = {}
    if cons_paths:
        try:
            import analyza_om.engine as _eng
            res = _eng.parse_consumption(analyza_id, cons_paths, None)
            if isinstance(res, dict) and res.get("status") == "ok":
                cons_summary = res.get("summary") or {}
                outs = res.get("outputs") or {}
                extracted.update({
                    "consumption_annual_mwh": cons_summary.get("annual_mwh"),
                    "consumption_peak_kw_15min": cons_summary.get("peak_kw_15min"),
                    "consumption_peak_kw_hourly": cons_summary.get("peak_kw_hourly"),
                    "consumption_avg_kw": cons_summary.get("avg_kw"),
                    "consumption_coverage_pct": cons_summary.get("coverage_pct"),
                    "consumption_profile_path": outs.get("profile_hourly_path") or f"{analyza_id}/consumption_profile.csv",
                    "consumption_15min_path": outs.get("profile_15min_path") or f"{analyza_id}/consumption_15min.csv",
                    "consumption_method": "intake_auto",
                })
                for w in (res.get("warnings") or []):
                    warnings.append(str(w))
                # doplň clean_path do 15min podkladov
                for rec in podklady:
                    if rec["kind"] == "15min":
                        rec["clean_filename"] = "consumption_15min.csv"
                        rec.setdefault("extracted", {})["annual_mwh"] = cons_summary.get("annual_mwh")
            else:
                detail = "; ".join(str(w)[:140] for w in (res or {}).get("warnings", [])[:6])
                warnings.append("Spotreba: " + str((res or {}).get("error",""))[:80] + (" | " + detail if detail else ""))
                # sniff prvých bajtov (HTML maskované ako .xls?)
                try:
                    import analyza_om.engine as _eng2
                    head = _eng2.storage_download(cons_paths[0])[:64]
                    sig = "HTML" if head.lstrip()[:6].lower() in (b"<html", b"<?xml", b"<table", b"<!doct") or b"<" in head[:8] else ("ZIP/xlsx" if head[:2]==b"PK" else ("BIFF/xls" if head[:4]==b"\xd0\xcf\x11\xe0" else "?"))
                    warnings.append(f"Formát 1. súboru: {sig} (head={head[:24]!r})")
                except Exception as _se:
                    warnings.append(f"sniff zlyhal: {_se}")
        except Exception as e:
            log.exception("intake consumption parse failed")
            warnings.append(f"Spotreba parse zlyhala: {str(e)[:150]}")

    # ulož podklady do DB
    saved = []
    for rec in podklady:
        try:
            r = sb.table("analyza_om_podklady").insert(rec).execute()
            saved.append((r.data or [rec])[0])
        except Exception as e:
            log.warning("podklad insert failed: %s", e)
            saved.append(rec)

    # aplikuj extrahované parametre na analyza_om (whitelist stĺpcov)
    _ALLOWED = {"consumption_annual_mwh","consumption_peak_kw_15min","consumption_peak_kw_hourly",
        "consumption_avg_kw","consumption_coverage_pct","consumption_profile_path","consumption_15min_path",
        "consumption_method","tarif_silova_eur_mwh","tarif_distribucia_eur_mwh","tarif_tps_eur_mwh",
        "tarif_oze_eur_mwh","tarif_ostatne_eur_mwh","tarif_fix_mes_eur","tarif_source","om_mrk_kw","om_rk_kw","om_sadzba"}
    upd = {k: v for k, v in extracted.items() if k in _ALLOWED and v is not None}
    if upd:
        try:
            upd["updated_at"] = "now()"
            sb.table("analyza_om").update(upd).eq("id", analyza_id).execute()
        except Exception as e:
            warnings.append(f"Uloženie parametrov: {str(e)[:120]}")

    summary_text = _ai_intake_summary(extracted, saved, warnings)
    return {"ok": True, "podklady": saved, "extracted": extracted,
            "warnings": warnings, "summary": summary_text}


def _ai_intake_summary(extracted: dict, podklady: list, warnings: list) -> str:
    """Krátke grounded zhrnutie 'čo som našiel'."""
    facts = {
        "podklady": [{"kind": p.get("kind"), "label": p.get("label"), "subor": p.get("original_filename")} for p in podklady],
        "najdene_parametre": {k: v for k, v in extracted.items() if not k.startswith("_")},
        "opis_od_klienta": (extracted.get("_customer_request") or "")[:1500],
        "upozornenia": warnings,
    }
    try:
        from anthropic import Anthropic
        sysp = (
            "Si AI Poradca Energovision. Práve ti používateľ hodil podklady k analýze odberného miesta. "
            "Napíš KRÁTKE (3-6 viet) zhrnutie po slovensky: čo si roztriedil, aké kľúčové parametre si vytiahol "
            "(spotreba, MRK, tarifa, profil) a aký je ďalší krok. Vykáš. POUŽI LEN čísla z JSON, nič nevymýšľaj. "
            "Ak chýba dôležité (napr. faktúra alebo spotreba), jemne to spomeň. Na konci navrhni: „Spustím analýzu?\""
        )
        msg = Anthropic().messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=700, temperature=0.3, system=sysp,
            messages=[{"role": "user", "content": "Podklady (JSON):\n" + json.dumps(facts, ensure_ascii=False)}])
        return msg.content[0].text.strip()
    except Exception as e:
        log.warning("intake AI summary failed: %s", e)
        kinds = ", ".join(sorted({p.get("label","") for p in podklady}))
        amwh = extracted.get("consumption_annual_mwh")
        bits = [f"Roztriedil som podklady: {kinds}."]
        if amwh: bits.append(f"Ročná spotreba {amwh} MWh.")
        if extracted.get("om_mrk_kw"): bits.append(f"MRK {extracted['om_mrk_kw']} kW.")
        if extracted.get("tarif_source") == "faktúra": bits.append("Tarifa načítaná z faktúry.")
        if warnings: bits.append("Upozornenia: " + "; ".join(warnings[:2]) + ".")
        bits.append("Spustím analýzu?")
        return " ".join(bits)
