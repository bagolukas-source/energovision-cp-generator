"""Report financovania (financovanie_report): SK výstup sa jazykovými mutáciami nesmie zmeniť.
Zlaté súbory: python tests/test_aom_i18n_financovanie.py --capture (len pred refaktorom / pri vedomej zmene)."""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
try:
    import weasyprint  # noqa: F401
except Exception:
    _w = types.ModuleType("weasyprint"); _w.HTML = _w.CSS = object; sys.modules["weasyprint"] = _w

from aom_i18n_fixtures import normalize_html  # noqa: E402

G = ROOT / "tests" / "golden" / "sk"


def _eur(v):
    return f"{v:,.0f}".replace(",", " ") + " €"


def _variant(key, label, color, init, gross, payment, irr, npv, payback, events, text, ppa_note=None,
             waiting=0):
    rows, cum = [], float(init)
    for y in range(1, 21):
        g = 0.0 if y <= waiting else round(gross(y), 2)
        p = round(payment(y), 2)
        net = round(g - p, 2)
        cum = round(cum + net, 2)
        rows.append({"y": y, "gross": g, "payment": p, "net": net, "cum": cum,
                     "is_waiting": y <= waiting, "event_label": events.get(y)})
    has_pay = any(r["payment"] > 0 for r in rows)
    return {"key": key, "label": label, "color": color, "irr_pct": irr, "npv": npv, "payback": payback,
            "initial_investment": abs(init), "init_inv": init, "has_payment_col": has_pay,
            "total_cum_25": cum, "total_gross": round(sum(r["gross"] for r in rows), 2),
            "total_payment": round(sum(r["payment"] for r in rows), 2), "cf_breakdown": rows,
            "description": text["description"], "items": text["items"], "pros": text["pros"],
            "cons": text["cons"], "ppa_note": ppa_note}


_SK = {
    "vl": {"description": "Vlastná kúpa z firemných zdrojov — plná investícia dnes, žiadny dlh.",
           "items": ["Vstupná investícia: 120 000 €", "Dlh: 0 €", "FVE vaša: okamžite"],
           "pros": ["Najvyššia NPV", "Žiadny dlh, žiadne záväzky"], "cons": ["Vyžaduje 120 000 € voľného kapitálu"]},
    "sih": {"description": "Zelený úver SIH so štátnou zárukou — 0 € vstup, splátka z úspor.",
            "items": ["Úroková sadzba: 4,9 %", "Splatnosť: 8 rokov", "Akontácia: 0 €"],
            "pros": ["Vlastníctvo od začiatku", "Kladný tok od prvého mesiaca"], "cons": ["Schvaľovanie banky"]},
    "ppa10": {"description": "PPA s ENERY na 10 rokov, potom bezplatný prevod FVE.",
              "items": ["PPA sadzba: 0,145 €/kWh", "Doba: 10 rokov"],
              "pros": ["Žiadny vklad"], "cons": ["Bez vlastníctva počas zmluvy"]},
    "dot": {"description": "Dotácia Zelená podnikom — čakanie na schválenie 2 roky.",
            "items": ["Dotácia: 45 %", "Vlastný vklad: 66 000 €"],
            "pros": ["Najnižšia celková cena"], "cons": ["Čakanie 2 roky", "Administratíva"]},
}

# EN/DE: CRM posiela texty variantov už v cieľovom jazyku → neutrálne ASCII, aby test videl len šablónu/generátor
_NEUTRAL = {"description": "Variant description.", "items": ["Item one: 1", "Item two"],
            "pros": ["Pro one"], "cons": ["Con one"]}


def _decay(base):
    return lambda y: base * (0.995 ** (y - 1))


