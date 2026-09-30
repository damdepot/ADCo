#!/usr/bin/env bash
#
# DCo tuning campaign: N rounds of (reset precondition -> DCo -> production A/B).
#
# Each round runs ONE full DCo tune-and-apply pass against a deterministically
# reset Postgres, then measures the live production workload twice:
#
#   arm "default"  pure Postgres defaults (ALTER SYSTEM RESET ALL + restart)
#   arm "dco"      exactly the knobs DCo applied in this round
#
# The two arms are interleaved rep-major (default then dco within each rep) so
# monotonic host drift cancels between arms. When DCo promotes nothing the two
# arms are identical configs: that is the per-round A/A control.
#
# The production benchmark is the in-container pgbench sort/hash query
# (benchmarks/tools/pgbench/sort_hash.pgb) that reads sbtest1 and is acutely
# sensitive to work_mem. All artifacts land in results/campaign/:
#
#   <scenario>_r<N>_<RUN_ID>.log    full DCo stdout/stderr for the round
#   <scenario>_r<N>_<RUN_ID>.json   structured per-round record
#   <scenario>_r<N>_<RUN_ID>_<arm>.csv(.json)   raw production samples
#
# Aggregate with:
#   .venv/bin/python benchmarks/scripts/campaign_report.py results/campaign/*.json
#
# Scenarios
#   s1_positive  defaults precondition,         expected lever = work_mem
#   s2_null      work_mem=256MB precondition,   expected = NO promotion
#
# Environment knobs (all optional):
#   ROUNDS=3 AB_REPS=3 SETTLE=10 PROD_SECONDS=30 PROD_CLIENTS=2 PROD_THREADS=2
#   FIRST_ROUND=2      resume an interrupted campaign without redoing round 1
#   TABLES=10 TABLE_SIZE=1500000 SYSBENCH_THREADS=4
#   SCREEN_MAX_ROWS=16000000 CONFIRM_REPETITIONS=5 DURABILITY_PROFILE=strict
#   SMOKE=1            shrink to 1 round of s2_null, 10x20000 rows, AB_REPS=1
#   SMOKE_SKIP_DCO=1   exercise all plumbing without invoking DCo
#
# Bash 3.2 compatible (macOS /bin/bash): no associative arrays.
#
# Usage:
#   bash benchmarks/run9_dco_campaign.sh s1_positive
#   ROUNDS=5 bash benchmarks/run9_dco_campaign.sh s2_null
#   SMOKE=1 bash benchmarks/run9_dco_campaign.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="${ROOT}/benchmarks/scripts"

CMDDocker="${SCRIPTS}/docker.sh"
CMDRunDCo="${SCRIPTS}/dco.sh"
SYSMEAS="${SCRIPTS}/sysbench_measure.py"
PGMEAS="${SCRIPTS}/pgbench_measure.py"
PGBSCRIPT="${ROOT}/benchmarks/tools/pgbench/sort_hash.pgb"
PYTHON="${ROOT}/.venv/bin/python"

C="adcoexp-db"
SERVICE="pgdb"
DB="adcodb"
DBTYPE="postgres"
# Application target scanned by DCo. Matches the existing successful pgbench
# screening runs: the TPC-C app + a pgbench screening workload + a workload hint.
DIRNAME="${DCO_DIRNAME:-tpcc}"

# ── Scenario selection ──
HAS_SCENARIO_ARG=0
[ "$#" -ge 1 ] && HAS_SCENARIO_ARG=1
SCENARIO="${1:-${SCENARIO:-s1_positive}}"

SMOKE="${SMOKE:-0}"
SMOKE_SKIP_DCO="${SMOKE_SKIP_DCO:-${NO_DCO:-0}}"

ROUNDS="${ROUNDS:-3}"
AB_REPS="${AB_REPS:-3}"
SETTLE="${SETTLE:-10}"
PROD_SECONDS="${PROD_SECONDS:-30}"
PROD_CLIENTS="${PROD_CLIENTS:-2}"
PROD_THREADS="${PROD_THREADS:-2}"
TABLES="${TABLES:-10}"
TABLE_SIZE="${TABLE_SIZE:-1500000}"
SYSBENCH_THREADS="${SYSBENCH_THREADS:-4}"

