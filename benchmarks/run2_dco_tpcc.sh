#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

source "${SCRIPTS}/bench_lib.sh"

CMDRunDCo="${SCRIPTS}/dco.sh"

dir_name="tpcc"
db_type="postgres"
db_name="tpcc"

RESULTS_DIR="${ROOT}/results/tpcc"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

# Pristine dataset snapshot: loaded once, restored per arm in seconds.
# Knob settings live in postgresql.auto.conf and survive the restore, so the
# restore resets data only, not tuning.
SEED_DB="${db_name}_seed"

run_dco_tool() {
    if ! "${CMDRunDCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: DCo tuning failed; skipping post-DCo TPCC measurement and CSV." >&2
        return 1
    fi
}

# Load the dataset exactly once, then snapshot it. Both arms restore from the
# snapshot instead of reloading, so deltas are not confounded by loader
# variance and the run is minutes shorter. Each arm still measures from
# identically restored data with cold buffers.
echo "----------------->> Seed (load once + snapshot) <<-----------------"
bench_seed_once "${db_name}" "${SEED_DB}"

echo "----------------->> Baseline <<-----------------"
bench_arm_from_seed "baseline" "baseline" "${db_name}" "${SEED_DB}"

# DCo's optimized arm runs the original app; only the knobs change.
echo "----------------->> DCo <<-----------------"
bench_arm_from_seed "dco" "baseline" "${db_name}" "${SEED_DB}" run_dco_tool
