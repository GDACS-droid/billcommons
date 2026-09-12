# Official source response deadlines — September 12, 2026

The Montana landing-page observer received robots successfully but repeatedly
timed out waiting for homepage response headers. A local probe reproduced
`request_timeout` after 5.40 seconds. An isolated ten-second read experiment
completed the permitted page request in 12.53 seconds overall.

The exact reviewed homepage `https://www.legmt.gov/` now selects a ten-second
response-read timeout. Robots, other paths, other hosts and webhook callers
retain the five-second default. Connection, TLS-handshake and send deadlines
are unchanged, as are the fifteen-second total budget, HTTPS admission, DNS/IP
pinning, redirect rejection, certificate verification and response byte caps.
The factory accepts only finite positive read timeouts up to fifteen seconds;
there is no environment or source-payload override.

## Total-deadline defect and fix

The source investigation exposed a pre-existing shared transport bug. A timeout
set before `HTTPResponse.begin()` or `HTTPResponse.read(8192)` does not cover all
their internal receives: a server can keep each receive progressing while one
high-level call runs beyond the overall deadline. Root reproduced the existing
body-drip scenario taking **2.471 seconds** to fail against a **0.6-second**
budget. Independent header-drip reproduction took 1.232 seconds against a
0.3-second budget.

The transport now gives `HTTPResponse` a buffered reader over an unbuffered
socket file. Before each underlying read, it recalculates the remaining total
budget and applies the smaller of that value and the per-read timeout. The
response file is explicitly closed on success and failure, followed by the
socket. This closes both the header- and body-drip gaps without disabling
ordinary buffering or retaining an open socket descriptor after failure.

Webhook delivery compatibility remains explicit: when `require_body=false`
and complete response headers already establish a successful status, a body
timeout still returns that status with `body=None`. A timeout before complete
headers, or a required-body timeout, remains a failure.

## Local evidence and limits

The shared SafeHTTP/TLS suites passed 78 tests, including wall-clock assertions
for header/body drips, descriptor closure, unchanged default reads, bounded
overrides, and delivery/challenge behavior. Log:
`/tmp/bc_deadline_safe_http_root_20260912.log`. A local test-server request
diagnostic appeared; pytest passed.

The disposable PostgreSQL consumer run passed 58 ingestion/discovery/repair
tests, 68 webhook API/contract tests, and 85 dispatcher tests. Log:
`/tmp/bc_http_consumers_root_pg_20260912.log`. It also exposed a Scout test
fixture race: parallel SQLite sessions shared a single physical connection.
The fixture now uses separate connections to a unique temporary file per
runner. Its final suite passed 107 tests, with one separately gated PostgreSQL
test skipped and 24 deprecation warnings. Log:
`/tmp/bc_scout_runner_connections_final_20260912.log`. The first fixture draft
reused a filename across two runners in one test and failed two cases; the
unique filename preserves their original isolation.

The separately gated source-history concurrency test then passed against an
owned disposable PostgreSQL 16 database with two synchronized writers. Log:
`/tmp/bc_scout_pg_source_history_20260912.log`. All temporary PostgreSQL clusters
were dropped. This closes that skip for the focused final validation.

The final production-code path passed a local robots-respecting Montana probe:
robots 200, page 200, 20 bounded links, 2,002,734 response bytes, and a 12.36-second
page request. No transport constants were monkeypatched in that final probe.
The exact response bytes, hashes and elapsed times are retained in
`~/.local/share/billcommons/reliability-release-20260908/mt-final-transport-probe-20260912.json`.
The page is close to the unchanged 2 MiB cap; later growth may correctly fail
that bound. This is landing-page discovery, not statewide semantic freshness.

Idaho separately passed using the unchanged defaults; no Idaho-specific
exception or production backoff reset was made. Neither these local probes
nor the test results are deployment proof or a canonical SHIP verdict.
