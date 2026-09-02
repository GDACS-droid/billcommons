# Bill Commons Scout — submission working brief

Do not publish the social post until the owner explicitly authorizes it.

## One-sentence pitch

Bill Commons Scout is an evidence-first government research layer that uses the
structured corpus when it can, direct official retrieval when that is sufficient,
and a bounded Solari cloud browser when it must navigate the government web.

## Problem and product value

Legislative databases normalize bills well but miss much of the surrounding primary
record: amendments, staff analyses, agendas, hearings, fiscal material, and old or
interactive portals. Scout turns that gap into a durable research job rather than an
unsupported chatbot answer. Every material finding links to retained official-source
metadata, excerpt, retrieval time, and content hash.

Florida P0 now provides incremental value beyond revisiting a known bill URL. From
an official Senate bill page it discovers same-session/same-bill staff analyses and
amendments, validates/fetches the bounded PDFs, and retains related findings and
change history. Direct HTTP remains the correct route for those documents.

Solari is used where a browser is the capability under test or interaction is needed.
The headline public example opens Online Sunshine chapter 43, visibly follows
§43.16, verifies current statutory language and chapter-law history, records the
session, then releases it. It does not claim that HTTP was impossible or that the
browser independently discovered HB 625's bill-to-law mapping.

## Architecture

```text
authenticated query
  → Bill Commons structured lookup
  → admitted direct official HTML/PDF
  → Solari only for an allowlisted browser route
  → normalize / hash / compare / extract
  → PostgreSQL evidence + findings + usage + durable events
  → evidence-first Scout UI
```

FastAPI owns owner-scoped jobs; PostgreSQL/Alembic owns queue/state and immutable
Scout blobs; a dedicated worker performs network/browser I/O; Next.js renders the
native `/scout` instrument. Provider calls stay behind `ResearchBrowserProvider`.

## Public repositories

- Live controlled beta: `https://billcommons.org/scout`.
- Reproducible Solari example, pinned at public commit `1233dd2`:
  `https://github.com/GDACS-droid/solari-cookbook/tree/1233dd2ac0782d91cb234359af021f2f0890ae2a/examples/bill-commons-scout-py`.
- Solari entry point:
  `https://github.com/GDACS-droid/solari-cookbook/blob/1233dd2ac0782d91cb234359af021f2f0890ae2a/examples/bill-commons-scout-py/main.py`.
- Bill Commons Scout product and final demo branch:
  `https://github.com/GDACS-droid/billcommons/tree/billcommons-scout`.

The cookbook is the small clone/install/run challenge entry. Bill Commons is the
permanent product implementation. Neither artifact contains the Solari key, private
replay capability, customer data, or fabricated usage metrics.

## Setup and tests

```bash
cd examples/bill-commons-scout-py
./setup-solari.sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --live
```

Free deterministic path:

```bash
python3 -m unittest discover -s tests -v
python3 main.py
```

The final component counts are API **572 passed / 8 skipped**, shared **166 passed**,
Scout worker **96 passed / 3 skipped**, monitoring **4 passed**, production operator
scripts **26 passed**, and web Scout **16 passed**. Targeted ESLint, TypeScript,
production build, PostgreSQL concurrency/restart/storage proof, live Florida discovery,
and the post-repair live Solari product-path lifecycle also passed. Exact commands are in
[TESTING.md](TESTING.md).

## Final 17.80-second demo

[Watch the final deployed-product artifact](demo/final/scout-challenge-final.mp4),
inspect its [contact sheet](demo/final/scout-challenge-final-contact.png), and read the
[capture and claim record](demo/final/VERIFICATION.md).

Suggested narration:

1. “This is Bill Commons Scout live, reusing retained research without new browser
   work.”
2. “The production record shows the real queued event, then HB 625 plus two official
   staff analyses and the primary Florida Senate evidence.”
3. “When browser navigation is useful, Scout can escalate to Solari. This is the
   actual cloud-browser run: Chapter 43, section 43.16 extracted, recording available,
   and cleanup confirmed.”

