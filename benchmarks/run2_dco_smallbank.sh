#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

export BENCH_CMDRUNTPCC="${SCRIPTS}/smallbank.sh"
export ACCOUNTS="${ACCOUNTS:-1000000}"

source "${SCRIPTS}/bench_lib.sh"

CMDRunDCo="${SCRIPTS}/dco.sh"

dir_name="smallbank"
db_type="postgres"
db_name="smallbank"

RESULTS_DIR="${ROOT}/results/smallbank"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

# Pristine dataset snapshot: loaded once, restored per arm in seconds.
# Knob settings live in postgresql.auto.conf and survive the restore, so the
# restore resets data only, not tuning.
SEED_DB="${db_name}_seed"

run_dco_tool() {
    if ! "${CMDRunDCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: DCo tuning failed; skipping post-DCo SmallBank measurement and CSV." >&2
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

# DCo's optimized arm runs the original app; only the knobs change.
echo "----------------->> DCo <<-----------------"
bench_arm_from_seed "dco" "baseline" "${db_name}" "${SEED_DB}" run_dco_tool
