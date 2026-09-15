# Florida Senate bill-text versions

Scout may retain one direct Florida Senate bill-text-version PDF after a
successful primary bill-page capture. Discovery accepts only the official,
bill-scoped route:

`/Session/Bill/{session}/{bill}/BillText/{safe_token}/PDF`

The host, session, bill number, and one route token are validated before the
request. The token is a label for that endpoint only. Scout does not call it a
filed, engrossed, enrolled, substituted, or other legislative stage unless the
retained PDF itself supports such a claim.

This is a distinct immutable job limit, `max_related_bill_versions`, capped at
one. It follows the primary page, up to two analyses/amendments, and one vote
record, so a normal Florida job can make at most five direct requests. Missing
or legacy limits authorize no bill-text fetch. The attachment requires both
`application/pdf` and PDF magic bytes, extracts text under the existing PDF
ceilings, and must include the bill identifier before Scout creates a finding.
Failures remain failed source observations; they never trigger browser routing.

## Read-only primary-source check

On 2026-09-08, a bounded read of the Florida Senate's
[`HB 625` page](https://www.flsenate.gov/Session/Bill/2026/625) identified the
canonical bill-text link
[`/Session/Bill/2026/625/BillText/er/PDF`](https://www.flsenate.gov/Session/Bill/2026/625/BillText/er/PDF).
The page was `text/html` with SHA-256
`65d85b400e8ecabdf1a4c1950db55e97e3f46023ac72e32b68bf6687eb38f469` at that
read. The linked 97,536-byte response declared `application/pdf`, began with
the PDF magic bytes, and had SHA-256
`eebb950a601a89a16118bebd3667de5e03e203492fef19b8bffe4bbaf6a6f65a`.

Bounded extraction found `CS/CS/HB 625`; its extracted-text SHA-256 was
`a3f2c71fd5f729bc6a5ce570aedef32f7ecd2d3782f3f9cf7a7ef3424ae9c897`.
The URL token is `er`. These hashes record one observed response, not a claim
that the current page or PDF will remain unchanged.

## Cache and monitor integration

Florida's cache namespace is `scout-p0-5-meeting-documents`, so completed
results from earlier namespaces are retained for audit but never returned as
fresh results. Explicit meeting queries now use the
[meeting-document path](FLORIDA_MEETING_DOCUMENTS.md). In the saved-monitor integration,
the shared `scout_admission._limits` snapshot includes
`max_related_bill_versions`, so API-created and scheduler-created jobs retain
the same immutable allowance.
