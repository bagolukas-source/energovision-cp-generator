#!/usr/bin/env bash
#
# FVZ Monitoring AI — one-command deploy assistant
#
# Spustí všetko čo sa dá automatizovať z Macu Lukáša:
#  1. Prerequisites check (git, gh CLI, psql, python)
#  2. Python venv + deps
#  3. Supabase migrácia (potrebuje SUPABASE_DB_URL v .env)
#  4. Huawei smoke test (potrebuje credentials v .env alebo v Supabase)
#  5. Git init + commit + push (potrebuje gh CLI prihlásený)
#  6. Render Blueprint guide (otvorí URL — Lukáš klikne Deploy)
#
# Použitie:
#     cd "/Users/lukasbago/Documents/Claude/Projects/Obchod/Obchod/2026-05-11_FVZ_Monitoring_AI"
#     ./scripts/one_command_deploy.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

color() { printf "\033[%sm%s\033[0m\n" "$1" "$2"; }
green()  { color "1;32" "$1"; }
yellow() { color "1;33" "$1"; }
red()    { color "1;31" "$1"; }
header() { echo ""; color "1;36" "=== $1 ==="; }

# =============================================================================
# 1. Prerequisites
# =============================================================================
header "1. Skontrolujem prerequisites"

MISSING=()
command -v git >/dev/null || MISSING+=("git")
command -v python3 >/dev/null || MISSING+=("python3")
command -v psql >/dev/null || MISSING+=("psql (brew install libpq && brew link --force libpq)")
command -v gh >/dev/null || MISSING+=("gh CLI (brew install gh)")

