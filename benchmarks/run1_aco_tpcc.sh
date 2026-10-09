#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

source "${SCRIPTS}/bench_lib.sh"

CMDRunACo="${SCRIPTS}/aco.sh"

dir_name="tpcc"
db_type="postgres"
db_name="tpcc"

RESULTS_DIR="${ROOT}/results/tpcc"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

# Single-writer: never Down/Restart the DB under a concurrent run.
bench_lock "${ROOT}"

# Pristine dataset snapshot: loaded once, restored per arm in seconds.
SEED_DB="${db_name}_seed"

run_aco_tool() {
    "${CMDRunACo}" "${dir_name}" "${db_type}" "${db_name}"
}

# Load the dataset exactly once, then snapshot it. Each arm still measures
# from identically restored data with cold buffers, so the delta is not
# confounded by cold-vs-warm host page cache.
echo "----------------->> Seed (load once + snapshot) <<-----------------"
bench_seed_once "${db_name}" "${SEED_DB}"

echo "----------------->> Baseline <<-----------------"
bench_arm_from_seed "baseline" "baseline" "${db_name}" "${SEED_DB}"

# ACo is rewrite-only: it does not tune the DB.
echo "----------------->> ACo <<-----------------"
bench_arm_from_seed "aco" "tpcc_aco" "${db_name}" "${SEED_DB}" run_aco_tool
