# Demo verification record

This sanitized record supports the separate live checks summarized in the Scout demo. It contains no API key, provider session ID, replay bearer URL, customer data, or private database value.

Recorded on 2026-09-01 against hardened code revision [`22a6376`](https://github.com/GDACS-droid/billcommons/commit/22a6376bb16c9e1f35a2808f987e9002c31d215e). Full context and failure history are in [`LIVE_TESTS.md`](../LIVE_TESTS.md).

## Shown run: deterministic product UI

The MP4 shows the real `/scout` UI consuming deterministic local queued, running, and completed API states. It is not a production recording and makes no network request to a government site. The shown job uses direct retrieval and correctly reports that no browser session was needed.

## Separately checked: HB 625 evidence contract

- Official source: <https://flsenate.gov/Session/Bill/2026/625/ByCategory>
- Opt-in test: `apps/api/tests/test_scout_postgres.py::test_live_direct_flsenate_hb_625_retains_exact_supported_evidence`
- Recorded result: pass
- Contract: exactly one direct official source and one finding; retained excerpt contains both `HB 625` and `Chapter No. 2026-141`; mock browser unused
- Scope: this verifies retention of a known official action. It does not claim Scout discovered a previously unknown development.

The test uses the repository's destructive-test guard and must target a newly created, explicitly acknowledged disposable PostgreSQL database:

```bash
BILLCOMMONS_TEST_DATABASE_URL='postgresql:///REPLACE_WITH_NEW_DISPOSABLE_DB?host=/var/run/postgresql' \
BILLCOMMONS_TEST_POSTGRES_URL="$BILLCOMMONS_TEST_DATABASE_URL" \
BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
BILLCOMMONS_SCOUT_LIVE_FINDING_CHECK=1 \
PYTHONPATH=apps/api:packages/schema:packages/shared:workers/scout \
.venv/bin/pytest -q \
  apps/api/tests/test_scout_postgres.py::test_live_direct_flsenate_hb_625_retains_exact_supported_evidence
```

Do not reuse that placeholder literally or point the command at a shared/production database.

## Separately checked: Solari lifecycle

- Official target: <https://www.leg.state.fl.us/robots.txt>
- Recorded result: `solari_check=ok`
- Non-reversible session fingerprint: `186a068f3858`
- Pages/actions: 1 / 1
- Runtime: 7,283 ms
- Deterministic assertion: `User-agent` marker present
- Recording: enabled
- Replay: available during the bounded post-release probe
- Cleanup: confirmed through idempotent release

Explicitly billable reproduction command:

```bash
BILLCOMMONS_SCOUT_ENABLED=true \
BILLCOMMONS_SCOUT_SOLARI_CHECK=1 \
PYTHONPATH=apps/api:packages/schema:packages/shared:workers/scout \
.venv/bin/python -m billcommons_scout solari-check
```

The command emits fixed safe fields and a SHA-256 session fingerprint. By design it does not log the API key, signed provider session ID, response body, raw exception text, or replay bearer URL. A replay URL was therefore not retained as a public artifact. The independently runnable public provider example is at [`GDACS-droid/solari-cookbook`](https://github.com/GDACS-droid/solari-cookbook/tree/billcommons-scout-challenge/examples/bill-commons-scout-py).

## Claim boundary

Solari was not used for the HB 625 run shown in the video. The Solari check proves the bounded cloud-browser recording/replay/cleanup lifecycle; a separate MyFloridaHouse redirect test proves Scout's persisted fallback path. Neither browser run produced the HB 625 finding.
