#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="$ROOT/benchmarks/tools/tpcc"
DRIVER="${DRIVER:-postgres}"

if [ ! -d "$TOOL_DIR/.git" ]; then
    echo "ERROR: $TOOL_DIR not found — run benchmarks/download_benchmarks.sh first" >&2
    exit 1
fi

cd "$TOOL_DIR"

if [ ! -f db.config ]; then
    echo "ERROR: db.config missing — run benchmarks/setup_staging.sh or setup_production.sh first" >&2
    exit 1
fi

uv run python tpcc.py "$DRIVER" \
    --config db.config \
    --warehouses 1 \
    --clients 1 \
    --duration 60 \
    --reset \
    --output-path "$ROOT/out/tpcc/baseline.dat"