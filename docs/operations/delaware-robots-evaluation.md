# Delaware robots evaluation — September 12, 2026

Goal: determine whether a bounded robots-only redirect exception could safely
recover Delaware's current landing-page discovery failure.

## Observed response chain

At 06:43–06:44 UTC, bounded read-only policy diagnostics established:

| Policy resource | HTTP response | Destination or representation |
| --- | --- | --- |
| `https://legis.delaware.gov/robots.txt` | 302 | `/404?initialRequestUrl=https%3A%2F%2Flegis.delaware.gov%2Frobots.txt` |
| Same-host `/404` destination above | 301 | `https://legis.delaware.gov/Error/PageNotFound` |
| `/Error/PageNotFound` | 200 | `text/html; charset=utf-8`, 2,339 bytes, HTML prefix |

The terminal representation's SHA-256 is
`83dd6bfea732471dacaef88b6133528f4523104f1536c493f2aea15e3eacc25f`.
Its body was not used as a robots policy or legislative material. No homepage
or material request followed these diagnostics.

Each request used the existing SafeHTTP HTTPS admission, DNS/all-address
validation, IP pinning, TLS verification, response deadlines and 256 KiB body
cap. Redirect rejection stayed enabled. A diagnostic-only `HTTPResponse.begin`
wrapper recorded bounded status/Location metadata before the normal redirect
exception. Requests were explicit and separately vetted; no automatic
redirect-following mode was enabled. The first chain diagnostic stopped before
the newly advertised error route; that exact same-host policy destination was
then inspected separately. Host spacing was at least two seconds.

Evidence under `~/.local/share/billcommons/reliability-release-20260908/`:

- `de-robots-destination-diagnostic-20260912.json`
- `de-robots-chain-diagnostic-20260912.json`
- `de-robots-terminal-diagnostic-20260912.json`

## Decision and future constraints

The earlier inference that the first redirect reaches a terminal HTTP 404 is
not supported. It reaches a second redirect, then an HTML error page with HTTP
200. A one-hop exception would not recover the source; treating the route name
or a parsed HTML page with no robots rules as permission would be unsafe.
Keep Delaware's current discovery failure and the global redirect rejection.
No registry change, production target reset or runtime policy change was made.

RFC 9309 specifies a UTF-8 `text/plain` robots resource and recommends following
at least five redirects, including across authorities. Its treatment of an
unavailable resource is based on the response status, rather than an error-like
URL path. The current conservative redirect policy is therefore not a claim of
full RFC redirect support. See [RFC 9309 §§2.3–2.3.1.4](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3).

Any later robots-only implementation must distinguish a policy representation
from a branded HTML error response, preserve original-origin policy semantics,
revalidate every allowed destination and enforce aggregate request/time/byte
budgets. It must retain failure on an unestablished policy, including loops,
unreviewed destinations, downgrades, TLS/DNS failure, oversize responses and
this observed two-redirect/HTML-200 chain. Webhook and material-request redirect
behavior must remain unchanged. Tests need to prove no material request occurs
in each failure case. A reviewed valid robots resource or an explicitly
authorized source-access path is still needed to recover Delaware.
