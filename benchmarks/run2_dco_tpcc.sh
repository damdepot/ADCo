#!/usr/bin/env bash
#
# Dual-workload DCo benchmark: baseline / strict / relaxed arms.
#
# A single invocation loads the TPC-C dataset once, tunes twice (strict and
# relaxed durability policies are genuinely different problems), then measures
# BOTH TPC-C and sysbench for each arm. The arm order ROTATES per rep (the
# starting arm shifts by rep index) so each arm occupies each position across
# reps; a fixed arm order would let monotonic host drift alias with arm identity
# (the arm always measured last would be systematically penalized). It reports
# per-workload verdicts and an overall arm verdict that includes the G1
# non-regression guard (a sysbench win with a proven TPC-C regression is
# DEGRADED, not a win).
#
# Protocol note: the A/A bands this report consumes come from run5_noise_*.sh.
# The TPC-C band is measured with the same dataset/load/checkpoint protocol used
# here. The sysbench band uses per-run prepare on a small dataset and does NOT
# describe the tuner's promotion gate (back-to-back reps on a larger mutating
# dataset); do not read it as validating that gate.
#
# Usage:
#   REPS=3 bash benchmarks/run2_dco_tpcc.sh
#   CPU_CORES=2 MEMORY_GB=8 REPS=1 bash benchmarks/run2_dco_tpcc.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

CMDRunTPCC="${SCRIPTS}/tpcc.sh"
CMDRunDCo="${SCRIPTS}/dco.sh"
CMDDocker="${SCRIPTS}/docker.sh"
CMDPgSettings="${SCRIPTS}/pg_settings.py"
CMDSysbench="${SCRIPTS}/sysbench_measure.py"
CMNDelta="${SCRIPTS}/tpcc_delta.py"

PYTHON="${ROOT}/.venv/bin/python"

db_container="adcoexp-db"
db_service="pgdb"

dir_name="tpcc"
db_type="postgres"
db_name="tpcc"

# ── Per-run resource contract ──
# Exported BEFORE the first docker call so compose substitution and docker.sh's
# post-Up verification both see the requested limits.
export CPU_CORES="${CPU_CORES:-2}"
export MEMORY_GB="${MEMORY_GB:-8}"

# ── Interleaved repetitions (rep-major, arm order rotates per rep) ──
REPS="${REPS:-3}"
# Idle seconds to let a just-finished TPC-C run's autovacuum quiesce before the
# next workload is measured (see settle() below).
SETTLE="${SETTLE:-5}"

RESULTS_DIR="${ROOT}/results/tpcc"
SYSBENCH_DIR="${ROOT}/results/sysbench"
CONFIG_DIR="${RESULTS_DIR}/config"
mkdir -p "${RESULTS_DIR}" "${SYSBENCH_DIR}" "${CONFIG_DIR}"

RUN_ID="$(date +%Y%m%d_%H%M%S)"
BAND_TPCC="${RESULTS_DIR}/noise_band.json"
BAND_SYSBENCH="${SYSBENCH_DIR}/noise_band.json"

DEFAULT_SNAPSHOT="${CONFIG_DIR}/default_${RUN_ID}.json"

# Per-arm applied flags (kept as plain variables for bash 3.2 compatibility).
APPLIED_STRICT=0
APPLIED_RELAXED=0

join_by() {
    local IFS="$1"
    shift
    echo "$*"
}

