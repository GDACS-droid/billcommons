# Scout live tests

Congress.gov authentication, conservative Florida direct retrieval, and the opt-in Solari browser gates ran on 2026-09-01. Unrelated customer outreach and billing checks are intentionally not recorded in this public feature document.

## Solari

Live authentication passed through the official Python SDK. The final post-repair smoke used a recorded Solari browser to visit the small official Florida Online Sunshine robots resource at `https://www.leg.state.fl.us/robots.txt` and asserted the deterministic `User-agent` marker.

- result: pass
- session fingerprint: `186a068f3858` (SHA-256 prefix; the signed provider capability is not retained in this document)
- actions/pages: 1 / 1
- elapsed runtime: 7,283 ms
- recording: enabled
- replay: available during the bounded post-release probe; URL not logged
- cleanup: independently confirmed through idempotent release

The live loop exposed useful source behavior before the pass. An initial pre-repair attempt had insufficient diagnostics and motivated the session-before-connect lifecycle fix. Subsequent bounded attempts against `www.flsenate.gov` classified a repeatable browser-side `connection_reset`; a diagnostic attempt confirmed cleanup. The same Florida Senate host remained healthy through direct HTTP. Scout therefore treats browser failure as a partial source result and does not claim universal browser reachability.

The current CLI prints only fixed failure categories and a non-reversible session fingerprint. It never prints the API key, signed session ID, response body, raw exception text, or replay bearer URL.

The sanitized public cookbook example was also run independently with the configured key. It passed in 4,935 ms with one page/action, the same deterministic marker, content SHA-256 `2eea2058576ce8bf11c5f93d987ee2d6eb44e046aef4e0904f57cddfc2b387a1`, session fingerprint `6ec77bde219c`, replay available, and cleanup complete.

The direct Online Sunshine and public cookbook checks are provider/SDK lifecycle proofs. Separately, an opt-in PostgreSQL/API product-path test against real MyFloridaHouse `BillId=84174` exercised `ScoutRunner`, direct-fetch `302` classification, browser-slot admission, fresh-context routing, durable `ScoutBrowserSession` persistence, and release. After the final security repair, the live test passed again in 5.57 seconds: exactly one Solari session was created and its durable status ended `released`. The browser context blocks service workers and WebSockets, closes unexpected popups, and fetches redirects without following them so each `Location` is admitted before Chromium receives it. The run intentionally produced no government finding, so bill-level finding generation remains fixture-verified rather than claimed as live evidence.

## Government source

The final opt-in direct product check used a guarded disposable PostgreSQL database and the corpus-shaped official Florida Senate URL for HB 625:

- source: `https://flsenate.gov/Session/Bill/2026/625/ByCategory`
- result: pass; direct retrieval only, mock browser unused
- retained: one official source and one finding
- evidence contract: the displayed excerpt supports both `HB 625` and `Chapter No. 2026-141`

This proves the live evidence-retention contract for a known structured action. It does not claim Scout discovered a previously unknown development. The visual QA result remains explicitly labeled as a fixture.
