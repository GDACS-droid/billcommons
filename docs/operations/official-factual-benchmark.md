# Offline official factual benchmark

`official_factual_benchmark` is a bounded offline regression runner for
directly asserted facts from reviewed official-source fixtures. It is intended
to be runnable by a CI or nightly shell job, but this repository does not
install, schedule, or deploy such a job.

Run the checked-in Florida case from the repository root:

```sh
PYTHONPATH=workers/ingest python3 -m billcommons_ingest.official_factual_benchmark \
  workers/ingest/tests/fixtures/official_factual_benchmark.json
```

The command writes one deterministic JSON report to standard output. It exits
zero only when every asserted fact passes; it exits one for a factual or pinned
fixture-hash mismatch and two for an invalid manifest or unsafe fixture path.
It performs no HTTP requests, corpus or database reads, source capture, parser
mutation, or schedule writes.

## Current scope

The version-one manifest contains one Florida (`FL`) Senate bill-history case:
2025 HB 7031. Its fixture is a **derived public fixture**, rather than the full
captured response. The manifest pins the derived fixture's SHA-256 and records
the original response SHA-256 only as provenance copied from the fixture's
capture comment. The original response bytes are not retained here, so the
runner does not and cannot independently verify that recorded original hash.

The runner reports that distinction explicitly. A passing result proves only
that the current parser extracts the declared direct facts from the exact
pinned derived fixture. It does not prove source freshness, agreement with a
Bill Commons corpus, an inferred canonical status, an amendment count beyond
the source's action wording, complete jurisdiction coverage, a 50-state
benchmark, or that an operational nightly run took place.

## Manifest contract

The manifest is UTF-8 JSON with `schema_version: 1` and
`benchmark_version: "official-factual-benchmark/1"`. It accepts only the
`fl-senate-bill-history` adapter and `FL` jurisdiction. Every fixture path is
relative to the manifest directory; absolute paths, traversal, symlinks, and
non-regular files are rejected before fixture bytes are read. Duplicate JSON
keys and non-finite JSON numbers are rejected.

The supported direct fact fields are:

- `identity`: session year, bill number, and displayed bill identifier.
- `title`, `table_row_count`, and `action_count`.
- `final_action`: one exact `{date, chamber, description}` source tuple.
- `contains_action`: one exact `{date, chamber, description}` source tuple.

The initial case uses `contains_action` for the direct Senate Appropriations
referral, the Appropriations action that states an amendment and its vote, and
a House conference-report vote. These are exact source strings, not derived
status or amendment-count claims.

To add a later reviewed adapter, add a narrowly scoped parser-to-fact adapter,
then extend the manifest validation and focused tests with a captured fixture
and hashes. Do not turn this runner into a source fetcher or corpus comparison.
