# FVZ Monitoring AI — Energovision

Vlastná SCADA + AI-powered dispečing pre ~400 fotovoltických inštalácií.
Nahrádza roztrúsené vendor portály (Huawei FusionSolar, Solinteg iSolar,
GoodWe SEMS, Fronius Solar.web, Sungrow iSolarCloud) jedným systémom
integrovaným do CRM (app.energovision.sk).

## Architektúra

```
┌───────────────────────────────────────────────────────────────────┐
│  CRM (app.energovision.sk) — Next.js + Supabase + Tailwind        │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ /dispatch/fleet   — mapa SK + tabuľka staníc + live status  │  │
│  │ /dispatch/site/[id] — drilldown grafy + KPI + alarmy        │  │
│  │ /dispatch/alarms  — inbox + routing + escalation            │  │
│  │ /klient/[token]   — klient portál (token alebo heslo)       │  │
│  │ /technik/PWA      — mobil-friendly field service            │  │
│  └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
                              ▲ Supabase Realtime + REST
                              │
┌───────────────────────────────────────────────────────────────────┐
│  Supabase Postgres + TimescaleDB extension                        │
│                                                                   │
│  ── Master data ──────────────────────────────────────────────    │
│  inverter_sites              (rozšírenie existujúceho)            │
│  inverter_vendor_credentials (existujúce)                         │
│  pvgis_baseline              (ročný profil per stanica)           │
│  customers                   (zákaznícke entity)                  │
│                                                                   │
│  ── Time-series (hypertables) ────────────────────────────────    │
│  telemetry_5min          ← raw ingest, chunk 1 deň, compress 7d   │
│  telemetry_15min         ← continuous aggregate                   │
│  telemetry_daily         ← continuous aggregate                   │
│  performance_kpis_daily  ← PR, yield, availability                │
│                                                                   │
│  ── Eventy / dispečing ───────────────────────────────────────    │
│  alarms                  (canonical alarms)                       │
│  alarm_routing_rules     (existujúce)                             │
│  inverter_commands       (existujúce)                             │
│  spot_state_transitions  (existujúce — SPOT reactor)              │
│  anomaly_predictions     (ML output, score + classification)      │
└───────────────────────────────────────────────────────────────────┘
                              ▲ asyncpg / supabase-py
                              │
┌───────────────────────────────────────────────────────────────────┐
│  Render Background Workers (Python)                               │
│                                                                   │
│  ingest/                                                          │
│  ├─ base.py            VendorAdapter abstrakcia                   │
│  ├─ canonical.py       Canonical data model                       │
│  ├─ vendor_huawei.py   Huawei FusionSolar / SmartPVMS Cloud API   │
│  ├─ vendor_solinteg.py Solinteg iSolar API                        │
│  ├─ vendor_goodwe.py   GoodWe SEMS API                            │
│  ├─ vendor_fronius.py  Fronius Solar.web API v2                   │
│  ├─ vendor_sungrow.py  Sungrow iSolarCloud API                    │
│  ├─ orchestrator.py    Spúšťa adaptéry ako cron / on-demand       │
│  └─ pvgis_baseline.py  Stiahne ročný PVGIS profil per stanicu     │
│                                                                   │
│  dispatch/                                                        │
│  ├─ kpi_engine.py        PR, yield, availability, CF              │
│  ├─ zero_export.py       Sliding window export breach detection   │
│  ├─ alarm_engine.py      Dedup, route, escalate                   │
│  ├─ anomaly_detector.py  Rule-based + cohort + ML classifier      │
│  └─ notification.py      Slack / email / SMS / push               │
└───────────────────────────────────────────────────────────────────┘
                              ▲ HTTPS
              ┌───────────────┼───────────────┬─────────────┐
              │               │               │             │
        ┌─────┴────┐    ┌─────┴────┐   ┌──────┴────┐ ┌──────┴────┐
        │ Huawei   │    │ Solinteg │   │  GoodWe   │ │  Sungrow  │
        │FusionPVMS│    │  iSolar  │   │   SEMS    │ │ iSolarClod│
        └──────────┘    └──────────┘   └───────────┘ └───────────┘
                          ┌──────────┐
                          │ Fronius  │
                          │Solar.web │
                          └──────────┘
```

## Dátové volumy

- 400 staníc × telemetria každých 5 min = ~115 000 záznamov/deň
- ročne ~42 M záznamov
- po TimescaleDB kompresii (chunk 1 deň, compress po 7 dňoch) ~5–8 GB/rok
- 5 rokov udržiavateľných na štandard Supabase Pro plane

## Rate limiting stratégia

Vendor cloud API limity (typicky 600–1200 req/h per installer account).
Naša stratégia:

- **Batch list endpoint** každých 5 min: 1 request vráti všetkých 400 staníc daného vendora s posledným realtime stavom
- **Per-plant detail** len pri evente: alarm, anomaly, manual drilldown z UI
- **Daily summary** raz denne pre každú stanicu
- **Alarm endpoint** každých 5 min cez batch

Tým ostávame pri ~5–10 req/min per vendor, hlboko pod limitmi.

## Inštalácia (Render)

```bash
git clone <repo> fvz-monitoring && cd fvz-monitoring
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# vyplň SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY a credentials pre každého vendora

# Spustí Supabase migrácie
psql "$SUPABASE_DB_URL" -f supabase/migrations/001_telemetry_schema.sql

# Test jedného vendora
python -m ingest.orchestrator --vendor huawei --action plant-list
python -m ingest.orchestrator --vendor huawei --action realtime
```

## Render cron rozvrh

| Job | Cron | Trvanie |
| --- | --- | --- |
| `orchestrator.py --action realtime --all-vendors` | `*/5 * * * *` | ~30s |
| `orchestrator.py --action daily-summary --all-vendors` | `15 1 * * *` | ~5 min |
| `orchestrator.py --action alarms --all-vendors` | `*/5 * * * *` | ~20s |
| `dispatch/zero_export.py` | `*/15 * * * *` | ~10s |
| `dispatch/anomaly_detector.py --mode cohort` | `0 */2 * * *` | ~2 min |
| `dispatch/kpi_engine.py --period daily` | `30 0 * * *` | ~3 min |
| `dispatch/alarm_engine.py --dedup` | `*/10 * * * *` | ~5s |
| `ingest/pvgis_baseline.py --backfill-new` | `0 3 * * *` | variable |

## Roadmap

| Fáza | Obsah | Trvanie |
| --- | --- | --- |
| **1. Ingestion + DB foundation** | TimescaleDB schema, 5 adaptérov, orchestrátor, PVGIS | 4–5 týždňov |
| **2. Dispečing UI + zero-export** | /dispatch/fleet + site detail, alarm engine, Slack | 3–4 týždne |
| **3. Inteligentný dispečing** | Cohort analytics, anomaly classifier, PWA technici | 4–6 týždňov |
| **4. Klient portál + AI co-pilot** | klient.energovision.sk dual auth, LLM agent, gen. reporty | 4–5 týždňov |

## Bezpečnostné brzdy

- `inverter_sites.dispatch_paused = TRUE` per stanica → ingestion vynechá
- Global pause: `POST /webhook/dispatch-pause` so secret (zo Slacku)
- Per-vendor disable: `VENDOR_HUAWEI_ENABLED=false` env
- Read-only mode: `DISPATCH_READ_ONLY=true` blokuje všetky `send_command`
