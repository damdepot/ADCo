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


# Durability policy is opt-in via the environment; default stays strict.
durability_profile="${DURABILITY_PROFILE:-strict}"

# Screening dataset sizing. Raise SCREEN_MAX_ROWS when the target's working set
# exceeds the instance memory: otherwise the proxy dataset stays cache-resident
# and the gate is blind to memory-knob improvements.
screen_total_rows="${SCREEN_TOTAL_ROWS:-0}"
screen_max_rows="${SCREEN_MAX_ROWS:-5000000}"
confirm_repetitions="${CONFIRM_REPETITIONS:-5}"

# Screening gate measurement: sysbench OLTP (default) or pgbench sort/hash.
screening_benchmark="${SCREENING_BENCHMARK:-sysbench}"

# Human-readable production workload context shown to the recommender.
workload_hint="${WORKLOAD_HINT:-}"

# Arguments are passed positionally (rather than through a word-split command
# string) so a multi-word workload hint survives as a single argv element.
uv run python -m src.adco "$source_dir" \
    --model=gemini-3.5-flash-lite \
    --mode=tune-only \
    --db-type="$db_type" \
    --db-name="$db_name" \
    --cpu-cores="$cpu_cores" \
    --memory="$memory_gb" \
    --apply-mode=persist-static \
    --durability-profile="$durability_profile" \
    --screen-total-rows="$screen_total_rows" \
    --screen-max-rows="$screen_max_rows" \
    --confirm-repetitions="$confirm_repetitions" \
    --screening-benchmark="$screening_benchmark" \
    --workload-hint="$workload_hint" \
    --verbose
