# Origin setup

The origin is ordinary nginx plus Xray. The CDN-specific parts are the port-80 entry point, generous header buffers, no buffering, and a local-only Xray inbound.

## Layout

A simple layout that is easy to back up and roll back:

```text
/etc/nginx/sites-enabled/cdn-origin.conf     # port 80 catch-all and optional 443 block
/usr/local/etc/xray/config.json              # XHTTP inbound + REALITY cascade
/var/www/acme/                                # certbot webroot
/root/vpn-cdn-backups/<UTC timestamp>/        # mode 700, files mode 600
~/.config/vpn-cdn/cdn-secret                  # CDN-to-origin shared secret, mode 600
~/.config/vpn-cdn/vlessenc.txt                # decryption/encryption pair, mode 600
```

Keep the direct REALITY inbound and the CDN XHTTP inbound in the same Xray config until the CDN path is accepted. Removing the fallback before acceptance makes diagnosis harder.

## nginx port-80 block

The tested edge fetched origin over HTTP/80. The block must be reachable without a redirect to HTTPS, because the edge does not follow a client-style redirect for these requests.

```nginx
# Replace CHANGE_ME_CDN_SECRET with `openssl rand -hex 32` output.
map $http_x_cdn_secret $cdn_secret_ok {
    default 0;
    "~CHANGE_ME_CDN_SECRET" 1;
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

    # Header placement base64-encodes the uplink payload. 3-4 KiB chunks stay
    # well below these buffers; the buffers are insurance, not the primary limit.
    client_header_buffer_size 16k;
    large_client_header_buffers 8 64k;

    keepalive_requests 100000;
    keepalive_timeout 600s;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

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
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-CDN-Secret "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_socket_keepalive on;
        proxy_buffer_size 32k;
        proxy_buffers 8 32k;
        client_max_body_size 0;
        add_header X-Accel-Buffering no always;
        add_header Cache-Control "no-store, no-transform" always;
    }
}
```

Why each piece matters:

- `default_server` catches the request if the edge sends a Host that does not match a named block. It is also a risk: keep the catch-all limited to the tunnel path and explicit control endpoints, not a general proxy.
- `client_header_buffer_size` and `large_client_header_buffers` prevent `400`/`431` when a padded header packet grows. Do not rely on the default 8 KiB.
- `proxy_http_version 1.1` and `Connection ""` keep the upstream connection reusable and avoid `close` semantics.
- `proxy_buffering off` and `proxy_request_buffering off` keep the stream moving. Buffering turns a long session into a stalled one.
- `proxy_cache off` plus `Cache-Control: no-store, no-transform` prevents an intermediate cache from storing or rewriting the response.
- Long read/send timeouts match the tunnel's long-lived connection.
- `client_max_body_size 0` avoids a local `413`, although the tested transport does not send a body.
- `keepalive_requests` and `keepalive_timeout` in both contexts let the edge reuse one connection for many XHTTP requests. nginx 1.15.3+ supports both directives in `upstream`; 100000 is a tuning value, not a measured optimum.
- `proxy_socket_keepalive on` keeps the upstream socket from going idle silently. It needs nginx 1.15.6+.
- `proxy_set_header X-CDN-Secret ""` strips the CDN credential before the request reaches Xray, so it cannot leak into tunnel logs.

### The secret gate

`X-CDN-Secret` is a shared secret between the CDN resource and the origin. It answers one question: did this request arrive through the configured CDN? It is not user authentication; the VLESS UUID does that. It does not stop an attacker who sends requests through the CDN — that is allowed by design. It stops direct scans of the origin IP from finding a working endpoint.

Generate it with `openssl rand -hex 32` so the value contains only `[0-9a-f]`, which is safe inside the regex. Set the same value on the CDN resource:

```bash
yc cdn resource update <RESOURCE_ID> \
  --static-request-headers X-CDN-Secret="$(cat ~/.config/vpn-cdn/cdn-secret)"
```

Use a regex `map`, not equality. nginx joins duplicate request headers with `", "`. If a client sends its own `X-CDN-Secret` through the CDN, the origin may see `"fake, real"`. Equality locks that traffic out; matching the secret anywhere in the value passes, and a direct scan still cannot guess it.

Gate only the tunnel paths. A server-level `return 444` breaks `/health`, ACME HTTP-01, and any other vhost. Keep `/health` open for the edge and monitoring, keep `/.well-known/acme-challenge/` open for certificates, and expose `/_cdn_secret_probe` as the deliberate control: `204` with the header, connection close without it. Use the probe to prove the CDN injection before enforcing the gate on real traffic.

If you need a named vhost instead of a catch-all, add `server_name cdn.example.com;` and keep a separate catch-all that returns `444` or proxies only the tunnel path. Verify with the access log that the intended block handled the request. Drop `default_server` if another vhost already owns the default on port 80, and use `listen 443 ssl http2;` instead of `http2 on;` on nginx older than 1.25.1.

## Optional 443 block

The CDN may fetch over 80, but keep a 443 block for direct checks and for providers that use HTTPS to origin. This block is optional; the certificate covers the public CDN hostname, not an internal origin alias.

