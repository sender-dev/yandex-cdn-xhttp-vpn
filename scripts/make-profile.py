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
from urllib.parse import quote, urlencode

VERSION = "1.1.0"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
ADDRESS_RE = re.compile(r"^[a-zA-Z0-9._:-]+$")

PRESETS = {
    "yandex-get": {
        "mode": "packet-up",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Data",
        "uplinkChunkSize": "3000-4000",
        "scMaxEachPostBytes": 8192,
        "scMaxBufferedPosts": 64,
        "scMinPostsIntervalMs": 10,
        "sessionPlacement": "header",
        "sessionKey": "X-Auth-Token",
        "seqPlacement": "query",
        "seqKey": "page",
        "xPaddingKey": "_rupl",
        "xPaddingHeader": "X-Cache",
        "xPaddingPlacement": "queryInHeader",
        "xPaddingMethod": "tokenish",
        "xPaddingObfsMode": True,
        "xPaddingBytes": "100-1000",
    },
    "minimal": {
        "mode": "packet-up",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkChunkSize": "3000-4000",
        "scMaxEachPostBytes": 8192,
        "scMaxBufferedPosts": 64,
        "scMinPostsIntervalMs": 10,
    },
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


def build_uri(address, sni, host, path, port, uuid, label, xhttp):
    extra = json.dumps(xhttp, separators=(",", ":"), sort_keys=True)
    query = urlencode(
        [
            ("encryption", "none"),
            ("security", "tls"),
            ("sni", sni),
            ("fp", "chrome"),
            ("type", "xhttp"),
            ("host", host),
            ("path", path),
            ("mode", xhttp.get("mode", "packet-up")),
            ("extra", extra),
        ],
        quote_via=quote,
        safe="",
    )
    uri_host = address if ":" not in address else "[" + address.strip("[]") + "]"
    return "vless://%s@%s:%d?%s#%s" % (
        uuid,
        uri_host,
        port,
        query,
        quote(label, safe=""),
    )


def build_client_config(address, sni, host, path, port, uuid, xhttp, socks_port):
    settings = {
        "path": path,
        "host": host,
        "mode": xhttp.get("mode", "packet-up"),
    }
    for key, value in xhttp.items():
        settings[key] = value
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
                            "users": [{"id": uuid, "encryption": "none"}],
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
    parser.add_argument("--preset", default="yandex-get", choices=sorted(PRESETS))
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
    xhttp = preset(args.preset)
    uri = build_uri(address, sni, host, path, args.port, uuid, args.name, xhttp)

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
            address, sni, host, path, args.port, uuid, xhttp, args.socks_port
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
