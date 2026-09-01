#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/benchmarks"

clone_repo() {
    local dir="$1" url="$2"
    if [ -d "$dir/.git" ]; then
        echo "Already cloned: $dir"
        return
    fi
    echo "Cloning $url -> $dir"
    git clone "$url" "$dir"
}

clone_repo "$BENCH/tools/smallbank" "https://github.com/dannykhant/py-smallbank"
clone_repo "$BENCH/tools/tpcc" "https://github.com/dannykhant/py-tpcc-python3"

echo "Download complete."