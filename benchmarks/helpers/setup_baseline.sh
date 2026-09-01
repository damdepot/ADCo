#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="$ROOT/benchmarks/baseline.config"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found" >&2
    exit 1
fi

python3 "$ROOT/benchmarks/helpers/write_configs.py" "$ROOT" "" "$CONFIG"