if [ ${#MISSING[@]} -gt 0 ]; then
    red "Chýbajú nástroje:"
    for m in "${MISSING[@]}"; do echo "  - $m"; done
    echo ""
    echo "Nainštaluj ich a spusti skript znova."
    exit 1
fi
green "Všetky nástroje nainštalované."

# =============================================================================
# 2. .env check
# =============================================================================
header "2. Skontrolujem .env"

if [ ! -f .env ]; then
    yellow "Nemáš .env — kopírujem .env.example..."
    cp .env.example .env
    red "→ Otvor .env a doplň aspoň: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,"
    red "  SUPABASE_DB_URL, HUAWEI_USERNAME, HUAWEI_PASS"
    red "Potom spusti znova: ./scripts/one_command_deploy.sh"
    exit 1
fi

# Načítaj .env
set -a; source .env; set +a

REQUIRED_VARS=(SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY SUPABASE_DB_URL HUAWEI_USERNAME HUAWEI_PASS)
MISSING_VARS=()
for v in "${REQUIRED_VARS[@]}"; do
    [ -z "${!v:-}" ] && MISSING_VARS+=("$v")
done

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    red "V .env chýbajú premenné:"
    for v in "${MISSING_VARS[@]}"; do echo "  - $v"; done
    exit 1
fi
green "Všetky kritické env premenné nastavené."

# =============================================================================
# 3. Python venv + deps
# =============================================================================
header "3. Python virtual env + dependencies"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    green "Vytvorený .venv"
fi
source .venv/bin/activate
pip install --quiet -r requirements.txt
green "Python deps nainštalované."

# =============================================================================
# 4. Supabase migrácia
# =============================================================================
header "4. Spúšťam Supabase migráciu"

read -p "Pokračovať s migráciou? (môže pridať stĺpce k existujúcim tabuľkám) [y/N] " yn
if [[ "$yn" =~ ^[Yy]$ ]]; then
    ./scripts/run_migration.sh
    green "Migrácia hotová."
else
    yellow "Preskakujem migráciu."
fi

# =============================================================================
# 5. Huawei smoke test
# =============================================================================
header "5. Huawei smoke test"

if [ "${VENDOR_HUAWEI_ENABLED:-true}" = "true" ]; then
    python -m ingest.orchestrator --vendor huawei --action plant-list 2>&1 | tail -20 || {
        red "Huawei health-check zlyhal. Skontroluj HUAWEI_USERNAME / HUAWEI_PASS."
        yellow "Toto NIE je fatal — môžeš pokračovať s git pushom a doladiť neskôr."
    }
else
    yellow "Huawei je VENDOR_HUAWEI_ENABLED=false — preskakujem."
fi

# =============================================================================
# 6. Git init + commit
# =============================================================================
header "6. Git repo"

# Energovision Vercel commit-author protection: musíme commitovať pod bago.lukas@gmail.com
git config user.email "bago.lukas@gmail.com"
git config user.name "bagolukas-source"
green "Git author nastavený na bago.lukas@gmail.com (Vercel safe)"

if [ ! -d .git ]; then
    git init -b main
    green "git init"
fi

git add .
if git diff --cached --quiet; then
    yellow "Nič nové na commit."
else
    git commit -m "FVZ Monitoring AI — Phase 1 (Huawei-only) skeleton

- TimescaleDB schema (telemetry_5min hypertable + continuous aggregates)
- VendorAdapter base + 5 vendor adaptérov (Huawei live, ostatní stubs)
- Ingest orchestrátor s Supabase persistence
- Dispatch engine: KPI, zero-export, alarmy, anomaly detector
- CRM dispatch UI (3 Next.js pages)
- Render Blueprint pre 7 cron jobov
- DEPLOY.md + API access tracker pre 4 čakajúcich vendorov"
    green "Commit vytvorený."
fi

# =============================================================================
# 7. GitHub push
# =============================================================================
header "7. GitHub push"

REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [ -z "$REMOTE" ]; then
    yellow "Nemáš git remote nastavený."
    read -p "Vytvor nové GitHub repo? (potrebuje gh CLI prihlásený) [y/N] " yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        read -p "Názov repa (default: energovision/monitoring): " REPO
        REPO=${REPO:-energovision/monitoring}
        gh repo create "$REPO" --private --source=. --remote=origin --push
        green "Repo vytvorené: $REPO"
    else
        yellow "Preskakujem GitHub push — spusti ručne:"
        echo "    gh repo create energovision/monitoring --private --source=. --remote=origin --push"
    fi
else
    git push origin main
    green "Pushnuté do $REMOTE"
fi

# =============================================================================
# 8. Render Blueprint deploy guide
# =============================================================================
header "8. Render Blueprint deploy"

cat <<EOF

$(yellow "Teraz musíš spraviť ručne (1-click deploy cez Render UI):")

1. Otvor: $(green "https://dashboard.render.com/blueprints")
2. Klikni: $(green "New Blueprint Instance")
3. Vyber GitHub repo: $(green "energovision/monitoring")
4. Render automaticky načíta render.yaml a vytvorí 7 cron jobov
5. Pri vytvorení ťa vyzve doplniť env premenné označené 'sync: false':
EOF

# Vypíš ktoré premenné treba zadať v Render
echo ""
echo "   Tieto skopíruj z tvojho .env:"
for v in "${REQUIRED_VARS[@]}"; do
    val="${!v:0:8}..."
    [ ${#v} -gt 5 ] && echo "     - $v = $val"
done

cat <<EOF

6. Klikni $(green "Apply") — Render začne deploy
7. Po 5 min skontroluj logy: $(green "https://dashboard.render.com/")
   service $(green "fve-monitoring-realtime") → Logs

$(yellow "Po overení (24h tečenia dát) zostáva už len:")

A) CRM UI deploy do energovision-fve-os repa:
   - Skopíruj crm_pages/*.tsx do apps/web/app/dispatch/
   - npm install recharts @supabase/supabase-js
   - git commit + push → Vercel auto-deploy

B) Pošli 4 emaily zo $(green "docs/vendor_api_requests/")
   - Solinteg, GoodWe, Fronius, Sungrow

C) Po obdržaní credentials → update $(green "API_ACCESS_TRACKER.md")
   + Render env: VENDOR_X_ENABLED=true

EOF

green "✓ Všetko čo sa dalo automatizovať je hotové."
