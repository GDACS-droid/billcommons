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

## Evaluating a pinned proposal locally

The native evaluator now runs the pinned baseline and candidate in separate
restricted interpreters and compares their full returned action facts with a
baseline computed by the trusted parent:

```bash
python -m billcommons_ingest.official_repair_evaluation /absolute/local/path/ca-repair-proposal \
  --proposal-sha256 TRUSTED_PROPOSAL_SHA256
```

This is an explicit execution command. The inert proposal's manifest is not an
execution authorization and is never rewritten. The report binds the proposal,
fixture, actual source bytes, and bootstrap digests, records the host runtime,
and labels its scope `one_retained_ca_archive`. Retain the report alongside the
separately trusted proposal digest. No application record, source target, Git
branch, or deployment is changed by evaluation.

`same_as_current_baseline` means that every returned action field and raw source
field matched for this archive, including ordering. Equal bill/event counts with
different content do not match. This result is a regression observation, not
proof of historical repair or general correctness. Different facts require an
independent expected-fact oracle; a rejected current baseline is explicitly
reported as requiring such an oracle. Without `--run-regressions`, authored tests
remain inert and `not_run`; every report sets `promotion_authorized: false`.
The default command exits zero for `same_as_current_baseline`, one for other
evaluation results, and two for rejected input evidence. With `--run-regressions`,
zero additionally requires both regression invocations to return the expected
observation; see “Authored regression observations” below for the contract and
its limitations. Automated callers must inspect the complete
JSON report, not classify host health by exit code alone. Missing, malformed or
unexpected reports also stop scheduling. The compatibility status
`isolation_unavailable` has the explicit detail
`bootstrap_failure_or_candidate_exit_78`: an untrusted candidate can choose that
exit code, so it does not prove that host confinement is absent. Baseline output
failures are reported separately from a factual runtime mismatch.

The supported runner host is Linux x86_64 with Landlock ABI 4 or newer,
`libseccomp.so.2`, and a responsive local `/tmp` filesystem. Missing required
confinement stops before candidate loading. There is no weaker fallback.
The parent launches a fresh `python -I -S -B` process with an empty environment,
closed inherited descriptors, `/dev/null` input and captured output pipes. The
child verifies one thread and the staged files before applying filesystem and
syscall restrictions. Only the staged inputs are readable; file writes,
networking, process creation, signals to other processes, and process-memory
access are denied. Filesystem metadata syscalls remain available. The supported
parser imports are the preloaded standard-library dependencies of the existing
CA parser; importing additional modules may fail in this environment.

Policy caps are 256 KiB source, 8 MiB archive, 256 MiB child address space,
five CPU seconds, 16 MiB combined stdout/stderr, and a 12-second staging/execution
budget. The parent requires ASCII output without NUL bytes and passes an explicit
ASCII string to the JSON decoder, so alternate-encoding detection cannot bypass
the byte scanner. Before JSON decoding, a constant-memory scanner caps nesting at eight,
containers at 32,768, scalar tokens at 524,288, individual encoded strings at
1 MiB, and unquoted tokens at 64 bytes. This prevents a short stream of tiny
objects from amplifying into millions of parent-side allocations.
Staging defaults to an owner-only directory beneath
`/tmp/bc-repair-supervisor-<uid>` rather than environment-selected `TMPDIR`;
the deadline is checked before launch, but a stalled host filesystem is outside
the wall-time guarantee. The parent kills the fresh process group before reaping
its leader, including when a descendant retains a pipe after leader exit. The
caller must own that child's wait status; an unrelated global child reaper is
not a supported supervisor environment. Reaping has a separate one-second
budget. A kernel task that does not exit after `SIGKILL` produces
`cleanup_pending`, preserves its private stage and an owner-only `recovery.json`
outside the candidate-readable stage, and stops subsequent evaluations until the
leader is reaped, the group is absent, and the stage is removed. The group probe is non-destructive; an
ambiguous reused ID conservatively retains the stop rather than being signaled.
Stage-removal errors remain cleanup failures even when no child was spawned;
a stage already removed after process cleanup does not wedge the supervisor.
A process-local mutex plus a nonblocking file lock rejects concurrent invocations
as `runner_busy`. Every automated caller under the same OS account must share the
same supervisor state directory. `BC_REPAIR_SUPERVISOR_STATE_DIR` is a trusted
configuration override for that directory; changing it is not a recovery action.

