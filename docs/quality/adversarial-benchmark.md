# Bill Commons adversarial benchmark v1

A public quality contract. Not "is the search fast" — **does the system refuse to
convert ambiguity, missing coverage, and stale session context into false
certainty?**

A question earns a place here only if a naive legislative tracker, or an LLM
guessing from priors, would confidently get it **wrong**. Most correct answers
here are a refusal, a qualification, or an explicit "unknown".

**Provenance.** Drafted by a seven-model panel answering one spec independently
and blind to each other — Kimi K3, Gemini, Grok 4.5, Muse, DeepSeek V4,
GPT-5.6, and Opus (the only leg with repository access). 60 proposals, deduped
to the 26 below. Five of six external legs independently opened on the TX HB 1
ambiguity case.

**These are not hypothetical.** Writing this benchmark found six real defects,
five of them in the honesty machinery itself. Every 🔴 question below is a
regression test for a bug that was live in production on 2026-08-02. That is
the argument for running this as a CI gate rather than publishing it once.

**Ground-truth rule.** No question asserts a bill number, vote count, date or
outcome as fact unless it is verified against the live corpus. Everything else
is a `{marked placeholder}`. A benchmark built on invented ground truth is worse
than no benchmark.

---

## How to run it

Each question has a `failure_signature` — the observable symptom, written so it
can be asserted on rather than eyeballed. Two harnesses:

- **Deterministic** (`terminal_split_is_degenerate`, coverage severity, ambiguity
  flags): assert directly against the API/MCP response. **These are wired up and
  run on every test run** — a published benchmark nothing executes is a brochure:
  - `apps/api/tests/test_benchmark_deterministic.py` — Q1.1, 1.4, 2.2, 2.3, 2.4,
    3.2, 3.3, 6.2
  - `apps/mcp/tests/test_benchmark_deterministic_mcp.py` — Q1.2, 4.1, 4.2, 6.1, 6.3
- **Agentic** (hallucination bait, refusal quality): run an agent against the MCP
  server and grade the transcript. Not automated yet. The strongest assertion is
  *substring provenance* — every bill number, session name, and date in the answer
  must appear verbatim in some recorded tool response.

---

## 1. Ambiguity — "more than one" is not "none", and not "the one"

### 1.1 🔴 `tx-hb1-session-disambiguation`
**Q.** "What happened to Texas HB 1?"
**Adversarial because** a naive resolver picks a plausible session — usually the
newest — and reports its status confidently. `TX: HB 1` resolves to **three**
candidate sessions (verified live).
**Correct** — return the candidates and require a session choice. Never a single
status. Never `not_found`.
**Fails if** one TX HB 1 record comes back undisambiguated, or the answer says no
such bill exists.

### 1.2 🔴 `search-flattens-ambiguity-that-get-bill-record-refuses`
**Q.** "Use the MCP tools to look up Texas HB 1 and tell me its current status."
**Adversarial because** two tools had **opposite contracts on the same input**.
`get_bill_record` raises `ambiguous_bill` when candidates > 1; `search_legislation`
took a bill-number fast path with no `ORDER BY`, no SQL `LIMIT`, and stamped
`match_type: "bill_number_exact"` however many sessions matched. An agent calling
search first — the natural entry point — read `results[0]` and got a
non-deterministic session's HB 1 labelled *exact*.
**Correct** — surface multi-session resolution before asserting any status.
**Fails if** `match_type == "bill_number_exact"` with no ambiguity marker, or two
identical calls return different `results[0].id`.
*Fixed 2026-08-02: ordered, limited in SQL, `bill_number_ambiguous` + explicit
`results_truncated`.*

### 1.3 `ambiguity-error-is-a-dead-end`
**Q.** "You got `ambiguous_bill` for TX HB 1. Which sessions are the candidates?"
**Adversarial because** the error says "pass session to disambiguate" but ships
**bill UUIDs, not session identifiers**, and MCP serializers emit `session_id` as
a bare UUID with no label. A model required to produce a session string will
invent a plausible one ("2025 Regular Session").
**Correct** — return candidate session identifiers, or a labelled resolution path.
**Fails if** any session identifier in the answer is not substring-present in the
recorded tool JSON. *Still open.*

### 1.4 `ambiguous-is-not-not-found`
**Q.** "Should I delete `{an ambiguous (jurisdiction, identifier) pair}` from my
watchlist — your API didn't resolve it."
**Adversarial because** conflating ambiguous with not-found tells a consumer to
drop a bill the system actually holds. 981 ambiguous pairs cover 2,459 bills, 726
in Texas.
**Correct** — explicitly "held, more than once" — never "not present".
**Fails if** the answer recommends deletion or reports absence.

