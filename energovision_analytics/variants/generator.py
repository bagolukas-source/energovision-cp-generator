"""VariantGenerator — generuje matrix PV × BESS × EMS variantov + spustí každý.

Pre obchodný workflow:
    1. Obchodník zadá range PV (4 sizes) × BESS (3 sizes) × EMS (1-2 stratégie)
    2. Engine vyrobí všetky kombinácie (typicky 12-24 variantov)
    3. Spustí každý cez plný pipeline (PV sim + EMS dispatch + Financial)
    4. Vráti list VariantResult ktorý sa potom rankuje cez scorer
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from energovision_analytics.battery import BatteryPack
from energovision_analytics.core.models import (
    BESSInput, BESSVyrobca, Chemia, Konfiguracia, ModulTyp, PVInput, SiteInput,
)
from energovision_analytics.ems import EMSConfig, RuleBasedEMS
from energovision_analytics.ems.dispatch_state import DispatchInterval, DispatchSummary
from energovision_analytics.financial import CashflowBuilder, sk_dotacia_zelena_podnikom
from energovision_analytics.financial.cashflow import FinancialResult
from energovision_analytics.pv import PVSystemSim
from energovision_analytics.tariff import RetailCalculator, TariffEngine


@dataclass
class VariantResult:
    """Výsledok jedného variantu (PV size × BESS size × EMS)."""
    variant_id: str
    pv_kwp: float
    bess_kwh: float
    bess_kw: float
    ems_strategy: str

    # Cost inputs (per kWp/kWh — bez DPH typicky)
    capex_pv_eur_per_kwp: float
    capex_bess_eur_per_kwh: float
    capex_total_eur: float
    dotacia_eur: float
    merchant_eur: float   # ročný merchant zisk (podpora bilančnej skupiny), 0 ak režim vypnutý

    # Engine outputs
    summary: DispatchSummary
    financial: FinancialResult
    intervals: Optional[list[DispatchInterval]] = field(default=None, repr=False)
    # Pre konzistentný rebuild financií s korektnou dotáciou (B1 fix) — neserializuje sa
    _cf_builder: object = field(default=None, repr=False, compare=False)
    _cf_kwargs: Optional[dict] = field(default=None, repr=False, compare=False)

    # KPI pre ranking
    @property
    def npv_eur(self) -> float:
        return self.financial.npv_eur

    @property
    def irr_pct(self) -> float:
        return self.financial.irr_pct or 0

    @property
    def payback_y(self) -> float:
        return self.financial.payback_simple_y

    @property
    def samospotreba_pct(self) -> float:
        return self.summary.samospotreba_pct

    @property
    def samostatnost_pct(self) -> float:
        return self.summary.samostatnost_pct

    @property
    def saving_y1_eur(self) -> float:
        return self.financial.annual_saving_y1_eur

    def label(self) -> str:
        """Krátky label pre UI."""
        bess_part = f"+ {self.bess_kwh:.0f}/{self.bess_kw:.0f} kWh/kW BESS" if self.bess_kwh > 0 else "(bez BESS)"
        return f"{self.pv_kwp:.0f} kWp FVE {bess_part}"


class VariantGenerator:
    """Matrix runner — vyrobí všetky kombinácie PV × BESS × EMS a spustí každý."""

    def __init__(
        self,
        site: SiteInput,
        load_df: pd.DataFrame,
        spot_eur_mwh: np.ndarray,
        timestamps: pd.DatetimeIndex,
        tariff_engine: TariffEngine,
        # Variant ranges
        pv_kwp_options: list[float] | None = None,
        bess_kwh_options: list[float] | None = None,
        bess_c_rate: float = 0.5,
        ems_strategies: list[str] | None = None,
        # Defaults pre PV/BESS
        pv_modul_typ: str = "TOPCon",
        pv_modul_wp: int = 700,
        pv_sklon: float = 25,
        pv_azimut: float = 180,
        pv_konfiguracia: str = "2xP",
        pv_inverter_ratio: float = 1.0,
        count_battery_replacement: bool = False,
        bess_vyrobca: str = "Huawei",
        bess_typ: str = "LUNA2000",
        # Cost inputs
        capex_pv_eur_per_kwp: float = 574,
        capex_pv_fixed_eur: float = 38000,
        capex_bess_eur_per_kwh: float = 318,
        # Financial — defaulty z core.defaults (centrálne)
        opex_pct: float | None = None,
        opex_bess_pct: float | None = None,
        discount_rate: float | None = None,
        horizon_years: int | None = None,
        dppo_pct: float | None = None,
        depr_years: int | None = None,
        price_escalation_pct: float = 0.0,
        savings_coefficient: float = 1.0,
        has_sufficient_profit: bool = True,
        export_price_eur_kwh: float = 0.06,
        merchant_mode: bool = False,
        merchant_organizer_fee_pct: float = 15.0,
        merchant_imbalance_eur_mwh: float = 0.0,   # BOD 3: odchýlka per MWh obchodu
        merchant_degradation_eur_mwh: float = 0.0, # BOD 3: cyklová degradačná rezerva per MWh
        bess_mode: str = "SITE_SUPPORT_ONLY",     # BOD 1: SITE_SUPPORT_ONLY | BALANCE_GROUP_MERCHANT_100
        ems_max_efc_per_year: float | None = None,   # override cyklov/rok z UI (inak warranty/horizon)
        ems_arb_min_spread_eur_mwh: float | None = None,  # override min spreadu arbitráže z UI
        merchant_revenue_share_pct: float = 1.0,   # R2 #6: podiel klienta z čistého merchant výnosu
    ) -> None:
        # Lazy import aby sa rieš cyklický import
        from energovision_analytics.core.defaults import ECON
        if opex_pct is None: opex_pct = ECON.opex.pv_pct_per_year
        if opex_bess_pct is None: opex_bess_pct = ECON.opex.bess_pct_per_year
        if discount_rate is None: discount_rate = ECON.financial.discount_rate_default
        if horizon_years is None: horizon_years = ECON.financial.horizon_years_default
        if dppo_pct is None: dppo_pct = ECON.dppo.default_pct
        if depr_years is None: depr_years = ECON.depreciation.pv_years
        self.site = site
        self.load_df = load_df
        self.spot = spot_eur_mwh
        self.timestamps = timestamps
        self.tariff_engine = tariff_engine

        # Defaults pre variant ranges
        self.pv_kwp_options = pv_kwp_options or [50, 100, 200, 300]
        self.bess_kwh_options = bess_kwh_options or [0, 100, 200]
        self.bess_c_rate = bess_c_rate
        self.ems_strategies = ems_strategies or ["rule_based"]

        # PV defaults
        self.pv_modul_typ = pv_modul_typ
        self.pv_modul_wp = pv_modul_wp
        self.pv_sklon = pv_sklon
        self.pv_azimut = pv_azimut
        self.pv_konfiguracia = pv_konfiguracia
        self.pv_inverter_ratio = pv_inverter_ratio

        # BESS defaults
        self.bess_vyrobca = bess_vyrobca
        self.bess_typ = bess_typ

        # Cost
        self.count_battery_replacement = count_battery_replacement
        self.capex_pv = capex_pv_eur_per_kwp
        self.capex_pv_fixed = capex_pv_fixed_eur
        self.capex_bess = capex_bess_eur_per_kwh
        self.opex_pct = opex_pct
        self.opex_bess_pct = opex_bess_pct
        self.discount_rate = discount_rate
        self.horizon_years = horizon_years
        self.dppo_pct = dppo_pct
        self.depr_years = depr_years
        self.price_escalation_pct = price_escalation_pct or 0.0
        self.savings_coefficient = savings_coefficient if (savings_coefficient and savings_coefficient > 0) else 1.0
        self.has_sufficient_profit = bool(has_sufficient_profit)
        self.export_price = float(export_price_eur_kwh) if export_price_eur_kwh else 0.06
        # Merchant mód: batéria slúži ako podpora bilančnej skupiny (grid-to-grid arbitráž
        # plnou paľbou, nie samospotreba). Default OFF → normálne varianty bez zmeny.
        # BOD 1: explicitný režim batérie. BALANCE_GROUP_MERCHANT_100 = merchant; mapuje sa na merchant_mode
        self.bess_mode = str(bess_mode or "SITE_SUPPORT_ONLY")
        self.ems_max_efc_per_year = ems_max_efc_per_year
        self.ems_arb_min_spread_eur_mwh = ems_arb_min_spread_eur_mwh
        self.merchant_mode = bool(merchant_mode) or self.bess_mode == "BALANCE_GROUP_MERCHANT_100"
        self.merchant_organizer_fee_pct = float(merchant_organizer_fee_pct)
        self.merchant_imbalance_eur_mwh = float(merchant_imbalance_eur_mwh or 0.0)
        self.merchant_degradation_eur_mwh = float(merchant_degradation_eur_mwh or 0.0)
        self.merchant_revenue_share_pct = float(merchant_revenue_share_pct if merchant_revenue_share_pct is not None else 1.0)

    # ------------------------------------------------------------------ Build inputs
    def _make_pv(self, kwp: float) -> PVInput:
        """Postaví PVInput pre danú kWp."""
        if kwp <= 0:
            return None
        n_modules = max(1, int(round(kwp * 1000 / self.pv_modul_wp)))
        # Re-adjust kwp aby sedelo s modules
        adjusted_kwp = n_modules * self.pv_modul_wp / 1000
        inverter_kw = adjusted_kwp / self.pv_inverter_ratio
        return PVInput(
            instalovany_kwp=adjusted_kwp,
            modul_typ=ModulTyp(self.pv_modul_typ),
            modul_wp=self.pv_modul_wp,
            pocet_modulov=n_modules,
            inverter_kw_ac=inverter_kw,
            sklon_stupne=self.pv_sklon,
            azimut_stupne=self.pv_azimut,
            konfiguracia=(Konfiguracia(self.pv_konfiguracia) if self.pv_konfiguracia in [c.value for c in Konfiguracia] else Konfiguracia.DVOJRADOVA_PORTRAIT),
        )

    def _make_bess(self, kwh: float) -> BESSInput:
        """Postaví BESSInput pre danú kWh."""
        if kwh <= 0:
            return None
        bess_kw = kwh * self.bess_c_rate
        # Solinteg/Huawei default: 8-98% SoC window
        usable = kwh * 0.90
        return BESSInput(
            vyrobca=BESSVyrobca(self.bess_vyrobca) if self.bess_vyrobca in [v.value for v in BESSVyrobca] else BESSVyrobca.INE,
            typ=f"{self.bess_typ}-{int(kwh)}",
            chemie=Chemia.LFP,
            nominal_kwh=kwh,
            usable_kwh=usable,
            power_kw_ac=bess_kw,
            c_rate_max=max(0.5, self.bess_c_rate),
        )

    # ------------------------------------------------------------------ Run single
    def run_single(
        self, pv_kwp: float, bess_kwh: float, ems_strategy: str = "rule_based",
        keep_intervals: bool = False,
    ) -> VariantResult:
        """Spustí 1 variant cez celý pipeline."""
        variant_id = f"PV{pv_kwp:.0f}_BESS{bess_kwh:.0f}_{ems_strategy}"
        pv = self._make_pv(pv_kwp)
        bess = self._make_bess(bess_kwh)

        # PV simulácia (alebo nuly ak pv=None)
        if pv:
            pv_sim = PVSystemSim(pv, self.site)
            pv_year_df = pv_sim.simulate_year(self.timestamps[0].year, 60)
            pv_kw = pv_year_df["pv_kw"].to_numpy()[:len(self.timestamps)]
        else:
            pv_kw = np.zeros(len(self.timestamps))

        # Battery + EMS
        load_kw = self.load_df["load_kw"].to_numpy()[:len(self.timestamps)]

        # Pad ak je krátky
        if len(load_kw) < len(self.timestamps):
            load_kw = np.concatenate([load_kw, np.zeros(len(self.timestamps) - len(load_kw))])
        if len(pv_kw) < len(self.timestamps):
            pv_kw = np.concatenate([pv_kw, np.zeros(len(self.timestamps) - len(pv_kw))])

        # Tariff
        tariff = self.tariff_engine.get(self.site.distribuutor, self.site.sadzba)
        retail = RetailCalculator(tariff, typ_tarify=self.site.typ_tarify)

        # BOD 11 FIX: v merchant móde batéria neslúži OM → site KPI/summary = PV-only
        # (samospotreba/samostatnosť odrážajú LEN PV; batéria zarába zvlášť ako merchant).
        if bess and not self.merchant_mode:
            battery = BatteryPack(bess, initial_soc_pct=0.5)
            ems = RuleBasedEMS(
                battery, self.site, tariff, retail,
                EMSConfig(
                    # UI override (analyza_om.max_efc_per_year) má prednosť; default = warranty/horizont
                    max_efc_per_year=int(self.ems_max_efc_per_year or (bess.warranty_cycles / self.horizon_years)),
                    **({"arb_min_spread_eur_mwh": float(self.ems_arb_min_spread_eur_mwh)}
                       if self.ems_arb_min_spread_eur_mwh else {}),
                    peak_shave_enabled=(self.site.sadzba.value == "VN"),
                ),
                export_price_eur_kwh=self.export_price,
            )
            intervals, summary = ems.run_year(load_kw, pv_kw, self.spot, self.timestamps, 60)
        else:
            # PV-only (alebo merchant: batéria mimo OM) — site summary len z PV
            summary = self._build_pv_only_summary(load_kw, pv_kw, retail)
            intervals = []

        # Financial
        # Reálny CAPEX FVE: FIXNÁ zložka (projekt/základ) + MARGINÁLNA €/kWp (úspory z rozsahu)
        capex_pv_total = (self.capex_pv_fixed + pv_kwp * self.capex_pv) if pv else 0
        capex_bess_total = bess_kwh * self.capex_bess if bess else 0
        total_capex = capex_pv_total + capex_bess_total

        saving_decomp = {
            "sav_solar_self_cons_eur": summary.sav_solar_self_cons_eur,
            "sav_solar_export_eur": summary.sav_solar_export_eur,
            "sav_bess_self_cons_eur": summary.sav_bess_self_cons_eur,
            "sav_arbitrage_eur": summary.sav_arbitrage_eur,
            "sav_peak_shaving_eur": summary.sav_peak_shaving_eur,
            "sav_mrk_penalty_avoided_eur": summary.sav_mrk_penalty_avoided_eur,
        }

        # Throughput batérie pre kalkuláciu výmeny článkov (default = EMS výboj)
        _bess_throughput_kwh = summary.bat_discharge_total_kwh

        # ── MERCHANT MÓD (podpora bilančnej skupiny) ─────────────────────────────
        # Batéria nerobí samospotrebu/peak — ide grid-to-grid arbitráž PLNOU PAĽBOU
        # (rozdiel nad spotrebu pokryje bilančná skupina). Hodnota batérie =
        # merchant spread × (1 − marža organizátora). Limit = RK import + export (MRK)
        # + výkon/SoC batérie, NIE spotreba. PV samospotreba/export zostávajú.
        if self.merchant_mode and bess:
            from energovision_analytics.financial.merchant_arbitrage import compute_merchant_arbitrage
            _power_kw = bess_kwh * self.bess_c_rate
            _rk_kw = float(self.site.rk_kw or 0.0)
            _export_kw = float(self.site.mrk_kw or self.site.rk_kw or 0.0)
            # RK/MRK nezadané (0/None) → merchant arbitráž by potichu vyšla 0 € (limit prietoku 0 kW).
            # Fallback: predpokladaj, že prípojka zvládne aspoň výkon batérie — inak batéria „zmizne" z posudku.
            if _rk_kw <= 0:
                _rk_kw = _power_kw
            if _export_kw <= 0:
                _export_kw = _power_kw
            _dt_h = 1.0
            try:
                _dt_h = float((self.timestamps[1] - self.timestamps[0]).total_seconds()) / 3600.0
            except Exception:
                _dt_h = 1.0
            _window = max(4, int(round(24.0 / _dt_h)))
            _m = compute_merchant_arbitrage(
                spot_eur_mwh=self.spot,
                dt_h=_dt_h,
                bess_kwh=bess_kwh,
                power_kw_ac=_power_kw,
                rk_kw=_rk_kw,
                export_kw=_export_kw,
                organizer_fee_pct=self.merchant_organizer_fee_pct,
                imbalance_cost_eur_mwh=self.merchant_imbalance_eur_mwh,
                degradation_cost_eur_mwh=self.merchant_degradation_eur_mwh,
                revenue_share_pct=self.merchant_revenue_share_pct,
                window=_window,
            )
            # Batéria neslúži záťaži → vynuluj jej samospotrebné/arbitráž/peak streamy
            saving_decomp["sav_bess_self_cons_eur"] = 0.0
            saving_decomp["sav_arbitrage_eur"] = 0.0
            saving_decomp["sav_peak_shaving_eur"] = 0.0
            saving_decomp["sav_merchant_eur"] = float(_m["annual_profit_eur"])
            # Throughput z merchantu (plná paľba → rýchlejšia degradácia/výmena)
            _bess_throughput_kwh = float(_m["throughput_mwh"]) * 1000.0

        # Výmena článkov batérie — OPCIA (default OFF). Default = bez výmeny (batéria
        # predpokladaná na celý horizont). Ak ZAPNUTÉ → výmena pri dosiahnutí warranty cyklov
        # (reálny ročný throughput, nie podhodnotené EFC), náklad 40 % BESS capexu, periodicky.
        _cells_repl_interval = None
        if bess and getattr(self, "count_battery_replacement", False):
            _usable = (bess.usable_kwh or (bess_kwh * 0.9))
            _ann_cycles = (_bess_throughput_kwh / _usable) if _usable > 0 else 0.0
            if _ann_cycles > 0:
                _life = bess.warranty_cycles / _ann_cycles
                if _life < self.horizon_years:
                    _cells_repl_interval = max(4, int(round(_life)))

        builder = CashflowBuilder(
            capex_solar_eur=capex_pv_total,
            capex_bess_eur=capex_bess_total,
            opex_solar_pct=self.opex_pct,
            opex_bess_pct=self.opex_bess_pct,
            insurance_pct=0.003,
            monitoring_eur_per_year=300,
            bess_inverter_replacement_year=12 if bess else None,
            bess_inverter_replacement_pct=0.10,
            bess_cells_replacement_interval_years=_cells_repl_interval,
            dppo_pct=self.dppo_pct,
            depr_years=self.depr_years,
            discount_rate=self.discount_rate,
            horizon_years=self.horizon_years,
            price_escalation_pct=self.price_escalation_pct,
            savings_coefficient=self.savings_coefficient,
            has_sufficient_profit=self.has_sufficient_profit,
        )
        # B1 fix: BÁZOVÝ cashflow je BEZ dotácie → IRR, payback aj cashflow_array sú konzistentné.
        # Správnu dotáciu aplikuje pipeline (engine_service) plným rebuildom cez tieto kwargs.
        _cf_kwargs = dict(
            annual_saving_y1_eur=sum(saving_decomp.values()),  # = sav_total (non-merchant) / konzistentné (merchant)
            saving_decomp_y1=saving_decomp,
            annual_degradation_pct=0.5,
            annual_bess_discharge_kwh=_bess_throughput_kwh,
        )
        financial = builder.build(dotacia_eur=0.0, **_cf_kwargs)

        return VariantResult(
            variant_id=variant_id,
            pv_kwp=pv_kwp,
            bess_kwh=bess_kwh,
            bess_kw=bess_kwh * self.bess_c_rate,
            ems_strategy=ems_strategy,
            capex_pv_eur_per_kwp=self.capex_pv,
            capex_bess_eur_per_kwh=self.capex_bess,
            capex_total_eur=total_capex,
            dotacia_eur=0.0,  # finálnu dotáciu nastaví pipeline (rebuild)
            merchant_eur=float(saving_decomp.get("sav_merchant_eur", 0.0)),
            summary=summary,
            financial=financial,
            intervals=intervals if keep_intervals else None,
            _cf_builder=builder,
            _cf_kwargs=_cf_kwargs,
        )

    def _build_pv_only_summary(self, load_kw, pv_kw, retail) -> DispatchSummary:
        """Pre PV-only variant — žiadna BESS, len priame self-cons + export."""
        n = len(load_kw)
        s = DispatchSummary(rok=int(self.timestamps[0].year), n_intervals=n)
        pv_to_load = np.minimum(pv_kw, load_kw)
        pv_to_grid = np.maximum(pv_kw - load_kw, 0)
        grid_import = np.maximum(load_kw - pv_kw, 0)

        s.load_total_kwh = float(load_kw.sum())
        s.pv_total_kwh = float(pv_kw.sum())
        s.pv_to_load_kwh = float(pv_to_load.sum())
        s.pv_to_grid_kwh = float(pv_to_grid.sum())
        s.grid_import_kwh = float(grid_import.sum())
        s.grid_export_kwh = s.pv_to_grid_kwh

        if s.pv_total_kwh > 0:
            s.samospotreba_pct = s.pv_to_load_kwh / s.pv_total_kwh * 100
        if s.load_total_kwh > 0:
            s.samostatnost_pct = s.pv_to_load_kwh / s.load_total_kwh * 100

        # Saving — PV samospotreba ocenená REÁLNYM tarifom per-interval (FIX→flat,
        # SPOT→hodinový), KONZISTENTNE s RuleBasedEMS. (Predtým hardcoded 0.20 → nafukovalo
        # PV-only a robilo batériu zdanlivo stratovou. Audit 2026-06-07.)
        try:
            _spot = self.spot
            _n2 = min(len(pv_to_load), len(_spot))
            _sav = 0.0
            for _i in range(_n2):
                _sav += float(pv_to_load[_i]) * retail.retail_buy_eur_kwh(float(_spot[_i]))
            s.sav_solar_self_cons_eur = _sav
        except Exception:
            # fallback: priemerný tarif z retail (FIX) ak spot nedostupný
            _avg = retail.retail_buy_eur_kwh(None) if retail.typ_tarify.value == "fix" else 0.146
            s.sav_solar_self_cons_eur = s.pv_to_load_kwh * _avg
        s.sav_solar_export_eur = s.pv_to_grid_kwh * self.export_price
        s.sav_total_eur = s.sav_solar_self_cons_eur + s.sav_solar_export_eur
        s.co2_avoided_t = (s.pv_to_load_kwh + s.pv_to_grid_kwh) * 0.25 / 1000
        s.n_state_normal = n
        return s

    # ------------------------------------------------------------------ Run all
    def run_all(
        self,
        keep_intervals_for_best: bool = True,
        parallel: bool = False,
        n_workers: int | None = None,
    ) -> list[VariantResult]:
        """Spustí všetky kombinácie PV × BESS × EMS.

        Args:
            parallel: ak True, použije ThreadPoolExecutor pre I/O bound tasky.
                Pre CPU-bound by sa mal použiť ProcessPoolExecutor, ale to
                rozbije picklovanie BatteryPack / EMS state. Zatiaľ ThreadPool
                ktorý ale kvôli GIL nezrýchli CPU. Pre väčšie zrýchlenie treba
                NumPy vektorizáciu EMS dispatch (Sprint 10).
            n_workers: počet workerov (default = počet variantov, max 8)
        """
        from energovision_analytics.core.logging import get_logger
        log = get_logger(__name__)

        tasks = [
            (pv_kwp, bess_kwh, ems)
            for pv_kwp in self.pv_kwp_options
            for bess_kwh in self.bess_kwh_options
            for ems in self.ems_strategies
        ]

        if not parallel or len(tasks) <= 2:
            results = []
            for pv_kwp, bess_kwh, ems in tasks:
                try:
                    r = self.run_single(pv_kwp, bess_kwh, ems, keep_intervals=False)
                    results.append(r)
                except Exception as e:
                    log.warning("Variant %s/%s/%s failed: %s", pv_kwp, bess_kwh, ems, e)
            return results

        # Parallel (ThreadPoolExecutor — limitovaný GIL ale safe pre stavový engine)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        n_workers = n_workers or min(8, len(tasks))
        results = []
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futures = {
                ex.submit(self.run_single, pv, bess, ems, False): (pv, bess, ems)
                for pv, bess, ems in tasks
            }
            for fut in as_completed(futures):
                pv, bess, ems = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as e:
                    log.warning("Variant %s/%s/%s failed: %s", pv, bess, ems, e)
        return results
