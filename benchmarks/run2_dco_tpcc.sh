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

run_dco_tool() {
    if ! "${CMDRunDCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: DCo tuning failed; skipping post-DCo TPCC measurement and CSV." >&2
        return 1
    fi
}

# Each standalone run measures its OWN symmetric baseline first, so the delta
# is not confounded by cold-vs-warm host page cache.
echo "----------------->> Baseline <<-----------------"
bench_arm "baseline" "baseline"

# Knob settings live in postgresql.auto.conf and survive the data reset, so the
# clean reload resets data only, not tuning. DCo's optimized arm runs the
# original app.
echo "----------------->> DCo <<-----------------"
bench_arm "dco" "baseline" run_dco_tool
