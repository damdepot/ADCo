#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

CMDRunTPCC="${SCRIPTS}/tpcc.sh"
CMDRunDCo="${SCRIPTS}/dco.sh"
CMDDocker="${SCRIPTS}/docker.sh"

db_container="adcoexp-db"
db_service="pgdb"

dir_name="tpcc"
db_type="postgres"
db_name="tpcc"

# ── Per-run resource contract ──
# Fixed at 2 CPU / 8 GB for easy runs; override via the environment if needed.
CPU_CORES="${CPU_CORES:-2}"
MEMORY_GB="${MEMORY_GB:-8}"

RESULTS_DIR="${ROOT}/results/tpcc"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

settle() {
    local elapsed=0
    local n=1
    while [ "${elapsed}" -lt 60 ]; do
        n="$(docker exec -u postgres "${db_container}" psql -tA -c "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'autovacuum worker';" 2>/dev/null || echo 1)"
        [ "${n}" = "0" ] && break
        sleep 2
        elapsed=$((elapsed + 2))
    done
    sleep "${SETTLE_SECS:-5}"
}

echo "----------------->> Refreshing Docker <<-----------------"
"$CMDDocker" Down "${db_container}"
"$CMDDocker" Up "${db_container}" "${db_service}"
"$CMDDocker" WaitFor "${db_container}"

echo "----------------->> Loading TPC-C dataset <<-----------------"
PHASE=load "$CMDRunTPCC" baseline "load_${RUN_ID}.csv"
"$CMDDocker" Checkpoint "${db_container}"
settle

echo "----------------->> Baseline <<-----------------"
"$CMDRunTPCC" baseline "baseline_${RUN_ID}.csv"

echo "----------------->> DCo <<-----------------"
if ! "$CMDRunDCo" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
    echo "ERROR: DCo tuning failed; skipping post-DCo TPCC measurement and CSV." >&2
    exit 1
fi
"$CMDDocker" Restart "${db_container}"
"$CMDDocker" WaitFor "${db_container}"

# Knob settings live in postgresql.auto.conf and survive data reset, so reload only resets data, not tuning.
echo "----------------->> Reloading clean dataset for optimized arm <<-----------------"
PHASE=load "$CMDRunTPCC" baseline "load_clean_${RUN_ID}.csv"
"$CMDDocker" Checkpoint "${db_container}"
settle

echo "----------------->> Optimized <<-----------------"
"$CMDRunTPCC" baseline "dco_${RUN_ID}.csv"