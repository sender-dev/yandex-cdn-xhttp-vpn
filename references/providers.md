# Provider comparison

The same architecture behaves very differently across CDN providers. This is what the field work found, with the caveat that provider behavior changes. Re-run the gate before committing.

## Summary

| Provider | Result for this workload | Edge behavior seen | Main blocker |
|---|---|---|---|
| Yandex Cloud CDN | Worked, with per-edge pinning | Mixed fleet; some edges carry sessions, some do not; POST body `413` through the tested edge | Uneven edges; WebSocket only through support |
| Selectel | Did not work | `403` for the resource's `selcdn.net` name, `404` for the custom domain after a clean deployment; rotating fallback certificate | Edge does not route the resource; terms say VPN/dynamic proxy unsupported |
| Gcore | Did not work on consumer sources | Datacenter worked; consumer TLS completed then blackholed; WS `101` but zero bytes | Source-based edge policy |
| VK Cloud CDN | Not viable on terms | VK Cloud CDN terms prohibit proxy/VPN use and traffic masking | Terms block this workload even before a technical test |
| Timeweb CDN | Not field-verified | Plausible: GET + body placement, larger chunks | Unknown |
| Beget CDN | Not field-verified | Plausible: GET + query session, `segment0.ts` path | Unknown |

"Not field-verified" means the configuration is plausible and recorded as a hypothesis, not that it passed a phone test. Do not present it as working.

## Yandex Cloud CDN

**Verdict:** viable when the transport is GET/header and the client pins a healthy edge.

What worked:

- Origin over HTTP/80, Host header set to the public hostname.
- Methods `GET`, `HEAD`, `OPTIONS`; no POST.
- Cache disabled, no transformation.
- Certificate for the public hostname, imported or issued through Certificate Manager.
- XHTTP `packet-up`, GET, header placement, 3–4 KiB chunks.
- Pinned edge IP in the client, domain profile as fallback.

What to watch:

- The fleet is uneven. In one window, two edges were consistently good, one partial, one useless. The domain profile inherits the bad ones.
- Edges rotate. Re-run discovery and session tests periodically.
- WebSocket is available only through a support request, according to the provider's release notes. It was not tested.
- The edge can serve a fallback certificate. Do not pin certificate hashes.
- The edge can reject a request that looks different from the tested pattern. Keep the profile stable once it works.

## Selectel

**Verdict:** not viable for this workload, and the terms are a separate reason to stop.

What was observed after a clean deployment that matched a public video guide:

- XHTTP GET uplinks received edge-local `403` while visually identical manual requests passed.
- The edge served a rotating fallback certificate instead of the imported one.
- The resource's own `selcdn.net` hostname returned `403`, and the custom domain returned `404`, after the deployment reported `Completed`.
- An API PATCH that replaced `allowed_http_methods` with `["POST","DELETE"]` removed GET and broke the tunnel. Always send the complete method list.

Some earlier edge observations in this study — stripped WebSocket `Upgrade` headers, `413` on a POST body, origin fetched over plain HTTP/80 — were recorded through the public hostname before its DNS was audited. That hostname was later found to terminate at a different provider's edge, so those observations are documented as behavior of the tested CDN path, not as Selectel facts. This is the attribution trap in practice: check the CNAME and console before blaming a provider.

The provider's FAQ explicitly says VPN and uncached dynamic reverse-proxy traffic are unsupported and may lead to restrictions. Even if the edge behavior were fixed, the workload would remain outside the supported use.

## Gcore

**Verdict:** not viable on consumer networks in the tested window, despite good documentation.

What was observed:

- The resource activated, the certificate issued, and the CNAME resolved.
- From a datacenter source, the full chain worked repeatedly.
- From residential and mobile sources, the TLS handshake completed with the correct certificate, then the first HTTP request inside the connection was silently dropped.
- WebSocket handshakes that reached the origin returned `101`, but nginx counted zero application bytes on phone connections versus several kilobytes on server-test connections.
- DNS returned an edge that did not serve the resource while another edge did.
- A reconnect storm from the phone plausibly triggered anti-abuse handling.

This is an edge policy difference between source classes, not a client configuration error. Escalate with source-class evidence before spending more time on tuning.

## VK, Timeweb, Beget

These providers have plausible configurations, but this repository ships no preset for them and this field study did not test them end to end. Treat them as hypotheses:

- **VK:** current VK Cloud CDN terms prohibit proxy/VPN use and traffic masking. Do not carry this workload on VK without a written exception; the technical notes below are for completeness only. GET with body placement, session and sequence in query, moderate chunks (100–300 KB range). GET with a body is unusual; verify the edge does not reject it.
- **Timeweb:** GET with body placement, session in query, larger chunks (400–800 KB). The larger body may hit edge buffering.
- **Beget:** GET with body placement, query session, an HLS-like path (`segment0.ts`), small padding. A session in the path can turn the URL into a directory-like path that some edges reject, so the session belongs in the query.

For any of them, run the same gate: method allowlist, short probe, session test, origin correlation, consumer-source test.

## Provider gate checklist

Use this before building on a new provider:

- [ ] Terms permit VPN or dynamic reverse proxy, or you accept the risk of a later block.
- [ ] GET is allowed at the edge.
- [ ] Origin can be fetched over HTTP/80 with a Host override, or HTTPS with a valid certificate and SNI.
- [ ] Cache can be disabled; responses are not transformed.
- [ ] POST body behavior is known (allowed, `405`, or `413`).
- [ ] WebSocket behavior is known if the design uses it.
- [ ] The edge fleet serves the resource from more than one address.
- [ ] Consumer-source behavior matches datacenter behavior, or you have a plan for the difference.
- [ ] The certificate covers the public hostname.
- [ ] There is a support channel for broken edges and enablement requests.
- [ ] The pricing model is understood for long-lived tunnel traffic.

## Legal and terms

A CDN is a content-delivery product. Carrying a VPN tunnel is not the intended workload for most providers, and several say so explicitly. A resource can be limited or blocked for it. Check the current terms in the provider's own documentation, keep the traffic within what you are authorized to do, and do not use a provider's network to attack or evade controls on systems you do not own.

Provider sources to check before building:

- [Selectel CDN FAQ](https://docs.selectel.ru/migrations/migrate-to-cdn/faq-cdn/) — VPN and uncached dynamic reverse proxy are unsupported.
- [VK Cloud CDN terms](https://cloud.vk.ru/docs/ru/start/legal/offer/private-special-conditions/policy-cdn) — proxy/VPN use and traffic masking are prohibited.
- [Yandex Cloud CDN documentation](https://yandex.cloud/ru/docs/cdn/) — review the current acceptable-use terms; WebSocket enablement is a support request.
