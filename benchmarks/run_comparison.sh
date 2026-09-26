#!/usr/bin/env bash
#
# Compare the TPC-C results produced by benchmarks/run0_tpcc.sh
# (baseline vs ACo vs DCo vs ADCo).
#
# Reports the transaction rate (txn/s) for every TPC-C transaction type and
# the percentage change relative to the baseline. Higher is better.
#
# Usage:
#   benchmarks/run_comparison.sh [RESULTS_DIR]
#
# RESULTS_DIR defaults to <repo>/results/tpcc. For each label the newest
# matching <label>.csv or <label>_*.csv file is used. Missing labels are skipped.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS_DIR="${1:-${ROOT}/results/tpcc}"

labels=(baseline aco dco adco)
files=()
present=()
for label in "${labels[@]}"; do
    latest=""
    for candidate in "${RESULTS_DIR}/${label}.csv" "${RESULTS_DIR}/${label}"_*.csv; do
        [[ -f "${candidate}" ]] || continue
        if [[ -z "${latest}" || "${candidate}" -nt "${latest}" ]]; then
            latest="${candidate}"
        fi
    done
    if [[ -n "${latest}" ]]; then
        files+=("${latest}")
        present+=("${label}")
    else
        echo "WARNING: missing ${RESULTS_DIR}/${label}.csv (skipping '${label}')" >&2
    fi
done

if [[ ${#files[@]} -eq 0 ]]; then
    echo "ERROR: no result CSVs found in ${RESULTS_DIR}" >&2
    echo "Run benchmarks/run0_tpcc.sh first, or pass the results directory." >&2
    exit 1
fi

echo "TPC-C comparison — transaction rate (txn/s), % change vs baseline"
echo "Source: ${RESULTS_DIR}"
echo

awk -F, -v labels="$(IFS=,; echo "${present[*]}")" '
BEGIN { nlab = split(labels, lab, ",") }
FNR == 1 { fileidx++; next }
{
    name = lab[fileidx]
    txn = $1
    rate = $4 + 0
    rate_by[name, txn] = rate
    if (!(txn in seen)) { seen[txn] = 1; order[++count] = txn }
}
END {
    printf "%-14s", "TRANSACTION"
    for (i = 1; i <= nlab; i++) printf "%22s", lab[i]
    printf "\n"

    for (i = 1; i <= count; i++) {
        t = order[i]
        base = rate_by[lab[1], t]
        printf "%-14s", t
        for (j = 1; j <= nlab; j++) {
            if (!((lab[j], t) in rate_by)) {
                printf "%22s", "-"
            } else if (j == 1) {
                printf "%22.2f", rate_by[lab[j], t]
            } else {
                v = rate_by[lab[j], t]
                pct = (base > 0 ? (v - base) / base * 100 : 0)
                printf "%22s", sprintf("%.2f (%+.1f%%)", v, pct)
            }
        }
        printf "\n"
    }
}
' "${files[@]}"

echo
echo "Note: higher is better; percentages are relative to baseline."
