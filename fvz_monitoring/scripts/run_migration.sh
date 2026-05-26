#!/usr/bin/env bash
#
# Spustí Supabase migráciu lokálne (alebo z Render shell-u).
# Vyžaduje psql + SUPABASE_DB_URL v env.
#
# Použitie:
#   export SUPABASE_DB_URL="postgresql://postgres:[PASS]@db.xxx.supabase.co:5432/postgres"
#   ./scripts/run_migration.sh
#
# ALEBO inline:
#   SUPABASE_DB_URL="..." ./scripts/run_migration.sh

set -euo pipefail

if [ -z "${SUPABASE_DB_URL:-}" ]; then
    echo "ERROR: SUPABASE_DB_URL nie je nastavené."
    echo "Skopíruj DB URL zo Supabase Dashboard → Settings → Database → Connection string (URI)."
    exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: psql nie je nainštalované."
    echo "Mac:    brew install libpq && brew link --force libpq"
    echo "Ubuntu: apt-get install postgresql-client"
    exit 1
fi

MIGRATION="supabase/migrations/001_telemetry_schema.sql"

if [ ! -f "$MIGRATION" ]; then
    echo "ERROR: $MIGRATION nenájdený. Spúšťaj z root priečinka projektu."
    exit 1
fi

echo "→ Pingnem databázu..."
psql "$SUPABASE_DB_URL" -c "SELECT current_database(), current_user, version();" || {
    echo "ERROR: pripojenie zlyhalo. Skontroluj SUPABASE_DB_URL."
    exit 1
}

echo ""
echo "→ Overujem TimescaleDB extension dostupnosť..."
TS_AVAILABLE=$(psql "$SUPABASE_DB_URL" -tAc "SELECT 1 FROM pg_available_extensions WHERE name='timescaledb';" || echo "0")
if [ "$TS_AVAILABLE" != "1" ]; then
    echo "VAROVANIE: TimescaleDB nie je dostupný v tvojom Supabase plane."
    echo "Buď ho aktivuj v Supabase Dashboard → Database → Extensions,"
    echo "alebo upgradni plan. Migrácia bude pokračovať a možno padne."
    read -p "Pokračovať aj tak? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || exit 1
fi

echo ""
echo "→ Spúšťam migráciu $MIGRATION ..."
psql "$SUPABASE_DB_URL" -f "$MIGRATION" -v ON_ERROR_STOP=1

echo ""
echo "→ Overujem výsledok..."
psql "$SUPABASE_DB_URL" <<'SQL'
SELECT 'hypertables' AS check_type, COUNT(*)::text AS result
  FROM timescaledb_information.hypertables WHERE hypertable_name = 'telemetry_5min'
UNION ALL
SELECT 'continuous_aggregates', COUNT(*)::text
  FROM timescaledb_information.continuous_aggregates
UNION ALL
SELECT 'new_tables', COUNT(*)::text
  FROM information_schema.tables
  WHERE table_schema='public' AND table_name IN ('pvgis_baseline','performance_kpis_daily','alarms','anomaly_predictions','customers');
SQL

echo ""
echo "✓ Migrácia hotová."
echo ""
echo "Ďalšie kroky:"
echo "  1. Vyplň .env zo .env.example"
echo "  2. ./scripts/smoke_test.sh         (overí prihlásenie do vendor API)"
echo "  3. python -m ingest.orchestrator --vendor solinteg --action plant-list"
