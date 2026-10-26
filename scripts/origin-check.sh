#!/usr/bin/env bash
# Verify the origin directly: TLS, health, header budget, cache directives,
# and method behavior. This is the control that must pass before blaming a CDN.
set -euo pipefail

HOST=""
SNI=""
IP=""
SCHEME="https"
PORT="443"
HEALTH="/health"
TIMEOUT="10"
JSON=0
PAD_BYTES="12000"
CDN_SECRET="${CDN_SECRET:-}"
GATE_PATH="/_cdn_secret_probe"

usage() {
    cat <<'EOF'
Usage:
  origin-check.sh --host <origin-host> [options]

Options:
  --host <host>       Origin hostname used in the URL
  --sni <host>        TLS SNI (default: --host; https only)
  --ip <address>      Pin the connection to this address (--resolve)
  --scheme <scheme>   http or https (default https)
  --port <port>       Origin port (default 443; use 80 with --scheme http)
  --health <path>     Health path (default /health)
  --timeout <seconds> Per-request timeout (default 10)
  --pad-bytes <n>     Size of the synthetic request header (default 12000)
  --cdn-secret <s>    X-CDN-Secret value; also read from CDN_SECRET. Enables the
                      gate check: direct origin must reject a request without
                      the header, and accept one with it. When --ip is omitted
                      (request goes through the CDN), only the accept side is
                      checked because the CDN injects the header itself.
  --json              Machine-readable summary
  -h, --help          This help

Exit codes: 0 all core checks pass, 1 a core check failed, 2 setup error.
EOF
}

die() {
    echo "error: $*" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --host|--sni|--ip|--scheme|--port|--health|--timeout|--pad-bytes|--cdn-secret)
            [ -n "${2:-}" ] || die "$1 requires a value"
            case "$1" in
                --host) HOST=$2 ;;
                --sni) SNI=$2 ;;
                --ip) IP=$2 ;;
                --scheme) SCHEME=$2 ;;
                --port) PORT=$2 ;;
                --health) HEALTH=$2 ;;
                --timeout) TIMEOUT=$2 ;;
                --pad-bytes) PAD_BYTES=$2 ;;
                --cdn-secret) CDN_SECRET=$2 ;;
            esac
            shift 2
            ;;
        --json) JSON=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

