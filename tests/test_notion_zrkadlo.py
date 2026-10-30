"""Pravidlá Notion zrkadla: do projects idú len tri stĺpce, polia CRM sa len reportujú."""
import ast
import logging
from pathlib import Path

import notion_zrkadlo as nz


def stranka(pid="aa-bb", nazov="P-25-1 Firma s.r.o.", stav="V realizácii", todo="čakáme na ZoP", suma=1000.0,
            zod="2026-03-02", fs="2026-05-22T09:30:00.000+02:00", termin="2026-06-30", obchodnik=("Bago",)):
    return {"id": pid, "properties": {
        "Zákazka": {"type": "title", "title": [{"plain_text": nazov}]},
        nz.P_STAV: {"type": "status", "status": {"name": stav} if stav else None},
        nz.P_TODO: {"type": "rich_text", "rich_text": [{"plain_text": todo}] if todo else []},
        nz.P_SUMA: {"type": "number", "number": suma},
        nz.P_ZOD: {"type": "date", "date": {"start": zod} if zod else None},
        nz.P_FS: {"type": "date", "date": {"start": fs} if fs else None},
        nz.P_TERMIN: {"type": "date", "date": {"start": termin} if termin else None},
        nz.P_OBCHODNIK: {"type": "multi_select", "multi_select": [{"name": o} for o in obchodnik]},
    }}


def projekt(**kw):
    return {"id": "p1", "name": "P-25-1 Firma s.r.o.", "notion_page_id": "aabb", **kw}


def test_vyplnene_polia_crm_sa_neprepisu_ani_pri_rozdiele():
    p = projekt(contract_value_no_vat=999.0, contract_signed_at="2026-01-01T00:00:00+00:00",
                fs_termin="2026-01-02T00:00:00+00:00", expected_end_date="2026-01-03", obchodnik="Maronová")
    plan, rep = nz.naplanuj([stranka()], [p], set())
    zmeny = plan["zmeny"][0][1]
    assert not set(zmeny) & set(nz.POLIA_CRM.values())
    assert sorted(u["payload"]["pole"] for u in plan["udalosti"]) == ["fs", "obchodnik", "suma", "termin", "zod"]
    assert all(u["verb"] == "notion_rozdiel" and u["entity_id"] == "p1" for u in plan["udalosti"])


def test_zapis_ide_len_s_env_poistkou():
    assert not nz.zapis_povoleny(False, {})  # takto volá cron po merge
    assert not nz.zapis_povoleny(True, {"NOTION_ZRKADLO_ZAPIS": "1"})
    assert nz.zapis_povoleny(False, {"NOTION_ZRKADLO_ZAPIS": "1"})


def test_tajomstvo_prazdne_na_serveri_odmietne():
    assert not nz.tajomstvo_ok("", "") and not nz.tajomstvo_ok(None, "") and not nz.tajomstvo_ok("x", "")
    assert not nz.tajomstvo_ok(None, "s3") and not nz.tajomstvo_ok("zle", "s3")
    assert nz.tajomstvo_ok("s3", "s3")


def _beh_zrkadla(stranky, projekty, env, dry_run):
    """Spustí skutočný _notion_zrkadlo_beh z app.py so sieťou nahradenou záznamom (app.py sa nedá importovať bez flask)."""
    zdroj = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(zdroj).body if isinstance(n, ast.FunctionDef) and n.name == "_notion_zrkadlo_beh")
    zapisy = []

    class Odp:
        ok, text = True, ""

        def __init__(self, data=None):
            self.data = data

        def json(self):
            return self.data

        def raise_for_status(self):
            pass

    class Siet:
        def get(self, url, **_):
            return Odp(projekty if "/projects" in url else [])

        def patch(self, url, json=None, **_):
            zapisy.append(("PATCH", url, json))
            return Odp()

        def post(self, url, json=None, **_):
            zapisy.append(("POST", url, json))
            return Odp()

    class Env:
        environ = env

    g = {"_nz": nz, "os": Env, "requests": Siet(), "SUPABASE_URL": "http://x", "_supa_headers": dict,
         "DASHBOARD_DB_ID": "d", "_notion_query_all": lambda *_: stranky, "log": logging.getLogger("t")}
    exec(compile(ast.Module([fn], []), "app.py", "exec"), g)
    return g["_notion_zrkadlo_beh"]([], 10, dry_run), zapisy


def test_patch_obsahuje_len_tri_povolene_stlpce():
    p = projekt(stav_celkovy_notion="Obchod", contract_value_no_vat=None, contract_signed_at=None, obchodnik="Maronová")
    rep, zapisy = _beh_zrkadla([stranka()], [p], {"NOTION_ZRKADLO_ZAPIS": "1"}, False)
    patche = [z for z in zapisy if z[0] == "PATCH"]
    assert len(patche) == 2 and rep["chyby_zapisu"] == 0
    assert set().union(*(z[2] for z in patche)) == nz.ZAPIS_STLPCE
    assert [z[1].rsplit("/", 1)[1].split("?")[0] for z in zapisy if z[0] == "POST"] == ["events"]


