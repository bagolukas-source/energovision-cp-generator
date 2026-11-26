"""Text nadvýroby v cenovej ponuke: nad +100 % ako násobok, nie „o 993 % viac"."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_cp_html import _nadvyroba_veta  # noqa: E402


def test_do_sto_percent_v_percentach():
    assert _nadvyroba_veta(25) == "Vyrobí o 25 % viac"
    assert _nadvyroba_veta(100) == "Vyrobí o 100 % viac"


def test_nad_sto_percent_ako_nasobok():
    assert _nadvyroba_veta(150) == "Vyrobí približne 2,5× viac"
    assert _nadvyroba_veta(993) == "Vyrobí približne 11× viac"
