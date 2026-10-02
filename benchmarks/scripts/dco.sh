#!/bin/bash
set -euo pipefail

dir_name="${1:-}"
db_type="${2:-}"
db_name="${3:-}"
cpu_cores="${4:-}"
memory_gb="${5:-}"

source "$(cd "$(dirname "$0")" && pwd)/bench_lib.sh"
validate_tuning_args

exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
source_dir="${exp_path}/benchmarks/tools/$dir_name"


# Screening dataset sizing. Raise SCREEN_MAX_ROWS when the target's working set
# exceeds the instance memory: otherwise the proxy dataset stays cache-resident
# and the gate is blind to memory-knob improvements.
screen_total_rows="${SCREEN_TOTAL_ROWS:-0}"
screen_max_rows="${SCREEN_MAX_ROWS:-5000000}"

# Screening gate measurement: sysbench OLTP (default) or pgbench sort/hash.
screening_benchmark="${SCREENING_BENCHMARK:-sysbench}"

# Measurement timing. Defaults match the tuner CLI (10/10/2/4).
measure_reps="${MEASURE_REPS:-10}"
measure_seconds="${MEASURE_SECONDS:-10}"
measure_warmup_seconds="${MEASURE_WARMUP_SECONDS:-2}"
early_stop_min_reps="${EARLY_STOP_MIN_REPS:-4}"

# Tuner loop caps. Defaults match the tuner (10/20).
max_attempts="${MAX_ATTEMPTS:-10}"
max_set_knobs="${MAX_SET_KNOBS:-20}"

uv run python -m src.adco "$source_dir" \
    --model=gemini-3.5-flash-lite \
    --mode=tune-only \
    --db-type="$db_type" \
    --db-name="$db_name" \
    --cpu-cores="$cpu_cores" \
    --memory="$memory_gb" \
    --apply-mode=live \
    --screen-total-rows="$screen_total_rows" \
    --screen-max-rows="$screen_max_rows" \
    --screening-benchmark="$screening_benchmark" \
    --measure-reps="$measure_reps" \
    --measure-seconds="$measure_seconds" \
    --measure-warmup-seconds="$measure_warmup_seconds" \
    --early-stop-min-reps="$early_stop_min_reps" \
    --max-attempts="$max_attempts" \
    --max-set-knobs="$max_set_knobs" \
    --verbose
