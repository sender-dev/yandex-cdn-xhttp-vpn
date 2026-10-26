#!/usr/bin/env bash
# Validate assets/nginx-origin.conf and exercise the secret gate for real.
# Uses Docker when available (nginx:1.26.3), otherwise a local nginx binary.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ASSET="$ROOT/assets/nginx-origin.conf"
IMAGE=${NGINX_IMAGE:-nginx:1.26.3}
SECRET="0123456789abcdef0123456789abcdef"

pick_port() {
    python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
}
PORT80=${NGINX_TEST_PORT80:-$(pick_port)}
PORT443=${NGINX_TEST_PORT443:-$(pick_port)}
if [ "$PORT80" = "$PORT443" ]; then
    PORT443=$(pick_port)
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/nginx-check.XXXXXX")
CID=""
cleanup() {
    if [ -n "$CID" ]; then
        docker rm -f "$CID" >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

[ -f "$ASSET" ] || { echo "missing $ASSET" >&2; exit 2; }

mkdir -p "$TMP/certs" "$TMP/conf.d"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -subj "/CN=cdn.example.com" \
    -keyout "$TMP/certs/privkey.pem" -out "$TMP/certs/fullchain.pem" \
    >/dev/null 2>&1

sed -e "s|CHANGE_ME_CDN_SECRET|$SECRET|g" \
    -e "s|/etc/letsencrypt/live/cdn.example.com|__CERTDIR__|g" \
    "$ASSET" > "$TMP/site.conf"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    sed "s|__CERTDIR__|/certs|g" "$TMP/site.conf" > "$TMP/conf.d/default.conf"
    CID=$(docker run -d \
        -p "127.0.0.1:$PORT80:80" -p "127.0.0.1:$PORT443:443" \
        -v "$TMP/conf.d:/etc/nginx/conf.d:ro" \
        -v "$TMP/certs:/certs:ro" \
        "$IMAGE")

    ready=0
    for _ in $(seq 1 50); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 1 "http://127.0.0.1:$PORT80/health" || true)" = "200" ]; then
            ready=1
            break
        fi
        sleep 0.2
    done
    if [ "$ready" -ne 1 ]; then
        echo "nginx container did not become ready" >&2
        docker logs "$CID" >&2 || true
        exit 1
    fi

    fail=0
    check() {
        local name=$1 expected=$2 actual=$3
        if [ "$actual" != "$expected" ]; then
            echo "FAIL: $name: expected $expected, got $actual" >&2
            fail=1
        fi
    }

    base="http://127.0.0.1:$PORT80"
    probe="$base/_cdn_secret_probe"
    tunnel="$base/api-stream"

    check "health" 200 "$(curl -s -o /dev/null -w '%{http_code}' "$base/health")"
    check "probe without header" 000 "$(curl -s -o /dev/null -w '%{http_code}' "$probe" 2>/dev/null || true)"
    check "probe wrong header" 000 "$(curl -s -o /dev/null -w '%{http_code}' -H 'X-CDN-Secret: wrong' "$probe" 2>/dev/null || true)"
    check "probe with header" 204 "$(curl -s -o /dev/null -w '%{http_code}' -H "X-CDN-Secret: $SECRET" "$probe")"
    check "probe duplicate header" 204 "$(curl -s -o /dev/null -w '%{http_code}' \
        -H 'X-CDN-Secret: fake' -H "X-CDN-Secret: $SECRET" "$probe")"
    check "tunnel without header" 000 "$(curl -s -o /dev/null -w '%{http_code}' "$tunnel" 2>/dev/null || true)"
    tunnel_status=$(curl -s -o /dev/null -w '%{http_code}' -H "X-CDN-Secret: $SECRET" "$tunnel" || true)
    case "$tunnel_status" in
        5*) ;;
        *) echo "FAIL: tunnel with header: expected an upstream 5xx (no backend), got $tunnel_status" >&2; fail=1 ;;
    esac

    origin_json="$TMP/origin-check.json"
    if ! bash "$ROOT/scripts/origin-check.sh" \
        --host cdn.example.com --ip 127.0.0.1 --scheme http --port "$PORT80" \
        --cdn-secret "$SECRET" --json > "$origin_json"; then
        echo "FAIL: origin-check.sh rejected the hardened config" >&2
        cat "$origin_json" >&2 || true
        fail=1
    else
        if ! python3 - "$origin_json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    result = json.load(stream)
assert result["secret_gate_checked"] is True, result
assert result["secret_gate_mode"] == "direct", result
assert result["secret_gate_ok"] is True, result
assert result["secret_gate_no_header"] in ("000", "444"), result
assert result["secret_gate_with_header"] == "204", result
PY
        then
            echo "FAIL: origin-check.sh reported an unexpected secret gate result" >&2
            fail=1
        fi
    fi

    if [ "$fail" -ne 0 ]; then
        echo "nginx behavior: FAILED" >&2
        exit 1
    fi
    echo "nginx behavior: ok (health, secret gate, duplicate header, tunnel gate)"
    exit 0
fi

if command -v nginx >/dev/null 2>&1; then
    version=$(nginx -v 2>&1 | sed -n 's|.*nginx/\([0-9.]*\).*|\1|p')
    case "$version" in
        1.2[5-9].*|1.[3-9][0-9].*|[2-9].*) ;;
        *) echo "nginx behavior: skipped (local nginx $version is older than 1.25.1; set NGINX_IMAGE with Docker)" ; exit 0 ;;
    esac
    cat > "$TMP/nginx.conf" <<EOF
events {}
http {
    include $TMP/conf.d/default.conf;
}
EOF
    sed "s|__CERTDIR__|$TMP/certs|g" "$TMP/site.conf" > "$TMP/conf.d/default.conf"
    nginx -t -c "$TMP/nginx.conf" -p "$TMP"
    echo "nginx syntax: ok (behavior test needs Docker)"
    exit 0
fi

echo "nginx check: skipped (neither Docker nor nginx is available)"
exit 0
