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

echo "----------------->> Refreshing Docker <<-----------------"
"$CMDDocker" Down "${db_container}"
"$CMDDocker" Up "${db_container}" "${db_service}"
"$CMDDocker" WaitFor "${db_container}"

echo "----------------->> Baseline <<-----------------"
"$CMDRunTPCC" baseline "baseline_${RUN_ID}.csv"

echo "----------------->> DCo <<-----------------"
if ! "$CMDRunDCo" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
    echo "ERROR: DCo tuning failed; skipping post-DCo TPCC measurement and CSV." >&2
    exit 1
fi
"$CMDDocker" Restart "${db_container}"
"$CMDDocker" WaitFor "${db_container}"

echo "----------------->> Optimized <<-----------------"
"$CMDRunTPCC" baseline "dco_${RUN_ID}.csv"
