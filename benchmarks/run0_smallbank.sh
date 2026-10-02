#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

export BENCH_CMDRUNTPCC="${SCRIPTS}/smallbank.sh"
export ACCOUNTS="${ACCOUNTS:-1000000}"

source "${SCRIPTS}/bench_lib.sh"

CMDRunACo="${SCRIPTS}/aco.sh"
CMDRunDCo="${SCRIPTS}/dco.sh"
CMDRunADCo="${SCRIPTS}/adco.sh"

dir_name="smallbank"
db_type="postgres"
db_name="smallbank"

RESULTS_DIR="${ROOT}/results/smallbank"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

run_aco_tool() {
    "${CMDRunACo}" "${dir_name}" "${db_type}" "${db_name}"
}

run_dco_tool() {
    if ! "${CMDRunDCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: DCo tuning failed; skipping post-DCo SmallBank measurement and CSV." >&2
        return 1
    fi
}

run_adco_tool() {
    if ! "${CMDRunADCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: ADCo tuning failed; skipping post-ADCo SmallBank measurement and CSV." >&2
        return 1
    fi
}

# ── Shared baseline, measured once via the symmetric protocol (no tool) ──
echo "----------------->> Baseline <<-----------------"
bench_arm "baseline" "baseline"

# ── ACo (rewrite-only; same symmetric protocol) ──
echo "----------------->> ACo <<-----------------"
bench_arm "aco" "smallbank_aco" run_aco_tool

# ── DCo (tunes knobs; optimized arm runs the original app) ──
echo "----------------->> DCo <<-----------------"
bench_arm "dco" "baseline" run_dco_tool

# ── ADCo (rewrites the app and tunes knobs) ──
echo "----------------->> ADCo <<-----------------"
bench_arm "adco" "smallbank_adco" run_adco_tool

echo "----------------->> Comparison <<-----------------"
RESULTS_DIR="${ROOT}/results/smallbank" "${ROOT}/benchmarks/run_comparison.sh" "${RUN_ID}"
