# Reconciliation laboratory

`billcommons_shared.reconciliation` compares a recorded official legislative
event fixture to a recorded local event fixture. It is a deterministic,
read-only laboratory tool for adapters and historical audits. It makes no
network calls, reads only the two paths explicitly supplied to its CLI, opens
no database, and never proposes or applies a production mutation.

The tool reconciles event **occurrences**, not bill importance or policy
content. It does not score, rank, prioritize, or make recommendations about
bills, legislators, parties, or policies.

## Fixture contract

Each input is a JSON array of event objects, or an object containing an
`events` array. Preserve the complete source object in each event; the report
returns it as `raw_evidence` alongside its normalized comparison evidence.

An event is matched only with an explicit, shared identity:

```json
{
  "occurrence_id": "ca-leginfo:AB-17:action:892",
  "event_type": "committee_referral",
  "description": "Referred to Committee on Rules",
  "date": "2026-01-15",
  "chamber": "lower",
  "stage": "committee",
  "source_url": "https://leginfo.legislature.ca.gov/...",
  "raw_capture": {"adapter": "ca", "retrieved_at": "2026-01-16T02:00:00Z"}
}
```

Use either `occurrence_id` or `source_identity`; values must be stable within
the source namespace and appear unchanged in both fixtures. `occurrence_id`
is preferred. When neither fixture supplies `jurisdiction`, `session`,
`bill_id`, or `source_namespace`, the chosen identity must be globally unique
across those scopes. When a scope field is supplied, the comparator treats a
different known value as conflicting evidence and an absent counterpart as
uncertain evidence. Text, a date, a chamber, or an input-array position never
form an identity. If an upstream source cannot provide an occurrence identity,
keep that field absent: the result is `ambiguous_identities`, never a guessed
merge.

Dates accept `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`. A month and a day inside that
month are compatible but still imprecise evidence, so their comparison is
`uncertain_evidence` unless both values and precisions are identical. Missing
core date or description evidence also becomes `uncertain_evidence`; a true
mismatch requires contradictory known evidence.

## Run it

```bash
python -m billcommons_shared.reconciliation \
  --official fixtures/official-actions.json \
  --local fixtures/local-actions.json > reconciliation-report.json
```

The result has these categories:

| Field | Meaning |
| --- | --- |
| `matched` | One explicit occurrence identity on each side with compatible evidence. |
| `missing_from_local` | An identified official occurrence is absent from the local fixture. |
| `local_only_not_deletion` | An identified local occurrence is absent from the official fixture. This is an observation, never a deletion instruction. |
| `mismatched_evidence` | The same explicit occurrence has contradictory known evidence. |
| `uncertain_evidence` | The same explicit occurrence has missing or imprecise evidence, so no contradiction is asserted. |
| `ambiguous_identities` | Identity is absent or duplicated; all duplicate raw occurrences remain visible. |

The tool rejects malformed JSON, malformed dates, non-object events, a fixture
larger than 2 MiB, an event larger than 16 KiB, more than 1,000 events per
side, and reports larger than 8 MiB. Those limits prevent a broken adapter
fixture from making the audit process unbounded; they do not alter source data.

## Adapter-lab workflow

1. Record a small official response and the matching local export as fixtures.
2. Run the comparator before and after an adapter change.
3. Inspect `missing_from_local`, `mismatched_evidence`, and
   `ambiguous_identities` with their retained raw evidence.
4. Add the case to adapter-specific tests before any ingest change.
5. Let the ingest owner decide on a bounded repair and verify it against the
   same fixtures.

This laboratory is the shared foundation for state adapters. It does not claim
that an official snapshot authorizes deleting local history: a source can be
partial, revised, or unavailable.

Florida Senate bill-history observations always retain one exact source page
and never mutate the corpus. A comparison is permitted only when the retained
source year maps to the exact local session identifier ``YYYY Regular Session``
with ``regular`` classification and exactly one normalized base ``HB``/``SB``
bill in that session. A missing or ambiguous mapping is recorded as a partial
run; it never falls back to the same bill number in another session. The
Florida comparator compares a multiset of exact day, chamber, and normalized
description. Source row and bullet positions, local record IDs, and upstream
IDs remain evidence only, never occurrence identities. Local actions without
an organization linked to the same jurisdiction with `lower`/`upper`
classification stay ambiguous; display names do not determine chamber. Bulk
imports preserve explicit, resolvable source jurisdiction IDs and reject
conflicting attribution before changing organization rows. They never infer
jurisdiction from the selected session. A
cross-jurisdiction organization rejects the comparison. Agreement does not
prove an occurrence match, source completeness, statewide coverage, freshness,
or authorize an insertion or deletion. Version two adds separate shared-content,
overlap-record and surplus-record counters so unequal duplicate counts expose
their overlap. Existing agreement counters still count equal-multiplicity
content groups. Ambiguous records are excluded from these counters, and a
surplus does not prove an absent occurrence. Version-one reports retain their
original shape and replay path. Replay parses the retained page and
uses the recorded local snapshot and diff only; it never reads current corpus
actions. The current 2025 HB 7031 target remains a partial comparison until a
matching ``2025 Regular Session`` local bill exists.

### Ambiguous-record shapes in schema version 1

Treat `ambiguous_identities` as a union discriminated by `reason`. For
`duplicate_explicit_identity`, `identity` is present and both `official` and
`local` are lists (either may be empty). For `missing_explicit_identity`,
`identity` is null and exactly one side key is present; its value is a single
event object. Consumers must branch on `reason` before iterating either side.
This preserves the existing version-1 output and retained replay bytes.
