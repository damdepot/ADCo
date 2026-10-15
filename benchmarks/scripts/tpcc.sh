#!/bin/bash
set -euo pipefail

dir="${1:-}"
file_name="${2:-}"
# Repetitions: positional arg wins over env. Load phase always runs once.
reps="${3:-${REPS:-1}}"

if [ -z "$dir" ] || [ -z "$file_name" ]; then
    echo "Usage: $(basename "$0") <dir> <file_name> [reps]" >&2
    exit 2
fi
if ! [[ "${reps}" =~ ^[1-9][0-9]*$ ]]; then
    echo "Usage: reps must be a positive integer (got '${reps}')" >&2
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
warehouses="${WAREHOUSES:-6}"
clients="${CLIENTS:-6}"
duration="${DURATION:-60}"
# Seconds of throwaway workload before each measured rep; 0 disables.
warmup="${WARMUP:-15}"
if ! [[ "${warmup}" =~ ^[0-9]+$ ]]; then
    echo "Usage: warmup must be a non-negative integer (got '${warmup}')" >&2
    exit 2
fi
# Container to CHECKPOINT before each execute rep (keeps reps symmetric: a
# 60s TPC-C run writes ~100s of MB of WAL, so without it a WAL-volume
# checkpoint fires mid-window on an arbitrary rep).
db_container="${DB_CONTAINER:-adcoexp-db}"
exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
workload_path="${exp_path}/benchmarks/tools/tpcc"

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
    $CMD
else
    for (( rep = 1; rep <= reps; rep++ )); do
        if [ "${reps}" -eq 1 ]; then
            rep_file="${file_name}"
        elif [[ "${file_name}" == *.* ]]; then
            rep_file="${file_name%.*}_r${rep}.${file_name##*.}"
        else
            rep_file="${file_name}_r${rep}"
        fi
        log_fname="${exp_path}/results/tpcc/${rep_file}"
        echo "----------------->> TPCC rep ${rep}/${reps} (${rep_file}) <<-----------------"
        "${exp_path}/benchmarks/scripts/docker.sh" Checkpoint "${db_container}"
        if [ "${warmup}" -gt 0 ]; then
            echo "----------------->> TPCC warmup ${warmup}s (rep ${rep}/${reps}) <<-----------------"
            $CMD --duration="${warmup}" --no-load
        fi
        $CMD --output-path="${log_fname}" --no-load
    done
fi
