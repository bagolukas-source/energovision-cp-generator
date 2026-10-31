from generate_from_notion import lead_from_notion


def _lead(orientacia):
    return lead_from_notion({"Zákazník": "Ján Testovací", "Mesto": "Senec", "Spotreba": 5000, "Počet panelov": 12, "Orientácia": orientacia}, "A")


def test_orientacia_ide_z_crm():
    assert _lead("JV")["orientacia"] == "JV"
    assert _lead("juh")["orientacia"] == "J"


def test_neznama_orientacia_nie_je_juzna():
    assert _lead("")["orientacia"] == ""
