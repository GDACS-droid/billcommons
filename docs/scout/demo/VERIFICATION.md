# Demo verification record

This sanitized record supports the separate live checks summarized in the Scout demo. It contains no API key, provider session ID, replay bearer URL, customer data, or private database value.

Recorded on 2026-09-01 against hardened code revision [`22a6376`](https://github.com/GDACS-droid/billcommons/commit/22a6376bb16c9e1f35a2808f987e9002c31d215e). Full context and failure history are in [`LIVE_TESTS.md`](../LIVE_TESTS.md).

## Shown product run: deterministic UI

The first 28.96 seconds of the MP4 show the real `/scout` UI consuming deterministic local queued, running, and completed API states. It is not a production recording and makes no network request to a government site. The shown job uses direct retrieval and correctly reports that no browser session was needed.

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

## Shown live Solari proof: separate non-recorded session

The final five seconds are an actual screenshot captured from a Solari-controlled cloud browser while it displayed the harmless official target. This was a new session, separate from both the deterministic HB 625 product sequence and the earlier recorded/replay smoke.

- Official target: <https://www.leg.state.fl.us/robots.txt>
- Recorded result: `solari_live_visual=ok`
- Non-reversible session fingerprint: `b0dd77da148d`
- Pages/actions: 1 / 1
- Runtime: 3,412 ms
- Deterministic assertion: `User-agent` marker present
- Recording: disabled for this public visual proof
- Cleanup: confirmed before the derivative image was added to the repository
- Public proof SHA-256: `13e92dbcbbfb61ea738cd882587f13d2cae27cc8b4cdd2e7b1b00b71ca71b840`

The derivative image contains no address bar, API key, WebSocket endpoint, provider session ID, replay URL, cookies, or account data. Disabling recording avoids creating another retained rrweb artifact merely for marketing evidence; the prior opt-in smoke remains the recording/replay lifecycle proof.

## Claim boundary

Solari was not used for the HB 625 product run shown in the video. The final segment is explicitly a separate live Solari visual proof. The recorded Solari check proves the bounded recording/replay/cleanup lifecycle; a separate MyFloridaHouse redirect test proves Scout's persisted fallback path. None of those browser runs produced the HB 625 finding.
