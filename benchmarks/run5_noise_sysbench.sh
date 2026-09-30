#!/usr/bin/env bash
#
# A/A control for the sysbench workload: run the UNCHANGED database N times and
# report the noise band. The spread between identical runs is the smallest delta
# this benchmark can distinguish; any tuning effect below it is unproven.
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │ PROTOCOL MISMATCH WARNING                                                 │
# │ This band is measured with a FRESH prepare before every control run, on a  │
# │ small default dataset (10 tables x 10k rows, 30s). The tuner's promotion   │
# │ gate runs back-to-back repetitions on a LARGER, MUTATING screening dataset. │
# │ Those are different protocols, so this band does NOT validate the gate's   │
# │ variance and must not be read as doing so. It is valid for comparing arms  │
# │ measured the same way (e.g. run2's sysbench measurement, which also uses   │
# │ per-run prepare). The parameters actually used are recorded in the band's  │
# │ "protocol" object so the mismatch is machine-readable.                     │
# └──────────────────────────────────────────────────────────────────────────┘
#
# Usage:
#   CONTROL_RUNS=5 bash benchmarks/run5_noise_sysbench.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

CMDMeasure="${SCRIPTS}/sysbench_measure.py"
CMDDocker="${SCRIPTS}/docker.sh"

db_container="adcoexp-db"
db_service="pgdb"

CONTROL_RUNS="${CONTROL_RUNS:-5}"
# Dedicated database so sysbench never touches the TPC-C database.
SYSBENCH_DB="${SYSBENCH_DB:-adcodb}"
# Measurement shape, exported into the band's protocol field so it cannot drift.
SYSBENCH_TABLES="${SYSBENCH_TABLES:-10}"
SYSBENCH_TABLE_SIZE="${SYSBENCH_TABLE_SIZE:-10000}"
SYSBENCH_SECONDS="${SYSBENCH_SECONDS:-30}"
SYSBENCH_THREADS="${SYSBENCH_THREADS:-4}"
SYSBENCH_SUB_RUNS="${SYSBENCH_SUB_RUNS:-3}"

# ── Per-run resource contract ──
# Exported BEFORE the first docker call so compose substitution and docker.sh's
# post-Up verification both see the requested limits (matches run5_noise_tpcc).
export CPU_CORES="${CPU_CORES:-2}"
export MEMORY_GB="${MEMORY_GB:-8}"

RESULTS_DIR="${ROOT}/results/sysbench"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
PYTHON="${ROOT}/.venv/bin/python"

echo "----------------->> Refreshing Docker (CPU_CORES=${CPU_CORES} MEMORY_GB=${MEMORY_GB}) <<-----------------"
"$CMDDocker" Down "${db_container}"
"$CMDDocker" Up "${db_container}" "${db_service}"
"$CMDDocker" WaitFor "${db_container}"

files=()
for i in $(seq 1 "${CONTROL_RUNS}"); do
    name="noise_${i}_${RUN_ID}.csv"
    # Re-prepare before EVERY run. sysbench oltp_read_write mutates the dataset
    # (inserts/deletes), so on the small 10x10k default a reused dataset bloats
    # and degrades mid-run -- per-rep TPS was observed falling ~17% within one
    # invocation, giving a 15% run-to-run band. A cleanup+prepare is cheap here
    # and gives every control run an identical starting dataset.
    echo "----------------->> Control run ${i}/${CONTROL_RUNS} (sysbench on ${SYSBENCH_DB}) <<-----------------"
    "$PYTHON" "${CMDMeasure}" \
        --output "${RESULTS_DIR}/${name}" \
        --db-name "${SYSBENCH_DB}" \
        --runs "${SYSBENCH_SUB_RUNS}" --repetitions 1 \
        --tables "${SYSBENCH_TABLES}" --table-size "${SYSBENCH_TABLE_SIZE}" \
        --seconds "${SYSBENCH_SECONDS}" --threads "${SYSBENCH_THREADS}" \
        --prepare \
        --label "control ${i}/${CONTROL_RUNS}"
    files+=("${RESULTS_DIR}/${name}")
done

echo "----------------->> Noise band <<-----------------"
"${PYTHON}" "${SCRIPTS}/noise_stats.py" \
    --write-band "${RESULTS_DIR}/noise_band.json" \
    --protocol workload=sysbench \
    --protocol harness=run5_noise_sysbench \
    --protocol control_runs="${CONTROL_RUNS}" \
    --protocol sub_runs="${SYSBENCH_SUB_RUNS}" \
    --protocol repetitions=1 \
    --protocol prepare=per_invocation \
    --protocol tables="${SYSBENCH_TABLES}" \
    --protocol table_size="${SYSBENCH_TABLE_SIZE}" \
    --protocol seconds="${SYSBENCH_SECONDS}" \
    --protocol threads="${SYSBENCH_THREADS}" \
    --protocol db="${SYSBENCH_DB}" \
    --protocol cpu_cores="${CPU_CORES}" \
    --protocol memory_gb="${MEMORY_GB}" \
    --protocol gate_valid=false \
    --protocol gate_mismatch="tuner gate measures back-to-back reps on a larger mutating dataset; this band does not validate the gate" \
    "${files[@]}"
cp "${RESULTS_DIR}/noise_band.json" "${RESULTS_DIR}/noise_band_${RUN_ID}.json"
