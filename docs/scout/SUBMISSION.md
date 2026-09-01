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

- Reproducible Solari example:
  `https://github.com/GDACS-droid/solari-cookbook/tree/billcommons-scout-challenge/examples/bill-commons-scout-py`
  at public commit `1233dd2`.
- Bill Commons Scout feature branch:
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

The latest backend/operations Bill Commons suites total **862 passed / 8 skipped**,
with 10 passing web contracts plus passing targeted lint, TypeScript, production build,
PostgreSQL concurrency/restart/vertical storage proof, live Florida discovery, and a
post-repair live Solari product-path lifecycle. Exact commands are in
[TESTING.md](TESTING.md).

## Truthful 20-second demo

[Watch the 20.16-second artifact](demo/scout-demo.mp4), inspect the
[poster](demo/scout-demo-poster.png), [two-frame Solari proof](demo/scout-solari-live-proof.png),
and [verification record](demo/VERIFICATION.md).

Suggested narration:

1. “Bill Commons already normalizes legislation. Scout investigates the official
   record around it and keeps the evidence.”
2. “This HB 625 case ties the Senate bill action to Chapter 2026-141, then verifies
   the current §43.16 text from a second primary source.”
3. “The product segment uses deterministic durable job fixtures and says so on
   screen. This next sequence is the actual Solari run: chapter contents, click
   §43.16, exact result, recording available, cleanup confirmed.”

No dead opening, no fake timer, no secret replay URL, and no claim that the browser
proved more than it did.

## Live proof and economics

The real run passed in 10.137 seconds with one page, two actions, 38 admitted routed
requests, recording/replay available, and confirmed release. At current first-party
browser rates, that is approximately $0.00042 Free / $0.00028 Starter / $0.00020
Professional. An equivalent fresh cache hit launches no browser. See
[LIVE_TESTS.md](LIVE_TESTS.md) for calculations and claim boundaries.

## Analytics and traction boundary

The existing Vercel Analytics integration records Scout open, example selection,
job create/start/partial/complete/fail, cache hit, evidence open, replay resolve/open,
and related product events. There are no public adoption, retention, revenue, or
usefulness metrics yet. Do not fabricate them.

## Known limitations

- Florida-only P0; no arbitrary URL input or generic 50-state planner.
- Browser navigation remains narrowly allowlisted; generic clicks/downloads require
  additional policy and tests.
- Scout blobs in Postgres are appropriate for bounded P0 volume; object storage is a
  future scale step behind the existing interface.
- Scout is dark-deployed behind a one-account server allowlist. Production inventory,
  additive migration, backup/restore, monitoring, and exact-image rollback are proven;
  public API/web/navigation enablement still requires explicit owner authorization.
- Stripe live-account webhook verification is separate and incomplete.

## Recommended X post — owner sends later

> I built Bill Commons Scout as a permanent evidence-first research layer. It checks our structured legislative corpus, uses direct official sources when they are enough, and invokes a real @getsolari browser for the messy government web. Here is the actual Florida Legislature run: [demo link] [code link] @harrychow_

## Recommended LinkedIn post — owner sends later

> Legislative databases normalize bills, but professional research also depends on amendments, staff analyses, agendas, hearings, and older government portals. I built Bill Commons Scout as a permanent evidence-first layer: structured corpus first, direct primary sources second, and a bounded Solari browser only when interaction is useful. Findings retain the source, excerpt, hash, retrieval path, and browser lifecycle. The demo shows the deterministic product workflow and then the actual recorded Florida Legislature browser sequence, including confirmed cleanup. [demo] [code]

Before posting: verify the final public Bill Commons commit/link, attach the MP4
natively, review every claim, and explicitly tag `@harrychow_` and `@getsolari`.
