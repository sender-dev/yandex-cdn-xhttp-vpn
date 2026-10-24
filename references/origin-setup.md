# Origin setup

The origin is ordinary nginx plus Xray. The CDN-specific parts are the port-80 entry point, generous header buffers, no buffering, and a local-only Xray inbound.

## Layout

A simple layout that is easy to back up and roll back:

```text
/etc/nginx/sites-enabled/cdn-origin.conf     # port 80 catch-all and optional 443 block
/usr/local/etc/xray/config.json              # XHTTP inbound + REALITY cascade
/var/www/acme/                                # certbot webroot
/root/vpn-cdn-backups/<UTC timestamp>/        # mode 700, files mode 600
```

Keep the direct REALITY inbound and the CDN XHTTP inbound in the same Xray config until the CDN path is accepted. Removing the fallback before acceptance makes diagnosis harder.

## nginx port-80 block

The tested edge fetched origin over HTTP/80. The block must be reachable without a redirect to HTTPS, because the edge does not follow a client-style redirect for these requests.

```nginx
upstream xhttp_backend {
    server 127.0.0.1:9081;
    keepalive 32;
}

server {
    listen 80 default_server;
    server_name _;

    # Header placement base64-encodes the uplink payload. 3-4 KiB chunks stay
    # well below these buffers; the buffers are insurance, not the primary limit.
    client_header_buffer_size 16k;
    large_client_header_buffers 8 64k;

    location /api-stream {
        proxy_pass http://xhttp_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 0;
        add_header Cache-Control "no-store, no-transform" always;
    }

    location = /health {
        access_log off;
        add_header Cache-Control "no-store" always;
        return 200 "ok\n";
    }
}
```

Why each piece matters:

- `default_server` catches the request if the edge sends a Host that does not match a named block. It is also a risk: keep the catch-all limited to the tunnel path and a health endpoint, not a general proxy.
- `client_header_buffer_size` and `large_client_header_buffers` prevent `400`/`431` when a padded header packet grows. Do not rely on the default 8 KiB.
- `proxy_http_version 1.1` and `Connection ""` keep the upstream connection reusable and avoid `close` semantics.
- `proxy_buffering off` and `proxy_request_buffering off` keep the stream moving. Buffering turns a long session into a stalled one.
- `proxy_cache off` plus `Cache-Control: no-store, no-transform` prevents an intermediate cache from storing or rewriting the response.
- Long read/send timeouts match the tunnel's long-lived connection.
- `client_max_body_size 0` avoids a local `413`, although the tested transport does not send a body.

If you need a named vhost instead of a catch-all, add `server_name cdn.example.com;` and keep a separate catch-all that returns `444` or proxies only the tunnel path. Verify with the access log that the intended block handled the request. Drop `default_server` if another vhost already owns the default on port 80, and use `listen 443 ssl http2;` instead of `http2 on;` on nginx older than 1.25.1.

## Optional 443 block

The CDN may fetch over 80, but keep a 443 block for direct checks and for providers that use HTTPS to origin. This block is optional; the certificate covers the public CDN hostname, not an internal origin alias:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name cdn.example.com;

    ssl_certificate     /etc/letsencrypt/live/cdn.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cdn.example.com/privkey.pem;

    client_header_buffer_size 16k;
    large_client_header_buffers 8 64k;

    location /api-stream {
        proxy_pass http://xhttp_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 0;
        add_header Cache-Control "no-store, no-transform" always;
    }
}
```

Include every hostname the origin answers for in `server_name`, and expand the certificate to cover the origin name if the CDN fetches it by name. A missing SAN or a missing `server_name` sends the request to the wrong block, which usually looks like an edge `404` or `502`.

## Xray inbound

The tested inbound is VLESS with XHTTP, bound to loopback, routing to a REALITY cascade. The full example is in [../assets/xray-inbound.example.json](../assets/xray-inbound.example.json). The essential shape:

```json
{
  "listen": "127.0.0.1",
  "port": 9081,
  "protocol": "vless",
  "settings": {
    "clients": [{ "id": "<UUID>", "flow": "" }],
    "decryption": "none"
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

- `serverMaxHeaderBytes` is a server-side acceptance limit. Do not bury it inside a share-link `extra` string; set it in the inbound JSON.
- `mode: "auto"` is valid only when the uplink stays in the body/default placement. Xray refuses to start with `mode: "auto"` plus `uplinkDataPlacement: "header"` ("UplinkDataPlacement can be header only in packet-up mode"), so the header-placement inbound uses `packet-up`.
- The inbound must not listen publicly. Bind loopback and let nginx be the only public entry.
- `security: "none"` is correct here because the client-to-edge and edge-to-origin legs are separate; the origin listens on loopback. Do not expose this inbound directly.

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
# Only if the optional 443 block is deployed:
curl -fsS --resolve cdn.example.com:443:<ORIGIN_IP> https://cdn.example.com/health
```

Back up before editing, validate before installing, and verify health after reloading. If Xray runs as an unprivileged user, an installed config owned by root with mode `0600` will fail to start; fix ownership before restarting.

## Certificate notes

- Use DNS-01 for a hostname that is behind a CDN or not yet resolving.
- Create `_acme-challenge` TXT records at the DNS host and confirm them on the authoritative nameservers, not only on a public resolver.
- If the CDN panel also issues a certificate, keep the origin certificate and the CDN certificate for the same hostname consistent. A mismatch shows up as a TLS error at the edge, not at the client.
- Include the origin hostname in the certificate when the origin group uses a domain and verifies TLS.
