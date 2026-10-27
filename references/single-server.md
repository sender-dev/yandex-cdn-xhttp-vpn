# Single-server mode (origin = exit)

The default deployment in [USAGE.md](../USAGE.md) runs origin and exit on one server: the CDN fetches the origin directly, and Xray routes the tunnel to `freedom`. A two-server variant keeps a separate exit behind a REALITY cascade for networks where the CDN cannot reach the exit server.

Use this mode only after measuring that Yandex CDN edges can reach the origin reliably. A working two-server deployment is not evidence for that: the failing leg in a two-server setup is exactly the one single-server mode removes.

## Topology

```text
Phone (Happ / v2RayTun / Hiddify)
  -> TLS 443, address = pinned edge IP or CDN hostname, SNI = CDN hostname
  -> Yandex CDN edge
  -> origin :80 nginx (plain HTTP origin pull, X-CDN-Secret required)
  -> Xray VLESS/XHTTP inbound 127.0.0.1:9081 (tag: cdn-xhttp)
  -> routing -> outbound "direct" (freedom)
  -> Internet

Direct profile, same server:
  Phone -> TLS/REALITY :443 (tag: direct-reality) -> outbound "direct"
```

The configuration is [assets/xray-inbound.example.json](../assets/xray-inbound.example.json). Both inbounds route to `freedom`, because this server is at once the origin and the exit, and destinations in loopback, RFC1918, link-local, CGNAT, benchmark, and IPv6 ULA ranges are blocked so the tunnel cannot reach the server's LAN or metadata endpoints. The list uses explicit CIDRs rather than `geoip:private`, so Xray does not need `geoip.dat` next to the binary to load the config.

## One server or two

Single server is the default. The two-server cascade is the alternative: it costs a second host and earns its place only when the CDN cannot reach the exit.

| | Single server (origin = exit) | Two servers (cascade) |
|---|---|---|
| Hops | CDN to origin to internet | CDN to origin, REALITY to exit, internet |
| Cost and maintenance | One VPS | Two VPS |
| CDN-to-origin leg | Crosses the border if the server is abroad, over plain HTTP/80 | Stays on the domestic origin; the border hop is REALITY |
| Origin IP in the Yandex console | The exit IP | A domestic IP; the exit stays hidden |
| Failure mode it survives | CDN edges can reach the origin, which is also the exit | CDN edges cannot reach the origin, or the origin subnet is filtered |
| Changing the exit | Rebuild or migrate the only server | Swap the cascade outbound |

Choose single-server when the CDN reaches the VPS on port 80 without `502`/`504` and without DPI interference, and when exposing that IP as the origin is acceptable. Keep the cascade when the CDN cannot reach the exit server, when the plain-HTTP border hop is unacceptable, or when the exit IP must stay hidden.

Single-server does not remove the border hop. It removes the domestic hop and makes the remaining one plain HTTP unless the origin serves TLS. `vlessenc` on the CDN inbound makes the payload ciphertext even though the request itself is plain HTTP; it is optional, and the profile that was verified on a phone used plain VLESS with `encryption` absent from the share link.

## Port layout

This is the one place where single-server mode changes the origin files:

| Port | Owner | Purpose |
|---|---|---|
| `80` | nginx | CDN origin pull, secret gate, health, ACME |
| `443` | Xray `direct-reality` | Direct REALITY profile and control |
| `9081` | Xray `cdn-xhttp` on loopback | XHTTP backend behind nginx |

`assets/nginx-origin.conf` contains an optional `server { listen 443 ssl; ... }` block. In single-server mode Xray owns `:443`, so remove or comment that entire block and keep only the port-80 server, the `map` blocks, and the upstream. The tested Yandex resource fetches origin over port 80, so the 443 nginx block is not needed.

If nginx must keep `:443` for another hostname, change `direct-reality` in the asset to `8443` and use that port in the direct profile instead. Do not leave both processes configured for `:443`; the second one fails to bind.

## Keeping existing profiles working

Switching the CDN origin replaces the server behind a hostname that already-imported profiles still use. If those profiles must keep working, run the old inbound next to the new one and route by path:

