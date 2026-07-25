# How Bill Commons should acquire bill text (and keep it fresh)

**Status:** analysis 2026-07-25. Recommendations 1 and 2 are actionable now;
recommendation 3 is blocked on a licensing question that needs a human answer.

## Where we are

Metadata comes from **Open States** (bulk session CSV + v3 API), which supplies
bill records and *document URLs*. The full text itself is then fetched by
crawling each state's own site, one document at a time.

That design is correct on provenance -- the text is taken from the authoritative
source, not a re-publisher -- but it means every one of 51 sites' individual
misconfigurations becomes our outage. The record so far this month:

| failure | scope | status |
|---|---|---|
| Omitted TLS intermediate certificates | MI, MS, CT (~37k docs) | fixed (`aia.py`) |
| NUL bytes rejected by Postgres | corpus-wide | fixed |
| Immortal failing jobs deadlocking the queue | corpus-wide | fixed |
| robots.txt `Disallow: /` | TN, DC | **no technical fix -- see below** |

As of 2026-07-25 the crawl reaches **18.6% of obtainable text** (37,907 of
203,940) and sustains ~3,270 documents/hour.

## Verified facts about the blocked jurisdictions

Both were re-checked directly on 2026-07-25 rather than taken on trust, and
**both remain correctly classified as blocked**:

**Tennessee** — `https://www.capitol.tn.gov/robots.txt` grants an explicit
allow-list to named crawlers (Googlebot, Bingbot, Slurp, DuckDuckBot,
SiteimproveBot, ia_archiver, archive.org_bot) and then closes with:

```
User-agent: *
Disallow: /
```

We are not a named agent, so we are disallowed. 8,915 documents.

**District of Columbia** — `https://lims.dccouncil.gov/robots.txt` is
`User-agent: * / Disallow: /`. 1,530 documents. Note that the *main* council
site (`dccouncil.gov`) allows everything (`Disallow:` with an empty value), so
if bill text is reachable under that host it may be a sanctioned path; this has
not yet been confirmed and should be checked before assuming DC is closed.

Both jurisdictions grant named exceptions today, which means **asking is a real
option** and is the cheapest one. A public-interest, non-commercial project
requesting to be added to an allow-list is an ordinary request, and a sanctioned
channel is strictly better than any workaround.

## Recommendations

### 1. Keep direct-from-state as the primary source

Provenance matters for a platform republishing law, and it carries no
third-party licensing constraint. The fragility is real but it is now three
fixed bugs' worth of hardening rather than a recurring tax, and the stall
detector (`billcommons_ingest.healthcheck`) means the next silent failure is
caught in minutes instead of hours.

### 2. Make the UPDATE path change-detection driven, not re-crawl driven

This is the highest-value change available and it does not depend on anyone's
permission.

Re-crawling 204k documents to discover which handful changed is the wrong
shape. Both Open States bulk archives and LegiScan's bulk API expose a
per-session archive with a version hash (`dataset_hash` in LegiScan's case);
storing that hash and comparing on the next pass identifies exactly which
sessions changed, so only their bills' documents need re-fetching. That turns
steady-state refresh from a 204k-document problem into a few-thousand-per-month
problem, and it makes the refresh cost proportional to actual legislative
activity rather than to corpus size.

### 3. LegiScan as a fallback for TN and DC only — BLOCKED on licensing

`getBillText` returns full text for all 50 states plus DC and Congress, which
would close exactly the two gaps we cannot crawl.

Two caveats, neither of which should be glossed:

* **Volume.** The free public tier is capped at **30,000 queries/month**. That
  is nowhere near a 204k-document backfill, but it is comfortably enough for
  TN + DC (10,445 documents, amortised) and for ongoing incremental updates.
* **Redistribution rights are not publicly documented.** Bill Commons
  republishes text, so this is the deciding question and it is *contractual*,
  not copyright: the underlying legislative text is not copyrightable at all
  under the government edicts doctrine, but LegiScan's terms of service can
  still restrict what we may do with copies obtained through their API. This
  needs a human answer from LegiScan before any integration is written.

**Do not implement 3 before 4.**

### 4. Ask TN and DC for access first

Cheapest, cleanest, and it removes the licensing question entirely. TN's
robots.txt already demonstrates they maintain a named allow-list.

## What is explicitly NOT on the table

Ignoring or working around `robots.txt`. It is respected and never bypassed
(SPEC, "Security"). A blocked jurisdiction is documented as an honest coverage
limitation -- see `dc-tn-fulltext-limitations.md` -- not routed around with a
spoofed user-agent, a proxy, or a headless browser. The three TLS-broken states
were fixed because their text was *sanctioned and reachable* and only our
client was at fault; that is a categorically different situation from a site
that has told us not to crawl it.

## Sources

- LegiScan API: https://legiscan.com/legiscan
- LegiScan bulk datasets: https://legiscan.com/datasets
- LegiScan API manual (v1.91): https://api.legiscan.com/dl/LegiScan_API_User_Manual.pdf
- DC LIMS: https://lims.dccouncil.gov/
- Retrieved and verified 2026-07-25.
