#!/usr/bin/env bash
#
# Commit-path ceiling probe (Option 2).
#
# The tuner's promotion gate is too underpowered to decide anything (its 95% CI
# spans zero at 3 reps), and it measured sysbench, not TPC-C. This script
# therefore bypasses the gate completely and answers the only question that
# matters for the TPS goal: how much TPC-C throughput is on the table from the
# commit path on THIS host?
#
# It hand-applies durability settings and measures them A/B on TPC-C:
#
#   default  synchronous_commit=on,  full_page_writes=on   (pure defaults)
#   syncoff  synchronous_commit=off, full_page_writes=on
#   maxoff   synchronous_commit=off, full_page_writes=off  (durability sacrificed)
#
# Design notes:
#  - Dataset is loaded ONCE, then only GUCs change between measurements.
#  - Forced CHECKPOINT before every run: a 60s TPC-C run writes ~356MB of WAL
#    against a 1GB default max_wal_size, so without it a WAL-volume checkpoint
#    fires mid-window and corrupts the measurement.
#  - Reps use a ROTATING arm order (starting arm shifts by rep index) so each
#    arm occupies each position across reps; a fixed order would let monotonic
#    host drift alias with arm identity.
#  - macOS Docker intermittently produces bad 60s windows (e.g. 61 txn/s vs
#    115 txn/s). A run is suspect when it falls OUTSIDE
#    median * [SUSPECT_RATIO, 1/SUSPECT_RATIO] -- both spuriously slow AND
#    spuriously fast runs are re-measured (up to MAX_RETRIES), so one bad window
#    cannot carry the verdict in either direction. Re-measured tags and reasons
#    are listed explicitly at the end. NOTE: retries run in a separate pass after
#    the interleaved sweep, so their position/state differs from the first pass;
#    the per-run values are printed so this is visible.
#  - Per-run WAL sync-time and WAL byte DELTAS are printed as mechanism evidence
#    (the underlying pg_stat_wal counters are cumulative, so both are
#    differenced rather than shown raw).
#
# Bash 3.2 compatible (macOS /bin/bash): no associative arrays.
#
# Usage:
#   REPS=3 bash benchmarks/run6_commit_path_ab.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"
source "${SCRIPTS}/bench_lib.sh"

CMDRunTPCC="${SCRIPTS}/tpcc.sh"
CMDDocker="${SCRIPTS}/docker.sh"
PYTHON="${ROOT}/.venv/bin/python"

db_container="adcoexp-db"
db_service="pgdb"
BENCH_DB_CONTAINER="${db_container}"

export CPU_CORES="${CPU_CORES:-2}"
export MEMORY_GB="${MEMORY_GB:-8}"
export WAREHOUSES="${WAREHOUSES:-4}"
export CLIENTS="${CLIENTS:-4}"
export DURATION="${DURATION:-60}"

REPS="${REPS:-3}"
MAX_RETRIES="${MAX_RETRIES:-2}"
SUSPECT_RATIO="${SUSPECT_RATIO:-0.80}"

ARMS="default syncoff maxoff"
# Same three arms as an array so the measurement order can rotate per rep.
ARMS_ARR=(default syncoff maxoff)
N_ARMS=${#ARMS_ARR[@]}

RESULTS_DIR="${ROOT}/results/commit_path"
mkdir -p "${RESULTS_DIR}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
BAND="${ROOT}/results/tpcc/noise_band.json"

csv_of() { echo "${ROOT}/results/tpcc/$1.csv"; }

apply_arm() {
    # Clean slate, then the arm's settings. synchronous_commit and
    # full_page_writes are both sighup-context, so a reload suffices (no restart).
    psql -c "ALTER SYSTEM RESET ALL;" >/dev/null
    case "$1" in
        default) ;;
        syncoff) psql -c "ALTER SYSTEM SET synchronous_commit = 'off';" >/dev/null ;;
        maxoff)  psql -c "ALTER SYSTEM SET synchronous_commit = 'off';" \
                     -c "ALTER SYSTEM SET full_page_writes = 'off';" >/dev/null ;;
        *) echo "unknown arm: $1" >&2; exit 2 ;;
    esac
    psql -c "SELECT pg_reload_conf();" >/dev/null
}

wal_probe() {
    docker exec -u postgres "${db_container}" psql -tA -F'|' -c "
        SELECT wal_sync_time, wal_bytes FROM pg_stat_wal;" 2>/dev/null || echo "0|0"
}

run_one() {
    local arm="$1" tag="$2"
    apply_arm "${arm}"
    "$CMDDocker" Checkpoint "${db_container}" >/dev/null
    local before after bs by as ay
    before="$(wal_probe)"
    PHASE=execute "$CMDRunTPCC" baseline "${tag}.csv" >/dev/null 2>&1
    after="$(wal_probe)"
    IFS='|' read -r bs by <<<"${before}"
    IFS='|' read -r as ay <<<"${after}"
    # pg_stat_wal counters are cumulative; report deltas for BOTH, never a raw
    # absolute value behind a "+".
    printf '      %-8s %-34s wal_sync_ms=+%-7s wal_bytes=+%sMB\n' \
        "${arm}" "${tag}" "$(( ${as:-0} - ${bs:-0} ))" \
        "$(( (${ay:-0} - ${by:-0}) / 1048576 ))" >&2
}

# Echo the TOTAL of every tag belonging to an arm, one per line.
values_for_arm() {
    local arm="$1" tag
    for tag in ${TAGS}; do
        case "${tag}" in
            "${arm}_"*) total_of "$(csv_of "${tag}")" ;;
        esac
    done
}

