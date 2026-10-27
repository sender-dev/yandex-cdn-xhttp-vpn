---
name: yandex-cdn-xhttp-vpn
version: 2.0.0
description: Build, debug, and audit a VLESS/XHTTP tunnel carried through a CDN edge when the direct path is blocked or throttled — Yandex Cloud CDN is field-verified; Selectel, Gcore, VK, Timeweb, and Beget notes are included. Covers the hardened origin profile — GET/header uplink, X-CDN-Secret origin authentication, nginx keepalive tuning, and native VLESS Encryption (vlessenc). Use when the user mentions VPN через CDN, XHTTP packet-up, белые списки МТС, обход блокировок через CDN, Yandex Cloud CDN resource, pinning a live edge IP, CDN edge flapping or 20–35% timeouts, 403/405/413/431 at a CDN edge, nginx origin for XHTTP, VLESS share links for Happ/v2RayTun/Hiddify, or asks to plan or review such a deployment — even when no provider is named.
license: MIT
compatibility: Linux or macOS with bash, curl, dig, python3, and Xray-core on PATH. VLESS Encryption needs Xray-core 25.9.5 or newer. Provider console and DNS access are required for setup.
---

# CDN-carried VLESS/XHTTP tunnel

This skill encodes a field-tested way to keep a VLESS tunnel alive when the direct server IP is blocked or the mobile network only allows a curated set of services. A CDN edge becomes the public entry point; the origin still terminates the tunnel.

```text
Client (Happ / v2RayTun / Hiddify)
  -> TLS 443, address = pinned edge IP or CDN hostname, SNI = CDN hostname
  -> CDN edge
  -> origin :80 nginx (CDN injects X-CDN-Secret; 444 without it)
  -> Xray VLESS/XHTTP inbound on 127.0.0.1 (plain VLESS; vlessenc optional)
  -> outbound freedom (origin = exit)
  -> Internet
```

The CDN is not a magic bypass. It changes the IP and the network path the client talks to. Whether that path survives a given mobile restriction depends on the carrier, the region, and the restriction window. Only a test on the target SIM during an active restriction proves the whitelist case.

The edge-to-origin leg is public transit, not a private datacenter link. Two independent controls matter there: `X-CDN-Secret` proves that a request came through the configured CDN resource, and `vlessenc` encrypts the VLESS payload so the data placed in HTTP headers is not readable on that leg. The transport shape (path, method, sequence, session id) remains visible.

This repository is a single-skill package. Install or update it with `npx skills add sender-dev/yandex-cdn-xhttp-vpn -g`, or from a clone with `./install.sh`. All paths below are relative to this skill's directory.

## Read this first

These are the expensive lessons. Re-deriving them costs days.

