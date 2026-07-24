# DC and TN full text: documented limitations

**Status: no sanctioned full-text channel found for either jurisdiction.**
Bills for both remain fully **metadata-searchable** (number, title, session,
sponsors, actions, official URLs). Only the *inside* of the bill text is
missing, and this file records why, so the gap is a documented limitation
rather than a silent hole.

Researched 2026-07-24. Both verdicts should be revisited if a jurisdiction
publishes a bulk export or an API with terms, or if a direct request to the
clerk's office (below) succeeds.

## The rule this follows

Bill Commons **respects robots.txt absolutely and never bypasses it**
(docs/SPEC.md). A blanket `Disallow: /` is treated as a refusal for the
whole host, including API paths under it. "The endpoint returns HTTP 200" is
a technical fact, not a grant of permission. What we look for instead is a
*separate, sanctioned* channel — an official bulk download, a documented
public API, or an open-data portal — whose own terms permit programmatic
use.

## District of Columbia — NO_SANCTIONED_CHANNEL_FOUND (confidence: high)

`lims.dccouncil.gov` holds both the bill text and the JSON API
(`/api/v1/`, `/api/v2/`) that earlier notes flagged as a possible route. Its
robots.txt is `User-agent: * / Disallow: /` with **no carve-out for the API
paths**, so the API is inside the refusal, not beside it. No DC
Council-published terms of use, developer terms, or API documentation could
be found that would permit programmatic access anyway.

Two sources that *do* have permissive terms don't carry the data:

* `opendata.dc.gov` — CC0 1.0, explicitly "copy, modify, distribute ... even
  for commercial purposes, all without asking permission", and no
  restriction on automated access. But it hosts agency operational datasets
  (crime, GIS, tax, property), **not Council legislation or bill text**.
* `dccouncil.gov` — crawlable, but carries summaries and links back into
  LIMS rather than bill text.

Ruled out: GovInfo (federal only, no DC local legislation). The only "API"
descriptions found were a vendor marketing page and an incomplete
community-reverse-engineered doc — neither is DC Council speaking.

Worth a human's time before calling it final:

* `dcregs.dc.gov` (DC Register) is **unrestricted by robots.txt** and
  publishes Council acts/resolutions/notices. Unconfirmed whether issues
  carry complete text of every *introduced* bill or only enacted/final
  actions — someone should open a few issues and look.
* A direct ask to the Office of the Secretary / `councilperiod@dccouncil.gov`
  about a documented data-request or bulk-export process. Nothing of the
  sort surfaced in search, but absence in search is not a "no".

Unrelated to bills but noted for whoever looks next: `code.dccouncil.gov`
(the codified DC Code, a different corpus) disallows `ClaudeBot` by name,
along with GPTBot, Google-Extended, CCBot and others.

## Tennessee — NO_SANCTIONED_CHANNEL_FOUND (confidence: high)

`capitol.tn.gov`, `www.capitol.tn.gov` and `wapp.capitol.tn.gov` all serve
the same robots.txt: `Disallow: /` for `User-agent: *`, with narrow
exceptions naming seven search-engine crawlers (Googlebot, Bingbot, Slurp,
DuckDuckBot, ia_archiver, archive.org_bot, SiteimproveBot). We are not one
of those, and the blanket rule covers any bulk-data or API surface on those
hosts. No official bulk export, documented API, or FTP channel surfaced.

Two hosts are technically unblocked but unconfirmed as sources of
current-session introduced-bill text:

* `publications.tnsosfiles.com` / `sharetngov.tnsosfiles.com` — S3 buckets
  with no robots.txt object at all. Appear to hold **enacted** Public/Private
  Acts and the Blue Book, which is a different document set from pending
  bill text.
* `digitaltennessee.tnsos.gov` — State Library & Archives (bepress/Digital
  Commons), permissive robots.txt, "Bills and Resolutions, Public Chapters,
  and Legislative Records". Browsable/searchable, but no documented bulk
  download or API, and oriented toward archival rather than live
  current-session records. Its "Copyright" footer link resolves to generic
  Elsevier platform boilerplate, not TN-specific terms.

Both need a human to spot-check what is actually in them (current-session
bill text vs. enacted acts vs. historical archive) before either could be
relied on. A call to the TN General Assembly Clerk's office or the SOS
Publications division is the other obvious move.

Explicitly **not** a substitute: LegiScan is a third-party commercial
aggregator, not a government-sanctioned channel; its redistribution license
would need separate vetting. SPEC lists it as an optional Tier-3 route.

## How this shows up in coverage

As the crawl attempts each DC/TN document and gets refused by robots.txt, it
stamps that document `fulltext_status=robots_disallowed`, which is terminal.
Terminal documents drop out of `full_text_available_count` (see
docs/state-coverage/methodology.md), so both jurisdictions converge toward
`available == 0` — the case where SPEC GREEN criterion #5 ("full text
searchable wherever technically available") is **vacuously satisfied**.

They can therefore reach GREEN, but only carrying an explicit `known_gaps`
line saying no full text is obtainable and the bills are metadata-searchable
only. GREEN must never imply full-text search a user will not actually get.

The ~300 "dead" `fetch_text` jobs on these hosts are correct robots
compliance, not a bug.
