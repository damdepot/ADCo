#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="$ROOT/benchmarks/tools/smallbank"
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

echo "----->> Smallbank Benchmark for Baseline <<-----"
$CMDSetupBaseline
uv run python "$ROOT/benchmarks/helpers/create_database.py" "$DRIVER"
uv run python main.py test \
    --driver "$DRIVER" \
    --accounts 100000 \
    --transactions 10000 \
    --threads 1 \
    --output-path "$ROOT/out/smallbank/baseline.dat"


echo "----->> Smallbank Benchmark for Production <<-----"
$CMDSetupProduction
uv run python "$ROOT/benchmarks/helpers/create_database.py" "$DRIVER"
uv run python main.py test \
    --driver "$DRIVER" \
    --accounts 100000 \
    --transactions 10000 \
    --threads 1 \
    --output-path "$ROOT/out/smallbank/production.dat"