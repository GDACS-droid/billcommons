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
