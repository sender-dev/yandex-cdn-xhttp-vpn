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

Recommended values for a desktop client:

```json
{
  "uplinkChunkSize": 4000,
  "scMaxEachPostBytes": 32768,
  "scMaxBufferedPosts": 64,
  "scMinPostsIntervalMs": 10
}
```

`uplinkChunkSize` has two different representations, and mixing them is a
classic source of "the profile imports but never connects":

| Where | Representation | Why |
|---|---|---|
| Share-link `extra` blob | string, e.g. `"16384"` | The mobile importer models the field as a string and drops or rejects a number |
| Xray client JSON | plain integer, e.g. `4000` | Cores that predate range parsing decode the field as `uint32` and abort with `cannot unmarshal string into Go struct field ...uplinkChunkSize of type uint32` |

A range string such as `"3000-4000"` is accepted by recent cores and rejected
by the older ones. `scripts/make-profile.py` therefore keeps the range out of
the default presets and warns when one is requested.

`scMaxEachPostBytes` is an upper bound for POST-mode chunks, not for GET/header uplink. With `uplinkHTTPMethod: GET` and `uplinkDataPlacement: header` it does not shape the request, but keep it consistent on both sides. `scMinPostsIntervalMs` prevents a tight POST loop and is irrelevant to GET/header as well; 10 ms is a reasonable floor if a POST mode is used. The proposal to set `scMaxEachPostBytes` to 500000–1000000 belongs to body/POST modes and contradicts a header budget.

Measured on loopback with Xray 26.3.27, `uplinkChunkSize: "3000-4000"`, and `xPaddingBytes: "100-1000"`: the complete request header block was 3152 bytes, including `X-Data-0`, session, sequence, padding, and browser-like headers. That is the practical number to compare against an edge limit.

## Encrypting the payload (vlessenc)

Header placement base64-encodes the VLESS payload. Base64 is not encryption: without `vlessenc`, an observer on the edge-to-origin leg can decode the destination and the data. VLESS Encryption adds an authenticated record layer inside VLESS, so the bytes placed in `X-Data-*` are ciphertext before XHTTP frames them.

**`vlessenc` is not the default for a mobile share link.** The key has to survive a trip through the mobile importer: it must carry a `encryption=` parameter, put it on the VLESS user, and keep the server inbound in `vlessenc` mode. Every one of those steps is a way to end up with a link that imports cleanly and then never establishes a session. The profile that was proven on a phone used plain VLESS with `encryption` absent from the link. Turn `vlessenc` on only after the plain path is stable and you have a device that can be tested, and keep the plain path as the fallback.

Generate one pair on the origin:

```bash
xray vlessenc > ~/.config/vpn-cdn/vlessenc.txt
```

The output contains two authentication options (X25519 and ML-KEM-768) and four strings. Each option is a valid pair; pick one option and keep its two lines together. To take the first option:

```bash
SERVER_KEY=$(sed -n 's/^"decryption": "\(.*\)"$/\1/p' ~/.config/vpn-cdn/vlessenc.txt | head -1)
CLIENT_KEY=$(sed -n 's/^"encryption": "\(.*\)"$/\1/p' ~/.config/vpn-cdn/vlessenc.txt | head -1)
```

```json
"decryption": "mlkem768x25519plus.native.600s.<server-key>"
"encryption": "mlkem768x25519plus.native.0rtt.<client-key>"
```

Server: only `settings.decryption`. Client: only `users[].encryption`. Do not put `encryption` on an inbound client:

```json
"settings": {
  "clients": [{ "id": "<UUID>", "flow": "" }],
  "decryption": "<server-key-string>"
}
```

```json
"users": [{ "id": "<UUID>", "encryption": "<client-key-string>" }]
```

Rules that prevent most failures:

- The two strings must come from the same option in the same run. Mixing `decryption` from one option with `encryption` from another, or from a second run, fails to decrypt.
- VLESS Encryption needs Xray-core 25.9.5 or newer. Older clients may ignore the field instead of failing at parse time, which looks like a stalled session.
- Support is not universal. Happ and v2RayTun bundle Xray-core and may support it; sing-box-based clients such as Hiddify may not. Keep a matching `none`/`none` fallback profile during rollout.
- A share link has to carry the client key as `encryption=<client-key>`. A mobile importer that does not model that parameter drops the key, the client then decrypts with `none`, and the server rejects the first packet.
- The transport metadata stays visible: path, method, `X-Auth-Token`, `page`, and `X-Data` header names. Encryption hides the payload, not the shape.
- Encrypt the 443 origin block too if it carries the tunnel; otherwise the plaintext path remains available.
- Rotate by generating a new pair, updating server and client profiles together, then restarting Xray. The old client key stops working immediately.

