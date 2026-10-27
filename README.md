# CDN XHTTP VPN

An agent skill and a small toolkit for carrying a VLESS/XHTTP tunnel through a CDN edge when the direct server path is blocked or throttled. Yandex Cloud CDN is field-verified; the repository also records why Selectel and Gcore failed and what to check before trying another provider.

The skill is written for coding agents (Claude Code, Codex, Qoder, and compatible runtimes) and is also usable by a human operator. It contains the playbook, reference notes, and dependency-light scripts that turn guesses into measurements. Version 2 adds a hardened origin profile: CDN-to-origin authentication with `X-CDN-Secret`, nginx keepalive tuning, and native VLESS Encryption (`vlessenc`) for head-placed payloads.

For the end-to-end walkthrough — from the Yandex console to a profile accepted on the target SIM — read [USAGE.md](USAGE.md).

## Architecture

```text
Client (Happ / v2RayTun / Hiddify)
  -> TLS 443, address = pinned edge IP or CDN hostname, SNI = CDN hostname
  -> CDN edge
  -> origin :80 nginx (X-CDN-Secret required; 444 without it)
  -> Xray VLESS/XHTTP inbound on 127.0.0.1 (vlessenc payload)
  -> outbound freedom (the origin is also the exit)
  -> Internet
```

The CDN changes the public IP and the network path. It does not guarantee that a carrier whitelist will allow the hostname, and it does not make the tunnel fast. Reachability is the goal; speed is secondary.

The edge-to-origin leg is public transit. `X-CDN-Secret` keeps direct scans of the origin from finding a working endpoint; it does not authenticate users (the UUID does) and does not block requests that travel through the CDN. `vlessenc` encrypts the VLESS payload inside the HTTP headers, so the destination and data in `X-Data-*` are opaque on that leg; the transport shape (path, method, session id, sequence) remains visible.

The default topology runs origin and exit on one machine: the CDN fetches the origin directly and Xray routes the tunnel to `freedom`. A two-server REALITY cascade remains available as an explicit alternative for cases where the CDN cannot reach the exit or the exit IP must stay hidden; it uses [assets/xray-inbound-cascade.example.json](assets/xray-inbound-cascade.example.json). Trade-offs and the rollback path are in [references/single-server.md](references/single-server.md).

## What is inside

| Path | Purpose |
|---|---|
| `SKILL.md` | The agent playbook: rules, provider gate, workflow, symptom table, evidence levels |
| `USAGE.md` | End-to-end usage guide: working with the agent, manual deployment, command reference |
| `references/yandex-cdn.md` | Resource shape, methods, certificate, CNAME, static request headers, edge behavior |
| `references/origin-setup.md` | Hardened nginx port 80/443, secret gate, buffers, keepalive, Xray inbound |
| `references/single-server.md` | Default origin = exit mode: port layout, when a cascade is still needed, switching and rollback |
| `references/xhttp-transport.md` | Modes, header budget, `vlessenc`, full `extra` object, share-link fields |
| `references/diagnostics.md` | Test ladder, vantage contamination, log correlation |
| `references/providers.md` | Yandex, Selectel, Gcore, VK, Timeweb, Beget comparison |
| `references/operations.md` | Backups, secrets, rotation, monitoring, incident runbook |
| `scripts/cdn-probe.py` | DNS, TLS, per-edge matrix, methods, cache checks (stdlib only) |
| `scripts/edge-test.sh` | End-to-end XHTTP session test per edge, with `vlessenc` support and PID cleanup |
| `scripts/origin-check.sh` | Origin control: health, header budget, TLS, methods, secret gate; HTTP/80 or HTTPS |
| `scripts/make-profile.py` | Share URI and matching Xray client config, with optional `vlessenc` key |

## Install as a skill

The repository is a single-skill package, so the open agent-skills CLI can install it directly:

```bash
# Global install, then choose the agents
npx skills add sender-dev/yandex-cdn-xhttp-vpn -g

# Qoder only, available in every project
npx skills add sender-dev/yandex-cdn-xhttp-vpn -a qoder -g

# Current project only
npx skills add sender-dev/yandex-cdn-xhttp-vpn
```

`bunx skills add ...`, `npm exec skills add ...`, and `pnpm dlx skills add ...` are equivalent. Requires Node.js 22.20 or later. The CLI sends anonymous telemetry by default; set `DISABLE_TELEMETRY=1` to opt out.

Install from a clone instead:

```bash
git clone https://github.com/sender-dev/yandex-cdn-xhttp-vpn.git
cd yandex-cdn-xhttp-vpn
./install.sh
```