# ── Smoke mode: shrink the protocol to prove plumbing end to end. DCo itself
# cannot be shortened, so we only shrink the dataset and repetition counts.
if [ "${SMOKE}" = "1" ]; then
    [ "${HAS_SCENARIO_ARG}" -eq 1 ] || SCENARIO="s2_null"
    ROUNDS=1
    AB_REPS=1
    TABLES=10
    TABLE_SIZE="${SMOKE_TABLE_SIZE:-20000}"
    PROD_SECONDS="${SMOKE_PROD_SECONDS:-5}"
    SETTLE="${SMOKE_SETTLE:-2}"
    SYSBENCH_THREADS="${SMOKE_SYSBENCH_THREADS:-2}"
    echo "[run9] SMOKE mode: scenario=${SCENARIO} rounds=${ROUNDS} tables=${TABLES} table_size=${TABLE_SIZE} prod_seconds=${PROD_SECONDS} skip_dco=${SMOKE_SKIP_DCO}"
fi

# ── Per-scenario contract ──
case "${SCENARIO}" in
    s1_positive)
        CPU_CORES="${CPU_CORES:-2}"
        MEMORY_GB="${MEMORY_GB:-2}"
        # Empty = leave Postgres defaults (work_mem=4MB) as the precondition.
        PRECONDITION_WORKMEM="${PRECONDITION_WORKMEM:-}"
        SCREENING_BENCHMARK="${SCREENING_BENCHMARK:-pgbench}"
        SCREEN_MAX_ROWS="${SCREEN_MAX_ROWS:-16000000}"
        CONFIRM_REPETITIONS="${CONFIRM_REPETITIONS:-5}"
        WORKLOAD_HINT="${WORKLOAD_HINT:-Production workload: an analytical reporting query performs a heavy GROUP BY over the wide (c, pad) columns of ~1.5M rows in sbtest1. At the default work_mem the sort/hash spills to disk; it needs about 256MB of work_mem to keep the hash aggregate in memory.}"
        EXPECTED_LEVER="work_mem"
        ;;
    s2_null)
        CPU_CORES="${CPU_CORES:-2}"
        MEMORY_GB="${MEMORY_GB:-2}"
        # Already-optimal precondition: work_mem is pre-set to the lever value.
        PRECONDITION_WORKMEM="${PRECONDITION_WORKMEM:-256MB}"
        SCREENING_BENCHMARK="${SCREENING_BENCHMARK:-pgbench}"
        SCREEN_MAX_ROWS="${SCREEN_MAX_ROWS:-16000000}"
        CONFIRM_REPETITIONS="${CONFIRM_REPETITIONS:-5}"
        # No hint: the already-optimal config should promote nothing.
        WORKLOAD_HINT="${WORKLOAD_HINT:-}"
        EXPECTED_LEVER="none"
        ;;
    *)
        echo "ERROR: unknown scenario '${SCENARIO}'. Supported: s1_positive, s2_null." >&2
        exit 2
        ;;
esac

# Smoke takes the tiny screen config last so it wins over scenario defaults.
if [ "${SMOKE}" = "1" ]; then
    SCREEN_MAX_ROWS=200000
    CONFIRM_REPETITIONS="${CONFIRM_REPETITIONS_SMOKE:-2}"
fi

export CPU_CORES MEMORY_GB

OUT="${ROOT}/results/campaign"
TMP="${OUT}/.tmp"
mkdir -p "${OUT}" "${TMP}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"

# ── psql helper ──
psql() { docker exec -u postgres "$C" psql -v ON_ERROR_STOP=1 -q "$@"; }

# Always leave the production DB at pure Postgres defaults on exit (success,
# failure or interrupt) so no tuned GUC survives the harness.
restore_defaults() {
    psql -d "$DB" -c "ALTER SYSTEM RESET ALL;" >/dev/null 2>&1 || true
    psql -d "$DB" -c "SELECT pg_reload_conf();" >/dev/null 2>&1 || true
    "$CMDDocker" Restart "$C" >/dev/null 2>&1 || true
    "$CMDDocker" WaitFor "$C" >/dev/null 2>&1 || true
}
trap restore_defaults EXIT

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

verify_precondition() {
    local wm sb
    wm="$(psql -d "$DB" -tAc "SHOW work_mem;")"
    sb="$(psql -d "$DB" -tAc "SHOW shared_buffers;")"
    echo "    precondition: work_mem=${wm} shared_buffers=${sb} cpu=${CPU_CORES} mem=${MEMORY_GB}GB"
    if [ -n "${PRECONDITION_WORKMEM}" ]; then
        if [ "${wm}" != "${PRECONDITION_WORKMEM}" ]; then
            echo "ERROR: expected work_mem=${PRECONDITION_WORKMEM}, got ${wm}" >&2
            exit 1
        fi
    else
        if [ "${wm}" != "4MB" ]; then
            echo "WARNING: expected default work_mem=4MB, got ${wm}" >&2
        fi
    fi
}

