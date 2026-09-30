#!/usr/bin/env bash
#
# Settled-config A/B on production.
#
# The tuner's gate measures baseline immediately after it bulk-loads the
# screening dataset. On a 15M-row dataset autovacuum is still churning at that
# moment, so baseline is depressed and every candidate looks like a win -- which
# is how a 2-no-op + autovacuum config scored "+45.71%". This script measures
# all arms from an EQUALLY SETTLED state (forced CHECKPOINT + idle period after
# every config change) so the comparison is honest.
#
# Deltas are judged against the sysbench A/A noise band
# (results/sysbench/noise_band.json, produced by run5_noise_sysbench.sh). If
# that band is missing the script prints UNKNOWN and refuses to call anything
# PROVEN rather than inventing a threshold. All arm settings are reset to
# Postgres defaults on exit (including on failure/interrupt).
#
# Arms:
#   default  pure Postgres defaults
#   dco      exactly what the tuner applied in run 20260929T120542Z
#   sbuf     shared_buffers=1GB + effective_cache_size=1536MB (the real lever
#            for an I/O-bound working set larger than instance memory)
#
# Usage: REPS=3 bash benchmarks/run7_settled_ab.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"
CMDDocker="${SCRIPTS}/docker.sh"
SYSMEAS="${SCRIPTS}/sysbench_measure.py"
PYTHON="${ROOT}/.venv/bin/python"

C=adcoexp-db
DB=adcodb
REPS="${REPS:-3}"
SETTLE="${SETTLE:-20}"
THREADS="${THREADS:-16}"
SECONDS_MEASURED="${SECONDS_MEASURED:-30}"
TABLE_SIZE="${TABLE_SIZE:-1500000}"
TABLES="${TABLES:-10}"

OUT="${ROOT}/results/settled_ab"
mkdir -p "${OUT}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
BAND="${ROOT}/results/sysbench/noise_band.json"

psql() { docker exec -u postgres "$C" psql -v ON_ERROR_STOP=1 -q "$@"; }

# Fail loudly if a CSV has no parseable TOTAL row, so an empty value can never
# flow into a median.
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

# Print the band's threshold basis then value (same precedence as tpcc_delta.py).
band_field() {
    "$PYTHON" -c '
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

# Always restore the production database to pure Postgres defaults so we never
# leave a tuned GUC behind (shared_buffers needs a restart to take effect).
restore_defaults() {
    psql -c "ALTER SYSTEM RESET ALL;" >/dev/null 2>&1 || true
    psql -c "SELECT pg_reload_conf();" >/dev/null 2>&1 || true
    "$CMDDocker" Restart "$C" >/dev/null 2>&1 || true
    "$CMDDocker" WaitFor "$C" >/dev/null 2>&1 || true
}
trap restore_defaults EXIT

apply_arm() {
    psql -c "ALTER SYSTEM RESET ALL;" >/dev/null
    case "$1" in
        default) ;;
        dco)
            psql -c "ALTER SYSTEM SET effective_cache_size = '4GB';" \
                 -c "ALTER SYSTEM SET checkpoint_completion_target = 0.9;" \
                 -c "ALTER SYSTEM SET autovacuum_vacuum_scale_factor = 0.15;" \
                 -c "ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 300;" >/dev/null
            psql -c "SELECT pg_reload_conf();" >/dev/null ;;
        sbuf)
            psql -c "ALTER SYSTEM SET shared_buffers = '1GB';" \
                 -c "ALTER SYSTEM SET effective_cache_size = '1536MB';" >/dev/null
            "$CMDDocker" Restart "$C" >/dev/null
            "$CMDDocker" WaitFor "$C" >/dev/null ;;
        *) echo "unknown arm $1" >&2; exit 2 ;;
    esac
    # Equal settling for every arm: flush, then let autovacuum quiesce.
    "$CMDDocker" Checkpoint "$C" >/dev/null
    sleep "${SETTLE}"
}

measure() {
    local arm="$1" rep="$2" csv
    csv="${OUT}/${arm}_r${rep}_${RUN_ID}.csv"
    "$PYTHON" "$SYSMEAS" --output "$csv" \
        --db-name "$DB" --tables "$TABLES" --table-size "$TABLE_SIZE" \
        --threads "$THREADS" --seconds "$SECONDS_MEASURED" \
        --runs 1 --repetitions 1 --no-prepare \
        --label "${arm} r${rep}" >/dev/null 2>&1
    total_of "$csv"
}

median_of() { printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'; }

ARMS="default dco sbuf"
TAGS=""
for rep in $(seq 1 "${REPS}"); do
    for arm in ${ARMS}; do
        echo "---- rep ${rep}: ${arm} ----"
        apply_arm "${arm}"
        tps="$(measure "${arm}" "${rep}")"
        echo "    ${arm} r${rep}: ${tps} txn/s"
        TAGS="${TAGS} ${arm}_r${rep}_${RUN_ID}"
    done
done

echo
echo "================= Settled A/B ${RUN_ID} ================="
med_default=""
for arm in ${ARMS}; do
    vals=""
    for tag in ${TAGS}; do
        case "${tag}" in "${arm}_"*) vals="${vals} $(total_of "${OUT}/${tag}.csv")" ;; esac
    done
    med="$(median_of ${vals})"
    [ "${arm}" = "default" ] && med_default="${med}"
    printf '%-8s median=%9.3f  runs=[%s ]\n' "${arm}" "${med}" "${vals}"
done

echo
band_ok=0
if [ -f "${BAND}" ]; then
    band_info="$(band_field "${BAND}")"
    band_basis="${band_info%%$'\n'*}"
    band_threshold="${band_info#*$'\n'}"
    if [ -n "${band_basis}" ] && [ "${band_basis}" != "none" ]; then
        band_ok=1
        echo "sysbench A/A noise band = ${band_threshold}% (basis: ${band_basis}; ${BAND})"
    fi
fi
if [ "${band_ok}" -eq 0 ]; then
    echo "sysbench A/A noise band = UNKNOWN: no usable band at ${BAND}"
    echo "Refusing to call any delta PROVEN without a measured band."
fi
echo
printf '%-8s %9s   %s\n' "arm" "delta%" "verdict"
for arm in ${ARMS}; do
    [ "${arm}" = "default" ] && continue
    vals=""
    for tag in ${TAGS}; do
        case "${tag}" in "${arm}_"*) vals="${vals} $(total_of "${OUT}/${tag}.csv")" ;; esac
    done
    med="$(median_of ${vals})"
    d="$(awk -v a="${med}" -v b="${med_default}" 'BEGIN{printf "%.2f", (a-b)/b*100}')"
    if [ "${band_ok}" -eq 1 ]; then
        verdict="$(awk -v d="${d}" -v s="${band_threshold}" 'BEGIN{print ((d<0?-d:d) > s) ? "PROVEN" : "within noise"}')"
    else
        verdict="UNKNOWN (no band)"
    fi
    printf '%-8s %8s%%   %s\n' "${arm}" "${d}" "${verdict}"
done
