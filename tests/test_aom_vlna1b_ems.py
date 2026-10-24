"""Regresné testy — audit AOM 2026-10-24, vlna 1b: EMS/batéria (A15–A17 = E08–E10).

Každý test pred opravou padal (reprodukcia nálezu), po oprave prechádza.
Spúšťanie: `python3 tests/test_aom_vlna1b_ems.py` alebo `pytest tests/test_aom_vlna1b_ems.py`
(z koreňa repa; ENERGO_SPOT_CSV / ENERGO_TARIFF_YAML sa dopĺňajú automaticky).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "audit_bess"))
os.environ.setdefault("ENERGO_SPOT_CSV", os.path.join(ROOT, "aom_data", "sk_spot_2025_hourly.csv"))
os.environ.setdefault("ENERGO_TARIFF_YAML", os.path.join(ROOT, "aom_data", "tariffs", "2026.yaml"))

import numpy as np  # noqa: E402

from harness import actual_cost, run_ems  # noqa: E402


def _bill_saving(load, iv):
    """Nezávislý rozdiel účtov: účet bez batérie (bez FVE) − reálny účet z intervalov."""
    base = sum(l * i.tarif_buy_eur_kwh for l, i in zip(load, iv))
    return base - actual_cost(iv)


# ---------- A15/E08: nákladový register nesie znamienko (záporná cena nabitia) ----------
_LOAD = [0.0, 10.0, 40.0]
_SPOT = [-100.0, 100.0, 100.0]   # retail h0 = −39,86 €/MWh → nabitie je príjem


def _a15(dyn):
    # pravidlová cesta: test drží presnú sumu z vlny 1b; účtovanie (register so znamienkom) je
    # spoločné pre pravidlá aj LP plán
    return run_ems(_LOAD, [0.0] * 3, _SPOT, 100.0, initial_soc_pct=0.08, use_dynamic_rte=dyn,
                   arb_min_spread_eur_mwh=30, dispatch="rules")


def test_a15_zaporna_cena_nabitia_staticke_rte():
    iv, s, bat, _ = _a15(False)
    assert iv[0].grid_to_bat_kwh > 0 and iv[0].tarif_buy_eur_kwh < 0      # nabíja pri zápornej cene
    streams = s.sav_bess_self_cons_eur + s.sav_arbitrage_eur
    real = _bill_saving(_LOAD, iv)
    # predtým 7,4992 € (max(0,…) zahodil záporný náklad) vs skutočných 9,0393 €
    assert abs(real - 9.0393) < 1e-3, real
    assert abs(streams - real) < 1e-6, (streams, real)


def test_a15_zaporna_cena_nabitia_dynamicke_rte():
    # Vedrá bežia na nominálnom RTE, batéria na dynamickom → časť nabitia ostane v registri ako
    # zásoba. Identita účtov: prúdy − ocenenie zostatku (so znamienkom) = skutočný rozdiel účtov.
    iv, s, bat, _ = _a15(True)
    streams = s.sav_bess_self_cons_eur + s.sav_arbitrage_eur
    real = _bill_saving(_LOAD, iv)
    # predtým 7,3047 € a zostatok orezaný na 0 vs skutočných 8,8447 €
    assert abs(streams - s.grid_bucket_leftover_cost_eur - real) < 1e-6, (streams, s.grid_bucket_leftover_cost_eur, real)


# ---------- A16/E09: kalendárne starnutie presne raz za časový krok ----------
def test_a16_necinnost_starne():
    iv, s, bat, _ = run_ems([0.0] * 24, [0.0] * 24, [100.0] * 24, 100.0)
    assert all(i.bat_to_load_kwh == 0 and i.pv_to_bat_kwh == 0 and i.grid_to_bat_kwh == 0 for i in iv)
    # predtým 0,0 dňa (kalendár sa posúval len v charge/discharge)
    assert abs(bat.degradation.total_calendar_days - 1.0) < 1e-9, bat.degradation.total_calendar_days
    assert bat.soh < bat.bess.initial_soh


def test_a16_aktivny_profil_15min_raz_za_krok():
    # 15-min krok, 2 dni; arb_band_pct=0.5 a cena v strede okna → v takom kroku vybíjanie AJ
    # nabíjanie zo siete (predtým 2× kalendár v takom kroku, 0× v nečinnom)
    n = 192
    h = (np.arange(n) // 4) % 24
    spot = np.array([100.0, 0.0, 200.0] * 64)
    load = np.where((h >= 7) & (h <= 20), 30.0, 5.0)
    iv, s, bat, _ = run_ems(load, np.zeros(n), spot, 100.0, timestep_min=15, arb_band_pct=0.5,
                            dispatch="rules")   # súčasné nabíjanie+vybíjanie v kroku robia len pravidlá
    both = sum(1 for i in iv if i.bat_to_load_kwh > 0 and i.grid_to_bat_kwh > 0)
    idle = sum(1 for i in iv if i.bat_to_load_kwh == 0 and i.grid_to_bat_kwh == 0 and i.pv_to_bat_kwh == 0)
    assert both > 0 and idle > 0, (both, idle)
    assert abs(bat.degradation.total_calendar_days - n * 0.25 / 24) < 1e-9, bat.degradation.total_calendar_days


# ---------- A17/E10: ročný limit cyklov je tvrdý ----------
def test_a17_budget_tvrdy_limit():
    spot = [10.0] * 12 + [200.0] * 12 + [10.0] * 12 + [200.0] * 12
    for budget in (0.1, 1.0):
        iv, s, bat, _ = run_ems([10.0] * 48, [0.0] * 48, spot, 100.0, initial_soc_pct=0.08,
                                max_efc_per_year=budget)
        # predtým 0,521 EFC pri budgete 0,1 (a 1,015 pri 1,0) — jeden krok prestrelil
        assert s.bat_efc <= budget + 1e-9, (budget, s.bat_efc)
        assert sum(i.efc_this_step for i in iv) <= budget + 1e-9


def test_a17_pv_do_baterie_az_po_vycerpanie_budgetu():
    # Malé PV prebytky (5 kWh/h, ~0,05 EFC/krok), plochý spot → bez grid arbitráže.
    # Predtým sa PV→bat zastavilo už pri 90 % budgetu → ~0,9 EFC namiesto 1,0.
    n = 96
    h = np.arange(n) % 24
    day = (h >= 8) & (h <= 17)
    pv = np.where(day, 15.0, 0.0)
    load = np.full(n, 10.0)
    iv, s, bat, _ = run_ems(load, pv, [100.0] * n, 100.0, initial_soc_pct=0.08, max_efc_per_year=1.0)
    assert sum(i.grid_to_bat_kwh for i in iv) == 0
    assert abs(s.bat_efc - 1.0) < 1e-9, s.bat_efc


if __name__ == "__main__":
    import traceback
    fails = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except Exception:
            fails += 1
            print(f"FAIL {n}")
            traceback.print_exc()
    print(f"{len(tests) - fails}/{len(tests)} OK")
    sys.exit(1 if fails else 0)
