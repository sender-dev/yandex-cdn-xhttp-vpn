---
name: yandex-cdn-xhttp-vpn
description: Build, debug, and audit a VLESS/XHTTP tunnel carried through a CDN edge when the direct path is blocked or throttled — Yandex Cloud CDN is field-verified; Selectel, Gcore, VK, Timeweb, and Beget notes are included. Use when the user mentions VPN через CDN, XHTTP packet-up, белые списки МТС, обход блокировок через CDN, Yandex Cloud CDN resource, pinning a live edge IP, CDN edge flapping or 20–35% timeouts, 403/405/413/431 at a CDN edge, nginx origin for XHTTP, VLESS share links for Happ/v2RayTun/Hiddify, or asks to plan or review such a deployment — even when no provider is named.
license: MIT
compatibility: Linux or macOS with bash, curl, dig, python3, and Xray-core on PATH for live tests. Provider console and DNS access are required for setup.
---

# CDN-carried VLESS/XHTTP tunnel

This skill encodes a field-tested way to keep a VLESS tunnel alive when the direct server IP is blocked or the mobile network only allows a curated set of services. A CDN edge becomes the public entry point; the origin still terminates the tunnel.

```text
Client (Happ / v2RayTun / Hiddify)
  -> TLS 443, address = pinned edge IP or CDN hostname, SNI = CDN hostname
  -> CDN edge
  -> origin :80 (plain HTTP in tested setups) nginx
  -> Xray VLESS/XHTTP inbound on 127.0.0.1
  -> VLESS/REALITY cascade to an exit server
  -> Internet
```

The CDN is not a magic bypass. It changes the IP and the network path the client talks to. Whether that path survives a given mobile restriction depends on the carrier, the region, and the restriction window. Only a test on the target SIM during an active restriction proves the whitelist case.

## Read this first

These are the expensive lessons. Re-deriving them costs days.

1. **Transport is the first decision, not the last.** The tested Yandex edge accepted `GET`, `HEAD`, and `OPTIONS` as methods; a POST body was rejected with `413` through the tested edge, and a reference deployment reports `405` on POST. WebSocket needs provider-side enablement on Yandex and did not pass through the tested edge. The transport that passed was **XHTTP `packet-up` with the uplink carried in GET headers**. Verify the method allowlist before designing anything else.
2. **CDN DNS returns dead edges.** A resource can have four or more edges; one or two may serve it correctly. The classic symptom is 20–35% of requests hanging until timeout while short requests pass. Pin a verified edge IP in the client profile and keep the domain profile as fallback. Edges rotate, so re-verify periodically.
3. **Both ends must agree.** XHTTP settings live at the top level of `xhttpSettings` in Xray JSON and inside a single URL-encoded `extra` object in a share link. Nesting them under `extra.xhttpSettings`, or setting them on only one side, silently falls back to defaults and produces mysterious `404`/`405` responses.
4. **Keep request headers under the edge limit.** Header placement base64-encodes the uplink payload. A 3–4 KiB data chunk becomes roughly 4–5.3 KiB on the wire before padding and keys. A 16 KiB chunk will exceed common 16 KiB header limits and produce `431`/`400`. Use 3–4 KiB chunks, 100–1000 bytes of padding, and raise nginx header buffers anyway.
5. **Disable caching and buffering at every layer.** A stale or transformed response breaks the session. Send `Cache-Control: no-store, no-transform`, disable `proxy_buffering` and `proxy_request_buffering`, and turn caching off on the CDN resource.
6. **Prove with a control and a real device.** A server-side success does not prove the mobile path works. Use a direct-origin control, a clean vantage point, timestamped series, and finally the actual phone on the actual SIM. Do not claim whitelist success from a datacenter test.
7. **Secrets stay out of the repository and logs.** UUIDs, private keys, origin IPs, resource IDs, and ticket numbers do not belong in a public repo or a shared transcript. Profiles are written mode `0600` outside the working tree.

## Provider gate

Run this gate before building. A CDN that fails any hard requirement will waste days.

| Requirement | Why it matters | How to check |
|---|---|---|
| Terms permit VPN / dynamic reverse proxy | Provider may block the resource later | Read the current FAQ and ToS; Selectel explicitly does not permit this workload |
| `GET` is allowed at the edge | XHTTP GET/header fails without it | `cdn-probe.py methods` |
| Origin can be fetched over HTTP/80 with a Host override | Tested edges fetched origin over plain HTTP into `default_server` | Check the resource settings and the origin access log |
| Cache can be disabled and responses not transformed | Sessions cannot survive cached or rewritten bodies | `cdn-probe.py cache` |
| The edge fleet actually serves the resource | Some edges return `403`/timeout for a valid resource | `cdn-probe.py matrix` |
| A certificate exists for the public hostname | TLS SNI must validate on the client | `cdn-probe.py probe` |
| The edge is reachable from consumer networks | Datacenter-source tests can pass while phones fail | Test from the target mobile network |
| A support channel exists | Broken edges and WebSocket enablement need a human | Provider console |

