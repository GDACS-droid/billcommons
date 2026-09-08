# Data reliability control plane — 2026-09-08

Bill Commons now has a deployed data-health report, durable official-source
observations, public evidence endpoints, and a shared upstream request budget.
The official observer has attempted all 59 registered targets across the 50
states and DC. This is an operational slice of the autonomous intelligence
mission, not a claim of statewide completeness, autonomous repair in every
state, or a freshness SLA.

## Deployed units and proof

| Unit | Source | Deployment | Evidence |
| --- | --- | --- | --- |
| API | `5126becb1fb9` | `cc670ca3-a419-4072-a03a-bdcf46e70f94` | 16 passing samples over 15 minutes; health, readiness, report, bill, web and MCP; live Scout owner canary |
| Sync worker | `c05ae429bb1f` | `2a8f43f3-912a-41c5-94c5-37374872ea3c` | Old deployment absent through its nominal wake; new bounded cycle and stable follow-up samples |
| Web | `f610d67c7e4e` | `dpl_HSW3n7vpBKA8ji9hw1UWS75e7Rex` | Scout-enabled build promoted; actual desktop/mobile research, cache reuse and vote evidence link passed |
| Official observer | `e155da72e7a8` | `a19bbb06-8a15-49ed-ad56-9d0e1daf0c32` | CA content comparisons plus FL source-only snapshot; all 59 targets restored and two healthy subsequent cycles |
| Scout worker | `5126becb1fb9` | `163338d8-a618-4642-a878-4a992dd184f5` | Exact old/intermediate process absence, bounded readiness, completed owner vote canary and cached public UI proof |

The additive migration from `0025` through `0030` was applied after creating a
3,211,108,251-byte backup and restoring it into an owned PostgreSQL 18 cluster.
The restored counts for all 13 checked tables, retained raw hashes and API
checks passed. No destructive rollback migration is part of this release.

The initial observer pass completed at **2026-09-08 06:39:31 UTC**: 43 successful
outcomes, 5 invalid archives, and 10 failed captures/discovery attempts. Of 51
landing-page discovery targets, 42 succeeded and 9 failed. A successful landing
page fetch proves a bounded link observation, not current legislation or source
completeness. California had one successful action-delta observation, five
invalid archives, and one capture failure.

The five invalid CA archives exposed a member-count cap that was smaller than
real source archives. The separately scoped candidate `815be4a82fb3` raises the
cap from 32 to 256 while preserving wire, decompression, per-member, ratio,
CRC, encryption and duplicate-name checks. Its five-family review passed and
the advocate concerns were resolved against source and actual retained-archive
replay. The deployed artifact excludes the held TX-only candidate.

After the Thursday canary passed, four other reproduced cap failures received
one bounded retry each. At **2026-09-08 06:51:25 UTC**, all 58 targets were enabled
with 48 successful and 10 failed latest observations. All six captured CA
weekday archives had completed comparisons: Mon 275, Tue 123, Wed 402, Thu 346,
Fri 244 and Sat 28, totaling **1,418 bill/archive pairs**. These are comparisons,
not unique bills, newly inserted actions or automatic corpus repairs. Hashes
and sample comparisons were replayed through the public evidence APIs; the
previous invalid observations remain in history. The Sunday capture and nine
landing-page failures remain explicit.

Final API/readiness/bill/web/MCP probes passed at 06:52 UTC. The implemented local
report still had 17 warnings and 17 informational conditions, with no critical
or error-level condition in that check set. The live browser at 06:53 UTC showed
six CA delta successes and both CA source failures, downloaded the actual
Thursday archive through its page link and verified its hash, and confirmed
mobile layout without horizontal overflow or browser/HTTP errors.

At **2026-09-08 08:40 UTC**, the subsequent official-worker release replaced the
unstable history-ID assumption with `ca-action-content-multiset/1`. Its Thursday
canary compared 346 bills: 8,521 official records had corresponding local
content, with 195 additional local records retained for investigation. All 346
output hashes and summaries matched the candidate benchmark when both retained
input hashes matched. Two comparisons also passed a complete public-blob replay.
Content agreement does not establish action occurrence identity; local excess
does not establish duplication or authorize deletion. Older comparisons retain
their original versions and remain available in observation history.

All 58 targets were restored with their cadence and backoff preserved. Two
post-restoration worker cycles, public desktop/mobile interactions, the linked
archive download and hash, and a fresh API/readiness/bill/web/MCP sample passed.
The source changes passed an accepted canonical review chain covering nine
families, including corrective-diff reviews and recovered full-diff legs. This
is not nine fresh reviews of the final commit; the original terminal HALT
records and the exact coverage adjudication are retained.

The sanitized [deployment summary](evidence/reliability-20260908/deployment-summary.json)
binds the current deployment IDs, archive/image hashes, outcomes and remaining
full-objective gaps.

Live browser proof exercised 51 jurisdictions, state search, issue filtering,
no-match and clear states, CA evidence details, two actual bill-evidence links,
mobile overflow and a German browser locale. There were no observed console,
page or HTTP errors during those interactions. These are bounded checks, not
proof that every route and device has been tested.

## What the system establishes

- `/data-health` and `GET /api/v1/data-health` distinguish overdue local syncs,
  deferred work and unavailable checks. The collector uses a read-only,
  repeatable-read transaction, five-second statement limits, a five-minute
  process cache and a nonblocking refresh lock. An expired report is never
  returned as fresh after collection fails.
