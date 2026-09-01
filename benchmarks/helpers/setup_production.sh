#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ ! -f "$ROOT/db.config" ]; then
    echo "ERROR: $ROOT/db.config not found" >&2
    exit 1
fi

python3 "$ROOT/benchmarks/helpers/write_configs.py" "$ROOT" production