If the provider fails the gate, record the evidence and stop. Do not "fix" it with more client tuning.

## Workflow

### 0. Inventory and back up

Record the current origin, Xray, nginx, and client profiles. Back up every file before changing it, with owner-only permissions and a timestamp. Keep the direct REALITY profile working as a fallback until the CDN path passes acceptance.

### 1. Prepare the origin

The origin needs an address reachable from the edge, an nginx entry point, and a local Xray inbound. A certificate is needed only for the optional HTTPS block or for a provider that fetches origin over TLS; the tested Yandex edge fetched origin over **HTTP port 80** even when the panel claimed HTTPS, so keep the port-80 block correct and verify which vhost handled the request.

Use [assets/nginx-origin.conf](assets/nginx-origin.conf) and [assets/xray-inbound.example.json](assets/xray-inbound.example.json). Read [references/origin-setup.md](references/origin-setup.md) for the reasoning behind the buffers and timeouts.

Validate locally:

```bash
bash scripts/origin-check.sh --host cdn.example.com --ip 203.0.113.10 --scheme http --port 80
sudo nginx -t
xray run -test -config /usr/local/etc/xray/config.json
```

The check covers the health path, a 12 KiB request header (the nginx buffer budget), `Cache-Control`, and method behavior. Add `--scheme https --port 443` to check certificate verification as well.

### 2. Create the CDN resource

Provider steps and console details are in [references/yandex-cdn.md](references/yandex-cdn.md). In short: origin group points at the origin on HTTP/80 with the origin Host header, the resource allows `GET`/`HEAD`/`OPTIONS`, caching is off, the certificate covers the public hostname, and the DNS CNAME points at the target the console shows.

Wait for the resource to become active, then wait again. "Active" does not mean every edge is serving it.

### 3. Discover and rank edges

```bash
python3 scripts/cdn-probe.py dns --host cdn.example.com
python3 scripts/cdn-probe.py matrix --host cdn.example.com --path /api-stream --attempts 3
```

`dns` asks the system resolver and several public resolvers, flags fake-IP ranges, and can query authoritative nameservers. `matrix` performs a short unique-path request per edge and classifies the result as `OK` (routed), `BLOCKED` (`403`/`405`/`413`/`501`), `ERROR` (5xx), `PARTIAL`, or `FAIL`. That separates edge routing and method filtering from long-session stalls.

A short request that passes proves the edge routes the resource. It does not prove the edge carries an XHTTP session. Rank candidates with both checks.

### 4. Build a profile

```bash
# Create the UUID once, outside the repository, and reuse it everywhere.
install -d -m 700 ~/.config/vpn-cdn
(umask 077; xray uuid > ~/.config/vpn-cdn/uuid)
python3 scripts/make-profile.py \
  --address 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid \
  --preset yandex-get --name 'CDN pinned' --out ~/.config/vpn-cdn/pinned.vless
```

The generator writes the share URI (`--out`) and, optionally, a matching Xray client config (`--client-config`). Use `--address` = a verified edge IP for the pinned profile, or the CDN hostname for the fallback. Read [references/xhttp-transport.md](references/xhttp-transport.md) before changing any XHTTP key.

### 5. Test the session end to end

```bash
bash scripts/edge-test.sh \
  --edge 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid --attempts 5
```

The script starts a temporary Xray client, sends a series through the edge to a known URL, and reports `LIVE`, `PARTIAL`, or `DEAD`. It cleans up by PID, never with `pkill -f`. Use `--dry-run` to validate the generated config without sending traffic.

A `LIVE` result from a datacenter is necessary but not sufficient. Continue to the device test.

### 6. Accept on the target device

- Turn Wi-Fi off and confirm the phone is on the target carrier.
- Import the profile; keep the old profile installed.
- Run 20 connection/health attempts and record each result and latency.
- During a restriction window, test the actual app (for example, send and receive a Telegram message).
- Correlate failures with origin logs by timestamp, method, status, bytes, and request length.
- If restrictions were not active, report ordinary mobile connectivity only.

### 7. Operate

Monitoring, edge rotation, secret rotation, and the incident runbook are in [references/operations.md](references/operations.md). The short version: watch 4xx/5xx counts, sessions with `101` but zero application bytes, reconnect rate, certificate expiry, and the per-edge success rate. Re-run discovery when the success rate drops.