Local proof, not a field claim: with `vlessenc` enabled, a captured CDN-to-origin request contained no target hostname, URL path, or payload marker; `X-Data-0` was ciphertext. End-to-end behavior through a Yandex edge and support in the target mobile client still need a device test.

## The extra object

`extra` is the XHTTP settings blob of a share link. Two shapes exist, and they
are not interchangeable.

### Mobile shape (field-verified)

A mobile importer that was proven on a device models exactly five keys and
nothing else:

```json
{
  "scMaxEachPostBytes": 32768,
  "scMinPostsIntervalMs": 10,
  "uplinkChunkSize": "16384",
  "uplinkDataPlacement": "header",
  "uplinkHTTPMethod": "GET"
}
```

`scripts/make-profile.py --preset happ-parity` emits exactly this set. Notes on
each point, because every one of them was a real difference between a link that
imported and a link that did not:

- `mode` is **not** part of `extra`. It is a top-level share-link parameter
  (`?mode=packet-up`). A client that models only the five keys ignores an
  `extra.mode`, and the transport silently falls back to its default mode.
- `uplinkChunkSize` is a **string** in the share link. The importer models it
  as text; a JSON number is dropped or refused.
- `uplinkDataKey` is absent. The default header name `X-Data` is what the
  client uses, and the server must therefore also use the default.
- `scMaxBufferedPosts` is absent. It is a server-side knob.
- Padding and session-placement keys (`xPadding*`, `sessionPlacement`,
  `sessionKey`, `seqPlacement`, `seqKey`) are absent. A strict importer can
  refuse a blob that carries keys it does not model.

### Desktop shape

`--preset yandex-get` is the richer set used for Xray client JSON and for
scripts that build a config directly:

```json
{
  "uplinkHTTPMethod": "GET",
  "uplinkDataPlacement": "header",
  "uplinkDataKey": "X-Data",
  "uplinkChunkSize": 4000,
  "scMaxEachPostBytes": 32768,
  "scMaxBufferedPosts": 64,
  "scMinPostsIntervalMs": 10
}
```

These keys also go at the top level of `xhttpSettings` in Xray JSON.

What the less obvious desktop keys do:

- `uplinkDataKey` is the header that carries the packet when placement is
  `header`. Leaving it out means the default `X-Data` on both ends.
- `scMaxBufferedPosts` bounds how many uplink requests the server buffers
  while waiting for the downstream side. Keep it identical on both ends.

## Server and client must match

Every key above that affects framing or encoding must be identical on the inbound and the client, or explicitly left at the same default on both sides. The common failure is a client that only sets `mode`, `uplinkHTTPMethod`, and `uplinkDataPlacement` while the server uses custom session and sequence keys. The client falls back to defaults, the server cannot parse the session, and the result is a `404`, a `400`, or a session that establishes and then dies.

Keep one source of truth: generate the client from a single preset, copy the same `xhttpSettings` block into the inbound, and run `xray run -test` on both. Inbound `mode` must be `packet-up` when header placement is used; `auto` plus `uplinkDataPlacement: header` is rejected at startup with "UplinkDataPlacement can be header only in packet-up mode".

The safest way to add a second tunnel is to make the new inbound a **verbatim copy** of the proven one and change nothing but the path and the UUID. Every knob that differs is a candidate for the mismatch, and the cheapest way to find the culprit is to not introduce any. The parity inbound also keeps plain VLESS (`"decryption": "none"`) until the plain path is proven on the device.

Xray silently ignores unknown key names. Validate, then inspect the parsed config:

```bash
xray run -test -dump -config client.json | jq '.outbounds[0].streamSettings.xhttpSettings'
```

If a key you wrote is absent from the dump, the running Xray does not implement it. This is how the old `sessionIDPlacement`/`sessionIDKey` names (and `enableXmux`) were found to be no-ops; the current names are `sessionPlacement` and `sessionKey`, and `xmux` is enabled by its presence, not by a flag.

## Share link

A VLESS share link for Happ, v2RayTun, or Hiddify:

