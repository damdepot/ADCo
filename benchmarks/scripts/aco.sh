#!/bin/bash

dir_name=$1
db_type=$2
db_name=$3

dbms="postgres"

exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
source_dir="${exp_path}/benchmarks/tools/$dir_name"
sandbox_dir="${exp_path}/out/${dir_name}_aco"
log_fname="${exp_path}/results/$dir_name"


CMD="uv run python -m src.adco $source_dir \
            --model=gemini-3.5-flash-lite \
            --mode=rewrite-only \
            --sandbox-dir=$sandbox_dir \
            --db-type=$db_type \
            --db-name=$db_name \
            --verbose"
$CMD
