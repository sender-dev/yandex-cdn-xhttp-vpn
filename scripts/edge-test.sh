#!/usr/bin/env bash
# Test whether a CDN edge carries a VLESS/XHTTP session end to end.
#
# The script starts a temporary Xray client whose XHTTP preset matches the
# origin inbound, sends a series of requests through a local SOCKS listener,
# and reports LIVE / PARTIAL / DEAD. It cleans up by PID; it never uses pkill.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
XRAY_BIN=${XRAY_BIN:-xray}
EDGE=""
SNI=""
HOST=""
PATH_="/api-stream"
UUID=""
UUID_FILE=""
PORT="11096"
ATTEMPTS="5"
URL="https://api.telegram.org/"
EXPECT="302"
TIMEOUT="12"
PRESET="happ-parity"
VLESS_ENCRYPTION=${VLESS_ENCRYPTION:-}
EDGE_PORT="443"
JSON=0
DRY_RUN=0
VERBOSE=0

usage() {
    cat <<'EOF'
Usage:
  edge-test.sh --edge <ip|host> --sni <host> [options]

Options:
  --edge <ip|host>      CDN edge IP, or the CDN hostname
  --sni <host>          TLS SNI and HTTP Host (usually the CDN hostname)
  --host <host>         HTTP Host if it differs from SNI
  --path <path>         XHTTP path (default /api-stream)
  --uuid <uuid>         VLESS UUID; prefer --uuid-file or VLESS_UUID
  --uuid-file <file>    Read the UUID from the first line (mode 0600 recommended)
  --port <n>            Local SOCKS port for the temporary client (default 11096)
  --attempts <n>        Number of requests (default 5)
  --url <url>           Test URL (default https://api.telegram.org/)
  --expect <code|any>   Expected HTTP status (default 302; 'any' accepts 2xx/3xx)
  --timeout <seconds>   Per-request timeout (default 12)
  --preset <name>       happ-parity, yandex-get or minimal (default happ-parity)
  --vless-encryption <key>  Client key from `xray vlessenc`; `none` (default)
  --edge-port <n>       TLS port on the edge (default 443)
  --json                Machine-readable summary
  --dry-run             Validate the generated client config, send nothing
  --verbose             Show the Xray log tail
  -h, --help            This help

Environment:
  VLESS_UUID            UUID when --uuid/--uuid-file is omitted
  VLESS_ENCRYPTION      Client key from `xray vlessenc` when the flag is omitted
  XRAY_BIN              Xray binary (default: xray)

Exit codes:
  0 LIVE, 1 PARTIAL, 2 setup error, 3 DEAD
EOF
}

die() {
    echo "error: $*" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --edge|--sni|--host|--path|--uuid|--uuid-file|--port|--attempts|--url|--expect|--timeout|--preset|--vless-encryption|--edge-port)
            [ -n "${2:-}" ] || die "$1 requires a value"
            case "$1" in
                --edge) EDGE=$2 ;;
                --sni) SNI=$2 ;;
                --host) HOST=$2 ;;
                --path) PATH_=$2 ;;
                --uuid) UUID=$2 ;;
                --uuid-file) UUID_FILE=$2 ;;
                --port) PORT=$2 ;;
                --attempts) ATTEMPTS=$2 ;;
                --url) URL=$2 ;;
                --expect) EXPECT=$2 ;;
                --timeout) TIMEOUT=$2 ;;
                --preset) PRESET=$2 ;;
                --vless-encryption) VLESS_ENCRYPTION=$2 ;;
                --edge-port) EDGE_PORT=$2 ;;
            esac
            shift 2
            ;;
        --json) JSON=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --verbose) VERBOSE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

