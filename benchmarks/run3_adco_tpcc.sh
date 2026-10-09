#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

source "${SCRIPTS}/bench_lib.sh"

CMDRunADCo="${SCRIPTS}/adco.sh"

dir_name="tpcc"
db_type="postgres"
db_name="tpcc"

RESULTS_DIR="${ROOT}/results/tpcc"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

# Single-writer: never Down/Restart the DB under a concurrent run.
bench_lock "${ROOT}"

# Pristine dataset snapshot: loaded once, restored per arm in seconds.
# Knob settings live in postgresql.auto.conf and survive the restore, so the
# restore resets data only, not tuning.
SEED_DB="${db_name}_seed"

run_adco_tool() {
    if ! "${CMDRunADCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: ADCo tuning failed; skipping post-ADCo TPCC measurement and CSV." >&2
        return 1
    fi
}

# Load the dataset exactly once, then snapshot it. Each arm still measures
# from identically restored data with cold buffers, so the delta is not
# confounded by cold-vs-warm host page cache.
echo "----------------->> Seed (load once + snapshot) <<-----------------"
bench_seed_once "${db_name}" "${SEED_DB}"

echo "----------------->> Baseline <<-----------------"
bench_arm_from_seed "baseline" "baseline" "${db_name}" "${SEED_DB}"

# ADCo rewrites the app and tunes knobs.
echo "----------------->> ADCo <<-----------------"
bench_arm_from_seed "adco" "tpcc_adco" "${db_name}" "${SEED_DB}" run_adco_tool