echo "================= Commit-path probe ${RUN_ID} ================="
echo "reps=${REPS} arms=[${ARMS}] clients=${CLIENTS} duration=${DURATION}s"

echo "----------------->> Refreshing Docker <<-----------------"
"$CMDDocker" Down "${db_container}"
"$CMDDocker" Up "${db_container}" "${db_service}"
"$CMDDocker" WaitFor "${db_container}"

echo "----------------->> Loading TPC-C dataset (once) <<-----------------"
PHASE=load "$CMDRunTPCC" baseline "load_${RUN_ID}.csv"

TAGS=""
for rep in $(seq 1 "${REPS}"); do
    start=$(( (rep - 1) % N_ARMS ))
    for offset in $(seq 0 $((N_ARMS - 1))); do
        arm="${ARMS_ARR[$(( (start + offset) % N_ARMS ))]}"
        tag="${arm}_r${rep}_${RUN_ID}"
        echo "----------------->> rep ${rep}: ${arm} <<-----------------"
        run_one "${arm}" "${tag}"
        TAGS="${TAGS} ${tag}"
    done
done

echo "----------------->> Outlier check (suspect outside median * [${SUSPECT_RATIO}, 1/${SUSPECT_RATIO}]) <<-----------------"
retries_default=0
retries_syncoff=0
retries_maxoff=0
REMEASURED=""
attempt=1
while [ "${attempt}" -le "${MAX_RETRIES}" ]; do
    changed=0
    for arm in ${ARMS}; do
        vals="$(values_for_arm "${arm}")"
        med="$(median_of_values ${vals})"
        low="$(awk -v m="${med}" -v r="${SUSPECT_RATIO}" 'BEGIN{print m*r}')"
        high="$(awk -v m="${med}" -v r="${SUSPECT_RATIO}" 'BEGIN{print m/r}')"
        tag=""
        for tag in ${TAGS}; do
            case "${tag}" in "${arm}_"*) ;; *) continue ;; esac
            v="$(total_of "$(csv_of "${tag}")")"
            reason=""
            if awk -v v="${v}" -v c="${low}" 'BEGIN{exit !(v < c)}'; then
                reason="low"
            elif awk -v v="${v}" -v c="${high}" 'BEGIN{exit !(v > c)}'; then
                reason="high"
            fi
            [ -n "${reason}" ] || continue
            rvar="retries_${arm}"
            if [ "${!rvar}" -lt "${MAX_RETRIES}" ]; then
                echo "  ${tag} = ${v} (median ${med}, ${reason} outlier) -> re-measuring"
                run_one "${arm}" "${tag}"
                printf -v "${rvar}" '%s' "$(( ${!rvar} + 1 ))"
                REMEASURED="${REMEASURED} ${tag}[${reason},was=${v}]"
                changed=1
            else
                echo "  ${tag} = ${v} (median ${med}, ${reason} outlier), retry budget exhausted"
            fi
        done
    done
    [ "${changed}" -eq 1 ] || break
    attempt=$(( attempt + 1 ))
done
echo "  re-measured:${REMEASURED:- none}"

echo
echo "================= Results ${RUN_ID} ================="
med_default="$(median_of_values $(values_for_arm default))"
med_syncoff="$(median_of_values $(values_for_arm syncoff))"
med_maxoff="$(median_of_values $(values_for_arm maxoff))"

for arm in ${ARMS}; do
    case "${arm}" in
        default) med="${med_default}" ;;
        syncoff) med="${med_syncoff}" ;;
        maxoff)  med="${med_maxoff}" ;;
    esac
    printf '%-9s median=%9.3f  runs=[%s]\n' "${arm}" "${med}" \
        "$(values_for_arm "${arm}" | tr '\n' ' ')"
done

echo
band_ok=0
if [ -f "${BAND}" ]; then
    band_info="$(band_field "${BAND}")"
    band_basis="${band_info%%$'\n'*}"
    band_threshold="${band_info#*$'\n'}"
    if [ -n "${band_basis}" ] && [ "${band_basis}" != "none" ]; then
        band_ok=1
        echo "TPC-C A/A noise band = ${band_threshold}% (basis: ${band_basis}; ${BAND})"
    fi
fi
if [ "${band_ok}" -eq 0 ]; then
    echo "TPC-C A/A noise band = UNKNOWN: no usable band at ${BAND}"
    echo "Refusing to print a PROVEN/within-noise verdict without a measured band."
fi
echo
if [ "${band_ok}" -eq 1 ]; then
    printf '%-9s %10s %14s   %s\n' "arm" "delta%" "vs band" "durability cost"
else
    printf '%-9s %10s %14s   %s\n' "arm" "delta%" "verdict" "durability cost"
fi
for arm in ${ARMS}; do
    [ "${arm}" = "default" ] && continue
    case "${arm}" in
        syncoff) med="${med_syncoff}"; cost="os crash may lose last ~200ms of commits" ;;
        maxoff)  med="${med_maxoff}";  cost="+ torn pages possible after crash" ;;
    esac
    d="$(awk -v a="${med}" -v b="${med_default}" 'BEGIN{printf "%.2f", (a-b)/b*100}')"
    if [ "${band_ok}" -eq 1 ]; then
        proven="$(awk -v d="${d}" -v s="${band_threshold}" 'BEGIN{print ((d<0?-d:d) > s) ? "PROVEN" : "within noise"}')"
    else
        proven="UNKNOWN (no band)"
    fi
    printf '%-9s %9s%% %14s   %s\n' "${arm}" "${d}" "${proven}" "${cost}"
done
echo
echo "Raw CSVs: ${ROOT}/results/tpcc/<arm>_r<rep>_${RUN_ID}.csv"
