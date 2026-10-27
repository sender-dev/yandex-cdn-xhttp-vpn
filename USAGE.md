# Usage guide

This guide describes the setup that was actually deployed and accepted: a VLESS/XHTTP tunnel through Yandex Cloud CDN, with the uplink carried in GET headers, accepted from an MTS SIM during an active restriction window in October 2026. It covers both ways of working: handing the task to an agent that has this skill, and doing the deployment by hand.

Version 2 adds the hardened origin profile. The base GET/header deployment is field-verified. The secret gate, keepalive tuning, and `vlessenc` were validated locally against Xray 26.3.27 and nginx 1.26.3, then in production on 2026-10-27 against Xray 26.10.21 and a live Yandex edge: the edge injected `X-CDN-Secret`, the gate closed direct scans, and `vlessenc` carried 10/10 sessions. The phone acceptance on the target SIM is still the step that proves the mobile path; [README.md](README.md) keeps the exact split.

The mobile path has its own contract. A share link is not the same artifact as the Xray client JSON, and the mobile importer is stricter: it models five XHTTP keys, keeps `mode` at the top level of the link, reads `uplinkChunkSize` as a string, and expects the `extra` blob to be percent-encoded twice. A link that breaks any of those rules imports and then never connects. The profile that was verified on a phone is plain VLESS with the five-key blob; `vlessenc` and the extra knobs are optional.

Everything below uses placeholders. Replace them with your own values and keep the real ones out of commits, tickets, and chat:

```bash
CDN_HOST=cdn.example.com        # public hostname, CNAME to the CDN target
ORIGIN_HOST=origin.example.com  # origin address or name used by the CDN
ORIGIN_IP=203.0.113.10          # origin server address
EDGE_IP=198.51.100.20           # a verified CDN edge address
TUNNEL_PATH=/api-stream         # XHTTP path, identical on both ends
```

The addresses above come from documentation ranges. Do not copy them literally.

## Contents

