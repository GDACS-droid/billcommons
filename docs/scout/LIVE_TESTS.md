# Scout live tests

Congress.gov authentication, conservative Florida direct retrieval, and the opt-in Solari browser gates ran on 2026-09-01. Unrelated customer outreach and billing checks are intentionally not recorded in this public feature document.

## Solari

Live authentication passed through the official Python SDK. The final smoke used a recorded Solari browser to visit the small official Florida Online Sunshine robots resource at `https://www.leg.state.fl.us/robots.txt` and asserted the deterministic `User-agent` marker.

- result: pass
- session fingerprint: `cebada2bd753` (SHA-256 prefix; the signed provider capability is not retained in this document)
- actions/pages: 1 / 1
- elapsed runtime: 3,650 ms
- recording: enabled
- replay: available during the bounded post-release probe; URL not logged
- cleanup: independently confirmed through idempotent release

The live loop exposed useful source behavior before the pass. An initial pre-repair attempt had insufficient diagnostics and motivated the session-before-connect lifecycle fix. Subsequent bounded attempts against `www.flsenate.gov` classified a repeatable browser-side `connection_reset`; a diagnostic attempt confirmed cleanup. The same Florida Senate host remained healthy through direct HTTP. Scout therefore treats browser failure as a partial source result and does not claim universal browser reachability.

The current CLI prints only fixed failure categories and a non-reversible session fingerprint. It never prints the API key, signed session ID, response body, raw exception text, or replay bearer URL.

The sanitized public cookbook example was also run independently with the configured key. It passed in 4,935 ms with one page/action, the same deterministic marker, content SHA-256 `2eea2058576ce8bf11c5f93d987ee2d6eb44e046aef4e0904f57cddfc2b387a1`, session fingerprint `6ec77bde219c`, replay available, and cleanup complete.

The direct Online Sunshine and public cookbook checks are provider/SDK lifecycle proofs. Separately, an opt-in PostgreSQL/API product-path test against real MyFloridaHouse `BillId=84174` exercised `ScoutRunner`, direct-fetch `302` classification, browser-slot admission, fresh-context routing, durable `ScoutBrowserSession` persistence, and release. After the final security repair, the live test passed again in 5.57 seconds: exactly one Solari session was created and its durable status ended `released`. The browser context blocks service workers and WebSockets, closes unexpected popups, and fetches redirects without following them so each `Location` is admitted before Chromium receives it. The run intentionally produced no government finding, so bill-level finding generation remains fixture-verified rather than claimed as live evidence.

## Government source

One bounded direct request used the corpus-retained official URL for Florida SB 1344:

- host: `flsenate.gov`
- outcome: HTTP 200, `text/html`
- bytes: 51,922
- SHA-256: `0b35ec1d4654da6ca3e468a6ce1ed63ce04a2e2d75be0d835bacabb0187b463c`

This proves conservative official retrieval only. It does not claim the page contained a new legislative development, and the visual QA finding remains explicitly labeled as a fixture.