1. **Transport is the first decision, not the last.** The tested Yandex edge accepted `GET`, `HEAD`, and `OPTIONS` as methods; a POST body was rejected with `413` through the tested edge, and a reference deployment reports `405` on POST. WebSocket needs provider-side enablement on Yandex and did not pass through the tested edge. The transport that passed was **XHTTP `packet-up` with the uplink carried in GET headers**. Verify the method allowlist before designing anything else.
2. **CDN DNS returns dead edges.** A resource can have four or more edges; one or two may serve it correctly. The classic symptom is 20–35% of requests hanging until timeout while short requests pass. Pin a verified edge IP in the client profile and keep the domain profile as fallback. Edges rotate, so re-verify periodically.
3. **Both ends must agree.** XHTTP settings live at the top level of `xhttpSettings` in Xray JSON and inside a single URL-encoded `extra` object in a share link. Nesting them under `extra.xhttpSettings`, or setting them on only one side, silently falls back to defaults and produces mysterious `404`/`405` responses.
4. **Keep request headers under the edge limit.** Header placement base64-encodes the uplink payload. A 3–4 KiB data chunk becomes roughly 4–5.3 KiB on the wire before padding and keys. A 16 KiB chunk will exceed common 16 KiB header limits and produce `431`/`400`. Use 3–4 KiB chunks, 100–1000 bytes of padding, and raise nginx header buffers anyway.
5. **Disable caching and buffering at every layer.** A stale or transformed response breaks the session. Send `Cache-Control: no-store, no-transform`, disable `proxy_buffering` and `proxy_request_buffering`, and turn caching off on the CDN resource.
6. **Prove with a control and a real device.** A server-side success does not prove the mobile path works. Use a direct-origin control, a clean vantage point, timestamped series, and finally the actual phone on the actual SIM. Do not claim whitelist success from a datacenter test.
7. **Secrets stay out of the repository and logs.** UUIDs, private keys, origin IPs, resource IDs, and ticket numbers do not belong in a public repo or a shared transcript. Profiles are written mode `0600` outside the working tree.
8. **Authenticate the edge to the origin.** Set a static `X-CDN-Secret` request header on the CDN resource and reject the tunnel path without it. This is not user authentication — the UUID still does that — and it does not stop requests that travel through the CDN. It stops direct scans of the origin IP from finding a working XHTTP endpoint.
9. **Encrypt head-placed payloads.** Header placement is base64, not encryption. Without `vlessenc`, anyone on the edge-to-origin path can read the destination and the data in `X-Data-*`. Generate a key pair with `xray vlessenc`, put the decryption string only on the server and the encryption string only on the client, and verify the wire before claiming the leg is protected. Not every client core supports it; keep a non-encrypted fallback profile where compatibility matters.

Do not put the secret check at server level. A blanket `return 444` breaks `/health`, ACME HTTP-01, and every unrelated vhost. Gate only the tunnel paths, leave liveness and challenge paths open, and add the `/_cdn_secret_probe` control endpoint so the plumbing can be tested without a live tunnel.

## Provider gate

Run this gate before building. A CDN that fails any hard requirement will waste days.

| Requirement | Why it matters | How to check |
|---|---|---|
| Terms permit VPN / dynamic reverse proxy | Provider may block the resource later | Read the current FAQ and ToS; Selectel explicitly does not permit this workload |
| `GET` is allowed at the edge | XHTTP GET/header fails without it | `cdn-probe.py methods` |
| Origin can be fetched over HTTP/80 with a Host override | Tested edges fetched origin over plain HTTP into `default_server` | Check the resource settings and the origin access log |
| The resource can send a static request header to the origin | Needed for `X-CDN-Secret`; without it the origin gate locks every request out | Provider docs and `/_cdn_secret_probe` through the CDN |
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

Decide the topology before installing Xray. The default is [assets/xray-inbound.example.json](assets/xray-inbound.example.json): origin and exit on one server, both inbounds routed to `freedom`. When CDN edges cannot reach the exit, the exit IP must stay hidden, or the plain-HTTP border hop is unacceptable, install [assets/xray-inbound-cascade.example.json](assets/xray-inbound-cascade.example.json): the same CDN-facing inbound plus a REALITY cascade to a separate exit. In single-server mode Xray owns `:443`; in cascade mode the nginx 443 block may be kept. Both assets share the same XHTTP framing, so the client preset does not change. Trade-offs and rollback are in [references/single-server.md](references/single-server.md).

Generate the CDN secret outside the repository and store it mode `0600`:

```bash
install -d -m 700 ~/.config/vpn-cdn
(umask 077; openssl rand -hex 32 > ~/.config/vpn-cdn/cdn-secret)
```

The VLESS Encryption key pair is optional. Generate it only when you intend to use it:

```bash
(umask 077; xray vlessenc > ~/.config/vpn-cdn/vlessenc.txt)
```

`xray vlessenc` prints two authentication options (X25519 and ML-KEM-768). Each option's `decryption` and `encryption` lines are a pair; use one option consistently.

