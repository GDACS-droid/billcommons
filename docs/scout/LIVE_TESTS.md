# Scout live tests

All live checks below ran on 2026-09-01. They are explicitly opt-in. No key,
provider session ID, WebSocket/CDP endpoint, cookie, or replay URL is retained.

## Headline public Solari workflow

The public cookbook example authenticated through `solari-browser==0.1.3`, created
one recorded cloud-browser session, opened the Florida Legislature's chapter 43
contents, followed the exact `43.16` link, extracted the current-law judge paragraph
and `s. 1, ch. 2026-141` history, then released the session.

| Field | Observed |
| --- | --- |
| Result | pass |
| Official host | `www.leg.state.fl.us` |
| Runtime | 10.137 seconds |
| Pages / actions | 1 / 2 |
| Routed requests | 38 of 48 maximum |
| Recording | enabled |
| Replay | available; capability URL withheld |
| Cleanup | confirmed |
| HTML SHA-256 | `2e70542918802d1e9e744ea98d8e7ecfc911731cbdca1f98fcd76cc7a9bb7e3c` |

Two reviewed same-session frames show the chapter link before navigation and the
section after navigation. The public branch is commit `1233dd2`. Twelve free tests
cover the same extraction and lifecycle contract. Earlier bounded Florida Senate
browser attempts reset during navigation and were released; direct HTTP on that host
remained healthy, which is why partial results and cheap-source-first routing matter.

## Live Florida incremental discovery

An opt-in API/worker test used the guarded disposable PostgreSQL database and the
official Florida Senate HB 625 page:

- Bill page: `https://flsenate.gov/Session/Bill/2026/625/ByCategory`
- Direct bill evidence retained `HB 625` and `Chapter No. 2026-141`.
- The page disclosed bill-scoped official staff analyses under `/Analyses/`.
- Scout fetched bounded same-session/same-bill PDFs, required `application/pdf` plus
  `%PDF-`, extracted bill-supported text, and retained at least one related finding.
- Browser provider was unused; status was `completed`.

The run first exposed two real defects: a 256 KiB limit rejected ordinary 419–455 KiB
official analyses, and an active session with null dates/source URL could shadow the
usable current bill under PostgreSQL null ordering. The product now uses a 2 MiB
payload limit with isolated child-process PDF resource caps, sorts session dates with
nulls last, and reports a source-less corpus hit as `official_source_missing` without
emitting an evidence-free finding. The live test passed again after those repairs.

## Cost estimate

Current first-party pricing checked at `https://docs.getsolari.com/pricing` on
2026-09-01 lists browser runtime at $0.15/hour Free, $0.10/hour Starter, and
$0.07/hour Professional. Applying those rates to the measured 10.137-second session:

- Free: approximately **$0.00042** per uncached run;
- Starter: approximately **$0.00028**;
- Professional: approximately **$0.00020**.

At identical runtime, the included $3 Free credit is roughly 7,100 runs; Starter's
200 included browser-hours are roughly 71,000 runs; Professional's approximately
2,850 included browser-hours are roughly 1.01 million runs. These are arithmetic
estimates, not billing guarantees, and exclude proxy/CAPTCHA usage (neither was used).
A fresh equivalent Scout cache hit creates no browser session, so browser cost is
zero until freshness expires or the source needs revisiting.

## Internal lifecycle checks

The native Scout provider also has opt-in SDK/API product-path coverage for durable
session creation, browser-required classification, release/replay, and reaping. The
small `billcommons-scout solari-check` remains an infrastructure check; it is not the
challenge showcase and makes no government-finding claim. Its bounded provider smoke
directly opens Online Sunshine §43.16 in one page/one navigation with no click,
checks only the stable `43.16` and `Justice Administrative Commission` markers, then
releases the session and probes replay. Its sanitized output labels that direct,
no-click capture and withholds the provider session ID and replay capability URL.

For an authorized named-account release canary, use
`billcommons-scout solari-lifecycle-canary --email <named-canary-email>` with
`BILLCOMMONS_SCOUT_SOLARI_LIFECYCLE_CANARY=1`. Unlike `solari-check`, it is a
durable operator validation: it rejects a disabled/public/non-allowlisted/absent
account, creates one fixed private owner-scoped job, persists the session ID before
connection through `ScoutRunner`, retains the official source and raw bytes, and
uses the ordinary release/reaper path. It deliberately creates **no finding** and
is one-shot once terminal for that account. The command's fixed output omits account,
job, provider-session, source URL/body, cookies, replay, and capability details.
This command has deterministic test coverage only at this point; no additional live
provider call has been made by this change.

After the final single-owner lifecycle repair, the opt-in real product-path test ran
one Solari session through the Bill Commons runner in **7.86 seconds** and verified a
`released` terminal ledger state. The provider uses a one-shot create; an outcome-
unknown create retains a conservative global/cost hold because no provider ID exists
to release.
