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
bench_arm "aco" "tpcc_aco" run_aco_tool
