#!/bin/bash

dir_name=$1
db_type=$2
db_name=$3

dbms="postgres"

exp_path="$(cd "$(dirname "$0")/../.." && pwd)"
source_dir="${exp_path}/benchmarks/tools/$dir_name"
log_fname="${exp_path}/results/$dir_name"


CMD="uv run python -m src.adco $source_dir \
            --model=gemini-3.5-flash-lite \
            --mode=tune-only \
            --db-type=$db_type \
            --db-name=$db_name \
            --cpu-cores=2 \
            --memory=8 \
            --verbose"
$CMD
