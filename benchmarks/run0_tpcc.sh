#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

source "${SCRIPTS}/bench_lib.sh"

CMDRunACo="${SCRIPTS}/aco.sh"
CMDRunDCo="${SCRIPTS}/dco.sh"
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

run_aco_tool() {
    "${CMDRunACo}" "${dir_name}" "${db_type}" "${db_name}"
}

run_dco_tool() {
    if ! "${CMDRunDCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: DCo tuning failed; skipping post-DCo TPCC measurement and CSV." >&2
        return 1
    fi
}

run_adco_tool() {
    if ! "${CMDRunADCo}" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "ERROR: ADCo tuning failed; skipping post-ADCo TPCC measurement and CSV." >&2
        return 1
    fi
}

# Load the dataset exactly once, then snapshot it. All four arms restore from
# the snapshot instead of reloading: identical data, cold buffers per arm, one
# slow load instead of eight.
echo "----------------->> Seed (load once + snapshot) <<-----------------"
bench_seed_once "${db_name}" "${SEED_DB}"

# ── Shared baseline, measured once (no tool) ──
echo "----------------->> Baseline <<-----------------"
bench_arm_from_seed "baseline" "baseline" "${db_name}" "${SEED_DB}"

# ── ACo (rewrite-only) ──
echo "----------------->> ACo <<-----------------"
bench_arm_from_seed "aco" "tpcc_aco" "${db_name}" "${SEED_DB}" run_aco_tool

# ── DCo (tunes knobs; optimized arm runs the original app) ──
echo "----------------->> DCo <<-----------------"
bench_arm_from_seed "dco" "baseline" "${db_name}" "${SEED_DB}" run_dco_tool

# ── ADCo (rewrites the app and tunes knobs) ──
echo "----------------->> ADCo <<-----------------"
bench_arm_from_seed "adco" "tpcc_adco" "${db_name}" "${SEED_DB}" run_adco_tool

echo "----------------->> Comparison <<-----------------"
"${ROOT}/benchmarks/run_comparison.sh" "${RUN_ID}"