[ -n "$EDGE" ] || die "--edge is required"
[ -n "$SNI" ] || die "--sni is required"
[ -n "$HOST" ] || HOST="$SNI"
case "$PATH_" in /*) ;; *) die "--path must start with /" ;; esac
case "$ATTEMPTS" in ''|*[!0-9]*) die "--attempts must be a positive integer" ;; esac
[ "$ATTEMPTS" -ge 1 ] || die "--attempts must be at least 1"
case "$PORT" in ''|*[!0-9]*) die "--port must be numeric" ;; esac
case "$EDGE_PORT" in ''|*[!0-9]*) die "--edge-port must be numeric" ;; esac
command -v "$XRAY_BIN" >/dev/null 2>&1 || die "Xray not found: $XRAY_BIN"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

if [ -z "$UUID" ] && [ -z "$UUID_FILE" ] && [ -z "${VLESS_UUID:-}" ]; then
    die "provide --uuid, --uuid-file, or VLESS_UUID"
fi

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/xhttp-edge-test.XXXXXX")
chmod 700 "$TMPDIR"
CFG="$TMPDIR/client.json"
LOG="$TMPDIR/xray.log"
RESULTS="$TMPDIR/results.tsv"
XPID=""
: > "$RESULTS"

cleanup() {
    if [ -n "${XPID:-}" ]; then
        kill "$XPID" 2>/dev/null || true
        wait "$XPID" 2>/dev/null || true
    fi
    rm -rf "$TMPDIR"
}
trap cleanup EXIT INT TERM

profile_args=(
    --address "$EDGE"
    --sni "$SNI"
    --host "$HOST"
    --path "$PATH_"
    --port "$EDGE_PORT"
    --preset "$PRESET"
    --socks-port "$PORT"
    --client-config "$CFG"
    --quiet
)
[ -n "$UUID" ] && profile_args+=(--uuid "$UUID")
[ -n "$UUID_FILE" ] && profile_args+=(--uuid-file "$UUID_FILE")
[ -n "$VLESS_ENCRYPTION" ] && profile_args+=(--vless-encryption "$VLESS_ENCRYPTION")

python3 "$SCRIPT_DIR/make-profile.py" "${profile_args[@]}" >/dev/null

if ! "$XRAY_BIN" run -test -config "$CFG" >/dev/null 2>&1; then
    echo "CONFIG_INVALID" >&2
    exit 2
fi

if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$JSON" -eq 1 ]; then
        printf '{"dry_run":true,"config_valid":true,"edge":"%s","sni":"%s","preset":"%s"}\n' \
            "$EDGE" "$SNI" "$PRESET"
    else
        echo "CONFIG_VALID edge=$EDGE sni=$SNI preset=$PRESET"
    fi
    exit 0
fi

"$XRAY_BIN" run -config "$CFG" >"$LOG" 2>&1 &
XPID=$!

ready=0
for _ in $(seq 1 50); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "$XPID" 2>/dev/null; then
        break
    fi
    sleep 0.1
done

if [ "$ready" -ne 1 ]; then
    echo "XRAY_START_FAILED" >&2
    tail -n 20 "$LOG" >&2 || true
    exit 2
fi

emit() {
    if [ "$JSON" -eq 1 ]; then
        printf "$@" >&2
    else
        printf "$@"
    fi
}

ok=0
fail=0
for i in $(seq 1 "$ATTEMPTS"); do
    if out=$(curl -sS --noproxy '*' -m "$TIMEOUT" \
        --proxy "socks5h://127.0.0.1:$PORT" \
        -o /dev/null -w "%{http_code} %{time_total}" "$URL" 2>"$TMPDIR/curl.err"); then
        rc=0
    else
        rc=$?
        out="000 0"
    fi
    code=${out%% *}
    elapsed=${out#* }
    err=$(tr '\n\t' '  ' < "$TMPDIR/curl.err" | cut -c1-120)
    success=0
    if [ "$rc" -eq 0 ]; then
        if [ "$EXPECT" = "any" ]; then
            case "$code" in 2*|3*) success=1 ;; esac
        elif [ "$code" = "$EXPECT" ]; then
            success=1
        fi
    fi
    if [ "$success" -eq 1 ]; then
        ok=$((ok + 1))
        emit "  %02d  %s  %ss\n" "$i" "$code" "$elapsed"
    else
        fail=$((fail + 1))
        emit "  %02d  FAIL rc=%s code=%s %ss %s\n" "$i" "$rc" "$code" "$elapsed" "$err"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$i" "$success" "$code" "$elapsed" "$rc" "$err" >> "$RESULTS"
done

emit '%s\n' "EDGE $EDGE => $ok/$ATTEMPTS ok, $fail fail"
if [ -s "$LOG" ]; then
    signatures=$(grep -Eo "bad status code:[0-9]+|unexpected status [0-9]+|malformed HTTP/1.x|timeout|connection reset|EOF" "$LOG" \
        | sort | uniq -c | head -5 || true)
    if [ -n "$signatures" ]; then
        emit '%s\n' "$signatures"
    fi
fi
if [ "$VERBOSE" -eq 1 ]; then
    if [ "$JSON" -eq 1 ]; then
        tail -n 20 "$LOG" >&2 || true
    else
        tail -n 20 "$LOG" || true
    fi
fi

if [ "$ok" -eq "$ATTEMPTS" ]; then
    verdict="LIVE"
    rc_final=0
elif [ "$ok" -gt 0 ]; then
    verdict="PARTIAL"
    rc_final=1
else
    verdict="DEAD"
    rc_final=3
fi

if [ "$JSON" -eq 1 ]; then
    python3 - "$RESULTS" "$EDGE" "$SNI" "$PATH_" "$PRESET" "$ok" "$ATTEMPTS" "$verdict" <<'PY'
import json
import sys

path, edge, sni, url_path, preset, ok, attempts, verdict = sys.argv[1:9]
rows = []
with open(path, encoding="utf-8") as stream:
    for line in stream:
        attempt, success, code, elapsed, rc, err = line.rstrip("\n").split("\t")
        rows.append({
            "attempt": int(attempt),
            "ok": success == "1",
            "code": code,
            "seconds": float(elapsed),
            "curl_rc": int(rc),
            "error": err,
        })
print(json.dumps({
    "edge": edge,
    "sni": sni,
    "path": url_path,
    "preset": preset,
    "ok": int(ok),
    "attempts": int(attempts),
    "verdict": verdict,
    "results": rows,
}, indent=2))
PY
else
    echo "VERDICT: $verdict"
fi

exit "$rc_final"