The recovery record is written before spawning, so an abrupt CLI exit leaves a
durable stop. The child also arms a parent-death `SIGKILL` before candidate loading,
closing the indefinitely sleeping orphan case. A fresh supervisor returns
`cleanup_pending` with `cleanup.state_path` for an unfinished record; it never
signals recorded PIDs or removes an unknown prior stage. Invalid recovery files
also stop execution, including non-regular files and excessively nested JSON.
Only the owning live process may resolve its in-memory pending child automatically.

Automated callers must stop scheduling on cleanup, busy, isolation or supervisor
failure results. Recovery after a supervisor restart requires checking the
record's origin, PID/start ticks when available, process-group absence, stage
ownership and host health while holding the same exclusive file lock. Remove
only the identified completed stage and its recovery record after those checks;
do not signal a stale PID or clear an unexplained record. The
whole child interpreter and its output remain untrusted; separate Python globals
are not an isolation boundary. Error output is discarded, and the parent
independently validates and hashes all returned facts. This kernel boundary does
not defend against a compromised host or kernel vulnerability.

The first retained-archive smoke proposal deliberately changed only a source
comment. Baseline and candidate both returned 275 bills / 7,094 events and full
fact SHA-256 `5150ff4a18ea9fcabbdb36d6b261566fa304672fbaf10359d65f869f8b1847df`.
Its local report is retained at
`/home/alberto/.local/share/billcommons/reliability-release-20260908/native-evaluation-smoke-20260915-th2sosgj/evaluation.json`.
This proves the execution/comparison path for that proposal, not a parser repair.

Automated repair authorship, executing proposed regression tests, representative
corpus selection, workflow integration, and promotion remain unfinished steps.

### Evaluating a separately pinned corpus

The local `official_repair_corpus` command now compares the candidate with
caller-pinned expected fact digests across up to 16 explicit CA fixtures. It
does not derive expectations from either candidate output or the current parser.
The caller must curate and review the expected facts independently of repair
authorship, then retain the SHA-256 of the exact `corpus.json` bytes separately.
Digest verification establishes artifact integrity; it cannot establish that
the annotations are true or that the selected corpus represents California.

Each corpus directory contains `corpus.json` and `<case-id>.zip` files. The
manifest has exactly `corpus_version` (`official-repair-corpus/1`), a nonempty
`review_reference` identifying the expectation review, and a `cases` array.
Each case contains exactly `id`, `fixture_sha256`, `source_url`, `retrieved_at`
(with timezone), and `expected`. Case IDs use lowercase letters, digits,
underscores and hyphens, start with a letter or digit, and are at most 64
characters. The expected object contains `bill_count`, `event_count` and
`facts_sha256`; counts are nonnegative integers. The fact digest covers the
complete parsed payload, including raw source fields, encoded as Python
`json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
allow_nan=False).encode("utf-8")`, without a trailing newline. Bill/event order
is part of this contract. The payload schema is the existing evaluator's
`status`/`bills` representation, not a count-only or selected-field projection.

```bash
PYTHONPATH=packages/shared:packages/schema:workers/ingest python -m billcommons_ingest.official_repair_corpus \
  /absolute/local/path/ca-proposal \
  --proposal-sha256 "$TRUSTED_PROPOSAL_SHA256" \
  --corpus-dir /absolute/local/path/reviewed-ca-corpus \
  --corpus-sha256 "$TRUSTED_CORPUS_SHA256"
```