The first 1.8 seconds are an authenticated moving capture of the deployed public beta
reusing a retained result. Three production screenshots—the real durable queue state,
completed direct result, and official evidence—are then held for 8.3 seconds so they
remain readable. The last 7.7 seconds hold two screenshots from one actual recorded
Solari session. Playback speed is unchanged. There is no fake progress, secret replay
URL, or claim that the browser proved more than it did.

## Live proof and economics

The final real run passed in **11.689 seconds** with one page, two actions, 38 admitted
routed requests, recording/replay available, and confirmed release. At the documented
Starter rate of $0.10/browser-hour, that is approximately **$0.00032**. An equivalent
fresh cache hit launches no browser. See
[LIVE_TESTS.md](LIVE_TESTS.md) for calculations and claim boundaries.

## Analytics and traction boundary

Vercel's production Web Analytics API now confirms end-to-end controlled-operator
events: `scout_opened` 1, `scout_job_created` 3, `scout_job_started` 1,
`scout_job_completed` 2, `scout_evidence_opened` 2, and `scout_cache_hit` 2. Two
analytics visitor identifiers generated the controlled event set. Counts do not form
a funnel: the historical `scout_job_created` name records a successful create-endpoint
response, including a cache/coalesced return, rather than proving a new database row.
Instrumentation was also deployed between controlled runs, so one completed run
predates the `started` event repair. Partial/failed and Solari UI branches are
instrumented and contract-tested but were not deliberately induced in production. The
production database also includes backend/private-canary jobs that never loaded the web
client; after the final recapture it has 8 completed Scout jobs, 22 retained official
sources, 21 findings, 118 durable events, and zero active jobs or unreleased browser
sessions. These are operator validation runs, not organic adoption, retention, revenue,
or usefulness metrics.

## Known limitations

- Florida-only P0; no arbitrary URL input or generic 50-state planner.
- Browser navigation remains narrowly allowlisted; generic clicks/downloads require
  additional policy and tests.
- Scout blobs in Postgres are appropriate for bounded P0 volume; object storage is a
  future scale step behind the existing interface.
- Scout is a controlled public beta: authentication, quotas, browser admission,
  allowlists, and kill switches remain enforced. Florida is the only production P0.
- Production analytics prove the instrumented lifecycle for controlled runs, not
  organic traction or repeat-user retention.
- Stripe live-account webhook verification is separate and incomplete.

## Recommended X post — owner sends later

> Bill Commons Scout is live: structured legislation first, official HTTP when enough, and a real @getsolari browser when navigation is useful. The demo shows Florida evidence, cache reuse, extraction, and confirmed cleanup.
>
> https://billcommons.org/scout
>
> @harrychow_

Recommended first reply:

> Reproducible live Solari example and code:
> https://github.com/GDACS-droid/solari-cookbook/tree/1233dd2ac0782d91cb234359af021f2f0890ae2a/examples/bill-commons-scout-py

## Recommended LinkedIn post — owner sends later

> Legislative databases normalize bills, but government-affairs research also depends on amendments, staff analyses, agendas, hearings, and older official portals.
>
> I built Bill Commons Scout as a permanent evidence-first research layer—not a chatbot bolted onto search. It routes each request through the cheapest reliable path: the Bill Commons corpus first, direct primary-source retrieval second, and a bounded Solari cloud browser when navigation is useful. Material findings retain the official URL, excerpt, content hash, retrieval mechanism, timestamps, and browser lifecycle.
>
> The 17.80-second demo uses the live public beta. Scout researches Florida HB 625, retains the official bill record and two staff analyses, opens the primary evidence, and reuses the cached result. It then shows the actual recorded Solari run through Florida Online Sunshine: Chapter 43 → §43.16, successful extraction, replay available, and cleanup confirmed. That browser session took 11.689 seconds and cost about $0.00032 at the documented Starter browser rate.
>
> Live product: https://billcommons.org/scout
> Reproducible Solari example: https://github.com/GDACS-droid/solari-cookbook/tree/1233dd2ac0782d91cb234359af021f2f0890ae2a/examples/bill-commons-scout-py
>
> Built with AI as production software for Bill Commons—not as a throwaway demo. @harrychow_ @getsolari

Before posting: attach the final MP4 natively, review every claim, and keep both
`@harrychow_` and `@getsolari` visible in the final platform-native post.