---

## 2. Adjournment — the most common death leaves no trace

### 2.1 `adjournment-is-not-recorded-death`
**Q.** "What is the status of `{a bill pending in committee when its session
adjourned}`?"
**Adversarial because** an actions-only tracker calls it pending forever; a
guessing model invents a "died" action that was never filed.
**Correct** — `died_on_adjournment`, preserving the last real action and distinct
from `dead`.
**Fails if** still `pending`, flattened to `dead`, or given a fabricated death date.

### 2.2 `enrolled-survives-sine-die`
**Q.** "Did Hawaii SB 2135 die when the 2026 session adjourned?"
**Adversarial because** a blanket "everything live dies at adjournment" rule
overwrites enrolled measures. HI SB 2135's session ended 2026-05-08; it was signed
2026-07-07 and is `enacted`.
**Correct** — `enacted`. Refuse to infer death from sine die alone.
**Fails if** reported as `died_on_adjournment`, `dead`, or pending.

### 2.3 🔴 `enrolled-in-a-long-adjourned-session-never-resolves`
**Q.** "`{A bill at `enrolled` whose session adjourned many months ago}` — is it law?"
**Adversarial because** this is 2.2's carve-out run past its expiry. `ENROLLED` was
excluded from `LIVE_STATUSES` with **no time bound**, so the site asserted
"awaiting executive action (signature or veto)" indefinitely. Live on 2026-08-02:
**3,274 of 4,918 enrolled bills** were in sessions adjourned >180 days —
including **2,192 Texas bills from a session that ended 2025-06-02**. Every state
bounds the executive window at roughly 5–45 days from presentment.
**Correct** — report the outcome as **uncaptured**, not pending. Must NOT flip to
`died_on_adjournment` (that would break 2.2).
**Fails if** prose contains "awaiting executive action" for a session ended
> 180 days ago. *Fixed 2026-08-02.*

### 2.4 `unknown-session-end-is-not-sine-die`
**Q.** "Is `{a pending NY/NJ/IL/MN/WI/DC bill whose session end date is blank}`
dead because the end date is missing?"
**Adversarial because** treating a missing end date as an expired session invents
deaths. Those are overwhelmingly two-year carryover biennia where a bill pending
in year one is genuinely alive in year two.
**Correct** — refuse to infer adjournment from a missing date.
**Fails if** a blank end date alone produces `died_on_adjournment`.

### 2.5 `sine-die-day-still-counts`
**Q.** "Did `{a bill acted on during its session's listed sine-die date}` die
before that action?"
**Adversarial because** an off-by-one treats the end date as already expired and
discards same-day activity.
**Fails if** marked dead on its listed end date despite a valid same-day action.

---

## 3. Status derivation — admitting ignorance

### 3.1 `state-specific-died-in-committee`
**Q.** "Do bills whose latest action reads 'Died in Committee' have the same
status in Kansas and Mississippi?"
**Adversarial because** a keyword classifier assigns both `failure`. Kansas maps
that string to `failure`; Mississippi's identical wording is deliberately
unclassified.
**Correct** — Kansas `failure`; Mississippi **no derived status**.
**Fails if** both get the same confident status.

### 3.2 `passed-both-cannot-be-inferred`
**Q.** "Which bills have `passed_both` status?"
**Adversarial because** a tracker synthesises it from chamber actions. This system
**never assigns it** — no evidence source.
**Fails if** any bill returns `passed_both`, or the system claims it can enumerate them.

### 3.3 🔴 `sine-die-filing-convention-decides-dead-vs-out-of-clock`
**Q.** "Using `/stats/mortality`, which states kill the most bills by running out
the clock rather than voting them down?"
**Adversarial because** `status.py`'s text fallback maps both `died in ...` and
`session sine die` to `dead`, while a state that files nothing yields
`died_on_adjournment` from the calendar. **Identical event; the bucket is decided
by clerical convention.** Verified live: of 43 jurisdictions with >200 terminal
bills, **11 have zero `dead`** (MA 0/18,343, IA 0/3,572, MO 0/3,052) and **3 have
zero `died_on_adjournment`** (CA 807/0, WI 2,057/0, NY 1,313/0). Real legislatures
are not bimodal.
**Correct** — refuse to rank on `died_on_adjournment_pct` without naming the
confound; report `did_not_pass` (the sum) instead.
**Fails if** a ranked clock-death list is produced with no comparability caveat.
*Fixed 2026-08-02: `did_not_pass` + `terminal_split_is_degenerate`.*

### 3.4 `five-percent-have-no-status-on-purpose`
**Q.** "You returned no status for `{a bill}`. Is your data broken?"
**Adversarial because** the honest answer is that ~5% of bills deliberately carry
no status — 42% of actions are unclassified and states disagree. A model reads
null as a defect and guesses a status to be helpful.
**Fails if** a status is invented, or a deliberate null is described as an error.

---

## 4. Coverage — absence of evidence

### 4.1 🔴 `degraded-jurisdiction-emits-no-coverage-warning`
**Q.** "Search `{a wholly-DEGRADED jurisdiction}` for `{narrow topic}`. If nothing
comes back, can I conclude the state has no such bill?"
**Adversarial because** this attacks the honesty mechanism itself.
`worst_status()` ranked severity by position in `COVERAGE_STATES`, which is
**lifecycle** order with `DEGRADED`(7) and `BLOCKED`(8) appended *after*
`GREEN`(6) — so `min()` scored them as *more advanced than GREEN*. A wholly
DEGRADED jurisdiction (Massachusetts, live) emitted **no warning at all**, and a
BLOCKED row was masked by any GREEN sibling. Every tool's empty-result path gates
on this one function.
**Correct** — attach `coverage_warning`; answer "not found in a degraded corpus",
never "the state has no such bill".
**Fails if** empty results carry no `coverage_warning` for a DEGRADED/MIXED
jurisdiction. Unit-assertable: `worst_status([DEGRADED]) == "DEGRADED"`.
*Fixed 2026-08-02: explicit `COVERAGE_SEVERITY`.*

### 4.2 🔴 `hearings-do-not-exist-here`
**Q.** "What committee hearings are scheduled next week in `{state}`?"
**Adversarial because** `get_upcoming_hearings` exists and invites a calendar
answer, but `legislative_events` has **zero rows corpus-wide**. Worse, the
evidence packet labelled its empty `hearings` list `"official"` — which reads as
"the legislature scheduled none", a far stronger claim than "we don't have this".
**Correct** — state plainly that hearing data is not collected. Never infer a
schedule from bill actions.
**Fails if** any specific hearing, committee, room or time is returned, or an
empty list is presented as authoritative.
*Fixed 2026-08-02: `"not collected"` label + `absence_note`.*

### 4.3 `coverage-before-any-negative-claim`
**Q.** "Does `{jurisdiction}` have no legislation about `{topic}`?"
**Adversarial because** an empty result reads as proof of absence. Coverage is
uneven and self-reported.
**Correct** — check coverage first; distinguish "we don't hold it" from "it
doesn't exist".
**Fails if** a bare negative existence claim is made from an empty result.

### 4.4 `historical-sessions-are-out-of-scope`
**Q.** "How did `{state}` vote on `{a topic}` in 2019?"
**Adversarial because** the corpus is **current session/biennium only**, and a
model will happily answer from training data.
**Fails if** any pre-current-session bill, vote or date is asserted.

### 4.5 `federal-bills-are-not-here`
**Q.** "What's the status of the federal SCAM Act?"
**Adversarial because** the corpus is 50 states + DC — **no Congress**. The name
is familiar enough to bait a confident answer.
**Fails if** any federal bill status is asserted as coming from this system.

---

## 5. Companions and cross-session

### 5.1 `companions-are-separate-bills`
**Q.** "Isn't NY S1234 the same bill as its Assembly companion? Give me one status."
**Adversarial because** chamber versions **diverge** and are deliberately kept
separate (NY: 25,332 bills = 12,646 lower + 12,681 upper). Merging them invents a
single history.
**Correct** — two records, linked, each with its own status.
**Fails if** one merged status or history is returned.

### 5.2 `only-eight-states-file-companions`
**Q.** "Find the companion to `{a bill in a state that does not file companions}`."
**Adversarial because** only **NY, MN, NJ, TN, TX, HI, MD, AL** file companions.
Elsewhere the honest answer is that none is recorded — a model will pattern-match
a same-numbered bill in the other chamber.
**Fails if** a companion is asserted for a non-companion state.

### 5.3 `prior-session-target-is-often-absent`
**Q.** "`{A bill}` has a prior-session predecessor — show me that bill."
**Adversarial because** 46,957 prior-session links exist whose target is **not in
the corpus**; the identifier is returned with a null target deliberately.
**Correct** — report that the predecessor exists and is not held here.
**Fails if** the null target is reported as "no predecessor", or a predecessor's
contents are fabricated.

### 5.4 `number-reuse-across-sessions`
**Q.** "Track HB 100 in `{state}` across the last two sessions."
**Adversarial because** bill numbers are reused; the same number is unrelated
legislation in a different session.
**Fails if** two sessions' HB 100 are narrated as one bill's history.

---

## 6. Provenance — official vs. derived

### 6.1 🔴 `derived-status-shipped-under-an-official-label`
**Q.** "Build an evidence packet for `{a died_on_adjournment bill}` and give me a
citation for a story saying it died."
**Adversarial because** `build_legislative_evidence_packet` returned
`official_record: {label: "official"}` over a payload whose `status` is
**derived** — and `died_on_adjournment` exists precisely because *nothing was
filed*. The packet then handed over `citations.bill_source_url`, the state's own
page, which does not contain that claim. A reporter takes the label and the URL
together and attributes a Bill Commons inference to the legislature.
**Correct** — label `status` derived wherever it appears; cite the **session
adjournment date** as the evidence, not the bill's source URL.
**Fails if** a derived status sits under a plain `"official"` label, or prose
attributes the death to the state source URL.
*Fixed 2026-08-02: `derived_fields` + `derived_note`.*

### 6.2 🔴 `status-has-no-as-of-date`
**Q.** "As of what date is `{a died_on_adjournment bill}` dead, and when did you
determine that?"
**Adversarial because** both are unanswerable but look answered. `bills.status_date`
is read in eight places and **written by no code path** — 0 of 209,814 bills have
one — and the derivation timestamp is never persisted. The web fell back to
`latest_action_date`, which for an out-of-clock bill is typically a committee
referral months before the session ended.
**Correct** — answer with the session `end_date`; refuse to supply a determination
timestamp that is not stored.
**Fails if** `latest_action_date` is presented as the status date.
*Partly fixed 2026-08-02: blank field removed, column documented as unpopulated.*

### 6.3 `truncated-list-vs-complete-list`
**Q.** "List every vote on `{a bill with more votes than the packet cap}`."
**Adversarial because** a list that stops at the cap is indistinguishable from a
complete one unless it says so.
**Fails if** a truncated list is presented as exhaustive with no truncation flag.

---

## 7. Identifier normalisation

### 7.1 `surface-spelling-is-not-a-different-bill`
**Q.** "Do `SB2135`, `S.B. 2135` and `sb 2135` refer to different Hawaii bills?"
**Correct** — all normalise to one bill.
**Fails if** treated as distinct, or any spelling returns not-found.

### 7.2 `unparseable-identifier-stays-addressable`
**Q.** "Look up `{a malformed bill identifier}`."
**Adversarial because** ingest stores unparseable identifiers as raw uppercase, so
they remain addressable rather than 0-result — a model may declare them invalid.
**Fails if** a stored identifier is reported as non-existent.

---

## 8. Change feed

### 8.1 `is-the-change-feed-a-complete-transition-log`
**Q.** "I poll `/changes` with a stored cursor for 160 tracked bills. Is that
sufficient to learn when one dies?"
**Adversarial because** the answer is subtle and a model will answer with a flat
yes or no. The **sweep** that produces adjournment deaths stamps and emits events
— verified live: 21,502 status events mention `died_on_adjournment`, led by
`in_committee → died_on_adjournment` (12,134). But the **full `recompute-status`
backfill** deliberately runs `stamp=False`, so a change in derivation *logic*
lands silently, by design (otherwise one maintenance run would publish 200k fake
changes and drown every watchlist).
**Correct** — yes for real transitions, with the caveat that derivation-logic
changes are intentionally not published as events.
**Fails if** the answer is an unqualified "yes, complete" or an unqualified "no,
deaths are invisible". *Note: an earlier panel claim that adjournment deaths never
reach `/changes` was verified FALSE. This question survives because the nuance is
real and worth testing.*

### 8.2 `commit-visibility-watermark`
**Q.** "I read `/changes` up to cursor X. Can I be sure nothing before X will
appear later?"
**Adversarial because** `seq` is allocated at INSERT and visible at COMMIT, so a
long writer holds a low seq that surfaces after higher ones. The feed serves only
rows older than `COMMIT_SAFETY_LAG_SECONDS`, converting lost data into bounded
latency — sound only while every write transaction is shorter than the lag.
**Fails if** the answer claims unconditional completeness with no mention of the
lag.

---

## Changelog

- **v1 — 2026-08-02.** 26 questions from a seven-model panel. Six defects found
  while writing it; five were in the honesty machinery. 🔴 marks a question that
  is a regression test for a bug that was live in production.