`install.sh` symlinks the repository into the skill directories it finds (`~/.agents/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.qoder/skills`) or creates `~/.agents/skills`. Use `--copy` to copy instead of symlink, `--dir <path>` to choose one directory, and `--force` to replace an existing install.

Manual install: copy `SKILL.md`, `references/`, `scripts/`, and `assets/` into `<skills-dir>/yandex-cdn-xhttp-vpn/`.

## Quick start

Requirements: bash, curl, dig, python3, and Xray-core on `PATH` for live tests. `openssl` is additionally required for `origin-check.sh --scheme https`. No Python packages are required.

```bash
# 0. Direct control first: if this fails, the CDN is not the problem.
bash scripts/origin-check.sh --host cdn.example.com --ip 203.0.113.10 --scheme http --port 80

# 0b. Hardened origin (recommended): install assets/nginx-origin.conf with a
#     generated secret, then make the CDN send the same header:
#       openssl rand -hex 32 > ~/.config/vpn-cdn/cdn-secret
#       yc cdn resource update <RESOURCE_ID> \
#         --static-request-headers X-CDN-Secret="$(cat ~/.config/vpn-cdn/cdn-secret)"
#     The gate applies to /api-stream only; /health and ACME stay open.

# 1. Discover the edges the CDN returns and flag fake-IP resolvers.
python3 scripts/cdn-probe.py dns --host cdn.example.com
python3 scripts/cdn-probe.py matrix --host cdn.example.com --path /api-stream --attempts 3

# 2. Create a UUID outside the repository, then build a pinned profile.
install -d -m 700 ~/.config/vpn-cdn
(umask 077; xray uuid > ~/.config/vpn-cdn/uuid)
python3 scripts/make-profile.py \
  --address 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid \
  --preset happ-parity --name 'CDN pinned' \
  --out ~/.config/vpn-cdn/pinned.vless \
  --client-config ~/.config/vpn-cdn/pinned.json

# 3. Prove the session end to end.
bash scripts/edge-test.sh \
  --edge 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid --attempts 5
```

`happ-parity` is the default preset and the share-link shape that was verified on a phone: five XHTTP keys in `extra`, `uplinkChunkSize` as a string, `mode` at the top level of the link, the blob percent-encoded twice, and no `encryption` parameter for plain VLESS.

Add `--vless-encryption "$client_key"` to the profile and test commands when the inbound uses a `vlessenc` decryption key. Omitting it against an encrypted inbound makes the first packet fail.

Add `--chunk-size 4000` when the importing client fails with `cannot unmarshal string into Go struct field ...uplinkChunkSize of type uint32`. A range string such as `3000-4000` is accepted by newer cores and rejected by older ones; an integer works on both. A profile built with `minimal` will not talk to an inbound that expects `uplinkDataKey`/session-placement/padding keys — keep the full preset and change only the chunk.

`edge-test.sh` exits `0` for `LIVE`, `1` for `PARTIAL`, `2` for a setup error, and `3` for `DEAD`. Use `--dry-run` to validate the generated Xray config without sending traffic, and `--json` for machine-readable output.

## What is verified, and what is not

Verified in the October 2026 field work:

- Yandex Cloud CDN with XHTTP `packet-up`, GET/header uplink, worked from an MTS SIM during an active restriction window.
- The same resource had working and non-working edges concurrently; pinning a verified edge removed a 20–35% stall.
- Selectel did not work: after a completed deployment its edge returned `403` for the resource's `selcdn.net` name and `404` for the custom domain, and served a rotating fallback certificate. Its FAQ says VPN and uncached dynamic proxy are unsupported.
- Gcore failed on consumer sources: WebSocket `101` reached the origin but zero application bytes flowed, while datacenter sources worked.
- Xray's HTTP/1.1 XHTTP client can negotiate h2 via ALPN and then write HTTP/1.1 framing, producing `malformed HTTP/1.x`.
- Provider attribution is a real failure mode: confirm the CNAME and console before assigning an edge symptom to a provider.

Verified locally on 2026-10-26 with Xray 26.3.27 and nginx 1.26.3:

- `vlessenc` keys are strings on both sides; the inbound accepts `decryption` only and rejects `encryption` on inbound clients. A generated pair carried a real XHTTP GET/header session.
- Captured bytes on the CDN-to-origin leg contained no target hostname, URL path, or payload marker; `X-Data-0` was ciphertext and the header block was about 3.1 KiB for a 3–4 KiB chunk.
- The `map`-based `X-CDN-Secret` gate returns `444` without the header, `204` with it, keeps `/health` and ACME open, and survives a duplicate header because nginx joins duplicates with `", "`.
- `keepalive_requests` and `keepalive_timeout` are accepted in both `server` and `upstream` contexts.

