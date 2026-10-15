#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

export BENCH_CMDRUNTPCC="${SCRIPTS}/smallbank.sh"
export ACCOUNTS="${ACCOUNTS:-1000000}"

source "${SCRIPTS}/bench_lib.sh"

CMDRunACo="${SCRIPTS}/aco.sh"

dir_name="smallbank"
db_type="postgres"
db_name="smallbank"

RESULTS_DIR="${ROOT}/results/smallbank"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

run_aco_tool() {
    "${CMDRunACo}" "${dir_name}" "${db_type}" "${db_name}"
}

# Each standalone run measures its OWN symmetric baseline first, so the delta
# is not confounded by cold-vs-warm host page cache.
echo "----------------->> Baseline <<-----------------"
bench_arm "baseline" "baseline"

# ACo is rewrite-only: it does not tune the DB. The same reset -> load -> tool
# -> restart -> clean reload -> measure protocol is applied for uniformity.
echo "----------------->> ACo <<-----------------"
bench_arm "aco" "smallbank_aco" run_aco_tool
