# Operations

A CDN-carried tunnel has more moving parts than a direct connection. Treat it like production: back up, validate, measure, and rotate.

## Backups

Before any change to nginx, Xray, DNS, or the CDN resource:

```bash
umask 077
backup_dir=/root/vpn-cdn-backups/$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 700 "$backup_dir"
sudo install -m 600 /usr/local/etc/xray/config.json "$backup_dir/xray-config.json"
sudo install -m 600 /etc/nginx/nginx.conf "$backup_dir/nginx.conf"
sudo install -m 600 /etc/nginx/sites-enabled/cdn-origin.conf "$backup_dir/cdn-origin.conf"
```

Rules:

- Back up the file before editing it, not after.
- Keep backups outside the repository and outside the web root.
- Do not print secret-bearing files while inspecting them.
- Keep the previous working client profile until the new one passes acceptance.
- Roll back by restoring the specific file, validating, reloading, and checking health. Do not delete backups to "clean up" during an incident.

## Secrets

What is sensitive:

- VLESS UUIDs and any REALITY private keys.
- The `X-CDN-Secret` value and the VLESS Encryption key pair. The secret gates the origin; the key decrypts the tunnel.
- Origin IPs and SSH credentials.
- CDN resource IDs, API tokens, and support ticket contents.
- QR codes and `.vless` files; they contain the UUID and, with `vlessenc`, the client key.

Handling:

- Generate UUIDs with `xray uuid` or a cryptographic random source.
- Pass the UUID to scripts through `--uuid-file` or the `VLESS_UUID` environment variable; avoid shell history.
- Write profiles with mode `0600` in a private directory such as `~/.config/vpn-cdn/`.
- Do not paste profiles into chat, tickets, issues, or commits. Redact the UUID when sharing a config.
- Rotate the UUID after any exposure. Update every device before retiring the old value.
- Keep a per-device profile so one device can be revoked without touching the others.

The repository's `.gitignore` excludes profile artifacts, but do not rely on it as a safety net. Check `git diff --cached` before every commit. `tests/secret-scan.sh` checks for private keys, tokens, and non-placeholder UUIDs; set `SECRET_SCAN_DENYLIST` to a file outside the repository (one extended regex per line) to catch your own hostnames, IPs, resource IDs, and ticket numbers.

## Rotating the secret gate

The CDN injects `X-CDN-Secret`; the origin accepts only a matching value. Rotate without a gap:

1. Put both the old and the new value in the nginx `map` regex, for example `"~(old|new)value"`.
2. Update the CDN resource with the new value: `yc cdn resource update <ID> --static-request-headers X-CDN-Secret=<new>`, sending the complete header set.
3. Verify through the CDN that `/_cdn_secret_probe` still returns `204`.
4. Remove the old value from the `map`, reload nginx, and verify again.
5. Update the runbook and store the new value outside the repository.

Skipping step 1 turns the rotation into an outage. The CDN may take time to roll the new header to every edge; step 3 is the gate for step 4.

## Rotating VLESS Encryption

1. Generate pairs with `xray vlessenc`. It prints two authentication options, X25519 and ML-KEM-768; the `decryption` and `encryption` lines inside one option form a pair. Use one option consistently on both sides.
2. Install the new `decryption` in the inbound, keeping the old inbound running until clients move. A new `decryption` string is not additive, so plan a window.
3. For a zero-gap rotation, keep a second inbound on a spare port with the new key, move clients, then remove the old inbound.
4. Update every profile with the new `encryption` key, validate with `xray run -test`, and restart the client.
5. Confirm an end-to-end session and watch for `tls`/handshake errors on clients that still hold the old key.

An old client key fails immediately after the server switches. There is no fallback inside `vlessenc`; the fallback is a separate `none` profile.

## Change procedure

1. Write the candidate to a temporary path with mode `600`.
2. Validate: `xray run -test -config <candidate>` and, for nginx, `nginx -t`.
3. Install atomically (`install -m 600`, then move) or with a backup.
4. Reload or restart only the affected service.
5. Verify health and the direct control.
6. Record what changed, when, and the measurement after the change.

Never combine a transport change and an infrastructure change in one step. If the result changes, you will not know which one caused it.

## Monitoring

Track these on the origin and the CDN:

| Signal | Why | Where |
|---|---|---|
| Requests to the tunnel path | Is the edge routing at all | nginx access log |
| `444` on the tunnel path | The edge stopped injecting the secret, or a scan hit the origin | nginx access log |
| `204` on `/_cdn_secret_probe` | The full CDN→origin chain and the secret still work | `origin-check.sh --cdn-secret` |
| 2xx / 4xx / 5xx ratio | Method, Host, or edge policy changes | nginx and CDN metrics |
| `$request_length` and `$bytes_sent` | Is data flowing in both directions | nginx log format |
| Sessions with `101` and zero bytes | Upgrade succeeded, data path failed | nginx log |
| Reconnect rate per device | Client is fighting a stall | Client logs and origin log |
| Client handshake errors after a key change | Wrong or stale `vlessenc` key | Client log and Xray log |
| Per-edge success rate | Which edges are healthy | `cdn-probe.py matrix` and `edge-test.sh` |
| Certificate expiry | TLS failure after a silent expiry | `cdn-probe.py probe` |
| DNS answer set | Edge rotation and dead edges | `cdn-probe.py dns` |

