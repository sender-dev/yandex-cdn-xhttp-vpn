#!/usr/bin/env python3
"""Probe a CDN edge without third-party dependencies.

Subcommands:
  dns       resolve the public hostname through several resolvers
  probe     one TLS + HTTP request through a domain or a specific edge IP
  matrix    a short unique-path request to every edge IP
  methods   method allowlist behavior (GET/HEAD/OPTIONS/POST)
  cache     detect caching or transformation of a unique URL
  all       dns + one probe per resolved edge

The hostname is always used for SNI and the Host header. --ip changes only the
connection target, which is how a pinned edge is tested.
"""

import argparse
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import time

VERSION = "1.0.0"
DEFAULT_RESOLVERS = ["system", "1.1.1.1", "8.8.8.8", "77.88.8.8", "9.9.9.9"]
FAKE_NETS = [
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "fc00::/7",
        "fe80::/10",
    )
]


def fake_ip_reason(value):
    """Return a label when an address is private, reserved, or fake-IP space."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    for network in FAKE_NETS:
        if address in network:
            return str(network)
    return None


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def dig(host, record="A", resolver=None, timeout=6):
    """Query one resolver with dig. Returns a list of answer strings."""
    command = ["dig", "+short", "+time=3", "+tries=1", record, host]
    if resolver:
        command.insert(1, "@" + resolver)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    answers = []
    for line in result.stdout.splitlines():
        line = line.strip().rstrip(".")
        if not line or line.startswith(";"):
            continue
        answers.append(line)
    return answers


def unique_path(path):
    separator = "&" if "?" in path else "?"
    return path + separator + "probe=" + format(time.time_ns(), "x")


def _certificate(sock):
    try:
        cert = sock.getpeercert()
    except (ValueError, OSError):
        return {}
    if not cert:
        return {}
    subject = ", ".join(
        "%s=%s" % (key, value) for group in cert.get("subject", ()) for key, value in group
    )
    issuer = ", ".join(
        "%s=%s" % (key, value) for group in cert.get("issuer", ()) for key, value in group
    )
    not_after = cert.get("notAfter", "")
    days = None
    try:
        days = int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)
    except (ValueError, TypeError):
        pass
    return {
        "subject": subject,
        "issuer": issuer,
        "not_after": not_after,
        "days_left": days,
        "san": [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"],
    }


def _request_bytes(host, method, path, body):
    lines = [
        "%s %s HTTP/1.1" % (method, path),
        "Host: " + host,
        "User-Agent: cdn-probe/" + VERSION,
        "Accept: */*",
        "Cache-Control: no-cache",
        "Pragma: no-cache",
        "Connection: close",
    ]
    if body:
        lines.append("Content-Length: %d" % len(body))
        lines.append("Content-Type: application/octet-stream")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _fetch(host, ip, port, path, method, body, timeout, verify):
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    target = ip or host
    started = time.monotonic()
    raw = socket.create_connection((target, port), timeout=timeout)
    try:
        tls = context.wrap_socket(raw, server_hostname=host)
        try:
            tls.sendall(_request_bytes(host, method, path, body))
            response = http.client.HTTPResponse(tls)
            response.begin()
            payload = response.read(8192)
            headers = {}
            for key, value in response.getheaders():
                key = key.lower()
                headers[key] = headers[key] + ", " + value if key in headers else value
            elapsed = int((time.monotonic() - started) * 1000)
            return {
                "ip": target,
                "host": host,
                "sni": host,
                "method": method,
                "path": path,
                "status": response.status,
                "reason": response.reason,
                "latency_ms": elapsed,
                "headers": headers,
                "body_bytes": len(payload),
                "body_sha256": hashlib.sha256(payload).hexdigest()[:16],
                "tls_protocol": tls.version(),
                "tls_cipher": tls.cipher()[0] if tls.cipher() else "",
                "certificate": _certificate(tls),
                "tls_verified": bool(verify),
                "error": "",
            }
        finally:
            tls.close()
    finally:
        try:
            raw.close()
        except OSError:
            pass


def probe(
    host,
    ip=None,
    port=443,
    path="/",
    method="GET",
    body=b"",
    timeout=8,
    insecure=False,
    unique=True,
):
    """Probe one endpoint. If verification fails, retry without it to gather data."""
    if unique:
        path = unique_path(path)
    try:
        return _fetch(host, ip, port, path, method, body, timeout, verify=not insecure)
    except ssl.SSLCertVerificationError as error:
        result = {
            "ip": ip or host,
            "host": host,
            "sni": host,
            "method": method,
            "path": path,
            "status": None,
            "reason": "",
            "latency_ms": None,
            "headers": {},
            "body_bytes": 0,
            "body_sha256": "",
            "tls_protocol": "",
            "tls_cipher": "",
            "certificate": {},
            "tls_verified": False,
            "error": "certificate: " + str(error),
        }
        try:
            retry = _fetch(host, ip, port, path, method, body, timeout, verify=False)
            retry["tls_verified"] = False
            retry["error"] = "certificate: " + str(error)
            return retry
        except (OSError, ssl.SSLError, http.client.HTTPException) as retry_error:
            result["error"] += "; " + str(retry_error)
            return result
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return {
            "ip": ip or host,
            "host": host,
            "sni": host,
            "method": method,
            "path": path,
            "status": None,
            "reason": "",
            "latency_ms": None,
            "headers": {},
            "body_bytes": 0,
            "body_sha256": "",
            "tls_protocol": "",
            "tls_cipher": "",
            "certificate": {},
            "tls_verified": False,
            "error": str(error),
        }


def resolve_edges(host, resolvers):
    """Collect unique A records from the requested resolvers."""
    seen = {}
    for resolver in resolvers:
        if resolver == "system":
            answers = dig(host)
        else:
            answers = dig(host, resolver=resolver)
        for answer in answers:
            try:
                ipaddress.ip_address(answer)
            except ValueError:
                continue
            seen.setdefault(answer, []).append(resolver)
    return seen


def print_json(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def cmd_dns(args):
    resolvers = args.resolvers or DEFAULT_RESOLVERS
    edges = {}
    report = {"host": args.host, "time": utc_now(), "cname": "", "resolvers": {}, "edges": []}
    report["cname"] = ", ".join(dig(args.host, "CNAME")) or "-"
    for resolver in resolvers:
        answers = dig(args.host) if resolver == "system" else dig(args.host, resolver=resolver)
        report["resolvers"][resolver] = answers
        for answer in answers:
            try:
                ipaddress.ip_address(answer)
            except ValueError:
                continue
            edges.setdefault(answer, []).append(resolver)
    if args.authoritative:
        nameservers = dig(args.host, "NS")
        report["authoritative"] = {}
        for nameserver in nameservers:
            answers = dig(args.host, resolver=nameserver)
            report["authoritative"][nameserver] = answers
            for answer in answers:
                try:
                    ipaddress.ip_address(answer)
                except ValueError:
                    continue
                edges.setdefault(answer, []).append("NS:" + nameserver)
    for address, sources in sorted(edges.items()):
        reason = fake_ip_reason(address)
        report["edges"].append(
            {"ip": address, "seen_by": sorted(set(sources)), "fake_ip_reason": reason}
        )
    if args.json:
        print_json(report)
        return
    print("host:   " + args.host)
    print("cname:  " + report["cname"])
    print("time:   " + report["time"])
    print()
    print("%-20s %s" % ("resolver", "answers"))
    for resolver in resolvers:
        values = ", ".join(report["resolvers"][resolver]) or "-"
        print("%-20s %s" % (resolver, values))
    if args.authoritative:
        print()
        print("%-30s %s" % ("authoritative NS", "answers"))
        for nameserver, values in report["authoritative"].items():
            print("%-30s %s" % (nameserver, ", ".join(values) or "-"))
    print()
    print("unique edge addresses: %d" % len(report["edges"]))
    for item in report["edges"]:
        warning = "  FAKE-IP (%s)" % item["fake_ip_reason"] if item["fake_ip_reason"] else ""
        print("  %-16s via %s%s" % (item["ip"], ", ".join(item["seen_by"]), warning))
    if any(item["fake_ip_reason"] for item in report["edges"]):
        print()
        print("WARNING: fake-IP or private answers mean the resolver path is proxied.")
        print("         Do not use these results for edge ranking.")


def human_probe(result):
    print("ip:        " + str(result["ip"]))
    print("sni/host:  " + result["host"])
    print("method:    " + result["method"])
    print("status:    " + (str(result["status"]) if result["status"] else "-"))
    print("latency:   " + (str(result["latency_ms"]) + " ms" if result["latency_ms"] else "-"))
    print("tls:       " + (result["tls_protocol"] or "-") + (" verified" if result["tls_verified"] else " NOT verified"))
    if result["certificate"]:
        cert = result["certificate"]
        print("cert:      " + cert.get("subject", "-"))
        print("issuer:    " + cert.get("issuer", "-"))
        print("expires:   " + str(cert.get("not_after", "-")) + " (%s days)" % cert.get("days_left"))
        print("san:       " + ", ".join(cert.get("san", [])))
    if result["headers"]:
        for key in ("server", "age", "x-cache", "x-cdn-edge-cache", "cache-control", "x-reason-code"):
            if key in result["headers"]:
                print("%-10s %s" % (key + ":", result["headers"][key]))
    if result["error"]:
        print("error:     " + result["error"])


def cmd_probe(args):
    result = probe(
        args.host,
        ip=args.ip,
        port=args.port,
        path=args.path,
        method=args.method,
        body=b"x" * args.body_size,
        timeout=args.timeout,
        insecure=args.insecure,
    )
    if args.json:
        print_json(result)
    else:
        human_probe(result)
    return 0 if result["status"] is not None else 2


def status_class(code):
    """Classify an HTTP status for edge-routing purposes."""
    if code is None:
        return "ERR"
    if code in (403, 405, 413, 501):
        return "BLOCKED"
    if code >= 500:
        return "ERROR"
    if code in (200, 201, 204, 301, 302, 307, 308, 400, 404):
        return "ROUTED"
    return "OTHER"


def cmd_matrix(args):
    resolvers = args.resolvers or DEFAULT_RESOLVERS
    if args.ips:
        ips = [item.strip() for item in args.ips.split(",") if item.strip()]
    else:
        ips = sorted(resolve_edges(args.host, resolvers))
    if not ips:
        print("no edge addresses resolved for " + args.host, file=sys.stderr)
        return 2
    rows = []
    for ip in ips:
        attempts = []
        for _ in range(args.attempts):
            attempts.append(
                probe(args.host, ip=ip, path=args.path, timeout=args.timeout, insecure=args.insecure)
            )
        classes = [status_class(item["status"]) for item in attempts]
        answers = [item for item in attempts if item["status"] is not None]
        latencies = [item["latency_ms"] for item in answers if item["latency_ms"] is not None]
        if not answers:
            verdict = "FAIL"
        elif "BLOCKED" in classes:
            verdict = "BLOCKED"
        elif "ERROR" in classes:
            verdict = "ERROR"
        elif len(answers) == len(attempts):
            verdict = "OK"
        else:
            verdict = "PARTIAL"
        rows.append(
            {
                "ip": ip,
                "verdict": verdict,
                "classes": classes,
                "statuses": [item["status"] for item in attempts],
                "latency_ms": latencies,
                "attempts": attempts,
            }
        )
    if args.json:
        print_json({"host": args.host, "path": args.path, "time": utc_now(), "edges": rows})
        return 0
    print("host: %s  path: %s  attempts/edge: %d" % (args.host, args.path, args.attempts))
    print()
    print("%-16s %-8s %-22s %s" % ("edge", "verdict", "statuses", "latency ms"))
    for row in rows:
        statuses = ", ".join(str(value) if value is not None else "ERR" for value in row["statuses"])
        latencies = ", ".join(str(value) for value in row["latency_ms"]) or "-"
        print("%-16s %-8s %-22s %s" % (row["ip"], row["verdict"], statuses, latencies))
    print()
    print("OK means every short request returned a response. It does not prove")
    print("that the edge carries a long-lived XHTTP session; run edge-test.sh.")
    return 0 if any(row["verdict"] != "FAIL" for row in rows) else 2


def cmd_methods(args):
    results = []
    for method, body in (
        ("GET", b""),
        ("HEAD", b""),
        ("OPTIONS", b""),
        ("POST", b""),
        ("POST", b"x" * 1024),
    ):
        item = probe(
            args.host,
            ip=args.ip,
            path=args.path,
            method=method,
            body=body,
            timeout=args.timeout,
            insecure=args.insecure,
        )
        results.append(
            {
                "method": method,
                "body_size": len(body),
                "status": item["status"],
                "latency_ms": item["latency_ms"],
                "error": item["error"],
            }
        )
    if args.json:
        print_json({"host": args.host, "path": args.path, "time": utc_now(), "results": results})
        return 0
    print("host: %s  path: %s" % (args.host, args.path))
    print()
    print("%-10s %-10s %-8s %s" % ("method", "body", "status", "note"))
    for item in results:
        note = item["error"] or ""
        if item["status"] in (403, 405, 413, 501):
            note = "blocked at or before the edge"
        print("%-10s %-10s %-8s %s" % (item["method"], item["body_size"], item["status"], note))
    print()
    print("GET must be routed for XHTTP GET/header. A `413` on a POST body was")
    print("measured on the tested Yandex path; `405` on POST is also reported.")
    print("Treat any status as provider-specific until measured on your resource.")
    return 0


def cmd_cache(args):
    path = unique_path(args.path)
    first = probe(
        args.host,
        ip=args.ip,
        path=path,
        timeout=args.timeout,
        insecure=args.insecure,
        unique=False,
    )
    time.sleep(args.delay)
    second = probe(
        args.host,
        ip=args.ip,
        path=path,
        timeout=args.timeout,
        insecure=args.insecure,
        unique=False,
    )
    age = second["headers"].get("age", "")
    cache_header = second["headers"].get("x-cache", second["headers"].get("x-cdn-edge-cache", ""))
    transformed = bool(first["body_sha256"] and second["body_sha256"] and first["body_sha256"] != second["body_sha256"])
    try:
        age_seconds = int(str(age).strip() or 0)
    except ValueError:
        age_seconds = 0
    cached = age_seconds > 0 or "hit" in cache_header.lower() or transformed
    result = {
        "host": args.host,
        "path": path,
        "first": first,
        "second": second,
        "age": age,
        "cache_header": cache_header,
        "body_changed": transformed,
        "cached_or_transformed": cached,
    }
    if args.json:
        print_json(result)
        return 0
    print("host: %s  unique path: %s" % (args.host, path))
    print("first:  status=%s age=%s cache=%s body=%s" % (first["status"], "-", "-", first["body_sha256"]))
    print("second: status=%s age=%s cache=%s body=%s" % (second["status"], age or "-", cache_header or "-", second["body_sha256"]))
    print("verdict: " + ("CACHED OR TRANSFORMED" if cached else "no cache signal on the unique URL"))
    return 0


def cmd_all(args):
    addresses = sorted(resolve_edges(args.host, args.resolvers or DEFAULT_RESOLVERS))
    probe_results = [
        probe(args.host, ip=ip, path=args.path, timeout=args.timeout, insecure=args.insecure)
        for ip in addresses
    ]
    result = {
        "host": args.host,
        "time": utc_now(),
        "edges": addresses,
        "probes": probe_results,
    }
    if args.json:
        print_json(result)
    else:
        print("host: " + args.host)
        print("edges: " + (", ".join(addresses) or "none"))
        print()
        print("%-16s %-8s %-8s %s" % ("edge", "status", "tls", "latency"))
        for item in probe_results:
            print(
                "%-16s %-8s %-8s %s"
                % (
                    item["ip"],
                    item["status"] if item["status"] is not None else "ERR",
                    "ok" if item["tls_verified"] else "FAIL",
                    ("%s ms" % item["latency_ms"]) if item["latency_ms"] else "-",
                )
            )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Probe a CDN edge for an XHTTP tunnel (no third-party dependencies).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    dns = sub.add_parser("dns", help="resolve through several resolvers")
    dns.add_argument("--host", required=True, help="public CDN hostname")
    dns.add_argument("--resolvers", help="comma-separated resolvers, or 'system'")
    dns.add_argument("--authoritative", action="store_true", help="also query the zone nameservers")
    dns.add_argument("--json", action="store_true")
    dns.set_defaults(func=cmd_dns)

    one = sub.add_parser("probe", help="one TLS + HTTP request")
    one.add_argument("--host", required=True, help="SNI and Host header")
    one.add_argument("--ip", help="connect to this address instead of resolving --host")
    one.add_argument("--port", type=int, default=443)
    one.add_argument("--path", default="/")
    one.add_argument("--method", default="GET")
    one.add_argument("--body-size", type=int, default=0)
    one.add_argument("--timeout", type=float, default=8)
    one.add_argument("--insecure", action="store_true", help="skip certificate verification")
    one.add_argument("--json", action="store_true")
    one.set_defaults(func=cmd_probe)

    matrix = sub.add_parser("matrix", help="short unique request to every edge")
    matrix.add_argument("--host", required=True)
    matrix.add_argument("--path", default="/")
    matrix.add_argument("--ips", help="comma-separated addresses; default: resolve --host")
    matrix.add_argument("--resolvers", help="comma-separated resolvers, or 'system'")
    matrix.add_argument("--attempts", type=int, default=3)
    matrix.add_argument("--timeout", type=float, default=8)
    matrix.add_argument("--insecure", action="store_true")
    matrix.add_argument("--json", action="store_true")
    matrix.set_defaults(func=cmd_matrix)

    methods = sub.add_parser("methods", help="GET/HEAD/OPTIONS/POST behavior")
    methods.add_argument("--host", required=True)
    methods.add_argument("--path", default="/")
    methods.add_argument("--ip")
    methods.add_argument("--timeout", type=float, default=8)
    methods.add_argument("--insecure", action="store_true")
    methods.add_argument("--json", action="store_true")
    methods.set_defaults(func=cmd_methods)

    cache = sub.add_parser("cache", help="detect caching or transformation")
    cache.add_argument("--host", required=True)
    cache.add_argument("--path", default="/")
    cache.add_argument("--ip")
    cache.add_argument("--timeout", type=float, default=8)
    cache.add_argument("--delay", type=float, default=1.0)
    cache.add_argument("--insecure", action="store_true")
    cache.add_argument("--json", action="store_true")
    cache.set_defaults(func=cmd_cache)

    everything = sub.add_parser("all", help="dns + per-edge probe")
    everything.add_argument("--host", required=True)
    everything.add_argument("--path", default="/")
    everything.add_argument("--resolvers", help="comma-separated resolvers, or 'system'")
    everything.add_argument("--timeout", type=float, default=8)
    everything.add_argument("--insecure", action="store_true")
    everything.add_argument("--json", action="store_true")
    everything.set_defaults(func=cmd_all)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "resolvers", None):
        args.resolvers = [item.strip() for item in args.resolvers.split(",") if item.strip()]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
