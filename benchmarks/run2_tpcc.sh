#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="$ROOT/benchmarks/tools/tpcc"
DRIVER="${DRIVER:-postgres}"

if [ ! -d "$TOOL_DIR/.git" ]; then
    echo "ERROR: $TOOL_DIR not found — run benchmarks/run0_download_benchmarks.sh first" >&2
    exit 1
fi

cd "$TOOL_DIR"

if [ ! -f db.config ]; then
    echo "ERROR: db.config missing — run benchmarks/helpers/setup_baseline.sh or setup_production.sh first" >&2
    exit 1
fi

CMDSetupBaseline="$ROOT/benchmarks/helpers/setup_baseline.sh"
CMDSetupProduction="$ROOT/benchmarks/helpers/setup_production.sh"

echo "----->> TPC-C Benchmark for Baseline <<-----"
$CMDSetupBaseline
uv run python tpcc.py "$DRIVER" \
    --config db.config \
    --warehouses 1 \
    --clients 1 \
    --duration 60 \
    --reset \
    --output-path "$ROOT/out/tpcc/baseline.dat"


echo "----->> TPC-C Benchmark for Production <<-----"
$CMDSetupProduction
uv run python tpcc.py "$DRIVER" \
    --config db.config \
    --warehouses 1 \
    --clients 1 \
    --duration 60 \
    --reset \
    --output-path "$ROOT/out/tpcc/production.dat"