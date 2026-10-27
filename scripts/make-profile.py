#!/usr/bin/env python3
"""Generate a VLESS/XHTTP share link and a matching Xray client config.

The XHTTP settings are kept in one preset so the share link, the client JSON,
and (by copy) the inbound cannot silently drift apart. Secrets are written with
mode 0600. The UUID can come from --uuid, --uuid-file, or VLESS_UUID.
"""

import argparse
import json
import os
import re
import stat
import sys

VERSION = "1.3.0"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
VLESS_ENCRYPTION_RE = re.compile(
    r"^mlkem768x25519plus(?:\.[A-Za-z0-9_-]+){3,}$"
)
HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
ADDRESS_RE = re.compile(r"^[a-zA-Z0-9._:-]+$")

# Characters a mobile share-link client leaves untouched when it percent-encodes
# a value. Colons and commas stay literal, which is what such clients emit.
URL_QUERY_ALLOWED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~!$&'()*+,;=:@/?"
)

PRESETS = {
    # Field-verified shape: the exact XHTTP knobs a real phone client accepted
    # and a real CDN origin accepted. Keep this preset free of keys the client
    # does not model (uplinkDataKey, scMaxBufferedPosts) and of client-only
    # tuning (xmux): every extra key is one more thing a strict importer can
    # reject. `mode` is a top-level share-link field, never part of `extra`.
    "happ-parity": {
        "scMaxEachPostBytes": 32768,
        "scMinPostsIntervalMs": 10,
        "uplinkChunkSize": 16384,
        "uplinkDataPlacement": "header",
        "uplinkHTTPMethod": "GET",
    },
    "yandex-get": {
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Data",
        "uplinkChunkSize": 4000,
        "scMaxEachPostBytes": 32768,
        "scMaxBufferedPosts": 64,
        "scMinPostsIntervalMs": 10,
    },
    "minimal": {
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkChunkSize": 4000,
        "scMaxEachPostBytes": 32768,
        "scMaxBufferedPosts": 64,
        "scMinPostsIntervalMs": 10,
    },
}

# Client-side HTTP/2 multiplexing is off by default: a single connection is
# what the verified profile used, and extra multiplexing only adds failure
# modes behind a CDN edge.
PRESET_CLIENT_XMUX = {
    "happ-parity": False,
    "yandex-get": True,
    "minimal": True,
}

XMUX = {
    "maxConcurrency": "0",
    "maxConnections": "4-8",
    "cMaxReuseTimes": "0",
    "hMaxRequestTimes": "300-600",
    "hMaxReusableSecs": "900-1800",
    "hKeepAlivePeriod": 0,
}


def fail(message):
    print("error: " + message, file=sys.stderr)
    raise SystemExit(2)


def validate_uuid(value):
    value = str(value or "").strip()
    if not UUID_RE.fullmatch(value):
        fail("invalid VLESS UUID")
    return value


def validate_host(value, label):
    value = str(value or "").strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(value):
        fail(label + " must be a domain name")
    return value


def validate_vless_encryption(value):
    value = str(value or "").strip()
    if not value or value == "none":
        return "none"
    if not VLESS_ENCRYPTION_RE.fullmatch(value):
        fail(
            "--vless-encryption must be a key from `xray vlessenc` "
            "(mlkem768x25519plus...) or `none`"
        )
    return value


def validate_address(value):
    value = str(value or "").strip()
    if not value or not ADDRESS_RE.fullmatch(value):
        fail("--address must be an IP address or a hostname")
    return value


def validate_path(value):
    value = str(value or "").strip()
    if not value.startswith("/") or " " in value or "\t" in value:
        fail("--path must start with / and contain no whitespace")
    if len(value) > 512:
        fail("--path is too long")
    return value


def validate_chunk_size(value):
    value = str(value or "").strip()
    if re.fullmatch(r"[0-9]+", value):
        size = int(value)
        if not 64 <= size <= 65536:
            fail("--chunk-size must be between 64 and 65536 bytes")
        return size
    match = re.fullmatch(r"([0-9]+)-([0-9]+)", value)
    if not match:
        fail("--chunk-size must be an integer or a MIN-MAX range")
    low, high = int(match.group(1)), int(match.group(2))
    if not 64 <= low <= high <= 65536:
        fail("--chunk-size range is invalid")
    return value


