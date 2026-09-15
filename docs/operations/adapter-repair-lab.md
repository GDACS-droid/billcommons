# CA adapter repair lab

This local review path turns one retained California **parse failure** into a
small evidence bundle. It is preparation for a human parser review. It does
not write a parser patch, retry an official target, enable a target, alter a
schedule, change corpus records, or promote a candidate.

Run it only against an explicitly selected read-only database session and an
explicit local output directory that is absent or empty:

```bash
python -m billcommons_ingest.official_repair_bundle OBSERVATION_UUID \
  --output-dir /absolute/local/path/ca-repair-bundle
```

The command refuses symlink and traversal output paths, non-empty destinations,
unverified retained bytes, unsupported adapters, capture/replay failures, and
records whose source URL or retrieval timestamp is not an exact reviewed CA
delta input. It stages the three output files beside the destination and only
publishes the directory once complete.

New artifacts use `official-repair-bundle/2`. Validation checks the historical
adapter, status, error class, observation identifiers, supersession fields,
diagnosis and provenance as well as the replay. The fixed generated regression
test pins the canonical manifest SHA-256, so editing historical metadata also
breaks validation. Duplicate JSON keys, non-finite numbers and excessive nesting
are rejected. Version 1 artifacts remain readable with their original fixed
test format; that format did not retain the historical error class or pin the
whole manifest, so it cannot provide those version 2 checks.

The CLI reports `manifest_sha256`. Retain that digest separately in a trusted
review record and pass it as `expected_manifest_sha256` to
`validate_repair_bundle` when verifying against that record. The generated test
and manifest establish internal consistency; replacing both together can only
be detected against a separately trusted digest. Offline validation does not
authenticate the originating database or rerun the historical parser.

`manifest.json` records the original observation as `recorded_before`, with
`recorded_not_rerun: true`, separately from the current candidate replay. It
contains the retained-fixture hash and size, the source hash of the **loaded
shared parser callable**, accepted or rejected replay evidence, bounded counts,
and a digest of a deterministic action-content sample (at most 20 bills and
50 actions per sampled bill). This sample detects changes in the sampled
content; it is not a full-corpus equivalence claim. The raw archive
is stored only as `retained-ca-archive.zip`; it is never printed by the command.

Old observations predate parser-source provenance. Their bundles remain useful
when their invalid status and `OfficialCaActionsError` support a CA parse
failure, but the manifest says `historical_parser_source_not_recorded`. The lab
never substitutes today's parser as an invented historical baseline.
If source fingerprinting is unavailable during a new observation, the original
parse failure and its backoff still persist; provenance is explicitly unavailable.
Both the latest eligible failure and a superseded eligible failure may produce
this regression artifact. A superseded record stays marked as such in
`recorded_before`; it cannot trigger a retry or target enablement.

The loaded parser's module retains a bounded import-time source digest and the
registered callable/code identities. Validation rejects source-file edits after
import, including edits confined to helpers, and unregistered replacement
callables. This is a source-provenance check, not general runtime attestation:
arbitrary in-memory changes to helper globals are outside its contract.

`test_repair_bundle_regression.py` is generated from fixed code and runs the
normal `billcommons_shared.ca_official_actions` parser against the fixture. It
checks the fixture hash, current parser source hash, replay outcome and bounded
evidence, while blocking socket connections. A source or fixture change causes
the generated test to fail until a reviewer deliberately creates a new bundle.

Use the local verification command below after materialization:

```bash
PYTHONPATH=packages/shared:workers/ingest python -m pytest --noconftest -c /dev/null -q /absolute/local/path/ca-repair-bundle/test_repair_bundle_regression.py
```

The manifest always ends in `promotion_state: "requires_human_review"` and
`execution_authorized: false`. A successful replay only supplies a candidate
fixture and regression evidence for a separately reviewed repair proposal and
controlled canary.

## Preparing proposed code and tests

Once a repair author supplies candidate parser source and regression test source,
bind both to a separately retained baseline manifest digest:

```bash
python -m billcommons_ingest.official_repair_proposal /absolute/local/path/ca-repair-bundle \
  --baseline-sha256 TRUSTED_MANIFEST_SHA256 \
  --candidate-source /absolute/local/path/candidate.py \
  --regression-source /absolute/local/path/test_candidate.py \
  --output-dir /absolute/local/path/ca-repair-proposal
```

This command validates and copies the baseline evidence, retains the exact
baseline parser source, and stores candidate source and tests as `.py.txt`
artifacts, alongside a unified `candidate.patch` restricted to the shared CA
parser path. It hashes every artifact and binds the baseline manifest in
`proposal.json`. It accepts only a changed parser and bounded UTF-8 source
files; it rejects symlinks and non-empty output directories. Source text is
neither compiled nor imported during preparation. The baseline validation runs
the installed baseline parser, as it does for the original bundle.

The command reports a canonical `proposal_sha256`; retain it separately from
the proposal. Before consuming the artifact, call
`validate_repair_proposal(proposal_dir, expected_proposal_sha256=trusted_digest)`
from `billcommons_ingest.official_repair_proposal`. It validates the exact file
topology, baseline bundle, source hashes and sizes, and canonical patch against
the retained baseline and candidate bytes. It does not execute candidate code.
Artifact names discourage accidental test collection; they are
not a security boundary. Candidate execution needs a separate isolated runner
without application credentials or network access. The proposal records
`evaluation.status: "not_run"` and does not claim that candidate syntax,
regressions, or repaired output have passed. The original baseline regression
test still pins the original parser; do not weaken it to accept the candidate.

Automated repair authorship, isolated candidate evaluation, comparison against
known-good archives, and promotion are still separate unfinished workflow steps.
