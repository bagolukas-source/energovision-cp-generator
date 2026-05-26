# Deploy návod — FVZ Monitoring AI

Krok-za-krokom od „kód na disku" po „live na Renderi monitoruje 400 staníc".

## 0. Predpoklady

- GitHub účet s prístupom do Energovision orgu
- Render účet (https://render.com) — Energovision business account
- Supabase projekt s Pro plánom (kvôli TimescaleDB extension)
- Installer credentials pre vendor cloud-y (Huawei FusionSolar, Solinteg iSolar, ...)

## 1. Založenie GitHub repa

Dve možnosti:

### A) Samostatné repo `energovision-monitoring` (odporúčam)

Čistejšie odpojené od `energovision-fve-os`, môže žiť na inej Vercel/Render účte ak treba.

```bash
cd "/Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod/2026-05-11_FVZ_Monitoring_AI"

git init -b main
git add .
git commit -m "Initial commit: FVZ Monitoring AI skeleton"

# Vytvor repo na GitHube (cez gh CLI alebo web)
gh repo create energovision/monitoring --private --source=. --remote=origin --push

# ALEBO ručne:
# 1. github.com → New repo → energovision/monitoring (private)
# 2. git remote add origin git@github.com:energovision/monitoring.git
# 3. git push -u origin main
```

### B) Subdirektory v existujúcom `energovision-fve-os`

Šetrí git ceremoniál ale zdieľa CI/CD a deploy pipeline s CRM.

```bash
cp -r "/Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod/2026-05-11_FVZ_Monitoring_AI" \
   "/path/to/energovision-fve-os/services/monitoring"
cd "/path/to/energovision-fve-os"
git add services/monitoring
git commit -m "Add FVZ Monitoring AI service"
git push
```

⚠️ Pri voľbe B uprav `render.yaml` paths a v Render Blueprint nastav root directory na `services/monitoring`.

## 2. Supabase migrácia

### 2a. Overenie TimescaleDB

Choď do Supabase Dashboard → Database → Extensions → vyhľadaj `timescaledb`.

- Ak je tam zelený toggle → je dostupný, len ho aktivuj
- Ak chýba → potrebuješ Pro plan (alebo upgrade Free → Pro)

### 2b. Spustenie migrácie

**Možnosť 1 — Supabase Dashboard SQL Editor:**
1. Skopíruj obsah `supabase/migrations/001_telemetry_schema.sql`
2. Paste do SQL Editor → Run
3. Skontroluj že nehlási chyby (môže byť 1–2 warningov o existujúcich indexoch — to je OK)

**Možnosť 2 — psql z terminálu:**
```bash
# Skopíruj DB URL zo Supabase Dashboard → Settings → Database → Connection string (URI)
export SUPABASE_DB_URL="postgresql://postgres:[PASSWORD]@db.xxxxx.supabase.co:5432/postgres"
./scripts/run_migration.sh
```

Skript skontroluje extension, spustí migráciu a verifikuje výsledok.

### 2c. Overenie

```sql
SELECT * FROM timescaledb_information.hypertables;
SELECT * FROM timescaledb_information.continuous_aggregates;
SELECT COUNT(*) FROM information_schema.tables
    WHERE table_schema='public' AND table_name LIKE 'telemetry%';
```

Očakávaný výsledok: 1 hypertable (`telemetry_5min`), 3 continuous aggregates,
3 telemetry tabuľky + ďalšie (alarms, pvgis_baseline, atď.).

## 3. Naplnenie credentials do Supabase

Pre každého vendor-a vlož 1 riadok do `inverter_vendor_credentials`:

```sql
INSERT INTO inverter_vendor_credentials (vendor, username, password, api_key, api_secret)
VALUES
  ('huawei',   'energovision_installer', '<heslo>',                NULL,           NULL),
  ('solinteg', 'energovision_installer', '<heslo>',                '<app_key>',    '<app_secret>'),
  ('goodwe',   'energovision_installer', '<heslo>',                NULL,           NULL),
  ('fronius',   NULL,                    NULL,                     '<client_id>',  '<client_secret>'),
  ('sungrow',  'energovision_installer', '<heslo>',                '<app_key>',    NULL);
```

⚠️ Pre Fronius treba aj `access_key_id` + `access_key_value` — buď do tabuľky pridaj stĺpce
alebo nastav cez env priamo v Render Blueprint.

## 4. Lokálny smoke test (voliteľné, ale odporúčané)

Než dáme na Render, overme si že to funguje lokálne aspoň pre 1 vendora.

```bash
cd "/Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod/2026-05-11_FVZ_Monitoring_AI"

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Vyplň: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
# a credentials pre VENDOR ktorého chceš testovať

./scripts/smoke_test.sh solinteg
```

Očakávaný výstup:
```
=== 1. Python deps === OK
=== 2. Env premenné === ✓
=== 3. Supabase ping === ✓ inverter_sites: ... riadkov
=== 4. Vendor health-check ===
{"vendor": "solinteg", "ok": true, "plant_count": 87}
```

Ak `ok: false`, pozri error message a doladí sa credentials / endpoint.

Potom plný plant-list:
```bash
python -m ingest.orchestrator --vendor solinteg --action plant-list
```

Skontroluj v Supabase že sa naplnil `inverter_sites`:
```sql
SELECT vendor, COUNT(*) FROM inverter_sites GROUP BY vendor;
```

## 5. Deploy na Render

### 5a. Blueprint deploy

1. **Render Dashboard** → New → **Blueprint**
2. Connect GitHub repo `energovision/monitoring`
3. Render automaticky načíta `render.yaml` a ponúkne vytvoriť 7 cron jobs
4. Pri vytvorení ťa vyzve doplniť env premenné označené `sync: false`:
   - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`
   - vendor credentials
   - `SLACK_BOT_TOKEN`, `SENDGRID_API_KEY`, atď. (voliteľné)
5. Klikni **Apply**

### 5b. Bezpečnostné prvé spustenie

Defaultne `render.yaml` má `DISPATCH_READ_ONLY=true` — nič sa fyzicky neposiela do meničov.
Po overení že telemetria sa zbiera správne 24h, manuálne prepni na `false` cez Render env.

Tiež v `VENDOR_*_ENABLED` máme defaultne enablovaných len Huawei + Solinteg. Ostatných
zapni postupne ako overíš že každý funguje samostatne.

### 5c. Sledovanie

- Render Dashboard → service → **Logs** → real-time stream
- Supabase Dashboard → Table Editor → `telemetry_5min` → vidíš ako pribúdajú riadky
- CRM (po deployi UI) `/dispatch/fleet` → live fleet status

## 6. CRM UI integrácia (Next.js / Vercel)

Skopíruj `crm_pages/*.tsx` do existujúceho `energovision-fve-os` repa:

```bash
cd /path/to/energovision-fve-os/apps/web/app
mkdir -p dispatch/fleet dispatch/site/[id] dispatch/alarms

cp /Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod/2026-05-11_FVZ_Monitoring_AI/crm_pages/dispatch_fleet.tsx \
   dispatch/fleet/page.tsx

cp /Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod/2026-05-11_FVZ_Monitoring_AI/crm_pages/dispatch_site_detail.tsx \
   "dispatch/site/[id]/page.tsx"

cp /Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod/2026-05-11_FVZ_Monitoring_AI/crm_pages/dispatch_alarms.tsx \
   dispatch/alarms/page.tsx
```

Doinštaluj závislosti:
```bash
cd apps/web
npm install recharts @supabase/supabase-js
```

Pridaj env premenné do Vercel CRM projektu:
- `NEXT_PUBLIC_SUPABASE_URL`
- `NEXT_PUBLIC_SUPABASE_ANON_KEY` (NIE service role — bezpečnostne dôležité!)

Commitni a pushni. Vercel automaticky deployne.

Skontroluj že `app.energovision.sk/dispatch/fleet` sa otvorí.

## 7. Postupné zapnutie vendorov

**Phase 1 (teraz) = HUAWEI ONLY.** Ostatných 4 vendorov (Solinteg, GoodWe,
Fronius, Sungrow) čakáme na schválenie installer API prístupu — žiadosti viď
[docs/API_ACCESS_TRACKER.md](docs/API_ACCESS_TRACKER.md) + email šablóny
v [docs/vendor_api_requests/](docs/vendor_api_requests/).

### Phase 1 — Huawei live (týždeň 1)

Huawei API už máme overené z existujúceho SPOT reactor stack (21 staníc live,
ostatných ~XX čakajú v dry_run). Postup pre rozšírenie na full monitoring:

1. V Render env potvrď `VENDOR_HUAWEI_ENABLED=true` (default)
2. V Render env potvrď že `VENDOR_SOLINTEG/GOODWE/FRONIUS/SUNGROW_ENABLED=false`
3. Sleduj logy prvých 30 min — `fve-monitoring-realtime` cron job
4. Po 1h skontroluj v Supabase:
   ```sql
   SELECT vendor, COUNT(*) FROM telemetry_5min
   WHERE ts > NOW() - INTERVAL '1 hour'
   GROUP BY vendor;
   ```
   Mal by si vidieť riadky pre Huawei
5. Po 24h: `SELECT * FROM v_fleet_status WHERE vendor='huawei' LIMIT 20;`
6. Po 48h overíš že KPI engine spočítal `performance_kpis_daily`:
   ```sql
   SELECT COUNT(*), AVG(performance_ratio)
   FROM performance_kpis_daily
   WHERE day = CURRENT_DATE - 1;
   ```

### Phase 2 — pridávanie ďalších vendorov (postupne ako prídu API)

Pre každý vendor keď príde API schválenie:

1. Pošli email zo šablóny `docs/vendor_api_requests/<vendor>_email.md`
2. Po obdržaní credentials:
   - Pridaj do `inverter_vendor_credentials` tabuľky v Supabase
   - V Render env nastav `VENDOR_X_ENABLED=true`
   - Update `docs/API_ACCESS_TRACKER.md` na 🟢 LIVE
3. Render automaticky pridá tohto vendora do ďalšieho cron behu
4. Sleduj logy + Supabase ako pri Huawei (krok 1)

### Súčasná koexistencia s existujúcim SPOT reactor

Nové monitoring stack a starý `huawei_spot.py` SPOT reactor **bežia paralelne**
— každý má vlastné cron jobs a Supabase tabuľky. Telemetria sa zapisuje aj cez
nový monitoring (do `telemetry_5min`) aj cez SPOT reactor (do `spot_prices`,
`spot_state_transitions`). Konflikty žiadne, lebo zápisy idú do iných tabuliek.

Možná konsolidácia neskôr — SPOT reactor sa môže refactorovať aby používal
canonical `VendorAdapter` z monitoring stack, ale to nie je priorita.

## 8. Bezpečnostný switch

V akomkoľvek bode môžeš všetko pauzovať:

```sql
-- Pauznúť všetky stanice
UPDATE inverter_sites SET dispatch_paused = TRUE WHERE monitoring_active = TRUE;

-- Alebo cez Render env: nastaviť DISPATCH_READ_ONLY=true
```

Ingest naďalej beží (zbiera dáta) ale žiadne `send_command` nepôjde von.

## 9. Troubleshooting

| Problém | Riešenie |
| --- | --- |
| `relation "telemetry_5min" does not exist` | Migrácia nebola spustená — kroky 2b |
| `extension "timescaledb" is not available` | Aktivuj v Supabase Extensions, alebo upgrade plan |
| Render cron padne s `Authorization: Bearer ...` 401 | Token expiroval — refresh credentials v Supabase tabuľke |
| Slack notifikácie neprichádzajú | Skontroluj `SLACK_BOT_TOKEN` má `chat:write` scope a je v channeli |
| `telemetry_15min` view je prázdny | Continuous aggregate sa updatuje s offsetom 15min — počkaj |
| Huawei vracia `APIG.0101` | Service Provider scope chýba — pošli autorizačnú žiadosť cez SmartPVMS |

## 10. Kontakt pri probléme

Logy z Render + výpis zo Supabase `alarms` tabuľky + výpis `last_seen_at` per vendor —
to je 80% diagnostických dát potrebných na akýkoľvek incident.