A useful daily check:

```bash
python3 scripts/cdn-probe.py dns --host cdn.example.com
python3 scripts/cdn-probe.py matrix --host cdn.example.com --path /api-stream --attempts 3
bash scripts/origin-check.sh --host cdn.example.com --scheme https --port 443 \
  --cdn-secret "$CDN_SECRET"
```

A weekly session check on the pinned edge and the domain fallback catches gradual fleet drift.

## Edge rotation

- Keep a pinned profile per healthy edge and one domain profile.
- Re-rank after a success-rate drop, a provider change, or a certificate change.
- Replace a pinned profile only after the new edge passes five session attempts and a device check.
- Expect IP churn. A profile that worked for weeks can fail overnight because the edge was reassigned.
- Do not pin more than a couple of edges in one profile; a multi-address profile reintroduces the dead-edge problem.

## Incident runbook

### Total outage

1. Direct control through the same client and UUID. If it fails, fix the origin/cascade.
2. If direct works, test the domain profile and the pinned profile separately.
3. Check DNS, certificate expiry, and resource status.
4. Check the origin access log for the test window.
5. If short probes fail through every edge, the resource or DNS is broken; escalate.
6. If short probes pass and sessions fail, switch to a different pinned edge or provider.

### Partial stalls

1. Measure the success rate with a 20-attempt series.
2. Run `matrix` to find failing edges.
3. Pin a passing edge.
4. If every edge stalls on sessions, the provider does not carry the flow; do not spend time on client tuning.

### Instant 403 or 405

1. Read the response headers and the CDN reason code, if any.
2. Confirm the method list still includes GET. An API PATCH may have replaced it.
3. Confirm Host and SNI match the resource.
4. Compare an identical manual request through the same edge.
5. If manual requests pass and tunnel requests fail, the edge is filtering the pattern; escalate or change provider.

### Instant 444 or empty replies

`444` is the origin gate closing the connection, not the CDN. Empty replies through the CDN often mean the same thing after the edge hides the status.

1. Check the secret on the origin and in the CDN resource; a partial `yc cdn resource update` can drop `staticRequestHeaders`.
2. Request `/_cdn_secret_probe` through the CDN with `origin-check.sh --cdn-secret`. A `204` means the chain and secret are intact; no response, or a non-`204`, means the edge is not injecting the header.
3. Confirm the probe and tunnel paths are covered by the `map` and that no server-level `return 444` was added. Health and ACME must stay reachable.
4. Reproduce directly at the origin with the correct header. Direct-with-header should be `204`; direct-without-header must be `000` or `444`.
5. If a CDN header change is recent, wait for edge rollout, then re-run the probe before touching nginx.

### Clients fail after enabling vlessenc

1. Confirm the client profile carries the client key from the same `xray vlessenc` run as the inbound `decryption`.
2. Validate the inbound: its clients must not have an `encryption` field.
3. Check the client core version; Xray-core older than 25.9.5 may ignore the field and hang.
4. If the client is sing-box based and does not support it, use the `none`/`none` fallback profile rather than downgrading the server.

### TLS errors

1. Check the certificate SAN and expiry through the edge and directly at the origin.
2. Check that the origin `server_name` covers the hostname the CDN sends.
3. If the edge serves a different certificate, reattach or reissue it in the panel.
4. Do not disable verification as a permanent fix.

### Reconnect storm

1. Reduce client concurrency and retry aggressiveness.
2. Fix the underlying stall; retries make edge anti-abuse more likely.
3. Check whether the client is rotating through dead edges.

## Acceptance checklist

A deployment is accepted only when all of these are true:

- [ ] Direct origin/cascade control passes 10/10.
- [ ] Direct origin without `X-CDN-Secret` returns `000` or `444`; with the header, the probe returns `204`.
- [ ] `/_cdn_secret_probe` through the CDN returns `204`.
- [ ] If `vlessenc` is enabled, the inbound has only `decryption`, the client only `encryption`, and a loopback or direct session passes.
- [ ] At least one edge passes 5/5 XHTTP sessions.
- [ ] The pinned profile passes 20/20 connect/health attempts from the target network.
- [ ] The target app sends and receives data through the tunnel.
- [ ] The target app version accepts the profile; if `vlessenc` is used, confirm the client core supports it.
- [ ] The test ran on the target SIM, with Wi-Fi off.
- [ ] If the claim is whitelist mode, the test ran during an active restriction window.
- [ ] Failures are correlated with origin logs; no failure is left as an unexplained timeout.
- [ ] The fallback profile still works.
- [ ] The profile is stored mode `0600` outside the repository.
- [ ] No UUID, key, origin IP, resource ID, or `X-CDN-Secret` value appears in notes, tickets, or commits.

If restrictions were not active, label the result "ordinary mobile connectivity", not "whitelist success".

## Capacity notes

- Header placement adds overhead per packet. A 3–4 KiB chunk with base64 and padding carries less payload than the raw number suggests.
- Upload throughput is limited by request rate and edge latency. Many small requests over a high-latency edge are slower than a direct connection; this transport is for reachability, not speed.
- `xmux` connection reuse reduces handshake cost. Keep the pool small; a large pool looks like a connection flood to an edge.
- Long-lived sessions consume edge resources. Some providers rate-limit or block them; watch for a sudden change in session lifetime.
