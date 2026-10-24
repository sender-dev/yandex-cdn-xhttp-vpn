#!/usr/bin/env bash
# Fail if the repository contains credentials or field identifiers that must
# never be published. This is a guard, not a replacement for reviewing a diff.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
fail=0

scan() {
    local name=$1
    local pattern=$2
    local hits
    hits=$(grep -RInE \
        --exclude-dir=.git \
        --exclude-dir=__pycache__ \
        --exclude-dir=.pytest_cache \
        --exclude-dir=.ruff_cache \
        "$pattern" . || true)
    if [ -n "$hits" ]; then
        echo "FAIL: $name" >&2
        echo "$hits" >&2
        fail=1
    fi
}

scan "private key block" 'BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY'
scan "GitHub token" 'gh[pousr]_[A-Za-z0-9]{20,}'
scan "AWS access key" 'AKIA[0-9A-Z]{16}'

# Optional private denylist: one extended regex per line for real hostnames,
# IPs, resource IDs, or ticket numbers. Keep that file outside the repository.
denylist=${SECRET_SCAN_DENYLIST:-}
if [ -n "$denylist" ]; then
    [ -f "$denylist" ] || { echo "FAIL: denylist not found: $denylist" >&2; exit 1; }
    while IFS= read -r pattern; do
        case "$pattern" in ''|'#'*) continue ;; esac
        scan "private denylist entry" "$pattern"
    done < "$denylist"
fi

uuid_hits=$(grep -RInE \
    --exclude-dir=.git \
    --exclude-dir=__pycache__ \
    --exclude-dir=.pytest_cache \
    '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' \
    . || true)
uuid_hits=$(printf '%s\n' "$uuid_hits" | grep -vE '00000000-0000-4000-8000-0000000000|11111111-1111-4111-8111-111111111111' || true)
if [ -n "$uuid_hits" ]; then
    echo "FAIL: non-placeholder UUID" >&2
    echo "$uuid_hits" >&2
    fail=1
fi

if [ "$fail" -ne 0 ]; then
    echo "secret scan failed" >&2
    exit 1
fi
echo "secret scan: clean"
