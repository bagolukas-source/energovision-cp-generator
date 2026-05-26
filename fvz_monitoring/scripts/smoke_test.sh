#!/usr/bin/env bash
#
# Smoke test — overí že:
#  1. Python deps sú nainštalované
#  2. .env je vyplnený
#  3. Supabase pripojenie funguje
#  4. Aspoň jeden vendor API odpovedá na health-check
#
# Použitie:
#   ./scripts/smoke_test.sh           # všetci enablovaní vendori
#   ./scripts/smoke_test.sh solinteg  # len jeden vendor

set -euo pipefail

VENDOR="${1:-all}"

# Load .env
if [ -f .env ]; then
    set -a; source .env; set +a
fi

echo "=== 1. Python deps ==="
python3 -c "import requests, supabase, tenacity; print('OK')" || {
    echo "Chýbajú deps. Spusti: pip install -r requirements.txt"
    exit 1
}

echo ""
echo "=== 2. Env premenné ==="
for var in SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY; do
    if [ -z "${!var:-}" ]; then
        echo "  ✗ $var nie je nastavené"
        exit 1
    fi
    echo "  ✓ $var nastavené"
done

echo ""
echo "=== 3. Supabase ping ==="
python3 -c "
import os
from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
res = sb.table('inverter_sites').select('id', count='exact').limit(1).execute()
print(f'  ✓ inverter_sites: {res.count} riadkov')
"

echo ""
echo "=== 4. Vendor health-check (vendor=$VENDOR) ==="
if [ "$VENDOR" = "all" ]; then
    python3 -m ingest.orchestrator --health-check
else
    python3 -c "
import os, json
from ingest.orchestrator import make_adapter, supabase_credentials_loader
adapter = make_adapter('$VENDOR', credentials_loader=supabase_credentials_loader)
print(json.dumps(adapter.health_check(), indent=2))
"
fi

echo ""
echo "✓ Smoke test prešiel."
