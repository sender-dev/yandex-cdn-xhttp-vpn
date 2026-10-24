# Diagnostics

CDN failures are easy to misattribute because three layers look similar from the client: the edge drops the request, the edge carries the request but not the response, or the origin is fine and the client profile is wrong. This ladder separates them.

## Evidence rules

- **Run a control.** A direct-origin request through the same Xray client proves the client, UUID, cascade, and exit are healthy. If the control fails, stop testing the CDN.
- **Use a clean vantage.** A workstation with a local proxy, fake-IP DNS, or a VPN tunnel does not measure the edge. Datacenter and consumer sources can receive different treatment from the same edge; test both when the phone fails.
- **Use unique paths or nonces.** A cached response or a shared path cannot be attributed. Add a random query value to every short probe.
- **Repeat.** One success is noise. Five attempts per edge, twenty for acceptance.
- **Record UTC timestamps and correlate with origin logs.** Log precision is often one second; unique paths make correlation reliable.
- **State the limitation.** A datacenter test does not prove a mobile whitelist. A test outside a restriction window does not prove the restriction case.

## Vantage contamination

Check the workstation before trusting a result:

```bash
dig +short cdn.example.com
ip -brief address
ip route
```

Red flags:

- Answers in `198.18.0.0/15` or `fc00::/7`: fake-IP DNS from a local proxy. Resolve through `1.1.1.1`, `8.8.8.8`, and the authoritative nameservers; pin the real edge IP.
- A `tun`/`utun` interface or a local Xray/Clash process: traffic may not leave the host the way you think.
- `ALL_PROXY`/`HTTPS_PROXY` set: curl may use a proxy you did not intend. Pass `--noproxy '*'` (an empty value does not disable the proxy) and confirm the measured path.
- Corporate or cloud egress: a datacenter source can be blocked by an edge policy that allows residential sources, or the reverse.

## Test ladder

### 1. DNS

```bash
python3 scripts/cdn-probe.py dns --host cdn.example.com
python3 scripts/cdn-probe.py dns --host cdn.example.com --authoritative
```

The first command queries the system resolver and several public resolvers. The second walks the authoritative nameservers, which bypasses a negative cache. Compare the sets:

- Same set everywhere, several addresses: normal CDN rotation.
- Different sets: resolver or propagation issue.
- Fake-IP range: the workstation is proxied; discard the result.
- Empty from authoritative: the CNAME is missing or the zone is not published.

### 2. TLS per edge

```bash
python3 scripts/cdn-probe.py probe --host cdn.example.com --ip 198.51.100.20 --path /health
```

Checks SNI, certificate validity, expiry, protocol, status, and latency. A certificate error through the edge but not directly at the origin means the edge is serving a fallback or has not attached the certificate. A connection reset right after the handshake is an edge policy symptom, not a certificate symptom.

### 3. Short request per edge

```bash
python3 scripts/cdn-probe.py matrix --host cdn.example.com --path /api-stream --attempts 3
```

`OK` means the edge routed the request; `BLOCKED` means the edge or its method policy rejected it (`403`/`405`/`413`/`501`); `ERROR` means the edge answered 5xx; `FAIL` means no HTTP answer. A short routed request does not prove the edge carries a long-lived XHTTP session.

### 4. Method and cache behavior

```bash
python3 scripts/cdn-probe.py methods --host cdn.example.com --path /api-stream
python3 scripts/cdn-probe.py cache   --host cdn.example.com --path /api-stream
```

Methods should show GET allowed. A `413` on a POST body was measured through the tested public host; a reference deployment also reports `405` on POST at a Yandex edge. Cache should show no `HIT` and no `Age` growth on a unique URL.

### 5. XHTTP session per edge

```bash
# ~/.config/vpn-cdn/uuid is created once, mode 0600 (see SKILL.md step 4).
bash scripts/edge-test.sh \
  --edge 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid --attempts 5
```

This is the discriminating test. Short probes pass and sessions fail means the edge does not carry the long-lived flow. Sessions pass on one edge and fail on another means the fleet is uneven.

### 6. Origin correlation

On the origin, during the test window:

```bash
sudo tail -f /var/log/nginx/cdn-access.log
```

For each attempt, look for a line with the same path, method, and timestamp. Then inspect:

- `$request_length` above zero: the client packet reached the origin.
- `$bytes_sent` above zero: the origin answered.
- `$request_time` near the timeout: the upstream or cascade stalled.
- No line: the edge never routed the request.

For XHTTP sessions, a healthy flow shows repeated requests, not one. A session that handshakes and then sends nothing usually shows a `200` with a small request and no follow-up data.

### 7. Real device

On the phone, with Wi-Fi off:

- Confirm the carrier in the status bar or settings.
- Import the profile without deleting the working one.
- Open a health URL through the tunnel and record the egress IP.
- Run 20 connect/health attempts.
- Use the actual app, including a send and a receive.
- Repeat during an active restriction window if the claim is about whitelist mode.

## Interpreting percentages

| Pattern | Meaning |
|---|---|
| Short probes 20/20, sessions 13/20 with 12 s timeouts | Edge fleet or long-session handling, not origin |
| Short probes fail on one IP, pass on another | That edge is not serving the resource |
| All short probes pass, sessions 0/20 | Edge does not carry this flow, or the profile is mismatched |
| Sessions pass from datacenter, fail from consumer | Source-based edge policy |
| Sessions pass from the server, phone fails, origin log shows phone requests | Phone-to-edge leg works; return path or app routing is the issue |
| All attempts fail before the origin log | Profile, SNI, Host, path, or edge routing |

## Log and client signatures

- `101 Switching Protocols` at the origin followed by zero application bytes: the upgrade succeeded, data did not flow. Check the return path and source policy.
- `403` at the edge with no origin line: edge-side filtering, method, Host, or inactive resource.
- `405`: method not allowed. The client is sending POST or OPTIONS where GET is required.
- `413`: a request body was sent to an edge that forbids it.
- `431`/`400` on large requests: header budget exceeded.
- `malformed HTTP/1.x` plus HTTP/2 SETTINGS bytes: ALPN mismatch; do not force HTTP/1.1.
- `XTLS rejected UDP/443` for Apple destinations: expected for Apple services that use UDP; it is not evidence that Telegram failed. Telegram messaging uses TCP.
- A TCP connect to a Telegram DC succeeds but the message does not arrive: TCP reachability is not delivery; check the return path and the app state.
- Hundreds of reconnects in minutes: the client is retrying a stalled path; reduce concurrency and fix the stall before the edge treats it as abuse.

## When to stop

Stop and change the provider when all of these are true:

- The origin and cascade pass the direct control.
- Short probes pass through multiple edges.
- Sessions stall or return `403` across edges and sources.
- The provider's terms do not clearly permit the workload, or support cannot explain the edge behavior.

That combination is a provider limitation, not a tuning problem.