Use [assets/nginx-origin.conf](assets/nginx-origin.conf) and [assets/xray-inbound.example.json](assets/xray-inbound.example.json). In the nginx file, replace `CHANGE_ME_CDN_SECRET` with the generated value. The inbound ships with `"decryption": "none"`, which matches a plain-VLESS client and is the configuration that was verified on a phone. To use VLESS Encryption, put the **decryption** string from `vlessenc.txt` in `settings.decryption` and leave `encryption` out of the inbound client entries; the client then gets the **encryption** string from the same option.

The gate belongs only on tunnel paths. `/health`, `/_cdn_secret_probe`, and `/.well-known/acme-challenge/` stay reachable; `/_cdn_secret_probe` returns `204` with the header and closes the connection without it. A server-level `return 444` is a self-inflicted outage.

Read [references/origin-setup.md](references/origin-setup.md) for the reasoning behind the buffers, keepalive, timeouts, and gate placement.

Validate locally:

```bash
bash scripts/origin-check.sh --host cdn.example.com --ip 203.0.113.10 \
  --scheme http --port 80 --cdn-secret "$(cat ~/.config/vpn-cdn/cdn-secret)"
sudo nginx -t
xray run -test -config /usr/local/etc/xray/config.json
```

The check covers the health path, a 12 KiB request header (the nginx buffer budget), `Cache-Control`, method behavior, and — when `--cdn-secret` is present — door control: rejected without the header, `204` with it. Add `--scheme https --port 443` to check certificate verification as well.

### 2. Create the CDN resource

Provider steps and console details are in [references/yandex-cdn.md](references/yandex-cdn.md). In short: origin group points at the origin on HTTP/80 with the origin Host header, the resource allows `GET`/`HEAD`/`OPTIONS`, caching is off, the certificate covers the public hostname, and the DNS CNAME points at the target the console shows.

Add the origin gate header to the resource:

```bash
yc cdn resource update <RESOURCE_ID> \
  --static-request-headers X-CDN-Secret="$(cat ~/.config/vpn-cdn/cdn-secret)"
```

Header names may contain letters, digits, `-`, and `_`; values may contain letters, digits, `-`, `_`, `/`, `:`, `=`, `.`, and spaces. A hex secret satisfies both. Read the resource back after the update. Then prove the injection through the CDN before building profiles:

```bash
bash scripts/origin-check.sh --host cdn.example.com --scheme https --port 443 \
  --cdn-secret "$(cat ~/.config/vpn-cdn/cdn-secret)"
```

A `204` on the probe through the CDN hostname means the edge sent the header. If the probe closes without a response, the resource is not sending it; do not proceed with the gate enabled.

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
  --preset happ-parity --name 'CDN pinned' --out ~/.config/vpn-cdn/pinned.vless
```

The generator writes the share URI (`--out`) and, optionally, a matching Xray client config (`--client-config`). Use `--address` = a verified edge IP for the pinned profile, or the CDN hostname for the fallback.

`--preset happ-parity` is the default and the shape that was verified on a phone: five XHTTP keys, `uplinkChunkSize` as a string, `mode` at the top level of the link, and no `encryption` parameter for plain VLESS. The `extra` blob is percent-encoded twice and pretty-printed the way mobile clients print it, so the generated link is byte-comparable with a link that was proven on a device.

To enable VLESS Encryption, add the client key from the same `xray vlessenc` run as the inbound `decryption`:

```bash
client_key=$(sed -n 's/^"encryption": "\(.*\)"$/\1/p' ~/.config/vpn-cdn/vlessenc.txt | head -1)
python3 scripts/make-profile.py ... --vless-encryption "$client_key"
```

Omit `--vless-encryption` when the server inbound still uses `"decryption": "none"`; a client and server with different encryption settings fail at the first packet. Read [references/xhttp-transport.md](references/xhttp-transport.md) before changing any XHTTP key.

### 5. Test the session end to end

```bash
bash scripts/edge-test.sh \
  --edge 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid \
  --attempts 5