```text
vless://<UUID>@<ADDRESS>:443?security=tls&sni=<CDN_HOST>&fp=chrome&type=xhttp&host=<CDN_HOST>&path=%2Fapi-stream&mode=packet-up&extra=<ENCODED-JSON>#<LABEL>
```

`encryption` is **absent** for plain VLESS. A link that carries
`encryption=none` is accepted by some importers and refused by others, so the
generator omits the parameter entirely when the transport is plain.

With `vlessenc`, `encryption` carries the client key instead:

```text
vless://<UUID>@<ADDRESS>:443?encryption=mlkem768x25519plus.native.600s.<CLIENT_KEY>&security=tls&sni=<CDN_HOST>&fp=chrome&type=xhttp&host=<CDN_HOST>&path=%2Fapi-stream&mode=packet-up&extra=<ENCODED-JSON>#<LABEL>
```

`scripts/make-profile.py --vless-encryption "$CLIENT_KEY"` writes exactly this. The key is sensitive: it decrypts the tunnel, so keep it out of the share link you paste into chats, tickets, or public repos.

Field meanings:

| Field | Value |
|---|---|
| `address` | A verified edge IP for a pinned profile, or the CDN hostname for the fallback |
| `port` | `443` |
| `security` | `tls` |
| `sni` | The CDN hostname; must match the certificate |
| `host` | The HTTP Host header; usually the same as SNI |
| `path` | The tunnel path, URL-encoded (`/api-stream` becomes `%2Fapi-stream`) |
| `mode` | `packet-up`, at the top level and never inside `extra` |
| `encryption` | Present only with `vlessenc`; omitted for plain VLESS |
| `extra` | The XHTTP settings blob, keys at the top level |
| `fp` | `chrome` is the tested fingerprint |

The nesting trap: `extra` must contain the XHTTP keys directly. Putting them under `extra.xhttpSettings` leaves the effective `extra` empty, the client uses defaults, and the server sees a different framing. The generator in `scripts/make-profile.py` does this correctly.

### How the extra blob is encoded

A mobile share link encodes `extra` **twice**: the JSON is percent-encoded once when it is placed in the query string, and the whole URI is percent-encoded once more by whoever produces the link. In the raw text of the URI that shows up as `%257B` for the opening brace. A link that carries only one layer decodes to a string of `%7B...` and the importer parses nothing.

The percent-encoding follows the character set that mobile clients use, which is the unreserved set plus `-._~!$&'()*+,;=:@/?`. Colons and commas stay literal, a double quote becomes `%22`, a space becomes `%20`.

The JSON inside is pretty-printed the way mobile clients pretty-print it: a space on both sides of the colon, a two-space indent, and a newline before the closing brace.

```text
{
  "scMaxEachPostBytes" : 32768,
  "scMinPostsIntervalMs" : 10,
  "uplinkChunkSize" : "16384",
  "uplinkDataPlacement" : "header",
  "uplinkHTTPMethod" : "GET"
}
```

Whitespace differences matter here: `"key": value` and `"key" : value` are
different strings, and an importer that compares the blob literally will
reject one of them. `scripts/make-profile.py` reproduces this format and the
test suite compares the generated blob byte for byte against a link that was
proven on a phone.

`--extra-encoding plain` writes the blob with a single layer of encoding, for
clients that expect the plain form.

Do not add `serverMaxHeaderBytes` to the share link. It belongs in the inbound JSON. If a client rejects an unknown key, remove optional keys first and keep the framing keys.


## Xray client JSON

The equivalent outbound, matching the mobile shape:

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
      "uplinkChunkSize": 16384,
      "scMaxEachPostBytes": 32768,
      "scMinPostsIntervalMs": 10
    }
  }
}
```

`users[].encryption` must always be present, even for plain VLESS. Recent
Xray-core versions refuse to start without it and print:

```text
VLESS users: please add/set "encryption":"none" for every user
```

Set it to the client key from `xray vlessenc` when the inbound has a
`decryption`, and to the literal `none` otherwise. The inbound side never has
an `encryption` field.

`uplinkChunkSize` is a plain integer here, unlike in the share link, so that
older cores can decode it.

`xmux` keeps a small pool of reusable connections. Do not set both
`maxConcurrency` and `maxConnections` to positive values: `xray run -test` may
accept the pair, but the connection fails when it is used. The desktop preset
uses connections, not concurrency. The `happ-parity` preset omits `xmux`
entirely: the profile that was proven on a phone used a single connection, and
multiplexing only adds failure modes behind a CDN edge.


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
