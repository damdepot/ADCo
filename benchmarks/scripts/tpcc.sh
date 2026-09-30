#!/bin/bash
set -euo pipefail

dir="${1:-}"
file_name="${2:-}"

if [ -z "$dir" ] || [ -z "$file_name" ]; then
    echo "Usage: $(basename "$0") <dir> <file_name>" >&2
    exit 2
fi

phase="${PHASE:-execute}"
case "${phase}" in
    load|execute) ;;
    *)
        echo "Usage: PHASE must be 'load' or 'execute' (got '${phase}')" >&2
        exit 2
        ;;
esac

dbms="postgres"
# Bench defaults target a less noisy, harder-to-move workload: more warehouses
# lower row-level contention, more clients raise offered load, and a longer run
# shrinks the relative variance of the reported rate. Override via the env.
warehouses="${WAREHOUSES:-4}"
clients="${CLIENTS:-4}"
duration="${DURATION:-60}"
exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
workload_path="${exp_path}/benchmarks/tools/tpcc"
log_fname="${exp_path}/results/tpcc/$file_name"

cd "${workload_path}"
source .venv/bin/activate

if [ "$dir" != "baseline" ]; then
    output_path="${exp_path}/out/$dir"
    cd "${output_path}"
    echo "------->>${output_path}<<-------"
fi

CMD="python tpcc.py ${dbms} \
                --config=${workload_path}/db.config \
                --clients=${clients} \
                --warehouses=${warehouses} \
                --duration=${duration}"

if [ "${phase}" = "load" ]; then
    CMD="${CMD} \
                --reset \
                --no-execute"
else
    CMD="${CMD} \
                --output-path=${log_fname} \
                --no-load"
fi

$CMD