# Step 1: deterministic precondition reset.
reset_precondition() {
    echo ">>> reset: Down/Up ${C} (cpu=${CPU_CORES} mem=${MEMORY_GB}GB)"
    "$CMDDocker" Down "$C" >/dev/null
    "$CMDDocker" Up "$C" "$SERVICE" >/dev/null
    "$CMDDocker" WaitFor "$C" >/dev/null

    echo ">>> reset: rebuild dataset ${TABLES}x${TABLE_SIZE} in ${DB}"
    "$PYTHON" "$SYSMEAS" \
        --output "${TMP}/rebuild_${RUN_ID}.csv" \
        --db-name "$DB" --tables "$TABLES" --table-size "$TABLE_SIZE" \
        --threads "$SYSBENCH_THREADS" --seconds 1 --runs 1 --repetitions 1 \
        --prepare --label rebuild >/dev/null

    echo ">>> reset: ANALYZE ${DB}"
    psql -d "$DB" -c "ANALYZE;" >/dev/null

    echo ">>> reset: apply scenario precondition"
    psql -d "$DB" -c "ALTER SYSTEM RESET ALL;" >/dev/null
    if [ -n "${PRECONDITION_WORKMEM}" ]; then
        psql -d "$DB" -c "ALTER SYSTEM SET work_mem = '${PRECONDITION_WORKMEM}';" >/dev/null
    fi
    psql -d "$DB" -c "SELECT pg_reload_conf();" >/dev/null
    "$CMDDocker" Restart "$C" >/dev/null
    "$CMDDocker" WaitFor "$C" >/dev/null
    verify_precondition
}

# Snapshot the dco result dirs that exist before a run so a failed run cannot be
# mistaken for an old one.
snapshot_dco_dirs() {
    ls -1d "${ROOT}"/results/dco/*/ 2>/dev/null | sed 's:/$::' > "$1" || true
}

new_run_dir_since() {
    local before="$1" d
    for d in $(ls -1dt "${ROOT}"/results/dco/*/ 2>/dev/null | sed 's:/$::'); do
        if ! grep -qx "$d" "$before" 2>/dev/null; then
            echo "$d"
            return 0
        fi
    done
    echo ""
}

# Step 2: run DCo, tee its log, resolve its run directory.
run_dco() {
    local log="$1" before="$2"
    if [ "${SMOKE_SKIP_DCO}" = "1" ]; then
        {
            echo "[run9] SMOKE_SKIP_DCO=1: skipping the DCo invocation (no LLM calls)."
            echo "Tuner status:        SMOKE_SKIPPED"
        } | tee "$log"
        DCO_STATUS="SMOKE_SKIPPED"
        DCO_RUN_DIR=""
        return 0
    fi

    echo ">>> DCo: screening=${SCREENING_BENCHMARK} max_rows=${SCREEN_MAX_ROWS} confirm_reps=${CONFIRM_REPETITIONS} durability=${DURABILITY_PROFILE:-strict}"
    echo ">>> DCo: workload_hint='${WORKLOAD_HINT}'"

    local rc=0
    set +e
    (
        cd "$ROOT"
        DURABILITY_PROFILE="${DURABILITY_PROFILE:-strict}" \
        SCREENING_BENCHMARK="${SCREENING_BENCHMARK}" \
        SCREEN_MAX_ROWS="${SCREEN_MAX_ROWS}" \
        SCREEN_TOTAL_ROWS="${SCREEN_TOTAL_ROWS:-0}" \
        CONFIRM_REPETITIONS="${CONFIRM_REPETITIONS}" \
        WORKLOAD_HINT="${WORKLOAD_HINT}" \
            "$CMDRunDCo" "$DIRNAME" "$DBTYPE" "$DB" "$CPU_CORES" "$MEMORY_GB"
    ) 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    set -e
    echo ">>> DCo exit code: ${rc}"

    DCO_STATUS="$(grep -o 'Tuner status:.*' "$log" | tail -1 | sed 's/.*Tuner status:[[:space:]]*//' | tr -d '\r' || true)"
    [ -n "${DCO_STATUS:-}" ] || DCO_STATUS="UNKNOWN"

    # Authoritative source: the "Artifacts written to <dir>" log line.
    local d
    d="$(grep -o 'Artifacts written to .*' "$log" | tail -1 | sed 's/.*Artifacts written to //' | tr -d '\r' || true)"
    if [ -z "$d" ] || [ ! -d "$d" ]; then
        d="$(new_run_dir_since "$before")"
    fi
    DCO_RUN_DIR="$d"

    # DCo applies the promoted config to the live DB with --apply-mode=persist-static.
    # Restart so any postmaster-level knob takes effect before we snapshot it.
    "$CMDDocker" Restart "$C" >/dev/null
    "$CMDDocker" WaitFor "$C" >/dev/null
}