def read_uuid(args):
    if args.uuid:
        return validate_uuid(args.uuid)
    if args.uuid_file:
        try:
            with open(args.uuid_file, encoding="utf-8") as stream:
                value = stream.readline().strip()
        except OSError as error:
            fail("cannot read --uuid-file: " + str(error))
        mode = os.stat(args.uuid_file).st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            print(
                "warning: %s is readable by group or others; use chmod 600"
                % args.uuid_file,
                file=sys.stderr,
            )
        return validate_uuid(value)
    value = os.environ.get("VLESS_UUID", "").strip()
    if value:
        return validate_uuid(value)
    fail("provide --uuid, --uuid-file, or VLESS_UUID")


def preset(name):
    if name not in PRESETS:
        fail("unknown preset: " + name)
    return dict(PRESETS[name])


def percent_encode(value):
    """Percent-encode a value the way mobile share-link clients do."""
    encoded = []
    for char in str(value):
        if char in URL_QUERY_ALLOWED:
            encoded.append(char)
        else:
            encoded.extend("%%%02X" % byte for byte in char.encode("utf-8"))
    return "".join(encoded)


def encode_json_like_apple(value, depth=0):
    """Pretty-print JSON the way Apple's JSONSerialization does.

    Apple puts a space on both sides of the colon and indents with two
    spaces. Share-link clients that build their own `extra` blob use this
    format, so reproducing it keeps generated links byte-comparable with a
    link that was proven on a device.
    """
    pad = "  " * depth
    inner_pad = "  " * (depth + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        entries = [
            "%s%s : %s" % (inner_pad, json.dumps(key), encode_json_like_apple(item, depth + 1))
            for key, item in value.items()
        ]
        return "{\n" + ",\n".join(entries) + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        entries = [
            "%s%s" % (inner_pad, encode_json_like_apple(item, depth + 1))
            for item in value
        ]
        return "[\n" + ",\n".join(entries) + "\n" + pad + "]"
    return json.dumps(value)


def build_extra(xhttp, encoding="happ"):
    """Render the `extra` blob of a share link.

    Share-link clients model `uplinkChunkSize` as a string, so it is rendered
    as a string here even though the Xray client config wants a number.
    """
    payload = dict(xhttp)
    if "uplinkChunkSize" in payload:
        payload["uplinkChunkSize"] = str(payload["uplinkChunkSize"])
    inner = encode_json_like_apple(payload)
    if encoding == "plain":
        return inner
    return percent_encode(inner)


def build_uri(
    address,
    sni,
    host,
    path,
    port,
    uuid,
    label,
    xhttp,
    encryption="none",
    extra_encoding="happ",
):
    params = []
    if encryption and encryption != "none":
        params.append(("encryption", encryption))
    params.extend(
        [
            ("security", "tls"),
            ("sni", sni),
            ("fp", "chrome"),
            ("type", "xhttp"),
            ("host", host),
            ("path", path),
            ("mode", xhttp.get("mode", "packet-up")),
            ("extra", build_extra(xhttp, extra_encoding)),
        ]
    )
    query = "&".join("%s=%s" % (key, percent_encode(value)) for key, value in params)
    uri_host = address if ":" not in address else "[" + address.strip("[]") + "]"
    return "vless://%s@%s:%d?%s#%s" % (
        uuid,
        uri_host,
        port,
        query,
        percent_encode(label),
    )


def build_client_config(
    address,
    sni,
    host,
    path,
    port,
    uuid,
    xhttp,
    socks_port,
    vless_encryption="none",
    include_xmux=True,
):
    settings = {
        "path": path,
        "host": host,
        "mode": xhttp.get("mode", "packet-up"),
    }
    for key, value in xhttp.items():
        settings[key] = value
    if include_xmux:
        settings["xmux"] = dict(XMUX)
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": address,
                            "port": port,
                            "users": [
                                {
                                    "id": uuid,
                                    "encryption": vless_encryption or "none",
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "xhttp",
                    "security": "tls",
                    "tlsSettings": {
                        "serverName": sni,
                        "fingerprint": "chrome",
                    },
                    "xhttpSettings": settings,
                },
            }
        ],
    }


