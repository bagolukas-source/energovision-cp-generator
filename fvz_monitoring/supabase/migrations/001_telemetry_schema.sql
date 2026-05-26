-- =============================================================================
-- FVZ Monitoring AI — Schema migrácia #001
-- TimescaleDB + canonical schema pre 400+ FVE inštalácií
-- =============================================================================
-- Spúšťať priamo cez `psql $SUPABASE_DB_URL -f 001_telemetry_schema.sql`
-- ALEBO cez Supabase Dashboard → SQL Editor.
-- Idempotentné — bezpečne sa dá pustiť viackrát (IF NOT EXISTS všade).

-- =============================================================================
-- 1. Extensions
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS pgcrypto;       -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS postgis;        -- na geo lokácie staníc

-- =============================================================================
-- 2. Master data — rozšírenie existujúceho inverter_sites
-- =============================================================================
-- inverter_sites už existuje (z SPOT reactor migrácie). Pridáme stĺpce pre monitoring.

ALTER TABLE IF EXISTS inverter_sites
    ADD COLUMN IF NOT EXISTS customer_id uuid REFERENCES customers(id),
    ADD COLUMN IF NOT EXISTS dispatch_paused boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS kw_dc_nominal numeric(8,3),       -- inštalovaný DC výkon
    ADD COLUMN IF NOT EXISTS kw_ac_nominal numeric(8,3),       -- AC výkon meniča
    ADD COLUMN IF NOT EXISTS battery_kwh_nominal numeric(8,3), -- batéria kapacita
    ADD COLUMN IF NOT EXISTS tilt_deg numeric(5,2),            -- sklon strechy
    ADD COLUMN IF NOT EXISTS azimuth_deg numeric(5,2),         -- 0=N, 90=E, 180=S, 270=W
    ADD COLUMN IF NOT EXISTS commissioning_date date,
    ADD COLUMN IF NOT EXISTS lat double precision,
    ADD COLUMN IF NOT EXISTS lon double precision,
    ADD COLUMN IF NOT EXISTS location_geom geography(POINT, 4326),
    ADD COLUMN IF NOT EXISTS distribution_area text,           -- 'ZSE','SSE','VSD'
    ADD COLUMN IF NOT EXISTS zero_export_required boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS zero_export_limit_kw numeric(6,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS monitoring_active boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS last_seen_at timestamptz,
    ADD COLUMN IF NOT EXISTS health_status text                -- 'ok'|'warn'|'alarm'|'offline'
        CHECK (health_status IN ('ok','warn','alarm','offline','unknown')) DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS idx_inverter_sites_customer ON inverter_sites(customer_id);
CREATE INDEX IF NOT EXISTS idx_inverter_sites_health   ON inverter_sites(health_status) WHERE monitoring_active;
CREATE INDEX IF NOT EXISTS idx_inverter_sites_location ON inverter_sites USING GIST(location_geom);

-- =============================================================================
-- 3. Customer entity (ak ešte neexistuje)
-- =============================================================================
CREATE TABLE IF NOT EXISTS customers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name   text,
    contact_name    text,
    email           text UNIQUE,
    phone           text,
    address         text,
    is_b2b          boolean NOT NULL DEFAULT false,
    portal_enabled  boolean NOT NULL DEFAULT true,
    portal_token    text UNIQUE,            -- magic-link token pre token-based prístup
    portal_user_id  uuid REFERENCES auth.users(id),  -- ak má heslo (Supabase Auth)
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- =============================================================================
-- 4. Telemetry hypertable (5min raw)
-- =============================================================================
CREATE TABLE IF NOT EXISTS telemetry_5min (
    site_id              uuid NOT NULL REFERENCES inverter_sites(id) ON DELETE CASCADE,
    ts                   timestamptz NOT NULL,
    -- AC strana
    ac_power_kw          numeric(8,3),     -- okamžitý AC výkon
    ac_energy_today_kwh  numeric(10,3),    -- energy counter dnes
    ac_energy_total_kwh  numeric(12,1),    -- lifetime
    voltage_l1_v         numeric(6,2),
    voltage_l2_v         numeric(6,2),
    voltage_l3_v         numeric(6,2),
    frequency_hz         numeric(5,3),
    -- DC strana / MPPT (JSONB pre variabilný počet stringov)
    dc_strings           jsonb,            -- [{mppt:1, voltage_v:680, current_a:9.3, power_w:6324}, ...]
    -- Batéria
    battery_soc_pct      numeric(5,2),
    battery_soh_pct      numeric(5,2),
    battery_power_kw     numeric(8,3),     -- + nabíja, − vybíja
    battery_temp_c       numeric(5,2),
    -- Smartmeter (export / import)
    grid_export_kw       numeric(8,3),     -- okamžitý export
    grid_import_kw       numeric(8,3),     -- okamžitý import
    grid_export_kwh_today numeric(10,3),
    grid_import_kwh_today numeric(10,3),
    self_consumption_kw  numeric(8,3),     -- spotreba domu = produkcia − export − vbatériu
    -- Stav meniča
    inverter_temp_c      numeric(5,2),
    inverter_status      text,             -- 'running'|'standby'|'fault'|'comm_lost'
    -- Vendor špecifické raw payload pre debug / audit
    raw_payload          jsonb,
    -- Metadáta
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, ts)
);

