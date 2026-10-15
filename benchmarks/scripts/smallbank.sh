#!/bin/bash
set -euo pipefail

dir="${1:-}"
file_name="${2:-}"
# Repetitions: positional arg wins over env. Load phase always runs once.
reps="${3:-${REPS:-1}}"

if [ -z "${dir}" ] || [ -z "${file_name}" ]; then
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
# Data size and workload. `load` seeds the DB (so the tuner can inspect it),
# `run` measures against that loaded data (native; single-connection). Same
# load-once/measure-reps shape as tpcc.sh. TRANSACTIONS is the offered load.
accounts="${ACCOUNTS:-1000000}"
transactions="${TRANSACTIONS:-60000}"
load_threads="${LOAD_THREADS:-8}"
db_container="${DB_CONTAINER:-adcoexp-db}"

exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
workload_path="${exp_path}/benchmarks/tools/smallbank"
results_dir="${exp_path}/results/smallbank"

cd "${workload_path}"
source .venv/bin/activate

if [ "${dir}" != "baseline" ]; then
    workdir="${exp_path}/out/${dir}"
    echo "------->>${workdir}<<-------"
else
    workdir="${workload_path}"
fi

if [ "${phase}" = "load" ]; then
    # Creates the schema and loads accounts so the tuner can inspect the target
    # DB; `test` in the execute phase resets and reloads for a clean arm.
    python main.py load \
        --driver "${dbms}" \
        --threads "${load_threads}" \
        --accounts "${accounts}" \
        --reset
else
    mkdir -p "${results_dir}"
    for (( rep = 1; rep <= reps; rep++ )); do
        if [ "${reps}" -eq 1 ]; then
            rep_file="${file_name}"
        elif [[ "${file_name}" == *.* ]]; then
            rep_file="${file_name%.*}_r${rep}.${file_name##*.}"
        else
            rep_file="${file_name}_r${rep}"
        fi
        log_fname="${results_dir}/${rep_file}"
        echo "----------------->> SMALLBANK rep ${rep}/${reps} (${rep_file}) <<-----------------"
        "${exp_path}/benchmarks/scripts/docker.sh" Checkpoint "${db_container}"
        cd "${workdir}"
        python main.py run \
            --driver "${dbms}" \
            --accounts "${accounts}" \
            --transactions "${transactions}" \
            --output-path "${log_fname}"
    done
fi