def write_private(path, content):
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as error:
        fail("cannot create " + directory + ": " + str(error))
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            if not content.endswith("\n"):
                stream.write("\n")
        os.chmod(path, 0o600)
    except OSError as error:
        fail("cannot write " + path + ": " + str(error))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a VLESS/XHTTP share link and matching Xray client config.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--address", required=True, help="edge IP or CDN hostname")
    parser.add_argument("--sni", required=True, help="TLS SNI; usually the CDN hostname")
    parser.add_argument("--host", help="HTTP Host header; defaults to --sni")
    parser.add_argument("--path", default="/api-stream")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--uuid")
    parser.add_argument("--uuid-file")
    parser.add_argument("--preset", default="happ-parity", choices=sorted(PRESETS))
    parser.add_argument(
        "--extra-encoding",
        default="happ",
        choices=("happ", "plain"),
        help="how the `extra` blob is percent-encoded: 'happ' matches mobile "
        "share-link clients (encoded twice), 'plain' encodes it once",
    )
    parser.add_argument(
        "--chunk-size",
        help="override uplinkChunkSize: an integer (e.g. 4000) or a range "
        "string (e.g. 3000-4000). Integer is required by older client cores "
        "that decode the field as uint32",
    )
    parser.add_argument(
        "--vless-encryption",
        help="client key from `xray vlessenc` (mlkem768x25519plus...); "
        "`none` keeps classic VLESS. Also read from VLESS_ENCRYPTION",
    )
    parser.add_argument("--name", default="CDN XHTTP")
    parser.add_argument("--out", help="write the share URI to this file (0600); '-' for stdout")
    parser.add_argument("--client-config", help="write an Xray client config (0600)")
    parser.add_argument("--socks-port", type=int, default=11096)
    parser.add_argument("--quiet", action="store_true", help="do not print the share URI")
    parser.add_argument("--json", action="store_true", help="print a secret-free summary")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.out == "-" and args.json:
        fail("--out - and --json both use stdout; choose one")
    address = validate_address(args.address)
    sni = validate_host(args.sni, "--sni")
    host = validate_host(args.host or sni, "--host")
    path = validate_path(args.path)
    if not 1 <= args.port <= 65535:
        fail("--port is out of range")
    if not 1 <= args.socks_port <= 65535:
        fail("--socks-port is out of range")
    uuid = read_uuid(args)
    vless_encryption = validate_vless_encryption(
        args.vless_encryption or os.environ.get("VLESS_ENCRYPTION", "")
    )
    xhttp = preset(args.preset)
    if args.chunk_size:
        chunk_size = validate_chunk_size(args.chunk_size)
        if isinstance(chunk_size, str):
            print(
                "warning: a chunk-size range is rejected by client cores that "
                "decode uplinkChunkSize as uint32 (Xray <= 26.2.x); use a "
                "plain integer for older phones",
                file=sys.stderr,
            )
        xhttp["uplinkChunkSize"] = chunk_size
    uri = build_uri(
        address,
        sni,
        host,
        path,
        args.port,
        uuid,
        args.name,
        xhttp,
        vless_encryption,
        args.extra_encoding,
    )

    if args.out == "-":
        print(uri)
    elif args.out:
        write_private(args.out, uri)
        if not args.quiet:
            print("wrote share URI to " + args.out + " (mode 0600)", file=sys.stderr)
    elif not args.quiet and not args.json:
        print(uri)

    if args.client_config:
        config = build_client_config(
            address,
            sni,
            host,
            path,
            args.port,
            uuid,
            xhttp,
            args.socks_port,
            vless_encryption,
            include_xmux=PRESET_CLIENT_XMUX.get(args.preset, False),
        )
        write_private(args.client_config, json.dumps(config, indent=2, sort_keys=True))
        if not args.quiet:
            print(
                "wrote client config to " + args.client_config + " (mode 0600)",
                file=sys.stderr,
            )

    if args.json:
        print(
            json.dumps(
                {
                    "address": address,
                    "sni": sni,
                    "host": host,
                    "path": path,
                    "port": args.port,
                    "preset": args.preset,
                    "extra_encoding": args.extra_encoding,
                    "vless_encryption_enabled": vless_encryption != "none",
                    "uri_written": bool(args.out),
                    "client_config_written": bool(args.client_config),
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