[ -n "$HOST" ] || die "--host is required"
[ -n "$SNI" ] || SNI="$HOST"
case "$SCHEME" in http|https) ;; *) die "--scheme must be http or https" ;; esac
case "$PORT" in ''|*[!0-9]*) die "--port must be numeric" ;; esac
case "$HEALTH" in /*) ;; *) die "--health must start with /" ;; esac
case "$PAD_BYTES" in ''|*[!0-9]*) die "--pad-bytes must be numeric" ;; esac
case "$CDN_SECRET" in
    *$'\n'*|*$'\r'*) die "--cdn-secret must not contain line breaks" ;;
esac
command -v curl >/dev/null 2>&1 || die "curl is required"
if [ "$SCHEME" = "https" ]; then
    command -v openssl >/dev/null 2>&1 || die "openssl is required for https checks"
fi

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/origin-check.XXXXXX")
chmod 700 "$TMPDIR"
trap 'rm -rf "$TMPDIR"' EXIT INT TERM

resolve_args=()
if [ -n "$IP" ]; then
    target="$IP"
    case "$IP" in
        *:*) resolve_args=(--resolve "$SNI:$PORT:[$IP]") ;;
        *) resolve_args=(--resolve "$SNI:$PORT:$IP") ;;
    esac
else
    target="$HOST"
fi

nonce="$(date +%s)-$RANDOM"
url="$SCHEME://$HOST:$PORT$HEALTH?probe=$nonce"
headers="$TMPDIR/headers"
body="$TMPDIR/body"

# 1. TLS + health.
if health_out=$(curl -sS --noproxy '*' -m "$TIMEOUT" "${resolve_args[@]}" \
    -D "$headers" -o "$body" -w "%{http_code} %{time_total} %{ssl_verify_result}" \
    "$url" 2>"$TMPDIR/curl.err"); then
    health_rc=0
else
    health_rc=$?
    health_out="000 0 1"
fi
health_status=${health_out%% *}
rest=${health_out#* }
health_time=${rest%% *}
tls_verify=${rest#* }
health_error=$(tr '\n\t' '  ' < "$TMPDIR/curl.err" | cut -c1-160)
cache_control=$(grep -i '^cache-control:' "$headers" 2>/dev/null | tail -1 | tr -d '\r' || true)

# 2. Certificate details and verification result (https only).
if [ "$SCHEME" = "https" ]; then
    tls_output=$(printf '' | timeout "$TIMEOUT" openssl s_client -connect "$target:$PORT" -servername "$SNI" 2>&1 || true)
    verify_line=$(printf '%s\n' "$tls_output" | grep -m1 'Verify return code' || true)
    cert_text=$(printf '%s\n' "$tls_output" | openssl x509 -noout -subject -issuer -enddate 2>/dev/null || true)
    cert_subject=$(printf '%s\n' "$cert_text" | sed -n 's/^subject=//p')
    cert_issuer=$(printf '%s\n' "$cert_text" | sed -n 's/^issuer=//p')
    cert_end=$(printf '%s\n' "$cert_text" | sed -n 's/^notAfter=//p')
else
    tls_verify="n/a"
    verify_line="not checked (http)"
    cert_subject=""
    cert_issuer=""
    cert_end=""
fi

# 3. Header budget: a request with a large synthetic header must not be 400/431.
pad=$(head -c "$PAD_BYTES" /dev/zero | tr '\0' 'a')
if pad_out=$(curl -sS --noproxy '*' -m "$TIMEOUT" "${resolve_args[@]}" \
    -H "X-Pad: $pad" -o /dev/null -w "%{http_code} %{time_total}" "$url" 2>"$TMPDIR/pad.err"); then
    pad_rc=0
else
    pad_rc=$?
    pad_out="000 0"
fi
pad_status=${pad_out%% *}
pad_time=${pad_out#* }
pad_error=$(tr '\n\t' '  ' < "$TMPDIR/pad.err" | cut -c1-160)

# 4. Method behavior at the origin (informational).
method_rows=()
for method in OPTIONS POST; do
    method_url="$SCHEME://$HOST:$PORT$HEALTH?method=$method&probe=$nonce"
    if [ "$method" = "POST" ]; then
        method_out=$(curl -sS --noproxy '*' -m "$TIMEOUT" "${resolve_args[@]}" \
            -X POST --data-binary 'x' -o /dev/null -w "%{http_code}" "$method_url" 2>/dev/null || true)
    else
        method_out=$(curl -sS --noproxy '*' -m "$TIMEOUT" "${resolve_args[@]}" \
            -X OPTIONS -o /dev/null -w "%{http_code}" "$method_url" 2>/dev/null || true)
    fi
    method_out=${method_out:-000}
    method_rows+=("$method $method_out")
done

# 5. Secret gate. Direct origin: no header must be rejected (444/000) and the
# correct header must return 204. Through the CDN the edge injects the header,
# so only the accept side is meaningful.
gate_checked=0
gate_mode="skipped"
gate_noheader="n/a"
gate_withheader="n/a"
gate_ok=0
if [ -n "$CDN_SECRET" ]; then
    gate_checked=1
    gate_url="$SCHEME://$HOST:$PORT$GATE_PATH?probe=$nonce"
    if [ -n "$IP" ]; then
        gate_mode="direct"
        gate_noheader=$(curl -sS --noproxy '*' -m "$TIMEOUT" "${resolve_args[@]}" \
            -o /dev/null -w "%{http_code}" "$gate_url" 2>/dev/null || true)
        gate_noheader=${gate_noheader:-000}
    else
        gate_mode="cdn"
    fi
    gate_withheader=$(curl -sS --noproxy '*' -m "$TIMEOUT" "${resolve_args[@]}" \
        -H "X-CDN-Secret: $CDN_SECRET" -o /dev/null -w "%{http_code}" \
        "$gate_url" 2>/dev/null || true)
    gate_withheader=${gate_withheader:-000}
    if [ "$gate_withheader" = "204" ]; then
        if [ "$gate_mode" = "direct" ]; then
            case "$gate_noheader" in
                000|444) gate_ok=1 ;;
            esac
        else
            gate_ok=1
        fi
    fi
fi

core_total=2
[ "$SCHEME" = "https" ] && core_total=3
core_ok=0
[ "$health_status" = "200" ] && core_ok=$((core_ok + 1))
[ "$SCHEME" = "https" ] && [ "$tls_verify" = "0" ] && core_ok=$((core_ok + 1))
if [ "$pad_rc" -eq 0 ]; then
    case "$pad_status" in 400|431) ;; *) core_ok=$((core_ok + 1)) ;; esac
fi
if [ "$gate_checked" -eq 1 ]; then
    core_total=$((core_total + 1))
    core_ok=$((core_ok + gate_ok))
fi

if [ "$JSON" -eq 1 ]; then
    python3 - "$HOST" "$SNI" "$IP" "$SCHEME" "$PORT" "$HEALTH" "$health_status" "$health_time" \
        "$tls_verify" "$cert_subject" "$cert_issuer" "$cert_end" "$verify_line" "$cache_control" \
        "$pad_status" "$pad_time" "$PAD_BYTES" "$health_error" "$pad_error" "$core_ok" \
        "$core_total" "$gate_checked" "$gate_mode" "$gate_noheader" "$gate_withheader" \
        "$gate_ok" "${method_rows[@]}" <<'PY'
import json
import sys

(host, sni, ip, scheme, port, health, status, elapsed, verify, subject, issuer,
 expires, verify_line, cache_control, pad_status, pad_time, pad_bytes, health_error,
 pad_error, core_ok, core_total, gate_checked, gate_mode, gate_noheader,
 gate_withheader, gate_ok, *methods) = sys.argv[1:]
method_map = {}
for item in methods:
    name, code = item.split(" ", 1)
    method_map[name] = code
print(json.dumps({
    "host": host,
    "sni": sni,
    "ip": ip,
    "scheme": scheme,
    "port": int(port),
    "health_path": health,
    "health_status": status,
    "health_seconds": float(elapsed),
    "tls_verify_result": verify,
    "tls_verify_line": verify_line,
    "certificate_subject": subject,
    "certificate_issuer": issuer,
    "certificate_expires": expires,
    "cache_control": cache_control,
    "pad_bytes": int(pad_bytes),
    "pad_status": pad_status,
    "pad_seconds": float(pad_time),
    "secret_gate_checked": bool(int(gate_checked)),
    "secret_gate_mode": gate_mode,
    "secret_gate_no_header": gate_noheader,
    "secret_gate_with_header": gate_withheader,
    "secret_gate_ok": bool(int(gate_ok)),
    "methods": method_map,
    "health_error": health_error,
    "pad_error": pad_error,
    "core_checks_passed": int(core_ok),
    "core_checks_total": int(core_total),
}, indent=2))
PY
else
    methods_text=""
    for row in "${method_rows[@]}"; do
        methods_text+="${row%% *}=${row##* } "
    done
    echo "origin:  $SCHEME://$HOST:$PORT  (sni=$SNI, ip=${IP:-resolved}, target=$target)"
    echo "health:  $health_status  ${health_time}s  $HEALTH"
    echo "tls:     verify=$tls_verify  ${verify_line:-no verify line}"
    echo "cert:    ${cert_subject:--}"
    echo "issuer:  ${cert_issuer:--}"
    echo "expires: ${cert_end:--}"
    echo "cache:   ${cache_control:--}"
    echo "header:  status=$pad_status ${pad_time}s with ${PAD_BYTES} bytes"
    echo "methods: $methods_text"
    if [ "$gate_checked" -eq 1 ]; then
        echo "gate:    mode=$gate_mode no-header=$gate_noheader with-header=$gate_withheader"
    else
        echo "gate:    not checked (pass --cdn-secret)"
    fi
    [ -n "$health_error" ] && echo "curl:    $health_error"
    [ -n "$pad_error" ] && echo "curl:    $pad_error"
    echo
    if [ "$core_ok" -eq "$core_total" ]; then
        gate_suffix=""
        [ "$gate_checked" -eq 1 ] && gate_suffix=", secret gate ok ($gate_mode)"
        if [ "$SCHEME" = "https" ]; then
            echo "VERDICT: OK (health 200, certificate verified, header budget accepted$gate_suffix)"
        else
            echo "VERDICT: OK (health 200, header budget accepted$gate_suffix)"
        fi
    else
        echo "VERDICT: CHECK FAILED ($core_ok/$core_total core checks passed)"
    fi
fi

if [ "$core_ok" -eq "$core_total" ]; then
    exit 0
fi
exit 1
