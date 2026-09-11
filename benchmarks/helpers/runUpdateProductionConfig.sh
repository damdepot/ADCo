#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 <tool> <db_name>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$BENCH_DIR/.." && pwd)"

SOURCE="$REPO_DIR/db.config"

tool="$1"
db="$2"
target="$BENCH_DIR/tools/$tool/db.config"

if [ ! -f "$target" ]; then
    echo "Unknown tool: $tool (expected tpcc or smallbank)"
    exit 1
fi

cp "$SOURCE" "$target"
sed -i '' "/^password = /a\\
database = $db" "$target"

echo "Updated $tool db.config from db.config"
