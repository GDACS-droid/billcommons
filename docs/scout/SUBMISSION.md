# Bill Commons Scout — submission working brief

## Pitch

Bill Commons Scout is an evidence-first government-research feature that checks the structured Bill Commons corpus, revisits admitted official bill sources for missing developments, and retains only findings supported by the fetched evidence instead of returning unsupported chat.

## Why it matters

Legislative databases are strongest at normalized bills and weakest at the messy surrounding web: agendas, amendments, fiscal notes, hearings, agency notices, and JS-heavy portals. The Florida P0 routes structured candidates and direct retrieval first, then uses a recorded Solari browser only for an explicitly admitted browser-required source. Independent source discovery beyond URLs already linked to Bill Commons bills is future adapter work, not a shipped claim.

## Public strategy

Bill Commons is already public at `GDACS-droid/billcommons` under Apache-2.0. The preferred challenge path is a documented Scout implementation here plus a small reproducible example contributed from a user fork of `solari-sdk/solari-cookbook`. Do not publish secrets, private customer data, fake metrics, or unverified government findings.

Published cookbook branch: `https://github.com/GDACS-droid/solari-cookbook/tree/billcommons-scout-challenge/examples/bill-commons-scout-py` at hardened commit `1095a02`. The free fixture, native provider, and public example live paths are verified and pushed.

Published Bill Commons feature branch: `https://github.com/GDACS-droid/billcommons/tree/billcommons-scout`. The final hardened head includes post-review lifecycle, evidence, analytics, polling, concurrency, budget, and outcome-unknown cleanup repairs. Use the branch's immutable head SHA in the final post after push. Production flags remain off; publishing source is not a production deployment.

## Setup

Bill Commons local configuration follows `.env.example`. Store the browser secret without printing or committing it:

```bash
./scripts/setup-solari.sh
```

The public cookbook example has a free deterministic path:

```bash
cd examples/bill-commons-scout-py
python main.py
```

and an explicitly paid path:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
SOLARI_API_KEY=... .venv/bin/python main.py --live
```

## Truthful 30-second demo

1. Open `/scout`: “Bill Commons already normalizes state legislation; Scout follows the official record beyond the database.”
2. Submit a real Florida bill/query selected by the live preflight. Show backend-driven stages, never timers.
3. Open the retained finding and official source. Say: “Every material claim has source metadata, an excerpt, and a content hash.”
4. If and only if that run required Solari, open Agent Replay: “Ordinary HTTP runs first; Solari handles the browser-only portal and is always released.”
5. Show sources checked, browser pages/actions/time, and cache state: “The same research is reusable, so spend is bounded.”

Do not use the visual fixture as a claimed government result. The infrastructure demo can truthfully show the verified official Online Sunshine SDK/provider browser/replay evidence. The production router/browser/session-lifecycle path is live-verified against a real MyFloridaHouse `302` route: exactly one Solari session was admitted and durably released. Separately, the direct live HB 625 check retained one finding only after the official page supported the exact bill/action. The browser-path run itself asserted no bill finding; a bill-level public recording that combines a supported finding with Solari still requires a suitable browser-only source.

## Product proof and analytics

The native client records Scout opened, example selected, job created/started/partial/completed/failed, cache hit, evidence opened, replay resolved, and replay opened through the existing Vercel Analytics integration. No adoption, retention, revenue, or usefulness metric exists yet; do not fabricate one.

## Known limitations

- Florida-only P0; no generic web crawl or arbitrary URL input.
- Solari auth/session/recording/replay/cleanup passed on the official Online Sunshine smoke. `www.flsenate.gov` reset the Solari browser connection during separate diagnostics even though direct HTTP was healthy; partial-source handling is therefore material, not theoretical.
- The Online Sunshine smoke calls the provider directly. A separate opt-in PostgreSQL/API product-path test against real MyFloridaHouse `BillId=84174` verified safe `302` escalation, database-backed browser-slot/session lifecycle, exactly one Solari session, and durable release. It did not produce a retained bill finding. A separate direct live Florida Senate HB 625 check did retain exact bill/action evidence, but did not require Solari.
- Florida P0 revisits official `Bill.source_url` candidates already present in the corpus. New agenda/agency/source discovery is not yet implemented.
- Broader popup/download planning, production cohorts, service telemetry, and graceful drain are rollout work. Daily job/runtime and per-job request/retry limits are implemented, with active-job ceilings bounding in-flight overshoot.
- Full legacy API regression is not green and is unsafe without isolated-database wiring.
- Stripe checkout creation passes, but webhook registration and a completed paid provisioning round trip are not verified.

## Recommended X post (send only after live pass)

> Built Bill Commons Scout: an evidence-first government research agent that checks structured legislative data first, uses ordinary HTTP when it can, and launches a recorded @getsolari browser only for the messy browser-only government web. Every finding links to retained primary evidence, and every browser run is bounded and released. [truthful demo URL] [public repo URL] @harrychow_

## Recommended LinkedIn post (send only after live pass)

> Legislative databases are good at normalized bills and weak at the surrounding government web: agendas, amendments, fiscal notes, hearings, and older interactive portals. I built Bill Commons Scout as a permanent evidence-first research feature. It checks Bill Commons and direct official retrieval first, then uses a recorded Solari browser only when interaction is genuinely required. The system retains provenance, hashes evidence, coalesces duplicate research, limits spend, handles partial failures, and exposes replay as audit material—not as primary evidence. Code and a reproducible cookbook example: [links]. @Harry Chow / Solari

Required remaining public sequence: push/share the immutable feature head, record a truthful demo, publish it, and tag `@harrychow_` and `@getsolari`. The live Solari SDK/provider gap, product browser-fallback/session-lifecycle gap, and direct official finding-retention contract are closed. Do not imply the Solari smoke discovered a bill development or that the known HB 625 action was previously unknown.
