#!/bin/bash
set -euo pipefail

dir_name="${1:-}"
db_type="${2:-}"
db_name="${3:-}"
cpu_cores="${4:-}"
memory_gb="${5:-}"

usage() {
    echo "Usage: $(basename "$0") <dir_name> <db_type> <db_name> <cpu_cores> <memory_gb>" >&2
}

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

dbms="postgres"

exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
source_dir="${exp_path}/benchmarks/tools/$dir_name"
log_fname="${exp_path}/results/$dir_name"


CMD="uv run python -m src.adco $source_dir \
            --model=gemini-3.5-flash-lite \
            --mode=tune-only \
            --db-type=$db_type \
            --db-name=$db_name \
            --cpu-cores=$cpu_cores \
            --memory=$memory_gb \
            --apply-mode=persist-static \
            --verbose"
$CMD
