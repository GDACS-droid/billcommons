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
