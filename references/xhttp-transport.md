# XHTTP transport

XHTTP is the part that makes or breaks a CDN-carried tunnel. This reference explains the modes, the settings that must match on both ends, and the header-size budget that keeps a CDN edge from rejecting packets.

## Modes

| Mode | Uplink shape | Works when | Notes |
|---|---|---|---|
| `packet-up` | Many small requests, uplink data in headers or body | The edge allows GET and forbids request bodies | The tested mode. Each packet is an HTTP request. |
| `stream-up` | One long-lived upload stream | The edge accepts POST bodies and does not buffer them | A POST body was rejected with `413` at the tested edge; treat this mode as provider-dependent until measured. |
| `auto` | Server accepts either | Mixed clients during a migration | Do not combine `auto` with header placement; Xray refuses that pair. |

Do not choose `stream-up` just because it looks faster. On a retrieval-oriented CDN, the edge is the limiter, not Xray.

## Why GET with header placement

The tested Yandex edge allowed `GET`, `HEAD`, and `OPTIONS`. A POST body returned `413` through the tested edge, and a reference deployment reports `405` on POST at a Yandex edge. XHTTP defaults to POST and body placement, so a profile that omits the overrides will fail with a method or size error before any tunnel data is exchanged.

The working pair is:

```text
mode                = packet-up
uplinkHTTPMethod    = GET
uplinkDataPlacement = header
```

`OPTIONS` plus an origin rewrite to POST was tested and was worse (mostly `405` while the edge propagated the method change). It solves a problem this setup does not have. Leave OPTIONS enabled for future experiments, but keep the client on GET.

## Header budget

Header placement base64-encodes each uplink packet into header fields. The budget is tighter than it looks:

```text
raw chunk           3-4 KiB
base64 expansion    ~4/3          -> 4.0-5.3 KiB
padding            100-1000 B     -> up to ~6.3 KiB
session, seq, keys  small         -> ~6.5 KiB
```

Keep the total request headers below roughly 8 KiB. The nginx origin can accept 16 KiB (`client_header_buffer_size 16k`) and 64 KiB large buffers, but the CDN edge may have its own limit. A 16 KiB raw chunk becomes more than 21 KiB after base64 and will produce `431` or `400` at the edge or origin.

Recommended values:

```json
{
  "uplinkChunkSize": "3000-4000",
  "scMaxEachPostBytes": 8192,
  "scMaxBufferedPosts": 64,
  "scMinPostsIntervalMs": 10,
  "xPaddingBytes": "100-1000"
}
```

`scMaxEachPostBytes` is an upper bound in bytes; keep it at or below the chunk range plus overhead. `scMinPostsIntervalMs` prevents a tight request loop; 10 ms is a reasonable floor on a CDN path.

## The full extra object

This is the hardened Yandex preset used by `make-profile.py --preset yandex-get` and by `edge-test.sh --preset yandex-get`. The same keys go at the top level of `xhttpSettings` in Xray JSON.

```json
{
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
  "xPaddingObfsMode": true,
  "xPaddingBytes": "100-1000"
}
```

What the less obvious keys do:

- `sessionPlacement` and `seqPlacement` decide where the session and packet sequence live. Moving the session into a header keeps it out of the URL path, which some edges filter less aggressively.
- `sessionKey` is the header name that carries the session token when `sessionPlacement` is `header`. It must match between client and server.
- `seqKey` (`page`) makes the request look like pagination rather than a tunnel.
- `xPaddingHeader` (`X-Cache`) and `xPaddingMethod: tokenish` make padding look like cache metadata. `queryInHeader` splits the padding between query and header.
- `xPaddingObfsMode` obfuscates the padding length. It costs a few bytes and is worth keeping.
- `uplinkDataKey` is the header that carries the packet when placement is `header`.

## Server and client must match

Every key above that affects framing or encoding must be identical on the inbound and the client, or explicitly left at the same default on both sides. The common failure is a client that only sets `mode`, `uplinkHTTPMethod`, and `uplinkDataPlacement` while the server uses custom session and sequence keys. The client falls back to defaults, the server cannot parse the session, and the result is a `404`, a `400`, or a session that establishes and then dies.

Keep one source of truth: generate the client from a single preset, copy the same `xhttpSettings` block into the inbound, and run `xray run -test` on both. Inbound `mode` must be `packet-up` when header placement is used; `auto` plus `uplinkDataPlacement: header` is rejected at startup with "UplinkDataPlacement can be header only in packet-up mode".

