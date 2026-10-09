#!/usr/bin/env bash

# ── Canonical TPC-C measurement protocol (shared by every runner) ──
# This file lives at <root>/benchmarks/scripts/bench_lib.sh.
BENCH_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "${BENCH_SCRIPTS_DIR}/../.." && pwd)"
BENCH_CMDDOCKER="${BENCH_CMDDOCKER:-${BENCH_SCRIPTS_DIR}/docker.sh}"
BENCH_CMDRUNTPCC="${BENCH_CMDRUNTPCC:-${BENCH_SCRIPTS_DIR}/tpcc.sh}"
BENCH_DB_CONTAINER="${BENCH_DB_CONTAINER:-adcoexp-db}"
BENCH_DB_SERVICE="${BENCH_DB_SERVICE:-pgdb}"

# Fixed resource + workload contract. Exported so docker compose (CPU_CORES /
# MEMORY_GB) and tpcc.sh (REPS / DURATION / WARMUP / WAREHOUSES / CLIENTS) read
# the exact values the runners measure with.
export CPU_CORES="${CPU_CORES:-2}"
export MEMORY_GB="${MEMORY_GB:-8}"
export REPS="${REPS:-3}"
export DURATION="${DURATION:-60}"
export WARMUP="${WARMUP:-10}"
export WAREHOUSES="${WAREHOUSES:-6}"
export CLIENTS="${CLIENTS:-6}"
export SETTLE_SECS="${SETTLE_SECS:-5}"

# Fresh default config + empty DB; CPU_CORES/MEMORY_GB already exported so the
# compose limits and docker.sh's limit verification use the contract values.
bench_reset() {
    "${BENCH_CMDDOCKER}" Down "${BENCH_DB_CONTAINER}"
    "${BENCH_CMDDOCKER}" Up "${BENCH_DB_CONTAINER}" "${BENCH_DB_SERVICE}"
    "${BENCH_CMDDOCKER}" WaitFor "${BENCH_DB_CONTAINER}"
}

# Wait (bounded to 60s) for autovacuum workers to quiesce, then hold SETTLE_SECS
# so both arms are measured from a comparably settled dataset.
bench_settle() {
    local elapsed=0
    local n=1
    while [ "${elapsed}" -lt 60 ]; do
        n="$(docker exec -u postgres "${BENCH_DB_CONTAINER}" psql -tA -c "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'autovacuum worker';" 2>/dev/null || echo 1)"
        [ "${n}" = "0" ] && break
        sleep 2
        elapsed=$((elapsed + 2))
    done
    sleep "${SETTLE_SECS:-5}"
}

# Load the shared baseline dataset via the original app, checkpoint, settle.
bench_load() {
    local file="$1"
    PHASE=load "${BENCH_CMDRUNTPCC}" baseline "${file}"
    "${BENCH_CMDDOCKER}" Checkpoint "${BENCH_DB_CONTAINER}"
    bench_settle
}

# Measure one arm. <dir> is 'baseline' or an out/<dir> rewritten app. Reps and
# workload shape come from the exported contract above.
bench_measure() {
    local dir="$1"
    local file="$2"
    "${BENCH_CMDRUNTPCC}" "${dir}" "${file}"
}

# Run one arm via the canonical symmetric protocol. Both baseline and optimized
# arms execute the identical sequence; optimized arms merely inject a tool.
#   baseline:  reset -> load -> restart -> wait -> clean reload -> measure
#   optimized: reset -> load -> tool -> restart -> wait -> clean reload -> measure
# <tag> names the load/measure CSVs (RUN_ID must be exported). <optimized_dir>
# is 'baseline' or an out/<dir> rewritten app. The optional <tool_fn> is a shell
# function name invoked between the initial and clean loads; a non-zero return
# aborts the run via errexit. Baseline passes no <tool_fn>.
bench_arm() {
    local tag="$1"
    local optimized_dir="$2"
    local tool_fn="${3:-}"
    local run_id="${RUN_ID:?RUN_ID must be set before bench_arm}"

    bench_reset
    bench_load "load_${tag}_${run_id}.csv"
    if [ -n "${tool_fn}" ]; then
        "${tool_fn}"
    fi
    "${BENCH_CMDDOCKER}" Restart "${BENCH_DB_CONTAINER}"
    "${BENCH_CMDDOCKER}" WaitFor "${BENCH_DB_CONTAINER}"
    bench_load "load_clean_${tag}_${run_id}.csv"
    bench_measure "${optimized_dir}" "${tag}_${run_id}.csv"
}

# Snapshot a pristine dataset once so multi-arm runs load only once.
# bench_snapshot <db> <seed>: CHECKPOINT, settle, then
#   CREATE DATABASE <seed> TEMPLATE <db>. The template stays untouched;
#   per-arm resets recreate <db> from it in seconds.
# bench_restore <db> <seed>: DROP + recreate <db> from <seed>, then
#   checkpoint and settle so measurement starts from identical data.
# Knob settings (postgresql.auto.conf) are cluster-level and survive both.
bench_snapshot() {
    local db="$1"
    local seed="$2"
    "${BENCH_CMDDOCKER}" Checkpoint "${BENCH_DB_CONTAINER}"
    bench_settle
    psql -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('${db}', '${seed}') AND pid <> pg_backend_pid();"
    psql -d postgres -c "DROP DATABASE IF EXISTS \"${seed}\" WITH (FORCE);"
    psql -d postgres -c "CREATE DATABASE \"${seed}\" TEMPLATE \"${db}\";"
}

