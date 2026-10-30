"""Pravidlá zápisu Notion zrkadla: vyplnené pole CRM sa neprepíše, rozdiel ide do udalosti."""
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


def test_nula_v_crm_je_vyplnena_hodnota():
    zmeny, rozdiely = nz.porovnaj({"suma": 500.0}, {"contract_value_no_vat": 0.0})
    assert not zmeny and rozdiely[0]["pole"] == "suma"


def test_prazdne_polia_sa_doplnia_a_nevznikne_rozdiel():
    plan, _ = nz.naplanuj([stranka()], [projekt(obchodnik="  ")], set())
    zmeny = plan["zmeny"][0][1]
    assert zmeny["contract_value_no_vat"] == 1000.0 and zmeny["contract_signed_at"] == "2026-03-02"
    assert zmeny["expected_end_date"] == "2026-06-30" and zmeny["obchodnik"] == "Bago"
    assert zmeny["fs_termin"].startswith("2026-05-22T09:30")
    assert not plan["udalosti"]


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
    zmeny, _ = nz.porovnaj(nz.hodnoty_stranky(pg), {"todo_notion": "ostáva", "stav_celkovy_notion": "V realizácii"})
    assert "todo_notion" not in zmeny


def test_computed_phase_sa_nikdy_nezapise():
    plan, _ = nz.naplanuj([stranka()], [projekt(computed_phase="bez_zod", bez_automatiky=True)], set())
    assert not {"computed_phase", "bez_automatiky"} & set(plan["zmeny"][0][1])


def test_rovnaky_den_v_inej_zone_nie_je_rozdiel():
    p = {"contract_signed_at": "2026-03-01T23:00:00+00:00", "fs_termin": "2026-05-22T00:00:00+00:00"}
    _, rozdiely = nz.porovnaj({"zod": "2026-03-02", "fs": "2026-05-22"}, p)
    assert not rozdiely


def test_obchodnik_v_multi_selecte_nie_je_rozdiel():
    assert not nz.porovnaj({"obchodnik": "Bago, Maronová"}, {"obchodnik": "maronová"})[1]


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


def test_riadok_bez_projektu_nic_nezaklada():
    plan, rep = nz.naplanuj([stranka(pid="cc")], [projekt()], set())
    assert not plan["zmeny"] and rep["bez_projektu"][0]["notion_page_id"] == "cc"


def test_dve_projekty_na_jednej_stranke_su_zla_vazba():
    plan, rep = nz.naplanuj([stranka()], [projekt(), {**projekt(), "id": "p2"}], set())
    assert not plan["zmeny"] and len(rep["zla_vazba"]) == 2


def test_stranka_ktora_v_notione_neexistuje_ide_do_reportu():
    _, rep = nz.naplanuj([], [projekt()], set())
    assert rep["zla_vazba"][0]["notion"] is None


def test_parovanie_nepoznaje_kod_v_nazve():
    plan, rep = nz.naplanuj([stranka(pid="cc")], [{"id": "p1", "name": "P-25-1 Firma s.r.o.", "notion_page_id": None}], set())
    assert not plan["ok_ids"] and rep["bez_projektu"]


def test_vazba_voci_realnym_nazvom():
    assert nz.vazba_ok("P-25-747 TAURIS, a.s. Mojmírovces", "P-25-747 TAURIS, a.s. Mojmírovce")
    assert nz.vazba_ok("P-26-318 Poľnohospodárske družstvo \"Radošinka”", "FVE Radošinka 200kW")
    assert nz.vazba_ok("P-25-985 CASTE SK a. s. (1. etapa)", "P-25-985 CASTE SK a. s.")
    assert not nz.vazba_ok("P-25-1 Firma", "P-25-2 Firma")