def _variants(neutral, keys):
    t = (lambda k: _NEUTRAL) if neutral else (lambda k: _SK[k])
    lab = (lambda k, sk: f"Variant {k.upper()}") if neutral else (lambda k, sk: sk)
    ev = (lambda sk: "Event") if neutral else (lambda sk: sk)
    note = (lambda sk: "Note.") if neutral else (lambda sk: sk)
    out = {
        "vl": _variant("vl", lab("vl", "Vlastná kúpa"), "#3b82f6", -120000.0, _decay(18000.0), lambda y: 0.0,
                       14.2, 98500.0, 6.3, {}, t("vl")),
        "sih": _variant("sih", lab("sih", "SIH zelený úver"), "#10b981", 0.0, _decay(18000.0),
                        lambda y: 15500.0 if y <= 8 else 0.0, None, 71000.0, 0,
                        {9: ev("↑ Splátky splatené (+15 500 €/rok)")}, t("sih")),
        "ppa10": _variant("ppa10", lab("ppa10", "PPA 10 rokov (ENERY)"), "#6366f1", 0.0,
                          lambda y: 3000.0 if y <= 10 else _decay(18000.0)(y), lambda y: 0.0, None, 45000.0, None,
                          {11: ev("↑ Prevod FVE — plný výnos od roku 11")}, t("ppa10"),
                          ppa_note=note("* Roky 1–10: prínos = rozdiel medzi trhovou cenou a PPA sadzbou.")),
        "dot": _variant("dot", lab("dot", "Dotácia Zelená podnikom"), "#8b5cf6", -66000.0, _decay(18000.0),
                        lambda y: 0.0, 9.8, 82000.0, 5.9, {3: ev("↑ Spustenie FVE")}, t("dot"),
                        ppa_note=note("Roky 1–2: čakanie na schválenie dotácie."), waiting=2),
    }
    return [out[k] for k in keys]


def _cf_data(variants):
    rows = []
    for y in range(0, 21):
        r = {"year": y}
        for v in variants:
            r[v["key"]] = v["init_inv"] if y == 0 else next(x["net"] for x in v["cf_breakdown"] if x["y"] == y)
        rows.append(r)
    return rows


def fixture_payload(neutral=False):
    """Plný report: vlastná kúpa (víťaz NPV), SIH úver (odporúčanie), PPA 10; FVE + BESS."""
    vs = _variants(neutral, ["vl", "sih", "ppa10"])
    return {"project_name": "Testovací závod", "client_name": "Testovacia firma s.r.o.",
            "site_address": "Priemyselna 1, Testovo", "report_date": "25. 10. 2026", "capex": 120000.0,
            "pv_kwp": 119.84, "annual_prod_mwh": 124.0, "scenario": "Scenario A" if neutral else "Realistický",
            "variants": vs, "cf_data": _cf_data(vs), "best_key": "vl", "best_label": vs[0]["label"],
            "best_irr": 14.2, "best_npv": 98500.0, "best_payback": 6.3, "tarif_buy": 0.1811, "tarif_sell": 0.06,
            "samosp_pct": 81, "ai_text": "AI paragraph one.\n\nAI paragraph two.", "bess_kwh": 112.5, "bess_kw": 56.0}


def fixture_payload_min(neutral=False):
    """Bez scenára (predvolený), bez BESS a tarify; dotácia (čakanie) vyhráva NPV, SIH odporúčaný."""
    vs = _variants(neutral, ["sih", "dot"])
    return {"client_name": "Testovacia firma s.r.o.", "report_date": "25. 10. 2026", "capex": 120000.0,
            "pv_kwp": 99.5, "variants": vs, "cf_data": _cf_data(vs), "ai_text": ""}


def render(payload):
    from financovanie_report.generator import render_financovanie_html
    return normalize_html(render_financovanie_html(payload))


GOLDEN = {"financovanie.html": fixture_payload, "financovanie_min.html": fixture_payload_min}


def test_financovanie_sk_golden():
    for name, f in GOLDEN.items():
        a, b = render(f()), render(f())
        assert a == b, f"{name}: nedeterministický výstup"
        assert (G / name).read_text(encoding="utf-8") == a, f"SK regresia: {name}"


if __name__ == "__main__" and "--capture" in sys.argv:
    for name, f in GOLDEN.items():
        (G / name).write_text(render(f()), encoding="utf-8")
        print("zapísané", G / name)