SELECT create_hypertable('telemetry_5min', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

-- Kompresia po 7 dňoch — pre 400 staníc × 288 záznamov/deň
ALTER TABLE telemetry_5min SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'site_id',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('telemetry_5min', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('telemetry_5min', INTERVAL '5 years', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_telemetry_5min_site_ts ON telemetry_5min (site_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_5min_status  ON telemetry_5min (inverter_status, ts DESC)
    WHERE inverter_status IS NOT NULL;

-- =============================================================================
-- 5. Continuous aggregates (15min, hodina, deň)
-- =============================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_15min
WITH (timescaledb.continuous) AS
SELECT
    site_id,
    time_bucket(INTERVAL '15 minutes', ts) AS ts,
    AVG(ac_power_kw)            AS ac_power_kw_avg,
    MAX(ac_power_kw)            AS ac_power_kw_max,
    SUM(ac_power_kw) * 0.0833   AS ac_energy_kwh,   -- 5min×12 -> 1h, /12 = kWh per 5min
    AVG(battery_soc_pct)        AS battery_soc_avg,
    SUM(grid_export_kw) * 0.0833 AS grid_export_kwh,
    SUM(grid_import_kw) * 0.0833 AS grid_import_kwh,
    AVG(voltage_l1_v)           AS voltage_l1_avg,
    COUNT(*)                    AS sample_count
FROM telemetry_5min
GROUP BY site_id, time_bucket(INTERVAL '15 minutes', ts)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('telemetry_15min',
    start_offset      => INTERVAL '7 days',
    end_offset        => INTERVAL '15 minutes',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists     => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_hourly
WITH (timescaledb.continuous) AS
SELECT
    site_id,
    time_bucket(INTERVAL '1 hour', ts) AS ts,
    AVG(ac_power_kw_avg)        AS ac_power_kw_avg,
    MAX(ac_power_kw_max)        AS ac_power_kw_max,
    SUM(ac_energy_kwh)          AS ac_energy_kwh,
    AVG(battery_soc_avg)        AS battery_soc_avg,
    SUM(grid_export_kwh)        AS grid_export_kwh,
    SUM(grid_import_kwh)        AS grid_import_kwh
FROM telemetry_15min
GROUP BY site_id, time_bucket(INTERVAL '1 hour', ts)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('telemetry_hourly',
    start_offset      => INTERVAL '30 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_daily
WITH (timescaledb.continuous) AS
SELECT
    site_id,
    time_bucket(INTERVAL '1 day', ts) AS ts,
    SUM(ac_energy_kwh)          AS ac_energy_kwh,
    MAX(ac_power_kw_max)        AS ac_power_kw_peak,
    AVG(battery_soc_avg)        AS battery_soc_avg,
    SUM(grid_export_kwh)        AS grid_export_kwh,
    SUM(grid_import_kwh)        AS grid_import_kwh,
    COUNT(*)                    AS hours_of_data
FROM telemetry_hourly
GROUP BY site_id, time_bucket(INTERVAL '1 day', ts)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('telemetry_daily',
    start_offset      => INTERVAL '1 year',
    end_offset        => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists     => TRUE);

-- =============================================================================
-- 6. PVGIS baseline — referenčný teoretický výnos
-- =============================================================================
-- PVGIS API stiahne hodinový profil typického roka pre daný bod (lat/lon)
-- + sklon + azimut. Pre 400 staníc × 8760 hodín = 3.5M riadkov, manažovateľné.

CREATE TABLE IF NOT EXISTS pvgis_baseline (
    site_id        uuid NOT NULL REFERENCES inverter_sites(id) ON DELETE CASCADE,
    hour_of_year   smallint NOT NULL CHECK (hour_of_year BETWEEN 0 AND 8783),
    -- hour_of_year = (month-1)*744 + (day-1)*24 + hour (anti-prestupný rok zjednodušene)
    month          smallint NOT NULL CHECK (month BETWEEN 1 AND 12),
    day_of_month   smallint NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
    hour_of_day    smallint NOT NULL CHECK (hour_of_day BETWEEN 0 AND 23),
    irradiance_w_m2 numeric(6,2),
    expected_power_kw numeric(7,3),  -- pre nominálny kWp ako 1 kWp; násobíme kw_dc_nominal
    ambient_temp_c numeric(5,2),
    fetched_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, hour_of_year)
);

CREATE INDEX IF NOT EXISTS idx_pvgis_site_month_hour
    ON pvgis_baseline(site_id, month, hour_of_day);

-- =============================================================================
-- 7. KPI agregácie (PR, yield, availability)
-- =============================================================================
CREATE TABLE IF NOT EXISTS performance_kpis_daily (
    site_id              uuid NOT NULL REFERENCES inverter_sites(id) ON DELETE CASCADE,
    day                  date NOT NULL,
    energy_kwh           numeric(10,2),
    expected_kwh         numeric(10,2),     -- z PVGIS pre tento deň
    performance_ratio    numeric(5,3),      -- energy / expected, 0–1.2
    specific_yield       numeric(6,3),      -- kWh / kWp
    availability_pct     numeric(5,2),      -- % času keď bola stanica online cez deň
    peak_power_kw        numeric(8,3),
    capacity_factor_pct  numeric(5,2),
    self_consumption_pct numeric(5,2),
    grid_independence_pct numeric(5,2),
    co2_avoided_kg       numeric(8,2),
    cohort_pr_median     numeric(5,3),       -- ako sa darí kohorte (cross-site comparison)
    cohort_z_score       numeric(6,3),       -- |z| > 2 = anomália
    calculated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, day)
);

CREATE INDEX IF NOT EXISTS idx_kpis_day        ON performance_kpis_daily(day DESC);
CREATE INDEX IF NOT EXISTS idx_kpis_anomalies  ON performance_kpis_daily(cohort_z_score)
    WHERE ABS(cohort_z_score) > 2;

-- =============================================================================
-- 8. Alarms canonical
-- =============================================================================
CREATE TABLE IF NOT EXISTS alarms (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id             uuid REFERENCES inverter_sites(id) ON DELETE CASCADE,
    vendor              text NOT NULL CHECK (vendor IN ('huawei','solinteg','goodwe','fronius','sungrow','internal')),
    vendor_alarm_id     text,                                -- ak ide o vendor alarm, jeho ID
    severity            text NOT NULL CHECK (severity IN ('info','warn','minor','major','critical')),
    category            text NOT NULL,                       -- 'comm_lost','zero_export_breach','underperformance','string_fault','overtemp','grid_anomaly','battery_fault','cohort_outlier','unknown'
    title               text NOT NULL,
    description         text,
    detected_at         timestamptz NOT NULL,
    resolved_at         timestamptz,
    acknowledged_at     timestamptz,
    acknowledged_by     uuid REFERENCES auth.users(id),
    assigned_to         uuid REFERENCES auth.users(id),
    ticket_id           text,                                -- napr. Raynet ticket
    deduplicated_into   uuid REFERENCES alarms(id),          -- ak je child duplikátu
    root_cause          text,
    root_cause_confidence numeric(4,3),                      -- 0–1, ML score
    auto_actions_taken  jsonb,                               -- napr. [{action:'notify_slack',ok:true,ts:...}]
    metadata            jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alarms_open
    ON alarms(detected_at DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alarms_site_severity
    ON alarms(site_id, severity, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_alarms_category
    ON alarms(category, detected_at DESC);

-- =============================================================================
-- 9. Anomaly predictions (ML output)
-- =============================================================================
CREATE TABLE IF NOT EXISTS anomaly_predictions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         uuid NOT NULL REFERENCES inverter_sites(id) ON DELETE CASCADE,
    predicted_at    timestamptz NOT NULL DEFAULT now(),
    model_version   text NOT NULL,
    prediction_type text NOT NULL,           -- 'underperformance','string_fault','degradation','imminent_failure'
    confidence      numeric(4,3) NOT NULL,   -- 0–1
    horizon_days    integer,                 -- pre predictive maintenance
    features        jsonb,                   -- snapshot featurov
    classification  text,
    recommendation  text
);

CREATE INDEX IF NOT EXISTS idx_anomaly_site_ts ON anomaly_predictions(site_id, predicted_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_high_conf
    ON anomaly_predictions(confidence DESC, predicted_at DESC)
    WHERE confidence >= 0.7;

-- =============================================================================
-- 10. RLS — Row Level Security
-- =============================================================================
-- Trojvrstvový model:
--   1. service_role (Render workers) — full access
--   2. installer / dispatcher (auth.users s rolou 'installer'|'dispatcher') — všetky stanice
--   3. customer (auth.users.id = customers.portal_user_id) — len jeho stanice

ALTER TABLE telemetry_5min            ENABLE ROW LEVEL SECURITY;
ALTER TABLE performance_kpis_daily    ENABLE ROW LEVEL SECURITY;
ALTER TABLE alarms                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE pvgis_baseline            ENABLE ROW LEVEL SECURITY;
ALTER TABLE anomaly_predictions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers                 ENABLE ROW LEVEL SECURITY;

-- Service role — full access pre Render workers
DO $$ BEGIN
    CREATE POLICY service_role_all ON telemetry_5min FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY service_role_all ON performance_kpis_daily FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY service_role_all ON alarms FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY service_role_all ON pvgis_baseline FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY service_role_all ON anomaly_predictions FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY service_role_all ON customers FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Customer-side: cez auth.uid() = customers.portal_user_id, JOIN-cez inverter_sites
-- (predpokladá funkciu auth.uid() — Supabase ju má)
DO $$ BEGIN
    CREATE POLICY customer_own_telemetry ON telemetry_5min
        FOR SELECT TO authenticated
        USING (
            site_id IN (
                SELECT s.id FROM inverter_sites s
                JOIN customers c ON c.id = s.customer_id
                WHERE c.portal_user_id = auth.uid()
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY customer_own_kpis ON performance_kpis_daily
        FOR SELECT TO authenticated
        USING (
            site_id IN (
                SELECT s.id FROM inverter_sites s
                JOIN customers c ON c.id = s.customer_id
                WHERE c.portal_user_id = auth.uid()
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY customer_own_alarms ON alarms
        FOR SELECT TO authenticated
        USING (
            site_id IN (
                SELECT s.id FROM inverter_sites s
                JOIN customers c ON c.id = s.customer_id
                WHERE c.portal_user_id = auth.uid()
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =============================================================================
-- 11. Pomocné funkcie
-- =============================================================================

-- Vráti aktuálny health status stanice na základe posledných dát
CREATE OR REPLACE FUNCTION compute_site_health(p_site_id uuid)
RETURNS text AS $$
DECLARE
    last_ts timestamptz;
    open_critical int;
    open_major int;
BEGIN
    SELECT MAX(ts) INTO last_ts FROM telemetry_5min WHERE site_id = p_site_id;
    IF last_ts IS NULL OR last_ts < now() - INTERVAL '30 minutes' THEN
        RETURN 'offline';
    END IF;
    SELECT COUNT(*) INTO open_critical FROM alarms
        WHERE site_id = p_site_id AND resolved_at IS NULL AND severity = 'critical';
    IF open_critical > 0 THEN RETURN 'alarm'; END IF;
    SELECT COUNT(*) INTO open_major FROM alarms
        WHERE site_id = p_site_id AND resolved_at IS NULL AND severity IN ('major','minor');
    IF open_major > 0 THEN RETURN 'warn'; END IF;
    RETURN 'ok';
END;
$$ LANGUAGE plpgsql STABLE;

-- =============================================================================
-- 12. View pre dispečing UI (rýchle čítanie fleet view)
-- =============================================================================
CREATE OR REPLACE VIEW v_fleet_status AS
SELECT
    s.id,
    s.site_name,
    s.vendor,
    s.kw_dc_nominal,
    s.battery_kwh_nominal,
    s.lat,
    s.lon,
    s.distribution_area,
    s.zero_export_required,
    s.last_seen_at,
    s.health_status,
    c.contact_name AS customer_name,
    c.email        AS customer_email,
    (
        SELECT energy_kwh FROM performance_kpis_daily
        WHERE site_id = s.id AND day = CURRENT_DATE - INTERVAL '1 day'
    ) AS energy_kwh_yesterday,
    (
        SELECT performance_ratio FROM performance_kpis_daily
        WHERE site_id = s.id AND day = CURRENT_DATE - INTERVAL '1 day'
    ) AS pr_yesterday,
    (
        SELECT COUNT(*) FROM alarms
        WHERE site_id = s.id AND resolved_at IS NULL
    ) AS open_alarms_count
FROM inverter_sites s
LEFT JOIN customers c ON c.id = s.customer_id
WHERE s.monitoring_active;

-- =============================================================================
-- Hotovo. Skontroluj výsledok:
--   SELECT * FROM timescaledb_information.hypertables WHERE hypertable_name = 'telemetry_5min';
--   SELECT * FROM timescaledb_information.continuous_aggregates;
-- =============================================================================