Xray silently ignores unknown key names. Validate, then inspect the parsed config:

```bash
xray run -test -dump -config client.json | jq '.outbounds[0].streamSettings.xhttpSettings'
```

If a key you wrote is absent from the dump, the running Xray does not implement it. This is how the old `sessionIDPlacement`/`sessionIDKey` names (and `enableXmux`) were found to be no-ops; the current names are `sessionPlacement` and `sessionKey`, and `xmux` is enabled by its presence, not by a flag.

## Share link

A VLESS share link for Happ, v2RayTun, or Hiddify:

```text
vless://<UUID>@<ADDRESS>:443?encryption=none&security=tls&sni=<CDN_HOST>&fp=chrome&type=xhttp&host=<CDN_HOST>&path=%2Fapi-stream&mode=packet-up&extra=<URL-ENCODED-JSON>#<LABEL>
```

Field meanings:

| Field | Value |
|---|---|
| `address` | A verified edge IP for a pinned profile, or the CDN hostname for the fallback |
| `port` | `443` |
| `security` | `tls` |
| `sni` | The CDN hostname; must match the certificate |
| `host` | The HTTP Host header; usually the same as SNI |
| `path` | The tunnel path, URL-encoded (`/api-stream` becomes `%2Fapi-stream`) |
| `mode` | `packet-up` |
| `extra` | One URL-encoded JSON object containing the XHTTP settings, at the top level |
| `fp` | `chrome` is the tested fingerprint |

The nesting trap: `extra` must contain the XHTTP keys directly. Putting them under `extra.xhttpSettings` leaves the effective `extra` empty, the client uses defaults, and the server sees a different framing. The generator in `scripts/make-profile.py` does this correctly.

Do not add `serverMaxHeaderBytes` to the share link. It belongs in the inbound JSON. If a client rejects an unknown key, remove optional padding keys first and keep the framing keys.

## Xray client JSON

The equivalent outbound:

```json
{
  "protocol": "vless",
  "settings": {
    "vnext": [{
      "address": "198.51.100.20",
      "port": 443,
      "users": [{ "id": "<UUID>", "encryption": "none" }]
    }]
  },
  "streamSettings": {
    "network": "xhttp",
    "security": "tls",
    "tlsSettings": { "serverName": "cdn.example.com" },
    "xhttpSettings": {
      "path": "/api-stream",
      "host": "cdn.example.com",
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
      "xPaddingObfsMode": true,
      "xPaddingBytes": "100-1000",
      "xmux": {
        "maxConcurrency": "0",
        "maxConnections": "4-8",
        "cMaxReuseTimes": "0",
        "hMaxRequestTimes": "300-600",
        "hMaxReusableSecs": "900-1800",
        "hKeepAlivePeriod": 0
      }
    }
  }
}
```

`xmux` keeps a small pool of reusable connections. Do not set both `maxConcurrency` and `maxConnections` to positive values: `xray run -test` may accept the pair, but the connection fails when it is used. The Yandex preset uses connections, not concurrency.

## TLS and ALPN

- `security: tls` with `serverName` = the CDN hostname. The address can be an IP; SNI stays the hostname.
- Leave ALPN at the default. Forcing `http/1.1` caused Xray to negotiate h2 via ALPN and then write HTTP/1.1 framing, producing `malformed HTTP/1.x` with HTTP/2 SETTINGS bytes.
- `allowInsecure` still exists in Xray 26.3.x and disables certificate verification. Do not use it on a CDN path: use a valid certificate chain and matching SNI. If you must pin, check the field name and type in your installed Xray version (`pinnedPeerCertSha256` is a string, not an array, where supported) and remember that CDN edges rotate certificates.
- The client fingerprint (`fp`) affects the TLS ClientHello. `chrome` was tested; changing it is a variable to test, not a default to assume.

## Tuning without guessing

Change one variable at a time and keep the control:

1. Direct-origin control through the same Xray client, 10 attempts. If this fails, the problem is not the CDN.
2. Short unique-path request through each edge. If this fails, the edge is not routing the resource.
3. XHTTP session through each edge, 5 attempts. If short requests pass and sessions stall, the edge does not carry long-lived sessions.
4. Only then change chunk size, padding, or mode.

A change that improves one edge and breaks another is not an improvement; it is edge variance. Test the full candidate set.