- [Part I: working with the skill](#part-i-working-with-the-skill)
- [Part II: deployment, step by step](#part-ii-deployment-step-by-step)
- [Part III: command reference](#part-iii-command-reference)
- [Troubleshooting](#troubleshooting)
- [Limits, safety, and terms](#limits-safety-and-terms)

## Part I: working with the skill

### Install

```bash
# All agents, global
npx skills add sender-dev/yandex-cdn-xhttp-vpn -g

# Qoder only
npx skills add sender-dev/yandex-cdn-xhttp-vpn -a qoder -g
```

From a clone, `./install.sh` does the same without the CLI. See the README for details.

### What the agent needs from you

The skill cannot read your provider console or your phone. Give it the values and access it cannot derive:

| Input | Why | How to share safely |
|---|---|---|
| Public CDN hostname | SNI, Host, certificate, DNS checks | It is public; fine to name in chat |
| Origin hostname and IP | nginx checks and log correlation | IP is sensitive; keep it out of public issues |
| CDN resource settings | Methods, cache, origin protocol, certificate | Name the values; do not paste API tokens |
| DNS control | CNAME and, for DNS-01, TXT records | You perform the change; the agent prepares it |
| UUID | Client and inbound must match | Generate once into `~/.config/vpn-cdn/uuid`, mode `0600`; never paste it |
| Target carrier and SIM | A datacenter test does not prove the mobile path | Name the carrier only |
| Existing working profile | It is the fallback and the control | Keep it; do not replace it before acceptance |

### How to phrase a task

The skill triggers on the project name and on the transport and symptom vocabulary. Useful prompts:

- "Use the yandex-cdn-xhttp-vpn skill. My CDN hostname is `cdn.example.com`, origin is `203.0.113.10`. Walk me through the Yandex console settings and tell me what to click."
- "Short probes pass but about 30% of sessions hang. Rank the edges and tell me which one to pin."
- "Generate a pinned profile for `198.51.100.20` and a domain fallback, then validate both configs."
- "Review my nginx and Xray configs before I reload them. Do not restart anything."
- "The tunnel fails on the phone but works from the server. Build the test ladder and tell me which layer is broken."

Russian phrasing works too: `VPN через CDN`, `XHTTP packet-up`, `белые списки МТС`, `обход блокировок через CDN`, `края CDN`, `20–35% таймаутов`.

### What the agent does on its own, and what it must not

The agent may run read-only checks and generate files:

- resolve DNS through public and authoritative resolvers;
- send a handful of requests to the edges you name;
- start a temporary Xray client and test a session;
- write profiles under `~/.config/vpn-cdn/` with mode `0600`;
- validate configs with `xray run -test` and `nginx -t`;
- read origin logs when you provide access.

It must stop and ask before:

- editing nginx, Xray, DNS, or the CDN resource;
- reloading or restarting services;
- deleting or replacing a working profile;
- publishing anything, opening tickets, or sending data to a provider;
- treating a datacenter result as proof of the mobile path.

### Evidence rules

These rules come from the field deployment and apply to the agent as much as to you:

1. Run a direct control first. If the origin path fails, the CDN is not the problem.
2. Use a clean vantage. Fake-IP DNS, a local proxy, or a VPN on the workstation invalidates results.
3. Use unique paths or nonces for short probes so a cache cannot answer for you.
4. Repeat. Five attempts per edge, twenty for acceptance.
5. Record UTC timestamps and correlate with origin logs.
6. State the limitation. A datacenter success is not a whitelist success.

## Part II: deployment, step by step

### 0. Topology and order

```text
Phone (Happ / v2RayTun / Hiddify)
  -> TLS 443, address = pinned edge IP or CDN hostname, SNI = CDN hostname
  -> Yandex CDN edge, injects X-CDN-Secret
  -> origin :80 nginx (plain HTTP in the tested resource)
       gate: no X-CDN-Secret -> 444
  -> Xray VLESS/XHTTP inbound on 127.0.0.1:9081
       vlessenc hides the payload on the CDN->origin leg
  -> outbound freedom (origin = exit)
  -> Internet
```

Provision in this order: origin, DNS, CDN resource, propagation, edge discovery, session test, profile, device acceptance. Building the client profile earlier gives you something to debug with no ground truth.

**Two-server cascade alternative.** This guide describes the single-server default: the origin is also the exit and Xray routes the tunnel to `freedom`. Keep the two-server topology when CDN edges cannot reach the exit, when the plain-HTTP border hop is unacceptable, or when the exit IP must stay hidden. It installs [assets/xray-inbound-cascade.example.json](assets/xray-inbound-cascade.example.json) instead of the primary asset and adds a REALITY hop; the CDN-facing nginx block and the client preset do not change. See [references/single-server.md](references/single-server.md#one-server-or-two).

### 1. Back up and inventory

Before changing nginx, Xray, DNS, or the CDN resource:

```bash
umask 077
backup_dir=/root/vpn-cdn-backups/$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 700 "$backup_dir"
sudo install -m 600 /usr/local/etc/xray/config.json "$backup_dir/xray-config.json"
sudo install -m 600 /etc/nginx/nginx.conf "$backup_dir/nginx.conf"
sudo install -m 600 /etc/nginx/sites-enabled/cdn-origin.conf "$backup_dir/cdn-origin.conf"
```

Keep the direct REALITY profile and inbound working. The direct path is your control and your fallback until the CDN path passes acceptance.

### 2. Prepare the origin

The origin needs a reachable address, an nginx entry point on port 80, and a loopback-only Xray inbound. A certificate is optional for the tested path: the edge fetched origin over plain HTTP/80 even when the panel reported HTTPS.

Generate the secrets outside the repository, with mode `0600`:

```bash
install -d -m 700 ~/.config/vpn-cdn
(umask 077; openssl rand -hex 32 > ~/.config/vpn-cdn/cdn-secret)
```

The VLESS Encryption pair is optional:

```bash
(umask 077; xray vlessenc > ~/.config/vpn-cdn/vlessenc.txt)
```

`vlessenc.txt` contains two authentication options; each option's `decryption` and `encryption` lines are a pair. Keep the pair out of git, chat, and tickets.

Copy [assets/nginx-origin.conf](assets/nginx-origin.conf) and [assets/xray-inbound.example.json](assets/xray-inbound.example.json) (the single-server default), then substitute the secret, names, paths, and the UUID. The asset ships with `"decryption": "none"`, which matches a plain-VLESS client and is the configuration that was verified on a phone; only substitute a `vlessenc` key when the client is known to carry it.

The non-negotiable nginx pieces:

```nginx
map $http_x_cdn_secret $cdn_secret_ok {
    default 0;
    "~<CDN_SECRET>" 1;
}
map $request_method $xhttp_uplink_method {
    OPTIONS POST;
    default $request_method;
}

upstream xhttp_backend {
    server 127.0.0.1:9081;
    keepalive 32;
    keepalive_requests 100000;
    keepalive_timeout 600s;
}

server {
    listen 80 default_server;
    server_name _;

    client_header_buffer_size 16k;
    large_client_header_buffers 8 64k;
    keepalive_requests 100000;
    keepalive_timeout 600s;

    location = /health {
        access_log off;
        add_header Cache-Control "no-store" always;
        return 200 "ok\n";
    }

    location = /_cdn_secret_probe {
        access_log off;
        if ($cdn_secret_ok = 0) { return 444; }
        return 204;
    }

    location /api-stream {
        if ($cdn_secret_ok = 0) { return 444; }
        proxy_method $xhttp_uplink_method;
        proxy_pass http://xhttp_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_set_header X-CDN-Secret "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_socket_keepalive on;
        client_max_body_size 0;
        add_header Cache-Control "no-store, no-transform" always;
    }
}
```

Why each line matters is in [references/origin-setup.md](references/origin-setup.md). In short: the buffers absorb header-placement packets; keepalive avoids a new upstream handshake per request; no-cache and no-buffer keep long sessions alive; the `map` gate rejects direct scans with `444`; and `default_server` catches the Host the edge sends. Keep the catch-all limited to the tunnel path, health, and ACME endpoints.

Do not put `return 444` at server level. It would break health checks and certificate renewal. The gate lives on the tunnel path and on `/_cdn_secret_probe`.

The inbound must match the client on every framing key, and the two halves of `vlessenc` must be on the correct side:

```json
"network": "xhttp",
"xhttpSettings": {
  "path": "/api-stream",
  "mode": "packet-up",
  "uplinkHTTPMethod": "GET",
  "uplinkDataPlacement": "header",
  "uplinkChunkSize": 16384,
  "scMaxEachPostBytes": 32768,
  "scMinPostsIntervalMs": 10,
  "serverMaxHeaderBytes": 65536
}
```

These are the framing keys that a mobile share link models. If the client preset carries `uplinkDataKey`, session placement, sequence placement, or padding keys, the inbound must carry the same ones; a key that is present on one side and absent on the other is a different framing, not a default.

The inbound's `decryption` carries the server key, or the literal `none` for plain VLESS. Its `clients` array contains only `id` and `flow`; an `encryption` field there is rejected at startup. The client profile carries `users[].encryption`, and it must always be present: a modern core refuses to start without it and prints `VLESS users: please add/set "encryption":"none" for every user`.

`mode: "auto"` with header placement is rejected at startup. `serverMaxHeaderBytes` belongs in the inbound JSON, not in a share link. Xray silently ignores unknown keys, so validate and dump:

```bash
xray run -test -config /usr/local/etc/xray/config.json
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl restart xray
```

Check the origin directly, before any CDN involvement:

```bash
CDN_SECRET=$(cat ~/.config/vpn-cdn/cdn-secret)
bash scripts/origin-check.sh \
  --host "$CDN_HOST" --ip "$ORIGIN_IP" --scheme http --port 80 \
  --cdn-secret "$CDN_SECRET"
```

This checks the health path, a 12 KiB request header (the nginx buffer budget), `Cache-Control`, method behavior, and the gate: no header must give `000` or `444`, the correct header must give `204`. If it fails, stop; the CDN cannot fix an origin problem.

### 3. DNS and certificate

Point the public hostname at the target the CDN console shows. In the tested resource the target was a `*.topology.gslb.yccdn.ru` name. That suffix means Yandex; confirm it in the console rather than guessing from DNS, and do not attribute edge behavior to another provider based on a hostname.

For a certificate, DNS-01 is the reliable path for a hostname behind a CDN. Create the `_acme-challenge` TXT record and confirm it on the authoritative nameservers, not only on a public resolver:

```bash
python3 scripts/cdn-probe.py dns --host "$CDN_HOST" --authoritative
```

An empty authoritative answer means the CNAME or TXT record is not published yet.

### 4. Create the CDN resource

In the Yandex Cloud console, in this order:

1. Create an origin group with one origin: the origin hostname or IP.
2. Set the origin protocol to HTTP and the port to 80. The tested resource fetched origin over HTTP even when the panel showed HTTPS.
3. Set the origin Host header to the public CDN hostname.
4. Create the CDN resource with the public hostname and attach the origin group.
5. Allow `GET`, `HEAD`, and `OPTIONS`. Send the complete list on every update: an API PATCH replaces the list rather than merging, so patching only the new method can remove `GET` and break the tunnel.
6. Disable caching, large-file segmentation, and compression or transformation.
7. Attach the certificate for the public hostname.
8. Add the static request header the origin expects: `X-CDN-Secret=<value from ~/.config/vpn-cdn/cdn-secret>`.
9. Create the CNAME at the DNS host.

The header can be set in the console or with the CLI. The CLI replaces the whole header set, so pass every header you need:

```bash
CDN_SECRET=$(cat ~/.config/vpn-cdn/cdn-secret)
yc cdn resource update "$CDN_RESOURCE_ID" \
  --static-request-headers "X-CDN-Secret=$CDN_SECRET"
```

This header gates the origin; it does not authenticate the user. Anyone who learns the CDN hostname still reaches the edge, and the secret only stops scans that bypass the CDN. Keep the VLESS UUID and `vlessenc` key as the real access control.

"Active" is a control-plane state. Edge fleets roll out separately, so wait, then measure. Do not build the client profile from the console state.

### 5. Discover and rank the edges

```bash
python3 scripts/cdn-probe.py dns --host "$CDN_HOST"
python3 scripts/cdn-probe.py dns --host "$CDN_HOST" --authoritative
python3 scripts/cdn-probe.py matrix --host "$CDN_HOST" --path "$TUNNEL_PATH" --attempts 3
```

Read DNS first:

- Same set from several resolvers: normal rotation.
- Different sets: a resolver or propagation issue.
- Answers in `198.18.0.0/15` or `fc00::/7`: fake-IP from a local proxy; the result is unusable.
- Empty from the authoritative servers: the CNAME is missing or the zone is not published.

Then read the matrix verdict per edge:

| Verdict | Meaning |
|---|---|
| `OK` | The edge routed the short request |
| `BLOCKED` | `403`/`405`/`413`/`501` from the edge or its method policy |
| `ERROR` | The edge answered 5xx |
| `PARTIAL` | Inconsistent results across attempts |
| `FAIL` | No HTTP answer |

A short request that passes proves routing only. It does not prove the edge carries a long-lived session.

Check TLS, methods, and cache:

```bash
python3 scripts/cdn-probe.py probe --host "$CDN_HOST" --ip "$EDGE_IP" --path /health
python3 scripts/cdn-probe.py methods --host "$CDN_HOST" --path "$TUNNEL_PATH"
python3 scripts/cdn-probe.py cache --host "$CDN_HOST" --path "$TUNNEL_PATH"
```

Expected: a valid certificate for the hostname, `GET` allowed, no `HIT`, and no growing `Age`. In the tested Yandex resource a POST body returned `413`; some deployments report `405` on POST. That is why the uplink uses GET with header placement.

Then confirm the edge injects the secret and reaches the origin:

```bash
CDN_SECRET=$(cat ~/.config/vpn-cdn/cdn-secret)
bash scripts/origin-check.sh --host "$CDN_HOST" --scheme https --port 443 \
  --cdn-secret "$CDN_SECRET"
```

In this mode the script sends no origin IP and expects the probe `/_cdn_secret_probe` to return `204` through the edge. If it fails while the direct origin check passed, the CDN resource is not sending `X-CDN-Secret` or has not rolled out yet.

### 6. Test an XHTTP session per edge

Create the UUID once, outside the repository:

```bash
install -d -m 700 ~/.config/vpn-cdn
(umask 077; xray uuid > ~/.config/vpn-cdn/uuid)
```

Then test each candidate edge:

```bash
bash scripts/edge-test.sh \
  --edge "$EDGE_IP" --sni "$CDN_HOST" --path "$TUNNEL_PATH" \
  --uuid-file ~/.config/vpn-cdn/uuid --attempts 5
```

Add `--vless-encryption "$CLIENT_KEY"` when the inbound uses a `vlessenc` key. `xray vlessenc` prints two authentication options; this takes the client key from the first. If you use the second option on the server, take its `encryption` line instead. Omit the flag when the inbound has no `decryption`. The script starts a temporary Xray client, sends a series through the edge, and reports `LIVE`, `PARTIAL`, or `DEAD`. Exit codes: `0` LIVE, `1` PARTIAL, `2` setup error, `3` DEAD. Use `--dry-run` to validate the generated config without sending traffic and `--json` for machine-readable output.

This is the discriminating test. In the tested deployment, short probes passed on every edge, but only two edges carried sessions consistently, one was partial, and one was unusable. The domain profile inherited a 20–35% stall rate. Pinning a verified edge removed it.

### 7. Build the profiles

Pinned profile:

```bash
python3 scripts/make-profile.py \
  --address "$EDGE_IP" --sni "$CDN_HOST" --path "$TUNNEL_PATH" \
  --uuid-file ~/.config/vpn-cdn/uuid \
  --preset happ-parity --name 'CDN pinned' \
  --out ~/.config/vpn-cdn/pinned.vless \
  --client-config ~/.config/vpn-cdn/pinned.json
```

Domain fallback, for when the pinned edge is reassigned:

```bash
python3 scripts/make-profile.py \
  --address "$CDN_HOST" --sni "$CDN_HOST" --path "$TUNNEL_PATH" \
  --uuid-file ~/.config/vpn-cdn/uuid \
  --preset happ-parity --name 'CDN fallback' \
  --out ~/.config/vpn-cdn/fallback.vless \
  --client-config ~/.config/vpn-cdn/fallback.json
```

`--out` writes the share URI; `--client-config` writes a matching Xray config. Both are created with mode `0600`. If the inbound uses `vlessenc`, add the client key to both commands:

```bash
  --vless-encryption "$(sed -n 's/^"encryption": "\(.*\)"$/\1/p' ~/.config/vpn-cdn/vlessenc.txt | head -1)"
```

The flag writes the key into the URI's `encryption` field and into `users[].encryption` in the client config. Validate before importing:

```bash
xray run -test -config ~/.config/vpn-cdn/pinned.json
xray run -test -dump -config ~/.config/vpn-cdn/pinned.json | jq '.outbounds[0].streamSettings.xhttpSettings'
```

If a key you wrote is missing from the dump, the installed Xray does not implement it. The effective names are `sessionPlacement` and `sessionKey`; `xmux` is enabled by the object's presence, not by a flag. The dump command uses `jq` for readability; any JSON filter works.

Import the URI into Happ, v2RayTun, or Hiddify. Keep the old profile installed. On iOS, a QR code is convenient but contains the UUID; do not share it.

A share link is not the same artifact as the client JSON, and the mobile importer is stricter than the desktop client. The `extra` blob of a mobile link carries exactly the keys the app models:

```json
{
  "scMaxEachPostBytes" : 32768,
  "scMinPostsIntervalMs" : 10,
  "uplinkChunkSize" : "16384",
  "uplinkDataPlacement" : "header",
  "uplinkHTTPMethod" : "GET"
}
```

`mode` stays at the top level of the link, `uplinkChunkSize` is a string, the blob is percent-encoded twice, and plain VLESS carries no `encryption` parameter. `make-profile.py --preset happ-parity` writes exactly this and the test suite compares the result byte for byte against a link that was proven on a phone.

When a link imports but the tunnel never starts, diff the `extra` blob of the failing link against the blob of a link that works before touching the server. The key set, the value types, the JSON whitespace, and the number of encoding layers are all part of the contract, and none of them are visible in the client's error message.

### 8. Accept on the target device

Acceptance is a ladder. Each rung must pass before the next one means anything:

1. Direct origin control (and the cascade control if that topology is deployed): 10/10.
2. Origin gate: no secret gives `000`/`444`, the correct secret gives `204`; the probe through the CDN gives `204`.
3. Short probes through several edges: routed.
4. XHTTP session on at least one edge: 5/5, with the matching `vlessenc` key if enabled.
5. Pinned profile from the target network: 20/20 connect/health attempts.
6. The actual app sends and receives data.
7. The test runs on the target SIM with Wi-Fi off.
8. If the claim is whitelist mode, the test runs during an active restriction window.

Record each attempt with a UTC timestamp. Correlate failures with the origin log:

```bash
sudo tail -f /var/log/nginx/cdn-access.log
```

What the log tells you:

- No line for the attempt: the edge never routed it.
- `$request_length` above zero: the client packet reached the origin.
- `$bytes_sent` above zero: the origin answered.
- `$request_time` near the timeout: the upstream or cascade stalled.
- Many reconnects in minutes: the client is fighting a stall; retries make edge anti-abuse more likely.

If restrictions were not active, report "ordinary mobile connectivity", not "whitelist success".

### 9. Operate

Daily:

```bash
python3 scripts/cdn-probe.py dns --host "$CDN_HOST"
python3 scripts/cdn-probe.py matrix --host "$CDN_HOST" --path "$TUNNEL_PATH" --attempts 3
```

Weekly, or after any change: run five session attempts on the pinned edge and on the domain fallback. A drop in the per-edge success rate means it is time to re-rank.

Rules that keep the deployment healthy:

- Keep one pinned profile per healthy edge, plus one domain profile. Do not pin several edges in one profile; that reintroduces the dead-edge problem.
- Expect IP churn. An edge that worked for weeks can be reassigned overnight.
- Replace a pinned profile only after the new edge passes five sessions and a device check.
- Rotate the UUID after any exposure, and update every device.
- Rotate the CDN secret with an overlap (old and new values in the `map`, update the resource, then remove the old value), and rotate `vlessenc` by moving clients to a second inbound or accepting a short window.
- Watch certificate expiry, 4xx spikes, `444` spikes on the tunnel path, and sessions with `101` but zero application bytes.
- Back up before editing, validate before installing, reload only the affected service, and verify health after.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| 20–35% of requests hang, short probes pass | Dead or misconfigured edges in rotation | Pin a verified edge; keep the domain as fallback |
| Instant `403` at the edge | Filtering, Host mismatch, method policy, inactive resource | Compare an identical manual request; check methods, Host, resource status |
| `405` on the uplink | Client sends POST or OPTIONS where GET is required | Set `uplinkHTTPMethod=GET` |
| `413` on the uplink | Request body sent to an edge that forbids it | Use header placement; remove the body |
| `431` or `400` on large requests | Header chunk or padding too large | Lower `uplinkChunkSize`, raise nginx buffers |
| Share link imports in the app but the tunnel never starts | The `extra` blob does not match what the mobile importer models | Diff it byte for byte against a known-good link; regenerate with `--preset happ-parity` |
| Client core refuses to start with `VLESS users: please add/set "encryption":"none"` | The profile has no explicit `encryption` on the VLESS user | Emit `"encryption": "none"` on every user |
| Origin closes immediately, `444` or an empty reply | Secret gate rejected the request | Check the CDN static request header and the nginx `map` value |
| Direct origin works, probe through CDN fails | Edge is not injecting `X-CDN-Secret` | Verify `staticRequestHeaders` on the resource; wait for edge rollout |
| Session establishes then dies after enabling `vlessenc` | Client and server keys are from different pairs, or the client core is too old | Regenerate and install both halves; use the `none` fallback if the client cannot support it |
| TLS reset right after the handshake | Consumer-source blackholing at the edge | Test from another source; provider support; consider another provider |
| `malformed HTTP/1.x` with HTTP/2 bytes | Client forced HTTP/1.1 while ALPN negotiated h2 | Do not force HTTP/1.1 |
| Origin log shows the request, client gets nothing | Return path broken | Correlate bytes; check the edge and upstream |
| Origin log shows nothing | Edge never routed the request | Check origin group, Host, port, certificate, resource status |
| Domain profile fails, pinned works | DNS returns edges that do not serve the resource | Pin; report bad edges to the provider |
| Direct profile works, CDN profile fails | SNI, Host, path, or client/server XHTTP mismatch | Validate both configs and dump the parsed settings |

Stop and change the provider when all of these are true: the direct control passes, short probes pass through multiple edges, sessions stall or return `403` across edges and sources, and support cannot explain the behavior. That is a provider limitation, not a tuning problem.

## Part III: command reference

```bash
# Secrets (mode 0600, outside the repository)
openssl rand -hex 32 > ~/.config/vpn-cdn/cdn-secret
xray vlessenc > ~/.config/vpn-cdn/vlessenc.txt
yc cdn resource update "$CDN_RESOURCE_ID" --static-request-headers "X-CDN-Secret=$CDN_SECRET"

# Origin control
bash scripts/origin-check.sh \
  --host "$CDN_HOST" --ip "$ORIGIN_IP" --scheme http --port 80 \
  --cdn-secret "$CDN_SECRET"

# Secret gate through the CDN (no --ip)
bash scripts/origin-check.sh --host "$CDN_HOST" --scheme https --port 443 \
  --cdn-secret "$CDN_SECRET"

# DNS
python3 scripts/cdn-probe.py dns --host "$CDN_HOST"
python3 scripts/cdn-probe.py dns --host "$CDN_HOST" --authoritative

# Per-edge TLS and short request
python3 scripts/cdn-probe.py probe --host "$CDN_HOST" --ip "$EDGE_IP" --path /health
python3 scripts/cdn-probe.py matrix --host "$CDN_HOST" --path "$TUNNEL_PATH" --attempts 3

# Edge policy
python3 scripts/cdn-probe.py methods --host "$CDN_HOST" --path "$TUNNEL_PATH"
python3 scripts/cdn-probe.py cache --host "$CDN_HOST" --path "$TUNNEL_PATH"

# Session
bash scripts/edge-test.sh --edge "$EDGE_IP" --sni "$CDN_HOST" \
  --path "$TUNNEL_PATH" --uuid-file ~/.config/vpn-cdn/uuid --attempts 5

# Profiles (add --vless-encryption "$VLESS_ENCRYPTION" when the inbound uses it)
python3 scripts/make-profile.py --address "$EDGE_IP" --sni "$CDN_HOST" \
  --path "$TUNNEL_PATH" --uuid-file ~/.config/vpn-cdn/uuid \
  --preset happ-parity --name 'CDN pinned' \
  --out ~/.config/vpn-cdn/pinned.vless \
  --client-config ~/.config/vpn-cdn/pinned.json

# Validation
xray run -test -config ~/.config/vpn-cdn/pinned.json
xray run -test -dump -config ~/.config/vpn-cdn/pinned.json | jq '.outbounds[0].streamSettings.xhttpSettings'
```

Where to read more:

- [SKILL.md](SKILL.md) — rules, provider gate, full workflow, symptom table.
- [references/yandex-cdn.md](references/yandex-cdn.md) — resource shape, console checks, support evidence.
- [references/origin-setup.md](references/origin-setup.md) — nginx 80/443, buffers, logging, certificate notes.
- [references/xhttp-transport.md](references/xhttp-transport.md) — modes, header budget, share-link fields.
- [references/diagnostics.md](references/diagnostics.md) — test ladder, vantage contamination, log correlation.
- [references/operations.md](references/operations.md) — backups, secrets, rotation, incident runbook, acceptance checklist.
- [references/providers.md](references/providers.md) — provider comparison and gate.

## Limits, safety, and terms

- This setup is for reachability, not speed. Header placement adds overhead; many small requests over a high-latency edge are slower than a direct connection.
- A CDN reduces the distributed-IP problem; it is not a universal whitelist bypass. One hostname passed in one region during one window. Re-test.
- Consumer and datacenter sources can be treated differently by the same edge. A server-side test does not prove the phone path.
- Long-lived sessions consume edge resources. Some providers rate-limit them or prohibit VPN traffic in their terms. Yandex was field-verified; several other providers explicitly disallow this workload or failed the test.
- Use this only on infrastructure you own or are authorized to administer. Do not use it to attack, scan, or evade controls on systems you do not own.
- `X-CDN-Secret` stops scans that reach the origin directly. It is not user authentication and does not help if someone uses the public CDN hostname; the UUID remains the access control.
- `vlessenc` needs Xray-core 25.9.5 or newer and is not proven on a mobile device. Happ and v2RayTun bundle Xray-core and may support it; sing-box-based clients such as Hiddify may not. Plain VLESS is the default; keep a `none`/`none` fallback profile while testing.
- Keep UUIDs, keys, origin addresses, resource IDs, and ticket contents out of the repository, chat, and public issues.

The deployment this guide describes is accepted only when the checklist in [references/operations.md](references/operations.md#acceptance-checklist) passes in full.
