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

python3 - <<'PY'
import json

for asset in ("assets/xray-inbound.example.json", "assets/xray-inbound-cascade.example.json"):
    with open(asset, encoding="utf-8") as stream:
        data = json.load(stream)
    cdn = next(inbound for inbound in data["inbounds"] if inbound["tag"] == "cdn-xhttp")
    settings = cdn["streamSettings"]["xhttpSettings"]
    assert settings["mode"] == "packet-up", (asset, settings)
    assert settings["uplinkHTTPMethod"] == "GET", (asset, settings)
    assert settings["uplinkDataPlacement"] == "header", (asset, settings)
    assert int(settings["uplinkChunkSize"]) == 16384, (asset, settings)
    assert settings["scMaxEachPostBytes"] == 32768, (asset, settings)
    assert settings["scMinPostsIntervalMs"] == 10, (asset, settings)
    assert settings["serverMaxHeaderBytes"] == 65536, (asset, settings)
    assert settings["enableXmux"] is True, (asset, settings)
    assert settings["scStreamUpServerSecs"] == "20-80", (asset, settings)
    assert settings["xPaddingObfsMode"] is False, (asset, settings)
    xmux = settings["xmux"]
    assert xmux["maxConcurrency"] == "16", (asset, xmux)
    assert xmux["hMaxRequestTimes"] == "600-900", (asset, xmux)
    assert xmux["hMaxReusableSecs"] == "1800-3000", (asset, xmux)
    assert xmux["cMaxReuseTimes"] == 0, (asset, xmux)
    assert xmux["hKeepAlivePeriod"] == 0, (asset, xmux)
PY
echo "asset framing: ok (parity set on both inbound examples)"

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

    vlessenc=$(xray vlessenc)
    decryption_key=$(printf '%s\n' "$vlessenc" | sed -n 's/^"decryption": "\(.*\)"$/\1/p' | head -1)
    encryption_key=$(printf '%s\n' "$vlessenc" | sed -n 's/^"encryption": "\(.*\)"$/\1/p' | head -1)
    test -n "$decryption_key"
    test -n "$encryption_key"

    python3 scripts/make-profile.py \
        --uuid 00000000-0000-4000-8000-000000000000 \
        --address 198.51.100.20 \
        --sni cdn.example.com \
        --path /api-stream \
        --vless-encryption "$encryption_key" \
        --client-config "$tmp/client-encrypted.json" \
        --out "$tmp/encrypted.vless" \
        --quiet
    xray run -test -config "$tmp/client-encrypted.json" >/dev/null
    python3 - "$tmp/client-encrypted.json" "$tmp/encrypted.vless" "$encryption_key" <<'PY'
import json
import sys
from urllib.parse import parse_qs, urlsplit

path, uri_path, key = sys.argv[1:4]
with open(path, encoding="utf-8") as stream:
    config = json.load(stream)
user = config["outbounds"][0]["settings"]["vnext"][0]["users"][0]
assert user["encryption"] == key, user
query = parse_qs(urlsplit(open(uri_path, encoding="utf-8").read()).query)
assert query["encryption"][0] == key, query
PY

    python3 - "$tmp/inbound.json" "$private_key" "$public_key" "$decryption_key" <<'PY'
import json
import sys

path, private, public, decryption = sys.argv[1:5]
with open("assets/xray-inbound-cascade.example.json", encoding="utf-8") as stream:
    data = json.load(stream)
for inbound in data["inbounds"]:
    settings = inbound.get("settings", {})
    if settings.get("decryption") == "<VLESS_DECRYPTION_KEY>":
        settings["decryption"] = decryption
        for client in settings.get("clients", []):
            client.pop("encryption", None)
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
assert settings["mode"] == "packet-up", settings
assert settings["uplinkHTTPMethod"] == "GET", settings
assert settings["uplinkDataPlacement"] == "header", settings
assert settings["uplinkChunkSize"] == 16384, settings
assert settings["scMaxEachPostBytes"] == 32768, settings
assert "sessionIDPlacement" not in settings, settings
assert "enableXmux" not in settings, settings
assert dump["inbounds"][0]["settings"]["decryption"] == "none", dump["inbounds"][0]["settings"]
PY

    python3 - "$tmp/inbound-single.json" "$private_key" "$decryption_key" <<'PY'
import json
import sys

path, private, decryption = sys.argv[1:4]
with open("assets/xray-inbound.example.json", encoding="utf-8") as stream:
    data = json.load(stream)
for inbound in data["inbounds"]:
    settings = inbound.get("settings", {})
    if settings.get("decryption") == "<VLESS_DECRYPTION_KEY>":
        settings["decryption"] = decryption
        for client in settings.get("clients", []):
            client.pop("encryption", None)
    reality = inbound.get("streamSettings", {}).get("realitySettings")
    if reality and reality.get("privateKey") == "<REALITY_PRIVATE_KEY>":
        reality["privateKey"] = private
        reality["shortIds"] = ["0123456789abcdef"]
with open(path, "w", encoding="utf-8") as stream:
    json.dump(data, stream, indent=2)
PY
    xray run -test -config "$tmp/inbound-single.json" >/dev/null
    xray run -test -dump -config "$tmp/inbound-single.json" > "$tmp/dump-single.json"
    python3 - "$tmp/dump-single.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    dump = json.load(stream)
by_tag = {inbound.get("tag"): inbound for inbound in dump["inbounds"]}
assert set(by_tag) == {"cdn-xhttp", "direct-reality"}, by_tag.keys()
assert by_tag["direct-reality"]["port"] == 443, by_tag["direct-reality"]
settings = by_tag["cdn-xhttp"]["streamSettings"]["xhttpSettings"]
assert settings["mode"] == "packet-up", settings
assert settings["uplinkHTTPMethod"] == "GET", settings
assert settings["uplinkDataPlacement"] == "header", settings
assert settings["uplinkChunkSize"] == 16384, settings
assert settings["scMaxEachPostBytes"] == 32768, settings
assert "sessionIDPlacement" not in settings, settings
assert "enableXmux" not in settings, settings
assert by_tag["cdn-xhttp"]["settings"]["decryption"] == "none", by_tag["cdn-xhttp"]["settings"]
tags = {outbound.get("tag") for outbound in dump["outbounds"]}
assert tags == {"direct", "blocked"}, tags
assert "cascade-reality" not in tags, tags
routed = {}
for rule in dump["routing"]["rules"]:
    for tag in rule.get("inboundTag", []):
        routed[tag] = rule.get("outboundTag")
assert routed == {"cdn-xhttp": "direct", "direct-reality": "direct"}, routed
PY
    echo "xray config: ok (client + vlessenc + single-server and cascade inbounds)"
else
    echo "xray config: skipped (xray not on PATH)"
fi

if command -v docker >/dev/null 2>&1 || command -v nginx >/dev/null 2>&1; then
    bash tests/nginx-check.sh
else
    echo "nginx check: skipped (neither docker nor nginx is available)"
fi

echo "ALL_CHECKS_OK"
