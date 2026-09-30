#!/usr/bin/env bash
#
# work_mem A/B on a sort/hash workload.
#
# Demonstrates a large, real tuning win: with the default work_mem=4MB the
# GROUP BY (c, pad) query in benchmarks/tools/pgbench/sort_hash.pgb spills a
# 1.5M-row sort to disk (external merge); at work_mem=256MB the same query
# builds an in-memory HashAggregate with no spill. On this host that is roughly
# a 6x throughput difference, far outside the run-to-run noise band.
#
# Arms:
#   default  pure Postgres defaults (work_mem=4MB)
#   wm256    work_mem=256MB (reloadable, no restart)
#
# Design (mirrors run7_settled_ab.sh):
#   - RESET ALL then apply the arm, so every arm starts from a clean slate.
#   - Forced CHECKPOINT + short idle period before every measurement so all
#     arms are compared from an equally settled state.
#   - Reps interleaved arm-major (default then wm256 within each rep) so any
#     monotonic host drift cancels between arms.
#   - pgbench runs inside the container to avoid the host<->container network
#     path; each transaction is a heavy sort, so TPS is small by design -- the
#     RATIO is the point.
#
# Bash 3.2 compatible (macOS /bin/bash): no associative arrays.
#
# Usage:
#   REPS=3 bash benchmarks/run8_workmem_ab.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"
CMDDocker="${SCRIPTS}/docker.sh"
MEASURE="${SCRIPTS}/pgbench_measure.py"
PGBSCRIPT="${ROOT}/benchmarks/tools/pgbench/sort_hash.pgb"
PYTHON="${ROOT}/.venv/bin/python"

C=adcoexp-db
DB=adcodb
REPS="${REPS:-3}"
SETTLE="${SETTLE:-5}"
CLIENTS="${CLIENTS:-2}"
THREADS="${THREADS:-2}"
SECONDS_MEASURED="${SECONDS_MEASURED:-30}"

ARMS="default wm256"

OUT="${ROOT}/results/workmem"
mkdir -p "${OUT}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

psql() { docker exec -u postgres "$C" psql -v ON_ERROR_STOP=1 -q "$@"; }

restore_defaults() {
    psql -c "ALTER SYSTEM RESET ALL;" >/dev/null 2>&1 || true
    psql -c "SELECT pg_reload_conf();" >/dev/null 2>&1 || true
}
trap restore_defaults EXIT

apply_arm() {
    psql -c "ALTER SYSTEM RESET ALL;" >/dev/null
    case "$1" in
        default) ;;
        wm256) psql -c "ALTER SYSTEM SET work_mem = '256MB';" >/dev/null ;;
        *) echo "unknown arm $1" >&2; exit 2 ;;
    esac
    psql -c "SELECT pg_reload_conf();" >/dev/null
    # Equal settling for every arm: flush, then let the background settle.
    "$CMDDocker" Checkpoint "$C" >/dev/null
    sleep "${SETTLE}"
}

measure() {
    local arm="$1" rep="$2"
    "$PYTHON" "$MEASURE" \
        --output "${OUT}/${arm}_r${rep}_${RUN_ID}.csv" \
        --db-name "$DB" --clients "$CLIENTS" --threads "$THREADS" \
        --seconds "$SECONDS_MEASURED" --script "$PGBSCRIPT" \
        --label "${arm} r${rep}"
}

total_of() { awk -F, '{print $NF}' "$1"; }
median_of() { printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'; }
spread_pct() {
    printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{m=(NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2; print (a[NR]-a[1])/m*100}'
}

echo "================= work_mem A/B ${RUN_ID} ================="
echo "reps=${REPS} arms=[${ARMS}] clients=${CLIENTS} threads=${THREADS} duration=${SECONDS_MEASURED}s"
echo "startup work_mem = $(psql -tAc "SHOW work_mem")"

TAGS=""
for rep in $(seq 1 "${REPS}"); do
    for arm in ${ARMS}; do
        echo "----------------->> rep ${rep}/${REPS}: ${arm} <<-----------------"
        apply_arm "${arm}"
        measure "${arm}" "${rep}"
        tps="$(total_of "${OUT}/${arm}_r${rep}_${RUN_ID}.csv")"
        echo "    ${arm} r${rep}: ${tps} txn/s"
        TAGS="${TAGS} ${arm}_r${rep}_${RUN_ID}"
    done
done

echo
echo "================= Results ${RUN_ID} ================="
med_default=""
for arm in ${ARMS}; do
    vals=""
    for tag in ${TAGS}; do
        case "${tag}" in "${arm}_"*) vals="${vals} $(total_of "${OUT}/${tag}.csv")" ;; esac
    done
    med="$(median_of ${vals})"
    spread="$(spread_pct ${vals})"
    [ "${arm}" = "default" ] && med_default="${med}"
    printf '%-8s median=%9.3f txn/s  spread=%6.2f%%  runs=[%s ]\n' \
        "${arm}" "${med}" "${spread}" "${vals}"
done

echo
echo "arm        delta%   vs default"
for arm in ${ARMS}; do
    [ "${arm}" = "default" ] && continue
    vals=""
    for tag in ${TAGS}; do
        case "${tag}" in "${arm}_"*) vals="${vals} $(total_of "${OUT}/${tag}.csv")" ;; esac
    done
    med="$(median_of ${vals})"
    d="$(awk -v a="${med}" -v b="${med_default}" 'BEGIN{printf "%.2f", (a-b)/b*100}')"
    ratio="$(awk -v a="${med}" -v b="${med_default}" 'BEGIN{printf "%.2fx", a/b}')"
    printf '%-8s %8s%%   %s\n' "${arm}" "${d}" "${ratio}"
done
echo
echo "Raw CSVs: ${OUT}/<arm>_r<rep>_${RUN_ID}.csv"
