# Legislative intelligence show-and-tell

Checked September 12, 2026, at approximately 15:09 UTC. This guide distinguishes
the deployed product from unreleased engineering work. The full autonomous
50-state intelligence objective remains incomplete.

## Five-minute product demonstration

1. Open https://billcommons.org/data-health. Explain that the system inspects
   ingestion across 50 states plus DC and reports stale syncs, dead jobs and
   suspected queue stalls. At this check it reported 19 defects: two Alaska
   errors and 17 overdue-sync warnings. API health and readiness both passed.
   Service availability and data freshness are separate claims.
2. Inspect the official-source evidence on the report. There are 59 registered
   targets across 51 jurisdictions: 49 observed and 10 failed at this check.
   California and Florida observations have retrieval times after the overnight
   development pause, demonstrating that the deployed observer kept running.
   Landing-page discovery is not statewide semantic coverage.
3. Show the Florida HB 7031 / 2025 source-history observation. Its September 12
   10:02 UTC capture reports 46 actions. Download the retained response using
   the evidence API below and recompute its hash. This proves the bytes behind
   the observation; it does not prove agreement with every local bill record.
4. Open https://billcommons.org/scout. Previously exercised product examples are
   Florida HB 625 / 2025 (including an official vote PDF) and California AB 1039 /
   2025-2026 (retained official archive action). These are bounded research
   examples. This resumption checked the page's HTTP availability, not a new
   authenticated submission. Previous rendered desktop/mobile evidence is
   recorded in `autonomous-intelligence.md`.

Public Florida response:

https://api.billcommons.org/api/v1/official-evidence/blobs/55621db4e261bd090917ced442b7fc6f7602a1a43ed2c7dc9aff9101dbda190f

The response returned HTTP 200 and 140,611 bytes. Its independently recomputed
SHA-256 exactly matched the URL digest at 15:09:35 UTC.

## New local engineering demonstrations

- **Florida reconciliation:** compares retained official bill history with a
  local snapshot and preserves replay evidence, chamber attribution, action
  ordering and duplicate multiplicity. Integrated, not deployed.
- **California repair lab:** turns a retained parse failure into a versioned,
  hash-bound fixture, manifest and executable regression. The retained real
  archive yields 275 scoped bills and 7,094 events; the content sample covers
  20 bills. Its generated offline regression passed again on this resumption.
  It does not author or promote parser patches automatically.
- **Factual regression:** the checked-in Florida derived fixture passed all
  eight declared fact assertions again. This is one offline case, not a live
  or nightly 50-state factual benchmark.
- **Failure isolation and access fixes:** local Alaska overflow isolation lets
  healthy bills commit while retaining blockers and withholding a successful
  watermark. Other local fixes cover Illinois's missing TLS intermediate,
  California's six published weekday deltas, per-read HTTP deadlines, and
  rejection of HTML error documents masquerading as robots policy.

## Release boundary

The latest fixes are unreleased. Canonical review runs ended HALT and subsequent
focused checks do not turn those verdicts into SHIP. The prior production
precheck found an empty migration-version table; coordination with other
operators and fresh schema evidence are required before its prepared recovery
or deployment. The question was asked again on resumption and remains pending.
The configured source budget also falls below the modeled cadence demand.

Saved monitors, broader state-specific semantics, automatic parser patch
generation/promotion and operational live factual benchmarks remain unfinished.

## Evidence from this resumption

Under `/home/alberto/.local/share/billcommons/reliability-release-20260908/`:

- `resume-demo-public-check.json`: six public HTTP-200 responses, including
  the full Data Health and official-overview payloads.
- `resume-demo-fl-blob-proof.json`: public retained-response hash verification.
- `ca-retained-failure-bundle-v2-20260912/`: retained California regression.

The California test ran with the existing Bill Commons virtual environment,
explicit current-worktree imports, no conftest and no pytest cache: one passed.
The initial system-Python invocation lacked pytest and ran no tests.

The factual runner is reproducible from the repository root:

```sh
PYTHONPATH=workers/ingest python3 -m billcommons_ingest.official_factual_benchmark \
  workers/ingest/tests/fixtures/official_factual_benchmark.json
```
