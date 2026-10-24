# CDN XHTTP VPN

An agent skill and a small toolkit for carrying a VLESS/XHTTP tunnel through a CDN edge when the direct server path is blocked or throttled. Yandex Cloud CDN is field-verified; the repository also records why Selectel and Gcore failed and what to check before trying another provider.

The skill is written for coding agents (Claude Code, Codex, Qoder, and compatible runtimes) and is also usable by a human operator. It contains the playbook, reference notes, and dependency-light scripts that turn guesses into measurements.

## Architecture

```text
Client (Happ / v2RayTun / Hiddify)
  -> TLS 443, address = pinned edge IP or CDN hostname, SNI = CDN hostname
  -> CDN edge
  -> origin :80 (plain HTTP in tested setups) nginx
  -> Xray VLESS/XHTTP inbound on 127.0.0.1
  -> VLESS/REALITY cascade to an exit server
  -> Internet
```

The CDN changes the public IP and the network path. It does not guarantee that a carrier whitelist will allow the hostname, and it does not make the tunnel fast. Reachability is the goal; speed is secondary.

## What is inside

| Path | Purpose |
|---|---|
| `SKILL.md` | The agent playbook: rules, provider gate, workflow, symptom table, evidence levels |
| `references/yandex-cdn.md` | Resource shape, methods, certificate, CNAME, edge behavior |
| `references/origin-setup.md` | nginx port 80/443, buffers, Xray inbound, logging |
| `references/xhttp-transport.md` | Modes, header budget, full `extra` object, share-link fields |
| `references/diagnostics.md` | Test ladder, vantage contamination, log correlation |
| `references/providers.md` | Yandex, Selectel, Gcore, VK, Timeweb, Beget comparison |
| `references/operations.md` | Backups, secrets, rotation, monitoring, incident runbook |
| `scripts/cdn-probe.py` | DNS, TLS, per-edge matrix, methods, cache checks (stdlib only) |
| `scripts/edge-test.sh` | End-to-end XHTTP session test per edge, with PID cleanup |
| `scripts/origin-check.sh` | Direct origin control: health, header budget, TLS, methods; HTTP/80 or HTTPS |
| `scripts/make-profile.py` | Share URI and matching Xray client config from one preset |

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

# 1. Discover the edges the CDN returns and flag fake-IP resolvers.
python3 scripts/cdn-probe.py dns --host cdn.example.com
python3 scripts/cdn-probe.py matrix --host cdn.example.com --path /api-stream --attempts 3

# 2. Create a UUID outside the repository, then build a pinned profile.
install -d -m 700 ~/.config/vpn-cdn
(umask 077; xray uuid > ~/.config/vpn-cdn/uuid)
python3 scripts/make-profile.py \
  --address 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid \
  --preset yandex-get --name 'CDN pinned' \
  --out ~/.config/vpn-cdn/pinned.vless \
  --client-config ~/.config/vpn-cdn/pinned.json

# 3. Prove the session end to end.
bash scripts/edge-test.sh \
  --edge 198.51.100.20 --sni cdn.example.com --path /api-stream \
  --uuid-file ~/.config/vpn-cdn/uuid --attempts 5
```

`edge-test.sh` exits `0` for `LIVE`, `1` for `PARTIAL`, `2` for a setup error, and `3` for `DEAD`. Use `--dry-run` to validate the generated Xray config without sending traffic, and `--json` for machine-readable output.

## What is verified, and what is not

Verified in the October 2026 field work:

- Yandex Cloud CDN with XHTTP `packet-up`, GET/header uplink, worked from an MTS SIM during an active restriction window.
- The same resource had working and non-working edges concurrently; pinning a verified edge removed a 20–35% stall.
- Selectel did not work: after a completed deployment its edge returned `403` for the resource's `selcdn.net` name and `404` for the custom domain, and served a rotating fallback certificate. Its FAQ says VPN and uncached dynamic proxy are unsupported.
- Gcore failed on consumer sources: WebSocket `101` reached the origin but zero application bytes flowed, while datacenter sources worked.
- Xray's HTTP/1.1 XHTTP client can negotiate h2 via ALPN and then write HTTP/1.1 framing, producing `malformed HTTP/1.x`.
- Provider attribution is a real failure mode: confirm the CNAME and console before assigning an edge symptom to a provider.

Not verified, and not claimed:

- Yandex WebSocket (support enablement required; the tested edge passed no Upgrade).
- VK: its CDN terms prohibit proxy/VPN use and traffic masking. Timeweb and Beget: provider notes only; no tested preset is shipped and this field study did not validate them.
- Any general statement that a CDN hostname is on a carrier whitelist. One hostname passed in one region and one window; re-test.

The repository intentionally contains no live edge IPs, UUIDs, origin addresses, resource IDs, or provider ticket numbers. Edges rotate and credentials are not documentation.

## Operational rules

- Pin a verified edge, keep the CDN hostname profile as fallback, and re-test after rotation.
- Keep the origin on HTTP/80 correct even if the panel claims HTTPS; verify in the access log.
- Disable caching and buffering at the edge, in nginx, and in the response headers.
- Keep XHTTP settings identical on both ends; generate them from one preset.
- Keep request headers under the edge limit (3–4 KiB chunks, 100–1000 bytes of padding).
- Test from a clean vantage point. Fake-IP DNS, local proxies, and datacenter sources can invalidate results.
- Accept only after a real device on the real SIM succeeds, during an active restriction window if that is the claim.
- Store profiles mode `0600` outside the repository.

## Safety and terms

Use this on infrastructure you own or are authorized to administer. Check the CDN provider's current terms before carrying VPN traffic; several providers explicitly prohibit it, and a resource can be blocked for it. The scripts make a small number of requests to the hosts you name. Do not use them to scan or attack systems you do not own.

## Development

```bash
bash tests/run.sh
```

The test suite runs shell syntax checks, Python compilation, unit tests, a secret scan, and, if `xray` is installed, validates a generated client config and the inbound example (including a check that Xray actually keeps the XHTTP keys). The CI workflow runs the same checks plus `shellcheck`. Set `SECRET_SCAN_DENYLIST` to a private regex list to catch your own identifiers before publishing.

## License

MIT. See [LICENSE](LICENSE).