Every manifest and fixture is authenticated before candidate execution.
Fixtures are capped at 8 MiB each and 64 MiB combined, and are pinned in memory
before the sequence begins. Only candidate source and the current fixture enter
the sandbox; expected answers remain in the parent. The report binds the
proposal, corpus, candidate, each fixture and bootstrap hashes, preserves each
expected/observed comparison, and identifies cases skipped after an unavailable
isolation boundary, busy supervisor or pending cleanup. A completed match exits
zero with `matches_pinned_expectations`; a mismatch or host stop exits one;
invalid input exits two. All reports retain `promotion_authorized: false`.

The 19 focused tests include two synthetic, explicitly annotated TSV fixtures,
an actual candidate with wrong text but unchanged counts, tampered later-case
evidence rejected before any candidate run, canonical CA URL and timestamp
validation, manifest bounds, and propagation of the runner's actual host-stop
statuses. All source URLs must pass the trusted CA delta URL contract before
execution; a malformed URL is an input failure, not a confinement diagnosis.
This new module is outside the evaluator aggregate
review pinned to `265a001` and is not yet canonically verified or deployed.

A first curated official corpus is retained at
`/home/alberto/.local/share/billcommons/reliability-release-20260908/ca-corpus-20260915/`.
Its exact manifest SHA-256 is
`21f65e4ad7eed6030ee6cd1cd3de7df65caa48a719d6bdb319d2cc537882052d`.
It contains every retained source row for regular-session AB 115 (11 actions)
and SB 114 (16 actions), selected from archive
`0a0bff772076a5879238cf3e1c69c02000b4a4ed2653bd4ca7192fdfc91ce7b8`
captured on September 8. Explicit ordered annotations preserve dates, sequence,
history identity, normalized text, source URL and all raw fields. The derived
ZIPs preserve source-row order, including SB 114's out-of-order input and tied
sequence numbers. The builder imports no application parser; an independent
read-only check verified all 27 annotations and complete row selection directly
against the retained archive, without using candidate output as the oracle.

`build_corpus.py` reproduces the fixtures and compares existing artifacts without
overwriting them. `review.json` records the source/derivation evidence, and
`evaluation-final.json` records both cases matching the pinned comment-only candidate
after integration of the durable supervisor. The deliberately altered-text
proposal in the sibling `ca-corpus-wrong-text-20260915` directory is a negative
control: both bill/event counts stay unchanged, but both full fact comparisons
fail. Its `evaluation.json` retains that result.
This establishes a two-bill historical corpus, not statewide coverage or current
freshness. Representative corpus expansion, authored-test execution, repair
authorship and workflow promotion remain unfinished.

### Canonical review and follow-up checks

Canonical run `/home/alberto/verify-runs/20260915T172457Z-1a99811` returned
**BLOCK**: Codex, AGY, Grok and Deepseek blocked; Muse, Opus and Ox returned SHIP.
All seven legs completed, but the final adversarial pass was inconclusive.
Grok reported truncated review input. No majority-vote approval is implied.

Confirmed findings prompted the pre-decode structural budget, bounded reaping
with explicit pending cleanup, and descriptor-pinned final artifact reads.
The replayed empty-action archive preserves an empty event mapping, contradicting
the alleged missing-key case. The unchanged retained parser also runs with
`fcntl` denied. The parent does not reap before group teardown, and the installed
Python subprocess cleanup only polls its tracked abandoned process objects;
the alleged unconditional global `waitpid(-1)` path is not present.

The final follow-up six-file offline suite passed **106 tests**. It includes actual
object-amplification rejection before decoding, directory replacement during a
verified read, a real zero-action archive, and injected delayed-reaping recovery.
The retained smoke replay again matched all 7,094 events; its report is
`native-evaluation-smoke-20260915-th2sosgj/evaluation-final.json` in the release
evidence directory above. Subsequent focused review added process-group checks
after leader reaping, retained-stage error handling, and a supervisor reservation;
these paths also passed in the final suite. These fixes have not received a new canonical SHIP
verdict, and the change remains undeployed.