bench_restore() {
    local db="$1"
    local seed="$2"
    psql -d postgres -c "DROP DATABASE IF EXISTS \"${db}\" WITH (FORCE);"
    psql -d postgres -c "CREATE DATABASE \"${db}\" TEMPLATE \"${seed}\";"
    "${BENCH_CMDDOCKER}" Checkpoint "${BENCH_DB_CONTAINER}"
    bench_settle
}

# Load the dataset exactly once for a whole run, then snapshot it for fast
# per-arm restores. Replaces N slow loader passes with one.
# bench_seed_once <db> <seed>
bench_seed_once() {
    local db="$1"
    local seed="$2"
    local run_id="${RUN_ID:?RUN_ID must be set before bench_seed_once}"
    bench_reset
    bench_load "load_seed_${run_id}.csv"
    bench_snapshot "${db}" "${seed}"
}

# Measure one arm from an already-snapshotted seed (see bench_seed_once).
# bench_arm_from_seed <tag> <dir> <db> <seed> [tool_fn]
# Same symmetry as bench_arm (identical data + cold buffers per arm) with
# restore -> [tool -> restore] -> restart -> measure instead of reset ->
# load -> tool -> restart -> clean reload -> measure. The post-tool restore
# keeps tool screening writes out of measurement.
bench_arm_from_seed() {
    local tag="$1"
    local optimized_dir="$2"
    local db="$3"
    local seed="$4"
    local tool_fn="${5:-}"
    local run_id="${RUN_ID:?RUN_ID must be set before bench_arm_from_seed}"

    bench_restore "${db}" "${seed}"
    if [ -n "${tool_fn}" ]; then
        "${tool_fn}"
        bench_restore "${db}" "${seed}"
    fi
    "${BENCH_CMDDOCKER}" Restart "${BENCH_DB_CONTAINER}"
    "${BENCH_CMDDOCKER}" WaitFor "${BENCH_DB_CONTAINER}"
    bench_measure "${optimized_dir}" "${tag}_${run_id}.csv"
}

# Mutual exclusion for DB-destructive runs. bench_lock <root> takes a
# non-blocking flock on <root>/.bench.lock (held until the script exits);
# a second concurrent run fails fast instead of Down/Restart-ing the
# database out from under the first one mid-load or mid-measure.
bench_lock() {
    local root="$1"
    exec 9>"${root}/.bench.lock"
    if ! flock -n 9; then
        echo "ERROR: another benchmark run holds ${root}/.bench.lock; refusing to run concurrently." >&2
        exit 1
    fi
}

psql() {
    docker exec -i -u postgres "${BENCH_DB_CONTAINER}" psql -v ON_ERROR_STOP=1 -q "$@"
}

total_of() {
    awk -F, '
        $1 ~ /TOTAL/ { v = $NF }
        END {
            if (v == "" || v !~ /^-?[0-9]+([.][0-9]+)?$/) {
                print "no parseable TOTAL row in " FILENAME > "/dev/stderr"
                exit 1
            }
            print v
        }
    ' "$1"
}

median_of() {
    printf '%s\n' "$@" | sort -n |
        awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'
}

median_of_values() { median_of "$@"; }

band_field() {
    "${PYTHON}" -c '
import json, sys
band = json.load(open(sys.argv[1]))
for key in ("mde_pct", "ci95_pct", "spread_pct"):
    if key in band:
        print(key)
        print(float(band[key]))
        break
else:
    print("none")
    print("0")
' "$1"
}

restore_defaults() {
    psql -d "${DB}" -c "ALTER SYSTEM RESET ALL;" >/dev/null 2>&1 || true
    psql -d "${DB}" -c "SELECT pg_reload_conf();" >/dev/null 2>&1 || true
    if [ "${RESTORE_RESTART:-1}" = "1" ]; then
        "${CMDDOCKER}" Restart "${BENCH_DB_CONTAINER}" >/dev/null 2>&1 || true
        "${CMDDOCKER}" WaitFor "${BENCH_DB_CONTAINER}" >/dev/null 2>&1 || true
    fi
}

usage() {
    echo "Usage: $(basename "$0") <dir_name> <db_type> <db_name> <cpu_cores> <memory_gb>" >&2
}

validate_tuning_args() {
    if [ -z "$dir_name" ] || [ -z "$db_type" ] || [ -z "$db_name" ] || [ -z "$cpu_cores" ] || [ -z "$memory_gb" ]; then
        echo "ERROR: missing required arguments." >&2
        usage
        exit 2
    fi

    if ! [[ "$cpu_cores" =~ ^[1-9][0-9]*$ ]]; then
        echo "ERROR: <cpu_cores> must be a positive integer (got '${cpu_cores}')." >&2
        usage
        exit 2
    fi

    if ! [[ "$memory_gb" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !(${memory_gb} > 0)}"; then
        echo "ERROR: <memory_gb> must be a positive number (got '${memory_gb}')." >&2
        usage
        exit 2
    fi
}