## Symptom -> diagnosis

| Symptom | Likely cause | Action |
|---|---|---|
| 20–35% of requests hang to the timeout, short probes pass | Dead or misconfigured edges in DNS rotation | Pin a verified edge; keep the domain as fallback |
| `101` handshake, then zero application bytes | Edge or upstream is not carrying the data path; possible consumer-source ACL | Check origin bytes; compare datacenter vs consumer source; provider support |
| Instant `403` at the edge | Edge-side filtering, host mismatch, method not allowed, resource not active | Compare an identical manual request; check methods, Host, and resource status |
| `405` on the uplink | Client uses POST or OPTIONS while the edge allows only GET | Set `uplinkHTTPMethod=GET` |
| `413` on the uplink | Request body sent where the edge forbids it | Switch to header placement; remove the body |
| `431` or `400` on large requests | Header chunk or padding too large | Lower `uplinkChunkSize`, raise nginx buffers |
| TLS reset or EOF right after the handshake | Consumer-source blackholing at the edge | Test from another source; provider support; try a different provider |
| `malformed HTTP/1.x` plus HTTP/2 SETTINGS bytes | Client forced HTTP/1.1 while ALPN negotiated h2 | Do not force `http/1.1`; leave ALPN at the default |
| Origin access log shows the request, client receives nothing | Return path broken | Correlate bytes; check edge and upstream; try another edge |
| Origin log shows nothing | Edge never routed the request | Check origin group, Host header, port, certificate, and resource status |
| Domain profile fails, pinned IP works | DNS returns edges that do not serve the resource | Pin; report the bad edges to the provider |
| Reconnect storm in origin logs | Client retry loop; possible anti-abuse response | Reduce concurrency, fix the stall, stop aggressive retries |

## What is verified, and what is not

Verified in the October 2026 field work behind this skill:

- Yandex Cloud CDN with XHTTP `packet-up` and GET/header uplink worked from an MTS SIM during an active restriction window.
- The same resource had working and non-working edges at the same time; pinning fixed the intermittent stalls.
- Selectel did not work: after a completed deployment its edge returned `403` for the resource's own `selcdn.net` name and `404` for the custom domain, and served a rotating fallback certificate. Its own FAQ says VPN and uncached dynamic proxy are unsupported.
- Gcore failed on consumer sources: WebSocket handshakes reached the origin (`101`) but no application bytes flowed; datacenter sources worked. Treat this as an edge policy difference, not a client bug.
- Xray's HTTP/1.1 XHTTP client can negotiate h2 via ALPN and then write HTTP/1.1 framing, producing a `malformed HTTP/1.x` failure. Do not force h1.
- Attribution itself is a failure mode: this study initially blamed one provider for edge behavior that was actually served by another. Confirm the CNAME and the console before attributing any edge symptom.

Not verified, and must not be presented as working:

- Yandex WebSocket: the edge did not pass an `Upgrade` in this study (0/20), and the provider enables the feature only through a support request. Not accepted as a working transport.
- VK: current VK Cloud CDN terms prohibit proxy/VPN use and traffic masking, so it fails the provider gate. Timeweb and Beget: provider notes only; this repository ships no tested preset, and this field study did not validate them.
- Any claim that a CDN hostname is on a carrier whitelist in general. One hostname passed in one region and one window. Re-test.

## Reference map

Read only what the task needs.

- [references/yandex-cdn.md](references/yandex-cdn.md) — Yandex Cloud CDN resource, methods, certificate, CNAME, edge behavior, support path.
- [references/origin-setup.md](references/origin-setup.md) — nginx port 80/443 blocks, buffers, Xray inbound, health checks, logging.
- [references/xhttp-transport.md](references/xhttp-transport.md) — XHTTP modes, header budget, the full `extra` object, share-link fields, client/server matching.
- [references/diagnostics.md](references/diagnostics.md) — test ladder, vantage contamination, log correlation, interpretation rules.
- [references/providers.md](references/providers.md) — Yandex, Selectel, Gcore, VK, Timeweb, Beget comparison and provider gate.
- [references/operations.md](references/operations.md) — backups, secrets, rotation, monitoring, incident runbook, acceptance checklist.

## Safety and scope

Use this on infrastructure you own or are authorized to administer. Check the CDN provider's current terms before carrying VPN traffic; several providers explicitly prohibit it, and a resource can be blocked for it. Do not use this skill to attack, scan, or evade controls on systems you do not own. The scripts send only a handful of requests to the hosts you name.