# Emit <knob>\t<value> for every knob DCo actually applied.
extract_applied_knobs() {
    local manifest="$1" out="$2"
    "$PYTHON" - "$manifest" "$out" <<'PY'
import json, sys
src, out = sys.argv[1], sys.argv[2]
rows = []
try:
    with open(src) as fh:
        m = json.load(fh)
    for k in m.get("applied_knobs") or []:
        if k.get("status") == "applied":
            rows.append((str(k.get("knob", "")), str(k.get("value", ""))))
except Exception:
    rows = []
with open(out, "w") as fh:
    for n, v in rows:
        fh.write("%s\t%s\n" % (n, v))
PY
}

# Step 3: production A/B.
apply_default_arm() {
    psql -d "$DB" -c "ALTER SYSTEM RESET ALL;" >/dev/null
    psql -d "$DB" -c "SELECT pg_reload_conf();" >/dev/null
    "$CMDDocker" Restart "$C" >/dev/null
    "$CMDDocker" WaitFor "$C" >/dev/null
    "$CMDDocker" Checkpoint "$C" >/dev/null
    sleep "${SETTLE}"
}

apply_dco_arm() {
    psql -d "$DB" -c "ALTER SYSTEM RESET ALL;" >/dev/null
    local i=0 n v
    while [ "$i" -lt "${#DCO_KNOB_NAMES[@]}" ]; do
        n="${DCO_KNOB_NAMES[$i]}"
        v="${DCO_KNOB_VALUES[$i]}"
        case "$n" in
            *[!A-Za-z0-9_.]* | "")
                echo "WARNING: skipping unsafe knob name '${n}'" >&2
                ;;
            *)
                psql -d "$DB" -c "ALTER SYSTEM SET ${n} = '${v}';" >/dev/null
                ;;
        esac
        i=$((i + 1))
    done
    psql -d "$DB" -c "SELECT pg_reload_conf();" >/dev/null
    "$CMDDocker" Restart "$C" >/dev/null
    "$CMDDocker" WaitFor "$C" >/dev/null
    "$CMDDocker" Checkpoint "$C" >/dev/null
    sleep "${SETTLE}"
}

measure_pgbench() {
    local arm="$1" rep="$2" out="$3"
    "$PYTHON" "$PGMEAS" \
        --output "$out" \
        --db-name "$DB" \
        --clients "${PROD_CLIENTS}" --threads "${PROD_THREADS}" \
        --seconds "${PROD_SECONDS}" --script "${PGBSCRIPT}" \
        --label "${arm} r${rep}" >/dev/null
    total_of "$out"
}

