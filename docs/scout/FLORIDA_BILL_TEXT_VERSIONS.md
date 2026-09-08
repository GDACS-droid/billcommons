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
