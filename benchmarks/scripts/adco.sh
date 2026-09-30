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
sandbox_dir="${exp_path}/out/${dir_name}_adco"


CMD="uv run python -m src.adco $source_dir \
            --model=gemini-3.5-flash-lite \
            --sandbox-dir=$sandbox_dir \
            --db-type=$db_type \
            --db-name=$db_name \
            --cpu-cores=$cpu_cores \
            --memory=$memory_gb \
            --apply-mode=persist-static \
            --verbose"
$CMD