write_round_json() {
    local json="$1"
    "$PYTHON" - "$json" \
        "${SCENARIO}" "${ROUND}" "${RUN_ID}" "${DCO_RUN_DIR}" "${DCO_STATUS}" \
        "${MANIFEST}" "${RESULT_JSON}" "${REVERSAL_STATUS}" "${DCO_WALL_FALLBACK}" \
        "${MED_DEFAULT}" "${MED_DCO}" "${EXPECTED_LEVER}" \
        "${AB_REPS}" "${TABLES}" "${TABLE_SIZE}" "${CPU_CORES}" "${MEMORY_GB}" "${SMOKE}" \
        "${DEFAULT_TPS_LIST}" "${DCO_TPS_LIST}" "${ROUND_VALID}" "${INVALID_REASON}" <<'PY'
import json, os, sys

(p, scenario, rnd, campaign_run_id, dco_run_dir, dco_status,
 manifest_p, result_p, rev_p, wall_fb,
 med_default, med_dco, expected, ab_reps, tables, tsize, cpu, mem, smoke,
 default_list, dco_list, round_valid, invalid_reason) = sys.argv[1:24]

valid = str(round_valid).strip() == "1"

applied = []
if manifest_p and os.path.exists(manifest_p):
    try:
        with open(manifest_p) as fh:
            m = json.load(fh)
        for k in m.get("applied_knobs") or []:
            if k.get("status") == "applied":
                applied.append({
                    "knob": k.get("knob"),
                    "value": k.get("value"),
                    "status": k.get("status"),
                })
    except Exception:
        applied = []

names = [k["knob"] for k in applied]

lcb = None
wall = None
if result_p and os.path.exists(result_p):
    try:
        with open(result_p) as fh:
            r = json.load(fh)
        attempts = r.get("validation_attempts") or []
        if attempts:
            confirm = attempts[-1].get("confirm") or {}
            if confirm.get("lcb_pct") is not None:
                lcb = float(confirm["lcb_pct"])
            walls = [a.get("wall_seconds") for a in attempts
                     if a.get("wall_seconds") is not None]
            if walls:
                wall = float(sum(walls))
    except Exception:
        pass
if wall is None and wall_fb:
    try:
        wall = float(wall_fb)
    except ValueError:
        wall = None

reversal = None
if rev_p and os.path.exists(rev_p):
    try:
        with open(rev_p) as fh:
            reversal = bool(json.load(fh).get("measured"))
    except Exception:
        reversal = None

def _f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None

prod_default = _f(med_default)
prod_dco = _f(med_dco)
prod_delta = (
    (prod_dco - prod_default) / prod_default * 100.0
    if (prod_default and prod_dco is not None)
    else None
)
transfer = (lcb - prod_delta) if (lcb is not None and prod_delta is not None) else None

if not valid:
    hit = None
elif expected == "work_mem":
    hit = "work_mem" in names
elif expected == "none":
    hit = (len(applied) == 0)
else:
    hit = False

def floats(s):
    return [float(x) for x in s.split() if x.strip()]

record = {
    "scenario": scenario,
    "round": int(rnd),
    "valid": valid,
    "invalid_reason": (str(invalid_reason) or None) if not valid else None,
    "run_id": campaign_run_id,
    "dco_run_id": os.path.basename(dco_run_dir) if dco_run_dir else None,
    "dco_status": dco_status,
    "dco_applied_knobs": applied,
    "dco_applied_knob_names": names,
    "dco_gate_lcb": lcb,
    "dco_reversal_measured": reversal,
    "dco_wall_seconds": wall,
    "ground_truth_expectation": expected,
    "ground_truth_hit": (None if hit is None else bool(hit)),
    "prod_default_median_tps": prod_default,
    "prod_dco_median_tps": prod_dco,
    "prod_delta_pct": prod_delta,
    "transfer_gap_pct": transfer,
    "prod_default_tps": floats(default_list),
    "prod_dco_tps": floats(dco_list),
    "ab_reps": int(ab_reps),
    "tables": int(tables),
    "table_size": int(tsize),
    "cpu_cores": float(cpu),
    "memory_gb": float(mem),
    "smoke": bool(int(smoke)),
}

with open(p, "w") as fh:
    json.dump(record, fh, indent=2)
    fh.write("\n")
print("    wrote %s" % p)
PY
}

echo "================= DCo campaign ${RUN_ID} ================="
echo "scenario=${SCENARIO} rounds=${ROUNDS} ab_reps=${AB_REPS} expected_lever=${EXPECTED_LEVER}"
echo "screening=${SCREENING_BENCHMARK} max_rows=${SCREEN_MAX_ROWS} confirm_reps=${CONFIRM_REPETITIONS}"
echo "dataset=${TABLES}x${TABLE_SIZE} prod=${PROD_SECONDS}s clients=${PROD_CLIENTS} threads=${PROD_THREADS}"
echo "results -> ${OUT}"