1. Copy the old inbound verbatim into the new Xray config on a second loopback port, with its own path, its own `decryption`, and the same UUIDs. "Verbatim" includes `scMaxEachPostBytes`, `scMinPostsIntervalMs`, `uplinkChunkSize`, `uplinkDataPlacement`, `serverMaxHeaderBytes`, and the whole `xmux` object. A near-copy that relies on defaults is a different framing and will stall.
2. Add a second nginx location for the old path pointing at that port. Prefix locations are matched longest-first, so `/api-stream2` and `/api-stream` can coexist without escaping.
3. Prove the old framing before switching: build a client from the *old* server settings, not from the new preset, and run a session against the new server locally.
4. Apply the same secret gate to both tunnel locations; the edge injects the header for both.
5. Re-test the old profile through the edge after the switch. It inherits the edge quality of the day, so a stall rate can be higher than the new profile even though both hit the same origin.

The old path carries whatever encryption the old inbound had (`none` for a pre-`vlessenc` profile). It is a compatibility path, not a second hardened one: retire it once every client has moved to the new profile.

## Rotating a key without breaking the tunnel

Two ways to add a new credential, and only one of them is low risk.

**New UUID on the proven path.** Add the new UUID to the *existing, working* inbound as a second client entry and keep every transport knob untouched. The framing, the path, and the edge are all already proven, so the only variable is the credential. This is the cheapest rotation and it keeps the old key alive as a fallback until the new one is confirmed on a device.

**New inbound on a new path.** This introduces a second framing into the system. The new inbound must be a byte-for-byte copy of the working one, including the `decryption` mode, the chunk size, the header budget, and the padding and session keys. The only permitted differences are the path and the UUID list. Anything else, and a client that works against the old path can still fail against the new one for reasons that are hard to see from the outside: the server may parse the session with the default session key while the client sends a custom one, or the server may expect a `vlessenc` handshake that the client never performs.

When a new path is unavoidable, prove the copy before the client sees it: run the new inbound locally, connect with a client built from the *same* settings, and only then generate the share link. Comparing the new profile against the old one, field by field, is faster than bisecting a phone.

Keep the working key and the working path until the new credential is confirmed on a real device. A fallback that has been switched off is not a fallback.

## Setup

1. Back up nginx, Xray, and the CDN resource as described in [operations.md](operations.md#backups).
2. Install [assets/xray-inbound.example.json](../assets/xray-inbound.example.json) as the Xray config. Replace the two UUIDs and the REALITY fields. The CDN inbound ships with `"decryption": "none"`, which matches a plain-VLESS client and is the configuration that was verified on a phone; only replace it with the output of `xray vlessenc` when the client side is known to carry the key.
3. Keep the CDN inbound framing identical to the client preset. Do not change individual XHTTP keys; regenerate the client with `make-profile.py`.
4. Install the nginx port-80 block and the CDN static header as in [USAGE.md](../USAGE.md). Remove the nginx 443 block.
5. Validate:

```bash
xray run -test -config /usr/local/etc/xray/config.json
sudo nginx -t
sudo systemctl restart xray
sudo systemctl reload nginx
```

6. Check the three paths separately:

```bash
# Origin gate and header budget, directly on port 80.
CDN_SECRET=$(cat ~/.config/vpn-cdn/cdn-secret)
bash scripts/origin-check.sh --host "$CDN_HOST" --ip "$ORIGIN_IP" \
  --scheme http --port 80 --cdn-secret "$CDN_SECRET"

# The CDN injects the header and reaches this origin.
bash scripts/origin-check.sh --host "$CDN_HOST" --scheme https --port 443 \
  --cdn-secret "$CDN_SECRET"

# The edge carries an XHTTP session.
# --vless-encryption is only needed when the inbound uses a vlessenc key.
bash scripts/edge-test.sh --edge "$EDGE_IP" --sni "$CDN_HOST" \
  --path /api-stream --uuid-file ~/.config/vpn-cdn/uuid \
  --attempts 5
```

7. Test the direct REALITY profile from an unrestricted network. It uses `:443` on the same address and does not pass through the CDN.

## Switching and rollback

To switch to the two-server cascade, restore a config based on [assets/xray-inbound-cascade.example.json](../assets/xray-inbound-cascade.example.json), set the exit host and keys, and re-enable the nginx 443 block if you use it. The CDN resource, hostname, and secret do not change.

Keep the working profile and the previous config until the new mode passes the acceptance checklist in [operations.md](operations.md#acceptance-checklist). The two modes differ in routing, not in the CDN-facing framing, so a profile with the same `vlessenc` setting can be reused across the switch; only the Xray routing changes.