In [single-server mode](single-server.md), Xray `direct-reality` owns `:443`. Remove this block entirely and keep nginx on `:80`; otherwise one of the two processes cannot bind. If nginx must keep `:443`, move `direct-reality` to `8443` in the Xray config and in the direct profile.

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name cdn.example.com;

    ssl_certificate     /etc/letsencrypt/live/cdn.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cdn.example.com/privkey.pem;

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

        proxy_pass http://xhttp_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-CDN-Secret "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_socket_keepalive on;
        proxy_buffer_size 32k;
        proxy_buffers 8 32k;
        client_max_body_size 0;
        add_header X-Accel-Buffering no always;
        add_header Cache-Control "no-store, no-transform" always;
    }
}
```

The `map` and `upstream` blocks from the port-80 example are shared; do not duplicate them. Protect the 443 tunnel path with the same gate. An unprotected 443 block on the same host defeats the purpose of the gate.

Include every hostname the origin answers for in `server_name`, and expand the certificate to cover the origin name if the CDN fetches it by name. A missing SAN or a missing `server_name` sends the request to the wrong block, which usually looks like an edge `404` or `502`.

## Xray inbound

The cascade inbound is VLESS with XHTTP, bound to loopback, routing to REALITY; its full example is [../assets/xray-inbound-cascade.example.json](../assets/xray-inbound-cascade.example.json). The default single-server example is [../assets/xray-inbound.example.json](../assets/xray-inbound.example.json). The essential shape:

```json
{
  "listen": "127.0.0.1",
  "port": 9081,
  "protocol": "vless",
  "settings": {
    "clients": [{ "id": "<UUID>", "flow": "" }],
    "decryption": "<VLESS_DECRYPTION_KEY>"
  },
  "streamSettings": {
    "network": "xhttp",
    "security": "none",
    "xhttpSettings": {
      "path": "/api-stream",
      "mode": "packet-up",
      "uplinkHTTPMethod": "GET",
      "uplinkDataPlacement": "header",
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
      "uplinkDataKey": "X-Data",
      "serverMaxHeaderBytes": 32768
    }
  }
}
```

Notes:

- `decryption` is the only encryption field on the inbound. Do not add `encryption` to the inbound client objects: Xray 26.3.27 rejects the config with `"encryption" should not be in inbound settings`. The client outbound carries the matching `encryption` string in `users[]`.
- Generate both strings once with `xray vlessenc`. Keep the pair together; a client key from a different run does not decrypt. VLESS Encryption needs Xray-core 25.9.5 or newer. If the target client cannot use it, set `decryption: "none"` on the server and `encryption: "none"` on the client.
- VLESS Encryption is independent of TLS. `security: "none"` still means there is no TLS between nginx and Xray on loopback; the payload inside the HTTP headers is encrypted by `vlessenc` before XHTTP frames it.
- `serverMaxHeaderBytes` is a server-side acceptance limit. Do not bury it inside a share-link `extra` string; set it in the inbound JSON.
- `mode: "auto"` is valid only when the uplink stays in the body/default placement. Xray refuses to start with `mode: "auto"` plus `uplinkDataPlacement: "header"` ("UplinkDataPlacement can be header only in packet-up mode"), so the header-placement inbound uses `packet-up`.
- The inbound must not listen publicly. Bind loopback and let nginx be the only public entry.
- `security: "none"` is correct here because the origin listens on loopback. Do not expose this inbound directly.

## Logging that answers questions

Use a dedicated log format so a failed test can be correlated:

```nginx
log_format cdn '$remote_addr - $host "$request_method $request_uri" '
               '$status $request_length $bytes_sent $request_time '
               '"$http_user_agent" upstream=$upstream_addr';

access_log /var/log/nginx/cdn-access.log cdn;
```

What to look for:

- A request that never appears means the edge did not route it.
- `200` with a small `$request_length` and a few kilobytes of `$bytes_sent` is a healthy short probe.
- A long session shows repeated requests with data in `$request_length` and `$bytes_sent` above zero.
- `101` in nginx is relayed upgrade behavior; for XHTTP you normally see `200` on the upstream requests.
- `$request_time` near the proxy timeout means the upstream held the connection; check Xray and the cascade.

Logs have second-level precision at best. For correlation, use unique paths or a nonce and record client timestamps in UTC.

## Validation and reload

```bash
sudo nginx -t
xray run -test -config /usr/local/etc/xray/config.json
sudo systemctl reload nginx
sudo systemctl restart xray
curl -fsS --resolve cdn.example.com:80:<ORIGIN_IP> http://cdn.example.com/health
bash scripts/origin-check.sh --host cdn.example.com --ip <ORIGIN_IP> \
  --scheme http --port 80 --cdn-secret "$(cat ~/.config/vpn-cdn/cdn-secret)"
# Only if the optional 443 block is deployed:
curl -fsS --resolve cdn.example.com:443:<ORIGIN_IP> https://cdn.example.com/health
# Prove the CDN injects the header (no --ip: the request goes through the CDN):
bash scripts/origin-check.sh --host cdn.example.com --scheme https --port 443 \
  --cdn-secret "$(cat ~/.config/vpn-cdn/cdn-secret)"
```

`origin-check.sh` treats the gate as a core check when `--cdn-secret` is present: with `--ip` it expects the origin to reject the probe without the header and return `204` with it; without `--ip` it expects `204` through the CDN, which proves the static header is applied. A `000`/`444` through the CDN means the resource is not sending it.

Back up before editing, validate before installing, and verify health after reloading. If Xray runs as an unprivileged user, an installed config owned by root with mode `0600` will fail to start; fix ownership before restarting.

## Certificate notes

- Use DNS-01 for a hostname that is behind a CDN or not yet resolving.
- Create `_acme-challenge` TXT records at the DNS host and confirm them on the authoritative nameservers, not only on a public resolver.
- If the CDN panel also issues a certificate, keep the origin certificate and the CDN certificate for the same hostname consistent. A mismatch shows up as a TLS error at the edge, not at the client.
- Include the origin hostname in the certificate when the origin group uses a domain and verifies TLS.