def test_cron_bez_env_nezapise_a_report_ostane_cely():
    p = projekt(contract_value_no_vat=999.0, computed_phase="dokoncene", stav_celkovy_notion="Obchod")
    rep, zapisy = _beh_zrkadla([stranka(), stranka(pid="cc")], [p], {}, False)
    assert not zapisy
    assert rep["rozdiely"] and rep["doplnenia"] and rep["bez_projektu"] and rep["rozpor_stavu"]


def test_polia_crm_sa_nezapisuju_ani_prazdne():
    plan, rep = nz.naplanuj([stranka()], [projekt(obchodnik="  ")], set())
    assert set(plan["zmeny"][0][1]) <= nz.ZAPIS_STLPCE
    assert not plan["udalosti"]
    assert {d["stlpec"] for d in rep["doplnenia"]} == set(nz.POLIA_CRM.values())


def test_vyplnene_polia_crm_sa_neprepisu_rozdiel_ide_do_udalosti():
    p = projekt(contract_value_no_vat=999.0, contract_signed_at="2026-01-01T00:00:00+00:00",
                fs_termin="2026-01-02T00:00:00+00:00", expected_end_date="2026-01-03", obchodnik="Maronová")
    plan, rep = nz.naplanuj([stranka()], [p], set())
    assert not set(plan["zmeny"][0][1]) & set(nz.POLIA_CRM.values())
    assert sorted(u["payload"]["pole"] for u in plan["udalosti"]) == ["fs", "obchodnik", "suma", "termin", "zod"]
    assert all(u["verb"] == "notion_rozdiel" and u["entity_id"] == "p1" for u in plan["udalosti"])
    assert not rep["doplnenia"]


def test_nula_v_crm_je_vyplnena_hodnota():
    zmeny, doplnenia, rozdiely = nz.porovnaj({"suma": 500.0}, {"contract_value_no_vat": 0.0})
    assert not zmeny and not doplnenia and rozdiely[0]["pole"] == "suma"


def test_zrkadlo_sa_prepisuje_vzdy_aj_vycistenim_todo():
    p = projekt(stav_celkovy_notion="Obchod", todo_notion="staré")
    plan, _ = nz.naplanuj([stranka(todo=None)], [p], set())
    zmeny = plan["zmeny"][0][1]
    assert zmeny["stav_celkovy_notion"] == "V realizácii" and zmeny["todo_notion"] is None


def test_prazdny_status_je_predvolena_hodnota_notionu():
    assert nz.hodnoty_stranky(stranka(stav=None))["stav"] == "Administratíva"


def test_chybajuca_property_pole_nezmaze():
    pg = stranka()
    del pg["properties"][nz.P_TODO]
    zmeny, _, _ = nz.porovnaj(nz.hodnoty_stranky(pg), {"todo_notion": "ostáva", "stav_celkovy_notion": "V realizácii"})
    assert "todo_notion" not in zmeny


def test_computed_phase_sa_nikdy_nezapise():
    plan, _ = nz.naplanuj([stranka()], [projekt(computed_phase="bez_zod")], set())
    assert "computed_phase" not in plan["zmeny"][0][1]


def test_rozpor_stavu_a_fazy_len_v_reporte():
    p = projekt(computed_phase="dokoncene", stav_celkovy_notion="V realizácii")
    plan, rep = nz.naplanuj([stranka(stav="V realizácii")], [p], set())
    assert rep["rozpor_stavu"][0]["stav_notion"] == "V realizácii" and rep["rozpor_stavu"][0]["computed_phase"] == "dokoncene"
    assert "computed_phase" not in plan["zmeny"][0][1]
    assert nz.rozpor_stavu("Porealizačná fáza", "projekcia")
    assert not nz.rozpor_stavu("V realizácii", "projekcia")
    assert not nz.rozpor_stavu("Pozastavené", "dokoncene")
    assert not nz.rozpor_stavu("Administratíva", None)


def test_rovnaky_den_v_inej_zone_nie_je_rozdiel():
    p = {"contract_signed_at": "2026-03-01T23:00:00+00:00", "fs_termin": "2026-05-22T00:00:00+00:00"}
    assert not nz.porovnaj({"zod": "2026-03-02", "fs": "2026-05-22"}, p)[2]


def test_obchodnici_sa_porovnavaju_ako_mnozina():
    assert not nz.porovnaj({"obchodnik": "Bago, Maronová"}, {"obchodnik": "maronová"})[2]
    assert not nz.porovnaj({"obchodnik": "Bago, Maronová"}, {"obchodnik": "Maronová, Bago"})[2]
    assert nz.porovnaj({"obchodnik": "Bago"}, {"obchodnik": "Bago, Maronová"})[2]


