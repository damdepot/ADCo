#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

CMDRunTPCC="${SCRIPTS}/tpcc.sh"
CMDRunACo="${SCRIPTS}/aco.sh"
CMDDocker="${SCRIPTS}/docker.sh"

db_container="adcoexp-db"
db_service="pgdb"

dir_name="tpcc"
db_type="postgres"
db_name="tpcc"

# ACo is rewrite-only and does not tune the database, so CPU_CORES/MEMORY_GB
# are not required for this run.
RESULTS_DIR="${ROOT}/results/tpcc"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

echo "----------------->> Refreshing Docker <<-----------------"
"$CMDDocker" Down "${db_container}"
"$CMDDocker" Up "${db_container}" "${db_service}"
"$CMDDocker" WaitFor "${db_container}"

echo "----------------->> ACo <<-----------------"
"$CMDRunACo" "${dir_name}" "${db_type}" "${db_name}"

echo "----------------->> Baseline <<-----------------"
"$CMDRunTPCC" baseline "baseline_${RUN_ID}.csv"

echo "----------------->> Optimized <<-----------------"
"$CMDRunTPCC" tpcc_aco "aco_${RUN_ID}.csv"