```

The script starts a temporary Xray client, sends a series through the edge to a known URL, and reports `LIVE`, `PARTIAL`, or `DEAD`. It cleans up by PID, never with `pkill -f`. Use `--dry-run` to validate the generated config without sending traffic.

A `LIVE` result from a datacenter is necessary but not sufficient. Continue to the device test.

A desktop test does not cover the mobile import path. When a link imports but never connects, compare its `extra` blob with a blob that is known to work, byte for byte, before touching the server.

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
| Client aborts with `cannot unmarshal string into Go struct field ...uplinkChunkSize of type uint32` | The client core predates range parsing and wants a plain integer | Regenerate with `--chunk-size 4000`; keep the range string only for cores that accept it |
| Client core refuses to start with `VLESS users: please add/set "encryption":"none" for every user` | The profile was written without an explicit `encryption` field on the VLESS user | Emit `"encryption": "none"` on every user; the generator does this |
| Share link imports in the app but the tunnel never starts, while another profile against the same server works | The `extra` blob does not match what the mobile importer models: wrong key set, wrong value type, wrong JSON whitespace, or the wrong number of percent-encoding layers | Compare the blob byte for byte with a known-good link; regenerate with `--preset happ-parity` |
| Share link imports, tunnel starts, then the first packet fails | The link carries a `vlessenc` key the server does not expect, or the server uses `vlessenc` and the link does not carry a key | Plain VLESS: no `encryption` parameter in the link and `"decryption": "none"` on the inbound |
| Empty reply or `444` through the CDN | The edge did not send `X-CDN-Secret`, or sent the wrong one | Read back `--static-request-headers`; test `/_cdn_secret_probe` through the CDN hostname |
| Direct origin request returns `444` (expected) | The gate is working; the origin is not meant to be public | Test through the CDN, not by IP |
| First packet fails right after enabling `vlessenc` | Client and server keys swapped or mismatched; old client core | Server gets `decryption`, client gets `encryption`; both from the same `xray vlessenc` run |
| Client connects but no payload flows after enabling `vlessenc` | Client core ignores the encryption field instead of rejecting it | Use an Xray-core build 25.9.5+ or keep a `none` fallback profile |
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
- A mobile share link has its own contract, separate from the Xray client JSON. The verified link carries exactly five XHTTP keys in `extra` (`scMaxEachPostBytes`, `scMinPostsIntervalMs`, `uplinkChunkSize`, `uplinkDataPlacement`, `uplinkHTTPMethod`), keeps `mode` at the top level of the link, renders `uplinkChunkSize` as a string, percent-encodes the blob twice, and omits `encryption` entirely for plain VLESS. A link that differs in any of these points imports and then never connects.
- The lowest-risk credential change is a new UUID on the proven path, with every transport knob untouched. A second inbound is a second framing, and the framing is what mobile clients reject.
- Selectel did not work: after a completed deployment its edge returned `403` for the resource's own `selcdn.net` name and `404` for the custom domain, and served a rotating fallback certificate. Its own FAQ says VPN and uncached dynamic proxy are unsupported.
- Gcore failed on consumer sources: WebSocket handshakes reached the origin (`101`) but no application bytes flowed; datacenter sources worked. Treat this as an edge policy difference, not a client bug.
- Xray's HTTP/1.1 XHTTP client can negotiate h2 via ALPN and then write HTTP/1.1 framing, producing a `malformed HTTP/1.x` failure. Do not force h1.
- Attribution itself is a failure mode: this study initially blamed one provider for edge behavior that was actually served by another. Confirm the CNAME and the console before attributing any edge symptom.

Verified in production on 2026-10-27 against a live Yandex edge and Xray 26.10.21:

- The edge injected `X-CDN-Secret`, `/_cdn_secret_probe` through the CDN returned `204`, and a direct request to the origin without the header was closed by the gate. The gate is enforced.
- The CDN inbound runs plain VLESS with `"decryption": "none"` and routes to `freedom`, so the origin is also the exit. Its parsed framing matches the shipped asset key for key apart from the path, including the `xmux` limits and `scStreamUpServerSecs`.
- XHTTP `packet-up` with GET/header uplink carried sessions through the edge. The residual failure rate was roughly half and was identical for the working profile and the new one, which places it in the edge and the upstream response rather than in the profile.
- A VLESS user without an explicit `encryption` field makes a modern core refuse to start. The field must be present on every user, with the literal `none` for plain VLESS.

Verified locally on 2026-10-26 against Xray 26.3.27 and nginx 1.26.3:

- `vlessenc` takes a string on both sides. The inbound accepts only `settings.decryption`; adding `encryption` to an inbound client is rejected with `"encryption" should not be in inbound settings`. A full pair generated by `xray vlessenc` produced a working XHTTP session with GET/header and `packet-up` on loopback.
- With `vlessenc` enabled, a request captured on the CDN-to-origin leg showed the target hostname, URL path, and payload marker in none of the bytes. `X-Data-0` contained ciphertext; the request header block was about 3.1 KiB for a 3–4 KiB chunk.
- `keepalive_requests` and `keepalive_timeout` are accepted in both `upstream` and `server` contexts. A gate using `map $http_x_cdn_secret` with `return 444` behaves as intended: no header closes the connection, the right header passes, `/health` and ACME stay open.
- nginx joins duplicate request headers with `", "`. A strict equality check would fail when a client prepends its own `X-CDN-Secret` through the CDN; a regex `map` that matches the secret anywhere in the value passes, and a direct scan still cannot guess it.
- Yandex Cloud CDN documents static request headers (`--static-request-headers`, API `staticRequestHeaders`) with header-name characters limited to letters, digits, `-`, and `_`.

Not verified, and must not be presented as working:

- `vlessenc` on a mobile device. The encrypted profile was verified end to end from a desktop through a live edge, but it did not establish on the target phone while the plain-VLESS profile on the same path did. The cause was not isolated to the encryption itself: the two profiles also differed in the share-link shape, in the pinned edge, and in the inbound. Treat plain VLESS as the default and `vlessenc` as an unproven addition until a device test says otherwise. Happ and v2RayTun bundle Xray-core and may work; sing-box-based clients such as Hiddify may not implement VLESS Encryption at all.
- `keepalive_requests 100000` under a long production soak. The directive is valid and the local test passed; the value is a tuning choice, not a measured optimum.

- Yandex WebSocket: the edge did not pass an `Upgrade` in this study (0/20), and the provider enables the feature only through a support request. Not accepted as a working transport.
- VK: current VK Cloud CDN terms prohibit proxy/VPN use and traffic masking, so it fails the provider gate. Timeweb and Beget: provider notes only; this repository ships no tested preset, and this field study did not validate them.
- Any claim that a CDN hostname is on a carrier whitelist in general. One hostname passed in one region and one window. Re-test.

## Reference map

Read only what the task needs.

- [references/yandex-cdn.md](references/yandex-cdn.md) — Yandex Cloud CDN resource, methods, certificate, CNAME, static request headers, edge behavior, support path.
- [references/origin-setup.md](references/origin-setup.md) — hardened nginx port 80/443 blocks, secret gate, buffers, keepalive, Xray inbound with `vlessenc`, logging.
- [references/single-server.md](references/single-server.md) — default origin = exit mode, port ownership, one server or two, switching and rollback.
- [references/xhttp-transport.md](references/xhttp-transport.md) — XHTTP modes, header budget, `vlessenc` key placement, the full `extra` object, share-link fields, client/server matching.
- [references/diagnostics.md](references/diagnostics.md) — test ladder, vantage contamination, log correlation, interpretation rules.
- [references/providers.md](references/providers.md) — Yandex, Selectel, Gcore, VK, Timeweb, Beget comparison and provider gate.
- [references/operations.md](references/operations.md) — backups, secrets, rotation, monitoring, incident runbook, acceptance checklist.

## Safety and scope

Use this on infrastructure you own or are authorized to administer. Check the CDN provider's current terms before carrying VPN traffic; several providers explicitly prohibit it, and a resource can be blocked for it. Do not use this skill to attack, scan, or evade controls on systems you do not own. The scripts send only a handful of requests to the hosts you name.
