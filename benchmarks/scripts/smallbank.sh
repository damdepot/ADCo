#!/bin/bash

dir=$1

dbms="postgres"
accounts=1000000
transactions=100000
exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
workload_path="${exp_path}/benchmarks/tools/smallbank"
log_fname="${exp_path}/results/$dir"

cd "${workload_path}"
source .venv/bin/activate

if [ "$dir" != "baseline" ]; then
    output_path="${exp_path}/out/$dir"
    cd "${output_path}"
    echo "------->>${output_path}<<-------"
fi

CMD="python main.py test \
                    --driver ${dbms} \
                    --threads 4 \
                    --accounts ${accounts} \
                    --transactions ${transactions} \
                    --output-path ${log_fname}"
$CMD
