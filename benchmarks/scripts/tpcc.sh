#!/bin/bash

dir=$1

dbms="postgres"
warehouses=4
clients=4
exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
workload_path="${exp_path}/benchmarks/tools/tpcc"
log_fname="${exp_path}/results/$dir"

cd "${workload_path}"
source .venv/bin/activate

if [ "$dir" != "baseline" ]; then
    output_path="${exp_path}/out/$dir"
    cd "${output_path}"
fi

CMD="python tpcc.py ${dbms} \
                --config=${workload_path}/db.config \
                --clients=${clients} \
                --warehouses=${warehouses} \
                --duration=60 \
                --output-path=${log_fname} \
                --reset"
$CMD