# Run the workloads in a rotating arm order so no arm is always first/last.
# All arms are always present here; skipped (NOT APPLIED) arms simply drop out.
ALL_ARMS=(baseline strict relaxed)
N_ARMS=${#ALL_ARMS[@]}

# Wait for autovacuum to quiesce (bounded), plus a short fixed settle. TPC-C
# mutates the dataset, and measuring sysbench immediately afterwards would run
# against a database still being vacuumed, inflating variance and biasing the
# arm. Bounded so a stuck autovacuum cannot hang the harness.
settle() {
    local deadline=$((SECONDS + 60))
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        local workers
        workers="$(docker exec -u postgres "${db_container}" psql -tA -c \
            "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'autovacuum worker';" \
            2>/dev/null || echo 1)"
        if [ "${workers:-1}" = "0" ]; then
            break
        fi
        sleep 2
    done
    sleep "${SETTLE}"
}

# Reset production to pure Postgres defaults and clear postmaster-level knobs.
reset_production() {
    "$CMDPgSettings" reset --container "${db_container}" --db "${db_name}"
    "$CMDDocker" Restart "${db_container}"
    "$CMDDocker" WaitFor "${db_container}"
}

# Establish the baseline (untuned) config for a measurement visit.
establish_baseline() {
    local rep="$1"
    reset_production
    local now="${CONFIG_DIR}/visit_pre_baseline_r${rep}_${RUN_ID}.json"
    "$CMDPgSettings" snapshot --container "${db_container}" --output "${now}"
    if ! "$CMDPgSettings" equals --left "${DEFAULT_SNAPSHOT}" --right "${now}"; then
        echo "ERROR: baseline arm is not at pure Postgres defaults (rep ${rep})." >&2
        exit 1
    fi
}

# Establish a tuned arm's captured config for a measurement visit.
establish_arm() {
    local arm="$1"
    local rep="$2"
    local delta="${CONFIG_DIR}/delta_${arm}_${RUN_ID}.json"
    local expected="${CONFIG_DIR}/arm_${arm}_${RUN_ID}.json"

    reset_production
    local pre="${CONFIG_DIR}/visit_pre_${arm}_r${rep}_${RUN_ID}.json"
    "$CMDPgSettings" snapshot --container "${db_container}" --output "${pre}"
    if ! "$CMDPgSettings" equals --left "${DEFAULT_SNAPSHOT}" --right "${pre}"; then
        echo "ERROR: production was not reset to defaults before re-applying ${arm} (rep ${rep})." >&2
        exit 1
    fi

    "$CMDPgSettings" sql --delta "${delta}" \
        | docker exec -i -u postgres "${db_container}" psql -v ON_ERROR_STOP=1 -q
    docker exec -u postgres "${db_container}" psql -v ON_ERROR_STOP=1 -q \
        -c "SELECT pg_reload_conf();" >/dev/null

    "$CMDDocker" Restart "${db_container}"
    "$CMDDocker" WaitFor "${db_container}"

    local post="${CONFIG_DIR}/visit_post_${arm}_r${rep}_${RUN_ID}.json"
    "$CMDPgSettings" snapshot --container "${db_container}" --output "${post}"
    if ! "$CMDPgSettings" equals --left "${expected}" --right "${post}"; then
        echo "ERROR: ${arm} arm config (rep ${rep}) does not match its captured snapshot." >&2
        exit 1
    fi
}

# ── Phase 0: bring up the container and load the dataset exactly once ──
echo "----------------->> Refreshing Docker (CPU_CORES=${CPU_CORES} MEMORY_GB=${MEMORY_GB}) <<-----------------"
"$CMDDocker" Down "${db_container}"
"$CMDDocker" Up "${db_container}" "${db_service}"
"$CMDDocker" WaitFor "${db_container}"

echo "----------------->> Loading TPC-C dataset (once) <<-----------------"
PHASE=load "$CMDRunTPCC" baseline "load_${RUN_ID}.csv"
"$CMDDocker" Checkpoint "${db_container}"

echo "----------------->> Capturing default (untuned) config snapshot <<-----------------"
"$CMDPgSettings" snapshot --container "${db_container}" --output "${DEFAULT_SNAPSHOT}"

# ── Phase 1: tune each durability policy once, capture its resulting config ──
for arm in strict relaxed; do
    echo "----------------->> DCo tuning: ${arm} <<-----------------"
    reset_production
    pre="${CONFIG_DIR}/pre_${arm}_${RUN_ID}.json"
    "$CMDPgSettings" snapshot --container "${db_container}" --output "${pre}"
    if ! "$CMDPgSettings" equals --left "${DEFAULT_SNAPSHOT}" --right "${pre}"; then
        echo "WARNING: production was not at defaults before the ${arm} DCo run." >&2
    fi

    dco_ok=1
    if ! DURABILITY_PROFILE="${arm}" \
        "$CMDRunDCo" "${dir_name}" "${db_type}" "${db_name}" "${CPU_CORES}" "${MEMORY_GB}"; then
        echo "WARNING: DCo run for '${arm}' failed; arm will be reported NOT APPLIED." >&2
        dco_ok=0
    fi

    "$CMDDocker" Restart "${db_container}"
    "$CMDDocker" WaitFor "${db_container}"

    arm_snap="${CONFIG_DIR}/arm_${arm}_${RUN_ID}.json"
    "$CMDPgSettings" snapshot --container "${db_container}" --output "${arm_snap}"
    arm_delta="${CONFIG_DIR}/delta_${arm}_${RUN_ID}.json"
    "$CMDPgSettings" delta \
        --before "${DEFAULT_SNAPSHOT}" \
        --after "${arm_snap}" \
        --output "${arm_delta}"
    changed="$("$CMDPgSettings" count --delta "${arm_delta}")"

    if [ "${dco_ok}" -eq 1 ] && [ "${changed}" -gt 0 ]; then
        echo "arm '${arm}': APPLIED (${changed} setting(s) changed)."
        if [ "${arm}" = "strict" ]; then APPLIED_STRICT=1; else APPLIED_RELAXED=1; fi
    else
        echo "arm '${arm}': NOT APPLIED (${changed} setting(s) changed, dco_ok=${dco_ok})."
    fi
done

# ── Phase 2: interleaved measurements (rep-major) ──
for rep in $(seq 1 "${REPS}"); do
    start=$(( (rep - 1) % N_ARMS ))
    for offset in $(seq 0 $((N_ARMS - 1))); do
        arm="${ALL_ARMS[$(( (start + offset) % N_ARMS ))]}"
        if [ "${arm}" = "strict" ] && [ "${APPLIED_STRICT}" -eq 0 ]; then
            echo "----------------->> rep ${rep}: strict NOT APPLIED — skipping measurement <<-----------------"
            continue
        fi
        if [ "${arm}" = "relaxed" ] && [ "${APPLIED_RELAXED}" -eq 0 ]; then
            echo "----------------->> rep ${rep}: relaxed NOT APPLIED — skipping measurement <<-----------------"
            continue
        fi

        echo "----------------->> rep ${rep}: establishing ${arm} config <<-----------------"
        if [ "${arm}" = "baseline" ]; then
            establish_baseline "${rep}"
        else
            establish_arm "${arm}" "${rep}"
        fi

        echo "----------------->> rep ${rep}: TPC-C (${arm}) <<-----------------"
        # Config-neutral variance control: one 60s TPC-C run writes ~356MB of WAL
        # against a 1GB default max_wal_size, so without a forced checkpoint a
        # WAL-volume checkpoint fires mid-window every ~3rd run. A CHECKPOINT is
        # a maintenance command, not a config change, so the baseline arm stays
        # untouched. Apply it symmetrically to every arm.
        "$CMDDocker" Checkpoint "${db_container}"
        PHASE=execute "$CMDRunTPCC" baseline "${arm}_r${rep}_${RUN_ID}.csv"

        echo "----------------->> rep ${rep}: sysbench (${arm}) <<-----------------"
        "$CMDDocker" Checkpoint "${db_container}"
        # The TPC-C run above mutated the dataset; wait for autovacuum to quiesce
        # so sysbench is not measured against a database being vacuumed.
        settle
        # Three fresh 30s samples (--runs 3 --repetitions 1) rather than three
        # back-to-back reps on one dataset: sysbench oltp_read_write mutates its
        # dataset, so reused reps degrade and inflate the spread. A fresh
        # dataset per sample is symmetric across arms and keeps it honest.
        "$PYTHON" "${CMDSysbench}" \
            --output "${SYSBENCH_DIR}/${arm}_r${rep}_${RUN_ID}.csv" \
            --runs 3 --repetitions 1 \
            --prepare \
            --label "${arm} rep ${rep}/${REPS}"
    done
done

# ── Phase 3: dual-workload report ──
echo "----------------->> Dual-workload report <<-----------------"
if [ ! -f "${BAND_TPCC}" ]; then
    echo "WARNING: no TPC-C noise band at ${BAND_TPCC}; run benchmarks/run5_noise_tpcc.sh." >&2
fi
if [ ! -f "${BAND_SYSBENCH}" ]; then
    echo "WARNING: no sysbench noise band at ${BAND_SYSBENCH}; run benchmarks/run5_noise_sysbench.sh." >&2
fi

baseline_tpcc=()
baseline_sys=()
for rep in $(seq 1 "${REPS}"); do
    baseline_tpcc+=("${RESULTS_DIR}/baseline_r${rep}_${RUN_ID}.csv")
    baseline_sys+=("${SYSBENCH_DIR}/baseline_r${rep}_${RUN_ID}.csv")
done

arm_specs=()
for arm in strict relaxed; do
    if [ "${arm}" = "strict" ]; then applied="${APPLIED_STRICT}"; else applied="${APPLIED_RELAXED}"; fi
    if [ "${applied}" -eq 1 ]; then
        tpcc_csvs=()
        sys_csvs=()
        for rep in $(seq 1 "${REPS}"); do
            tpcc_csvs+=("${RESULTS_DIR}/${arm}_r${rep}_${RUN_ID}.csv")
            sys_csvs+=("${SYSBENCH_DIR}/${arm}_r${rep}_${RUN_ID}.csv")
        done
        arm_specs+=("${arm}:$(join_by , "${tpcc_csvs[@]}"):$(join_by , "${sys_csvs[@]}")")
    else
        arm_specs+=("${arm}::")
    fi
done

delta_args=(
    --baseline-tpcc "${baseline_tpcc[@]}"
    --baseline-sysbench "${baseline_sys[@]}"
    --band-tpcc "${BAND_TPCC}"
    --band-sysbench "${BAND_SYSBENCH}"
    --run-id "${RUN_ID}"
    --reps "${REPS}"
    --output "${RESULTS_DIR}/dual_report_${RUN_ID}.json"
)
for spec in "${arm_specs[@]}"; do
    delta_args+=(--arm "${spec}")
done

"$PYTHON" "${CMNDelta}" dual "${delta_args[@]}"