def test_rovnaky_rozdiel_druhy_raz_nezapise_udalost():
    p = projekt(contract_value_no_vat=999.0)
    plan, _ = nz.naplanuj([stranka()], [p], set())
    videne = {(u["entity_id"], u["payload"]["pole"], u["payload"]["crm"], u["payload"]["notion"]) for u in plan["udalosti"]}
    druhy, rep = nz.naplanuj([stranka()], [p], videne)
    assert not druhy["udalosti"] and rep["rozdiely_polia"]["suma"] == 1


def test_zla_vazba_krup_nic_nezapise_a_ide_do_reportu():
    pg = stranka(nazov="P-22-250 KRUP s.r.o. - TRAFOSTANICA", suma=64939.4)
    p = projekt(name="P-22-250 KRUP s.r.o. Baliareň", contract_value_no_vat=635128.2)
    plan, rep = nz.naplanuj([pg], [p], set())
    assert not plan["zmeny"] and not plan["udalosti"] and not plan["ok_ids"]
    assert rep["zla_vazba"][0]["notion"].endswith("TRAFOSTANICA")


def test_archivovany_projekt_sa_preskoci_a_nie_je_bez_projektu():
    p = projekt(archived_at="2026-01-01T00:00:00+00:00", stav_celkovy_notion="Obchod")
    plan, rep = nz.naplanuj([stranka()], [p], set())
    assert not plan["zmeny"] and not plan["ok_ids"] and rep["archivovane"] == 1
    assert not rep["bez_projektu"] and not rep["zla_vazba"]
    assert not nz.naplanuj([], [p], set())[1]["zla_vazba"]


def test_riadok_bez_projektu_nic_nezaklada():
    plan, rep = nz.naplanuj([stranka(pid="cc")], [projekt()], set())
    assert not plan["zmeny"] and rep["bez_projektu"][0]["notion_page_id"] == "cc"


def test_dve_projekty_na_jednej_stranke_su_zla_vazba():
    plan, rep = nz.naplanuj([stranka()], [projekt(), {**projekt(), "id": "p2"}], set())
    assert not plan["zmeny"] and len(rep["zla_vazba"]) == 2


def test_stranka_ktora_v_notione_neexistuje_ide_do_reportu():
    _, rep = nz.naplanuj([], [projekt()], set())
    assert rep["zla_vazba"][0]["notion"] is None


def test_parovanie_nepozna_kod_v_nazve():
    plan, rep = nz.naplanuj([stranka(pid="cc")], [{"id": "p1", "name": "P-25-1 Firma s.r.o.", "notion_page_id": None}], set())
    assert not plan["ok_ids"] and rep["bez_projektu"]


def test_vazba_voci_realnym_nazvom():
    assert nz.vazba_ok("P-25-747 TAURIS, a.s. Mojmírovces", "P-25-747 TAURIS, a.s. Mojmírovce")
    assert nz.vazba_ok("P-26-318 Poľnohospodárske družstvo \"Radošinka”", "FVE Radošinka 200kW")
    assert nz.vazba_ok("P-25-985 CASTE SK a. s. (1. etapa)", "P-25-985 CASTE SK a. s.")
    assert nz.vazba_ok("P-24-810 WES-LER Invest s. r. o. 41,76kWp", "P-24-810 WES-LER Invest s. r. o. 41,76 kWp")
    assert not nz.vazba_ok("P-25-1 Firma", "P-25-2 Firma")
    assert not nz.vazba_ok("P-22-250 KRUP s.r.o. - TRAFOSTANICA", "P-22-250 KRUP s.r.o. Baliareň")


def test_vazba_rozlisuje_etapy_om_a_vykon():
    assert not nz.vazba_ok("P-24-1135 Albéa Slovakia, s.r.o. (1. etapa)", "P-24-1135 Albéa Slovakia, s.r.o. (2. etapa)")
    assert not nz.vazba_ok("P-24-1466 MIKON spol. s r.o. OM2", "P-24-1466 MIKON spol. s r.o. OM1")
    assert not nz.vazba_ok("P-23-369 Faveo Healthcare s. r. o. OM1 128,8 kWp", "P-23-369 Faveo Healthcare s. r. o. OM2 128,8 kWp")
    assert not nz.vazba_ok("P-24-810 WES-LER Invest s. r. o. 41,76kWp", "P-24-810 WES-LER Invest s. r. o. 74,82kWp")


def test_nazov_bez_kodu_neprejde_na_vseobecnom_slove():
    assert not nz.vazba_ok("FVE Radošinka 200kW", "FVE Lučenec 200kW")
    assert not nz.vazba_ok("P-26-318 Firma s.r.o. FVE", "FVE 200kW")
    assert not nz.vazba_ok("FVE", "FVE")
