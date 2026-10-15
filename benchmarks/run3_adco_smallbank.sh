#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

export BENCH_CMDRUNTPCC="${SCRIPTS}/smallbank.sh"
export ACCOUNTS="${ACCOUNTS:-1000000}"

source "${SCRIPTS}/bench_lib.sh"

CMDRunADCo="${SCRIPTS}/adco.sh"

dir_name="smallbank"
db_type="postgres"
db_name="smallbank"

RESULTS_DIR="${ROOT}/results/smallbank"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

run_adco_tool() {
    if ! "${CMDRunADCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: ADCo tuning failed; skipping post-ADCo SmallBank measurement and CSV." >&2
        return 1
    fi
}

# Each standalone run measures its OWN symmetric baseline first, so the delta
# is not confounded by cold-vs-warm host page cache.
echo "----------------->> Baseline <<-----------------"
bench_arm "baseline" "baseline"

# ADCo rewrites the app and tunes knobs; same symmetric protocol with the tool.
echo "----------------->> ADCo <<-----------------"
bench_arm "adco" "smallbank_adco" run_adco_tool
