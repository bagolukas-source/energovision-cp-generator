#!/usr/bin/env bash
#
# Lokálny dev — spustí jeden vendor pipeline raz, na overenie.
# Použiteľné pred deployom na Render.
#
# Použitie:
#   ./scripts/local_dev.sh solinteg realtime
#   ./scripts/local_dev.sh huawei plant-list
#   ./scripts/local_dev.sh solinteg alarms

set -euo pipefail

VENDOR="${1:-solinteg}"
ACTION="${2:-plant-list}"

if [ -f .env ]; then
    set -a; source .env; set +a
fi

echo "Spúšťam $VENDOR / $ACTION ..."
python3 -m ingest.orchestrator --vendor "$VENDOR" --action "$ACTION"
