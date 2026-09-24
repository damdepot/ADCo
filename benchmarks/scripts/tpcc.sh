#!/bin/bash

dir=$1
file_name=$2

dbms="postgres"
warehouses=2
clients=2
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
                --duration=30 \
                --output-path=${log_fname} \
                --reset"
$CMD
