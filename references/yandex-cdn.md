# Yandex Cloud CDN resource

This is the provider that passed the field test. The notes describe the resource shape that worked, the behavior that surprised us, and what to verify instead of assuming.

## Resource shape

- **Origin group**: one origin, the server that terminates XHTTP.
- **Origin protocol**: HTTP, port 80. The tested resource fetched origin over plain HTTP even when the panel reported HTTPS. Keep the port-80 nginx block correct.
- **Origin Host header**: the public CDN hostname (for example `cdn.example.com`). This is what makes nginx match the intended server block.
- **Public hostname**: the name the client uses for SNI and Host. The CNAME target is shown by the console; do not infer the provider from the suffix alone.
- **Allowed methods**: `GET`, `HEAD`, `OPTIONS` in the tested resource. A POST body returned `413` through the tested edge; a reference deployment reports `405` on POST. Add methods through the resource's HTTP method settings, and always send the full list when updating: an API PATCH replaces the whole list rather than merging it.
- **Caching**: off. Large-file segmentation: off. Compression/transformation: off. A cached or rewritten response breaks the tunnel.
- **Certificate**: a certificate for the public hostname, imported or issued through Certificate Manager. DNS-01 is the reliable path: create the TXT record at the DNS host and wait for the authoritative nameservers to serve it.
- **CNAME**: point the public hostname at the target the console shows. In the tested resource the target was a `*.topology.gslb.yccdn.ru` name. That suffix means Yandex, not Selectel; confirm with the console before diagnosing.

## Provisioning order

1. Create the origin group and origin. Use the origin's domain, not a bare IP, if the certificate and Host matching depend on it.
2. Create the CDN resource. Attach the origin group, set the public hostname, HTTP origin, Host header, methods, cache off, and the certificate.
3. Create the CNAME at the DNS host. Wait for authoritative nameservers to return it.
4. Wait for the resource to become active, then wait for propagation. Activation is a control-plane state; edge fleets roll out separately.
5. Run `cdn-probe.py dns` and `cdn-probe.py matrix`. Do not trust a single successful request.
6. Build the client profile only after at least one edge passes a short probe and an XHTTP session test.

## Edge behavior observed

- A resource can resolve to several IPv4 addresses at once. In the tested window, two edges consistently carried the session, one was partial, and one returned nothing useful. The domain profile therefore inherited a 20–35% stall rate.
- The stall is specific to long-lived sessions. Short unique-path requests passed quickly and appeared at the origin. This is how to distinguish "edge fleet does not carry the resource" from "edge is down".
- Pinning one verified edge IP in the client fixed the stall. The domain profile remained as a fallback, because pinned IPs rotate.
- Some edges returned `403` for the resource while others served it. Re-test before concluding the resource is misconfigured.
- The edge can serve a fallback or rotating certificate instead of the imported one. Certificate pinning (`pinnedPeerCertSha256`) is unreliable when the edge rotates certificates; validate by SNI and hostname instead.
- WebSocket is not a console toggle. Provider release notes say the feature is enabled through support. It was not tested in this field work.
- Origin access logs are the ground truth for "did the edge route this request". Log the Host, method, status, request length, bytes sent, and upstream address.

## Useful console checks

- Resource status and the last configuration change.
- Origin protocol, port, and Host header.
- Allowed HTTP methods, read back after every change.
- Cache settings, query-string handling, and compression.
- Certificate name, SANs, and expiry.
- CNAME target and current DNS answers from more than one resolver.
- Resource metrics: request count, 2xx/4xx/5xx, traffic. A sudden 4xx spike usually means a method or Host change, not client breakage.

## What to ask support

Open one ticket with evidence, not a vague complaint:

- Resource ID and public hostname.
- The list of edge IPs that fail, with timestamps in UTC and the exact request method/path.
- The same request succeeding through a working edge, with the origin log line.
- The symptom class: short probes pass but long sessions stall, or the edge returns `403`, or the edge returns `101` and then no bytes.
- A request to enable WebSocket, if the design needs it.

Keep credentials, UUIDs, and private keys out of the ticket.

## Common mistakes

- Treating `*.topology.gslb.yccdn.ru` as a different provider. Check the console; the DNS suffix is not the billing entity.
- Patching the method list with only the new method. That removes `GET` and breaks XHTTP.
- Testing from a workstation whose DNS is hijacked by a local proxy. Resolve through public and authoritative resolvers, and pin the edge IP.
- Declaring success after the resource turns active. Edges roll out later.
- Assuming HTTPS-to-origin. Verify the protocol in the access log; the tested edge used HTTP/80.
- Leaving a domain profile as the only profile. It includes dead edges by design.