Verified in production on 2026-10-27 (Yandex Cloud CDN, Xray 26.10.21, nginx 1.26.3):

- Single-server mode carried live sessions: the same host was CDN origin and exit, with the edge fetching origin over HTTP/80 and Xray routing to `freedom`.
- A live edge injected the static `X-CDN-Secret` request header: `/_cdn_secret_probe` through the CDN returned `204` with `X-CDN-Secret-Seen: yes` while the same probe without the header returned `no`. The gate was enabled only after that signal.
- `vlessenc` works end to end through a real edge: 10/10 XHTTP GET/header sessions on a verified edge. A client that omits the key against an encrypted inbound fails at the first packet.
- Aggressive edge flapping persists after a resource change: during rollout, one edge still served the previous origin, one returned no response at all, and one carried 10/10 for the encrypted profile but 7–8/10 for an older xmux profile. Rank edges again after every origin switch.
- Origin switching on the resource itself takes about 15 minutes to reach the edge fleet; `active: true` is not evidence of rollout. Distinguish origins by a response only the new one returns.
- A resource update needs `origin-group create` in addition to `resource update`; a role can allow reading the group list and updating the resource without allowing `origin-group get`.
- A mobile share link is not the same artifact as the Xray client JSON. The verified link's `extra` blob has exactly five keys, renders `uplinkChunkSize` as a string, is percent-encoded twice, and keeps `mode` at the top level. A blob that differs in key set, value type, JSON whitespace, or encoding depth imports and then never connects.
- A new credential is safest as a new UUID on the proven path, with the inbound copied verbatim. The tunnel framing is what mobile clients reject, not the UUID.

Not verified, and not claimed:

- `vlessenc` on a mobile device. The encrypted profile was verified from a desktop through a live edge but did not establish on the target phone, while the plain-VLESS profile on the same path did. The two profiles also differed in the share-link shape, in the pinned edge, and in the inbound, so the encryption was not isolated as the cause. Keep plain VLESS as the default and a `none` fallback profile until the target device passes.
- That every edge in the fleet injects `X-CDN-Secret`. Test the edges you pin, and re-test after rotation, before relying on the gate.
- `keepalive_requests 100000` as an optimum under a long production soak.
- Yandex WebSocket (support enablement required; the tested edge passed no Upgrade).
- VK: its CDN terms prohibit proxy/VPN use and traffic masking. Timeweb and Beget: provider notes only; no tested preset is shipped and this field study did not validate them.
- Any general statement that a CDN hostname is on a carrier whitelist. One hostname passed in one region and one window; re-test.

The repository intentionally contains no live edge IPs, UUIDs, origin addresses, resource IDs, or provider ticket numbers. Edges rotate and credentials are not documentation.

## Operational rules

- Pin a verified edge, keep the CDN hostname profile as fallback, and re-test after rotation.
- Keep the origin on HTTP/80 correct even if the panel claims HTTPS; verify in the access log.
- Set `X-CDN-Secret` on the CDN resource and gate only the tunnel paths; leave `/health` and ACME open. Protect the 443 tunnel block with the same gate.
- Enable `vlessenc` only when the target client core supports it, and keep a `none` fallback profile during rollout.
- Disable caching and buffering at the edge, in nginx, and in the response headers.
- Keep XHTTP settings identical on both ends; generate them from one preset.
- For a mobile share link, keep the `extra` blob identical to a link that is known to work on the target client; compare them byte for byte.
- Keep request headers under the edge limit (3–4 KiB chunks, 100–1000 bytes of padding).
- Test from a clean vantage point. Fake-IP DNS, local proxies, and datacenter sources can invalidate results.
- Accept only after a real device on the real SIM succeeds, during an active restriction window if that is the claim.
- Store profiles and keys mode `0600` outside the repository. Rotate the CDN secret and the `vlessenc` pair separately.

## Safety and terms

Use this on infrastructure you own or are authorized to administer. Check the CDN provider's current terms before carrying VPN traffic; several providers explicitly prohibit it, and a resource can be blocked for it. The scripts make a small number of requests to the hosts you name. Do not use them to scan or attack systems you do not own.

## Development

```bash
bash tests/run.sh
```

The test suite runs shell syntax checks, Python compilation, unit tests, a secret scan, an nginx configuration and secret-gate test (Docker `nginx:1.26.3`, or a local nginx for syntax only), and, if `xray` is installed, validates a generated client config, a `vlessenc` pair, and the inbound example (including a check that Xray actually keeps the XHTTP keys). The CI workflow runs the same checks plus `shellcheck`. Set `SECRET_SCAN_DENYLIST` to a private regex list to catch your own identifiers before publishing.

## License

MIT. See [LICENSE](LICENSE).
