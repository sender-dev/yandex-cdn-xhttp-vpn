#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

for file in install.sh scripts/*.sh tests/*.sh; do
    bash -n "$file"
done
echo "shell syntax: ok"

python3 -m py_compile scripts/*.py tests/test_*.py
echo "python compile: ok"

DD_TRACE_ENABLED=0 DD_CIVISIBILITY_ENABLED=0 \
    python3 -m unittest discover -s tests -p 'test_*.py' -v

bash tests/secret-scan.sh

if command -v xray >/dev/null 2>&1; then
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/xhttp-test.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT
    python3 scripts/make-profile.py \
        --uuid 00000000-0000-4000-8000-000000000000 \
        --address 198.51.100.20 \
        --sni cdn.example.com \
        --path /api-stream \
        --client-config "$tmp/client.json" \
        --quiet
    xray run -test -config "$tmp/client.json" >/dev/null

    keypair=$(xray x25519)
    private_key=$(printf '%s\n' "$keypair" | sed -n 's/^PrivateKey: //p')
    public_key=$(printf '%s\n' "$keypair" | sed -n 's/^Password (PublicKey): //p')
    python3 - "$tmp/inbound.json" "$private_key" "$public_key" <<'PY'
import json
import sys

path, private, public = sys.argv[1:4]
with open("assets/xray-inbound.example.json", encoding="utf-8") as stream:
    data = json.load(stream)
for inbound in data["inbounds"]:
    reality = inbound.get("streamSettings", {}).get("realitySettings")
    if reality and reality.get("privateKey") == "<REALITY_PRIVATE_KEY>":
        reality["privateKey"] = private
        reality["shortIds"] = ["0123456789abcdef"]
for outbound in data["outbounds"]:
    reality = outbound.get("streamSettings", {}).get("realitySettings")
    if reality and reality.get("publicKey") == "<REALITY_PUBLIC_KEY>":
        reality["publicKey"] = public
        reality["shortId"] = "0123456789abcdef"
    for vnext in outbound.get("settings", {}).get("vnext", []):
        if vnext.get("address") == "<EXIT_HOST>":
            vnext["address"] = "203.0.113.20"
with open(path, "w", encoding="utf-8") as stream:
    json.dump(data, stream, indent=2)
PY
    xray run -test -config "$tmp/inbound.json" >/dev/null
    xray run -test -dump -config "$tmp/inbound.json" > "$tmp/dump.json"
    python3 - "$tmp/dump.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    dump = json.load(stream)
settings = dump["inbounds"][0]["streamSettings"]["xhttpSettings"]
assert settings["sessionPlacement"] == "header", settings
assert settings["sessionKey"] == "X-Auth-Token", settings
assert "sessionIDPlacement" not in settings, settings
assert "enableXmux" not in settings, settings
PY
    echo "xray config: ok (client + inbound example)"
else
    echo "xray config: skipped (xray not on PATH)"
fi

echo "ALL_CHECKS_OK"
