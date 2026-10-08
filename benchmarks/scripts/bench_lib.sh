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