The next full aggregate, base `6639527` through target `265a001`, was reviewed as
`5ba0582` in `/home/alberto/verify-runs/20260915T175911Z-5ba0582`. The aggregate's
binary diff and target tree were verified identical to that source range.
Grok's installed `--verbatim` delivery option resolved the earlier prompt
offloading. The canonical result was **HALT**: Codex, Grok and Opus BLOCK;
AGY, Muse and Ox SHIP; DeepSeek failed. Its automatic repeat was stopped after
the initial failure, and the final adversarial pass was skipped. This is not a
verification pass.

Confirmed follow-up findings concern alternate-encoding JSON allocation,
abrupt supervisor death, and incomplete teardown/recovery. The child now arms
`PR_SET_PDEATHSIG` before candidate execution and checks parent identity on both
sides of that operation. Seven child tests pass, including a real sleeping
`futex` candidate killed after supervisor `SIGKILL`, and proof that candidate
`prctl` cannot clear the death signal. The
[Linux parent-death signal contract](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)
requires the identity recheck because the signal is not retroactive.

Grok's requested errno-bit shift was rejected against the
[libseccomp header](https://raw.githubusercontent.com/seccomp/libseccomp/main/include/seccomp.h.in):
`SCMP_ACT_ERRNO` uses the low 16 bits, as the implementation already does. Actual
denial tests also observe `EPERM`. No policy change was made for that claim.
Local follow-up fixes and corpus work require their own complete verification;
the HALT remains the latest canonical result.

The integrated follow-up seven-file offline suite passed **143 tests**. A first
combined run correctly stopped on an abandoned synthetic delayed-reaping test
record from the worker's earlier development run; its origin, exact synthetic
input, absent PID and absent process group were verified before removing only
that stage and record under the host lock. The recovery evidence is retained at
`native-supervisor-test-recovery-20260915.json` in the local release evidence
directory. Parent, evaluator and corpus tests now isolate their durable state.

After integration, the full retained archive again matched **275 bills / 7,094
events** with fact digest
`5150ff4a18ea9fcabbdb36d6b261566fa304672fbaf10359d65f869f8b1847df`;
`native-evaluation-smoke-20260915-th2sosgj/evaluation-durable-supervisor.json`
records that replay. The curated 27-event corpus matched the pinned comment-only
candidate and rejected the altered-text negative control with unchanged counts.
These are local runtime and correctness checks, not a new canonical verdict or
authorization to promote a repair.

## Native isolation feasibility — September 15, 2026

A trusted-only local probe established that the development host supports
Landlock ABI 4 and a libseccomp default-deny syscall policy. The earlier CLI
sandbox failure therefore does not establish that native Python isolation is
unavailable on this host. This is feasibility evidence, not a candidate executor
or authorization to run proposed code.

The unchanged shared CA parser, source SHA-256
`496bd79b54eabee11f37ec73652b7579f9d91a0045156ae612613afb22f25201`,
loaded after both restrictions were active and replayed the retained archive
SHA-256 `0a0bff772076a5879238cf3e1c69c02000b4a4ed2653bd4ca7192fdfc91ce7b8`.
It produced the recorded **275 bills and 7,094 events**. Its import-time source
witness remained intact; neither the parser nor its LZMA support was modified.
The probe preloaded its required standard-library modules before restriction,
then allowed file reads only in the private staged input directory.

Synthetic checks confirmed denial of outside-file reads and writes, staged-file
writes, IPv4/IPv6/Unix stream and datagram sockets, fork, exec, and signals.
Direct invalid-argument syscall probes also returned `EPERM` for ptrace,
process-memory reads/writes, pidfd creation, io_uring setup, clone3, mount and BPF.
The synthetic outside canary remained unchanged. A 512 MiB allocation failed
under a 256 MiB address-space limit; a separate infinite-loop probe terminated
with signal 9 after 5.01 seconds under a five-second CPU limit, before its
12-second parent timeout. These observations cover these probes only.

The original trusted prototype and results are retained locally in
`/home/alberto/.local/share/billcommons/reliability-release-20260908/native-sandbox-probe-20260915/`.
They must not be used to execute generated code. The review identified the
following requirements, now implemented by the separate evaluator above:
a fresh single-thread child, explicit failure handling, closed inherited
file descriptors, private immutable verified inputs, a bounded pipe reader,
wall timeout and kill/reap handling, and a separately trusted comparison of
returned facts. Treat the whole child interpreter and its output as untrusted:
separate Python globals do not provide isolation, and a child-reported pass or
source hash cannot attest a candidate. The host must bind actual staged bytes
to its evaluation report. Proposed regression tests and controlled promotion
remain subsequent gates.

The kernel documents that Landlock restrictions apply to the calling thread
and its future children, and that rights already obtained through open file
descriptors need separate handling. ABI 4 also lacks later signal and Unix
socket scoping, which is why filesystem rules alone are insufficient here.
See the [kernel Landlock documentation](https://docs.kernel.org/userspace-api/landlock.html)
and [seccomp API documentation](https://man7.org/linux/man-pages/man2/seccomp.2.html).

## Authored regression observations — September 15, 2026

The local evaluator accepts `--run-regressions` to execute the authenticated
`candidate-regression.py.txt` against the pinned baseline and candidate in
separate sandbox invocations. The default remains inert for existing callers.
The supported contract is a dependency-free function:

```python
def run_regression(parser, fixture, *, source_url, retrieved_at):
    batch = parser.parse_ca_official_actions_zip(
        fixture, source_url=source_url, retrieved_at=retrieved_at)
    assert batch.scoped_bill_ids
    # Assert independently justified expectations here; return None.
```

The archive is supplied as bytes. The function must raise on failure and return
`None` otherwise; pytest discovery and repository imports are not supported.
Imports are limited to standard-library modules already resident in the sandbox
bootstrap; arbitrary standard-library imports can also fail under confinement.
Both sources are encoded into a bounded program without host compilation or
imports. The existing native sandbox loads them, with all existing filesystem,
network, resource, process, and cleanup restrictions. Each module has a virtual
`__file__`; source files under those names are not materialized, so parser
import-time file witnesses may be unavailable. The parent binds the exact
original sources and complete executed program through hashes instead.

The report records the regression source digest, each executed program digest,
fixture digest, bootstrap digest, and separate baseline/candidate outcomes.
`returned_without_error` is an observation of untrusted output, **not a trusted
pass**: either proposed code file can bypass assertions or forge that output.
`trusted_correctness_proof` and `promotion_authorized` remain false. A difference
between baseline and candidate outcomes does not establish that a real defect
was repaired. Independently pinned source facts, representative corpus checks,
and the promotion review are still required.

Any sandbox host-stop status halts subsequent execution and retains its cleanup
evidence. Input substitution or an oversized combined program is rejected before
any child starts. With `--run-regressions`, the CLI returns nonzero if either
regression invocation fails to return the expected observation, even when the
archive comparison matches. CLI zero is still not promotion authorization.

Local verification of this addition: the eight-file repair suite passed **162
tests**, including real sandbox assertions, a baseline-fails/candidate-returns
probe, denied host-file writes, source substitution, invalid code and return
values, host-stop sequencing, forged observations, and CLI failure exits. The
retained 275-bill / 7,094-event archive matched the same fact digest recorded
above, and both authored count checks returned without error. Its evidence is
`authored-regression-smoke-20260915/evaluation.json` under the local reliability
release directory, report SHA-256
`e2a47a2938c20d5e37a0cf8f53d6da4997ca82596eb04af408a3641733ded7b5`.
This addition has not received a new canonical verify-ship verdict and is not
deployed. Automatic patch authorship and worker promotion remain incomplete.
