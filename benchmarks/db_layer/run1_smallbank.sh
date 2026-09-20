#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOL_DIR="$ROOT/benchmarks/tools/smallbank"
DRIVER="postgres"
DB_NAME="smallbank"

if [ ! -d "$TOOL_DIR/.git" ]; then
    echo "ERROR: $TOOL_DIR not found — run benchmarks/run0_download_benchmarks.sh first" >&2
    exit 1
fi

if [ ! -f "$TOOL_DIR/db.config" ]; then
    echo "ERROR: db.config missing — run benchmarks/helpers/runUpdateBaselineConfig.sh first" >&2
    exit 1
fi

CMDUpdateBaseline="$ROOT/benchmarks/helpers/runUpdateBaselineConfig.sh"
CMDUpdateProduction="$ROOT/benchmarks/helpers/runUpdateProductionConfig.sh"

run_benchmark() {
    local output="$1"
    ( cd "$TOOL_DIR" && uv run python main.py test \
        --driver "$DRIVER" \
        --accounts 100000 \
        --transactions 10000 \
        --threads 8 \
        --output-path "$output" )
}

echo "----->> Step 1: Update baseline config <<-----"
$CMDUpdateBaseline smallbank "$DB_NAME"

echo "----->> Step 2: Smallbank benchmark (baseline) <<-----"
run_benchmark "$ROOT/results/db_layer/smallbank/baseline.dat"

echo "----->> Step 3: Update production config <<-----"
$CMDUpdateProduction smallbank "$DB_NAME"

echo "----->> Step 4: ADCo knob tune (production) <<-----"
( cd "$ROOT" && make tune-only DIR="$TOOL_DIR" DB_TYPE="$DRIVER" DB_NAME="$DB_NAME" )

echo "----->> Step 5: Smallbank benchmark (production) <<-----"
run_benchmark "$ROOT/results/db_layer/smallbank/production.dat"