- Official targets have durable cadence, failure backoff, retained response
  hashes, source timestamps when supplied, and explicit outcomes. The observer
  uses a per-target row lock, a bounded observation transaction, and shutdown
  handling that stops new claims and allows the current observation to finish.
- California action comparisons retain the official archive, local snapshot
  and structured diff. A newly confirmed source limitation is that history IDs
  change across archives, including for unchanged historical action content;
  sequence numbers can also change. New comparisons use versioned content
  multisets that preserve raw records and multiplicity without inventing stable
  occurrence IDs. Historical identity-based comparisons remain visible with
  their original versions. Neither version authorizes automatic CA action
  mutation.
- Public official-evidence and bill-history endpoints expose retained source
  observations, comparisons and update evidence. Evidence added prospectively
  does not reconstruct provenance that older ingestion never stored.
- A database-backed source budget serializes reservations across processes and
  preserves pacing across midnight. The release conservatively seeded the
  current UTC day at 225 of 225 requests, granting no extra allowance. The new
  sync cycle deferred 17 jobs to future eligibility instead of treating the
  quota boundary as 17 ordinary source failures.

`LOCAL_SYNC_OVERDUE` measures the last successful incremental OpenStates sync,
not a bulk import or one-off repair. The sync service still wakes every 86,400
seconds even though scheduling policy expresses finer targets. This release
therefore does not promise those finer execution cadences. Pending age never
authorizes reclaiming work from a live owner.

## Remaining release gates

The final Texas nested FTP-to-HTTPS repair fix passed 70 focused PostgreSQL 16
checks and the confirmed Unicode admission defect was repaired. Its canonical
full-diff DeepSeek reviewer remained unavailable after the bounded recovery and
one automatic retry, ending with `HALT`. A smaller ASCII-fix review does not
satisfy that missing full-diff review. Texas repair is off, no successful Texas
repair canary is claimed, and no further blind reviewer retry is scheduled.

The generic crawl worker remains on its previous artifact. It continuously
claims work and has no deployed claim-pause control or graceful shutdown
handler. A database view with zero committed running rows is insufficient:
this worker holds its claim transaction across the HTTP request. It was not
terminated to force deployment. The official worker's tested pause mechanism
and the sync worker's observed sleeping boundary do not establish a drain
contract for that older process. An explicit one-time replacement approval has
been requested; the previous worker remains running pending that answer.

Source failures remain visible. The first discovery pass recorded robots
restrictions/unavailable robots for CA, HI, NY and TN; a rejected DE redirect;
a JavaScript-rendering requirement for IN; and timeouts for ID, MT and WY.
Those observations do not authorize bypassing source access controls. The CA
Sunday capture failed without a retained archive, so its exact cause is not
established by the stored error class alone.

## Commercial, analytics and discovery findings

At **2026-09-08 06:21:27 UTC**, application aggregates showed 2 customer rows,
1 usable live free developer key, and 3 metered requests from 1 key in the
current UTC date plus the preceding six calendar dates. There were no paid-plan
keys, Stripe-linked customer rows, subscription rows, active/dunning
subscriptions, Stripe-event rows, metered MCP calls or heavy requests. These
counts establish neither two organic users nor an audit of Stripe-account
revenue.

The existing restricted Stripe credential could not read account, webhook-list
or configured-price details. Payment activation remains subject to the actual
account owner's evidence in the [monetization runbook](monetization-runbook.md).
No purchase, charge or credential change was performed. The deployed data-only
web artifact preserves the prior billing/auth components; candidate funnel
changes in the integration branch are not claimed deployed.

Vercel reported the owning team's Pro plan and enabled Web Analytics. The CLI
command used during the earlier audit can enable Analytics; prior enablement
was not established. No plan upgrade was purchased. The live browser loaded
the configured Analytics SDK script successfully with no analytics warnings,
but the automated probe did not establish pageview or custom-event ingestion.

Public robots, sitemap, llms.txt, API docs, homepage JSON-LD and team/government
technology discovery copy were checked. Google/Bing verification metadata can
be supplied through server configuration, but their tags were absent in the
live page. Console or DNS ownership, sitemap submission, indexing, rankings,
and inclusion in AI answers remain unverified. No outreach was sent.

## Verification and retained evidence

The accepted deployed baseline `c05ae42` passed 542 shared/worker tests, 56 API
tests and 29 migration-guard tests in the documented disposable environments.
Canonical `verify-ship` findings were reconciled against actual source and
runtime evidence; its accepted release adjudication has no missing required
reviewer results. This does not waive the distinct unresolved Texas review.

The data-only web candidate passed focused tests, lint, TypeScript/build checks
and rendered interactions, followed by the live checks described above. The
CA member-cap candidate passed 13 focused parser tests and replay of all six
retained weekday archives, plus the 256/257 boundary and unchanged action output
when auxiliary ZIP entries precede required tables. The official pause test used real PostgreSQL 16 locks: 57 unlocked
targets paused while the active target retained its lock, then the final target
paused after committing its cursor. Cadence, failure counts and cursor state
were preserved; repeated pause and inventory/advisory-lock refusals were tested.

Release evidence is retained under the owned local directory
`~/.local/share/billcommons/reliability-release-20260908/`, including immutable
source/archive hashes, provider deployment/image bindings, backup and restore
proof, migration proof, review adjudication, bounded monitor results and live
browser artifacts. Do not publish credential files or raw customer data with
these operational records.
