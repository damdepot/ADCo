#!/usr/bin/env bash

psql() {
    docker exec -i -u postgres "${BENCH_DB_CONTAINER}" psql -v ON_ERROR_STOP=1 -q "$@"
}

total_of() {
    awk -F, '
        $1 ~ /TOTAL/ { v = $NF }
        END {
            if (v == "" || v !~ /^-?[0-9]+([.][0-9]+)?$/) {
                print "no parseable TOTAL row in " FILENAME > "/dev/stderr"
                exit 1
            }
            print v
        }
    ' "$1"
}

median_of() {
    printf '%s\n' "$@" | sort -n |
        awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'
}

median_of_values() { median_of "$@"; }

band_field() {
    "${PYTHON}" -c '
import json, sys
band = json.load(open(sys.argv[1]))
for key in ("mde_pct", "ci95_pct", "spread_pct"):
    if key in band:
        print(key)
        print(float(band[key]))
        break
else:
    print("none")
    print("0")
' "$1"
}

restore_defaults() {
    psql -d "${DB}" -c "ALTER SYSTEM RESET ALL;" >/dev/null 2>&1 || true
    psql -d "${DB}" -c "SELECT pg_reload_conf();" >/dev/null 2>&1 || true
    if [ "${RESTORE_RESTART:-1}" = "1" ]; then
        "${CMDDOCKER}" Restart "${BENCH_DB_CONTAINER}" >/dev/null 2>&1 || true
        "${CMDDOCKER}" WaitFor "${BENCH_DB_CONTAINER}" >/dev/null 2>&1 || true
    fi
}

usage() {
    echo "Usage: $(basename "$0") <dir_name> <db_type> <db_name> <cpu_cores> <memory_gb>" >&2
}

validate_tuning_args() {
    if [ -z "$dir_name" ] || [ -z "$db_type" ] || [ -z "$db_name" ] || [ -z "$cpu_cores" ] || [ -z "$memory_gb" ]; then
        echo "ERROR: missing required arguments." >&2
        usage
        exit 2
    fi

    if ! [[ "$cpu_cores" =~ ^[1-9][0-9]*$ ]]; then
        echo "ERROR: <cpu_cores> must be a positive integer (got '${cpu_cores}')." >&2
        usage
        exit 2
    fi

    if ! [[ "$memory_gb" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !(${memory_gb} > 0)}"; then
        echo "ERROR: <memory_gb> must be a positive number (got '${memory_gb}')." >&2
        usage
        exit 2
    fi
}
