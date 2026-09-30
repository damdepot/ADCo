#!/usr/bin/env bash
#
# A/A control: run the UNCHANGED database N times and report the noise band.
# The spread between identical runs is the smallest delta this benchmark can
# distinguish; any tuning effect below it is unproven.
#
# The TPC-C dataset is loaded exactly ONCE after the container is refreshed, and
# every control run reuses that same loaded dataset (no reload between runs).
# A CHECKPOINT is issued right after the load AND before every control run.
# Each 60s run writes ~356MB of WAL while the default max_wal_size is 1GB, so
# without this a WAL-volume checkpoint fires roughly every third run and its
# background flush lands inside a measured window, which was the dominant source
# of run-to-run spread. A forced CHECKPOINT costs ~0.5s, is a maintenance
# command rather than a config change (the baseline must stay untouched), and
# gives every run the same clean starting state.
#
# Protocol note: this band is produced on the same TPC-C dataset shape, load,
# and per-run CHECKPOINT discipline that run2 uses to measure TPC-C, so it is a
# valid A/A band for that measurement. It says nothing about the tuner's
# sysbench-based promotion gate: a band is only comparable to runs measured with
# the SAME protocol, and the parameters used are recorded in the band's
# "protocol" object so the scope is explicit.
#
# Usage:
#   CONTROL_RUNS=5 bash benchmarks/run5_noise_tpcc.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

CMDRunTPCC="${SCRIPTS}/tpcc.sh"
CMDDocker="${SCRIPTS}/docker.sh"

db_container="adcoexp-db"
db_service="pgdb"

CONTROL_RUNS="${CONTROL_RUNS:-5}"
# Match the benchmark defaults; override via the environment if needed.
export WAREHOUSES="${WAREHOUSES:-4}"
export CLIENTS="${CLIENTS:-4}"
export DURATION="${DURATION:-60}"

# ── Per-run resource contract ──
# Fixed at 2 CPU / 8 GB for easy runs; override via the environment if needed.
# Exported so docker.sh's compose substitution and post-Up docker inspect
# verification actually see them.
export CPU_CORES="${CPU_CORES:-2}"
export MEMORY_GB="${MEMORY_GB:-8}"

RESULTS_DIR="${ROOT}/results/tpcc"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
PYTHON="${ROOT}/.venv/bin/python"

echo "----------------->> Refreshing Docker <<-----------------"
"$CMDDocker" Down "${db_container}"
"$CMDDocker" Up "${db_container}" "${db_service}"
"$CMDDocker" WaitFor "${db_container}"

echo "----------------->> Loading dataset (once) <<-----------------"
# The file arg is unused in the load phase, but keep the interface identical.
PHASE=load "$CMDRunTPCC" baseline "load_${RUN_ID}.csv"
"$CMDDocker" Checkpoint "${db_container}"

files=()
for i in $(seq 1 "${CONTROL_RUNS}"); do
    name="noise_${i}_${RUN_ID}.csv"
    echo "----------------->> Control run ${i}/${CONTROL_RUNS} (${WAREHOUSES} wh / ${CLIENTS} clients / ${DURATION}s) <<-----------------"
    "$CMDDocker" Checkpoint "${db_container}"
    PHASE=execute "$CMDRunTPCC" baseline "${name}"
    files+=("${RESULTS_DIR}/${name}")
done

echo "----------------->> Noise band <<-----------------"
"${PYTHON}" "${SCRIPTS}/noise_stats.py" \
    --write-band "${RESULTS_DIR}/noise_band.json" \
    --protocol workload=tpcc \
    --protocol harness=run5_noise_tpcc \
    --protocol control_runs="${CONTROL_RUNS}" \
    --protocol warehouses="${WAREHOUSES}" \
    --protocol clients="${CLIENTS}" \
    --protocol duration_s="${DURATION}" \
    --protocol dataset=loaded_once \
    --protocol checkpoint=per_run \
    --protocol cpu_cores="${CPU_CORES}" \
    --protocol memory_gb="${MEMORY_GB}" \
    "${files[@]}"
cp "${RESULTS_DIR}/noise_band.json" "${RESULTS_DIR}/noise_band_${RUN_ID}.json"
