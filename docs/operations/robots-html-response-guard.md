# HTML robots-response guard — September 12, 2026

Hawaiʻi observation `310a1654-275c-4628-8f8a-4d8974f0651e` recorded a
2,337-byte HTTP-200 robots response before a homepage HTTP 403. Its retained
robots blob is actually an XHTML document beginning with `<!DOCTYPE html`.
The verified SHA-256 is
`4406aa2ad72f5ba8960f150118e4f12a8d3676a94159d6b532705ac3c47dbcd4`.

An offline replay of those exact bytes reproduced the problem: `RobotFileParser`
found no rules in the HTML, and capture attempted the homepage. The local fix
recognizes an HTML doctype or HTML root tag at the start of an HTTP-200 robots
body, after a UTF-8 BOM and leading whitespace. It returns
`robots_html_response` before policy parsing or material-request admission.
The response status and exact original policy bytes remain available for
evidence storage. BOM decoding also prevents a BOM from hiding an actual first
`User-agent` directive and its `Disallow` rule.

This is a narrow document-prefix guard, not a complete robots-format or HTML
validator. It deliberately preserves empty/comment-only policy behavior,
ordinary directives, HTML-looking text inside a robots comment/path, and the
existing HTTP-404/410 absence policy. HTTP 403/503 remains a failure. A MIME
label alone does not determine the result, preserving existing plain policies
served with an incorrect media type. No redirect behavior changed.

The guard applies to the common reviewed HTML capture path used by landing-page
discovery and the bounded Florida detail capture. The Florida parser and its
semantic replay versions are unchanged. This is an access-policy failure
classification, not a claim of new or removed legislative facts.

## Evidence and validation

The exact retained Hawaiʻi response now yields one robots request, no material
request and `robots_html_response` in an offline replay. No live Hawaiʻi request
was made for this investigation. Evidence under
`~/.local/share/billcommons/reliability-release-20260908/`:

- `hi-observations-readonly-20260912.json`
- `hi-retained-robots-20260911.bin`
- `hi-robots-baseline-replay-20260912.json`
- `hi-robots-fixed-replay-20260912.json`

The disposable PostgreSQL run passed **96 tests** across discovery, Florida
capture, observer and worker behavior. A separate focused database run passed
**four tests**, including actual capture-to-observation persistence of the
HTML failure, exact blob hash, absent page evidence, unestablished freshness
and scheduled retry. Logs:

- `/tmp/bc_robots_policy_root_pg_final_20260912.log`
- `/tmp/bc_robots_persistence_root_pg_20260912.log`

Both clusters were dropped. The initial database command used `pg_virtualenv`
`-p` as though it selected a port; it actually selects a package. The explicit
port guard refused that cluster before tests ran, and it was dropped. The
corrected command used `-c '-p 55493'`. The first replay invocation also refused
an import from the original checkout because the explicit schema path was
missing; the retained successful replay uses this worktree's schema and code.

Manual adversarial checks cover misleading MIME labels, BOM/whitespace/case,
HTML fragments in valid comments/paths, empty policies, HTTP status boundaries
and durable no-page evidence. This change has **not** received an independent
canonical review and remains undeployed. The earlier transport HALT verdict
does not cover or approve this later change. No new review loop is planned
before the user's 03:00 Eastern stop point.