ROUND="${FIRST_ROUND:-1}"
while [ "$ROUND" -le "$ROUNDS" ]; do
    echo
    echo "=========== ROUND ${ROUND}/${ROUNDS} (${SCENARIO}) ==========="

    reset_precondition

    LOG="${OUT}/${SCENARIO}_r${ROUND}_${RUN_ID}.log"
    JSON_OUT="${OUT}/${SCENARIO}_r${ROUND}_${RUN_ID}.json"
    DCO_BEFORE="${TMP}/dco_before_r${ROUND}_${RUN_ID}.txt"
    snapshot_dco_dirs "$DCO_BEFORE"

    DCO_STATUS="UNKNOWN"
    DCO_RUN_DIR=""
    run_dco "$LOG" "$DCO_BEFORE"
    echo ">>> DCo status: ${DCO_STATUS}"
    echo ">>> DCo run dir: ${DCO_RUN_DIR:-<none>}"

    MANIFEST=""
    RESULT_JSON=""
    REVERSAL_STATUS=""
    DCO_WALL_FALLBACK=""
    if [ -n "${DCO_RUN_DIR}" ] && [ -d "${DCO_RUN_DIR}" ]; then
        [ -f "${DCO_RUN_DIR}/manifest.json" ] && MANIFEST="${DCO_RUN_DIR}/manifest.json"
        [ -f "${DCO_RUN_DIR}/result.json" ] && RESULT_JSON="${DCO_RUN_DIR}/result.json"
        [ -f "${DCO_RUN_DIR}/baseline-reversal-status.json" ] && REVERSAL_STATUS="${DCO_RUN_DIR}/baseline-reversal-status.json"
    fi

    KNOB_TSV="${TMP}/knobs_r${ROUND}_${RUN_ID}.tsv"
    : > "$KNOB_TSV"
    [ -n "$MANIFEST" ] && extract_applied_knobs "$MANIFEST" "$KNOB_TSV"

    DCO_KNOB_NAMES=()
    DCO_KNOB_VALUES=()
    while IFS=$'\t' read -r kn kv; do
        [ -z "$kn" ] && continue
        DCO_KNOB_NAMES+=("$kn")
        DCO_KNOB_VALUES+=("$kv")
    done < "$KNOB_TSV"
    echo ">>> DCo applied knobs: ${DCO_KNOB_NAMES[*]:-<none>}"

    # A round where DCo produced no run directory (auth/network failure, crash)
    # has no evidence to compare: measuring "default vs default" would report a
    # meaningless delta as if it were a result. Mark it invalid and skip the A/B.
    ROUND_VALID=1
    INVALID_REASON=""
    if [ -z "${DCO_RUN_DIR}" ] || [ ! -d "${DCO_RUN_DIR}" ]; then
        ROUND_VALID=0
        INVALID_REASON="DCo produced no run (status=${DCO_STATUS}); no production A/B performed."
        echo ">>> ROUND INVALID: ${INVALID_REASON}"
    fi

    MED_DEFAULT=""
    MED_DCO=""
    DEFAULT_TPS_LIST=""
    DCO_TPS_LIST=""
    if [ "${ROUND_VALID}" -eq 1 ]; then
        echo ">>> production A/B (interleaved, rep-major; ${AB_REPS} reps x ${PROD_SECONDS}s)"
        rep=1
        while [ "$rep" -le "$AB_REPS" ]; do
            echo "    rep ${rep}/${AB_REPS}: default"
            apply_default_arm
            tps="$(measure_pgbench default "$rep" "${TMP}/${SCENARIO}_r${ROUND}_default_r${rep}_${RUN_ID}.csv")"
            echo "        default r${rep}: ${tps} txn/s"
            DEFAULT_TPS_LIST="${DEFAULT_TPS_LIST} ${tps}"

            echo "    rep ${rep}/${AB_REPS}: dco"
            apply_dco_arm
            tps="$(measure_pgbench dco "$rep" "${TMP}/${SCENARIO}_r${ROUND}_dco_r${rep}_${RUN_ID}.csv")"
            echo "        dco r${rep}: ${tps} txn/s"
            DCO_TPS_LIST="${DCO_TPS_LIST} ${tps}"
            rep=$((rep + 1))
        done

        MED_DEFAULT="$(median_of ${DEFAULT_TPS_LIST})"
        MED_DCO="$(median_of ${DCO_TPS_LIST})"
        echo ">>> medians: default=${MED_DEFAULT} dco=${MED_DCO}"
    fi

    write_round_json "$JSON_OUT"

    ROUND=$((ROUND + 1))
done

echo
echo "================= Campaign complete ${RUN_ID} ================="
echo "Round JSONs: ${OUT}/${SCENARIO}_r*_${RUN_ID}.json"
echo "Aggregate with: ${PYTHON} ${SCRIPTS}/campaign_report.py ${OUT}/${SCENARIO}_r*_${RUN_ID}.json"
