/**
 * The adversarial benchmark, as structured data.
 *
 * This module is the single source for the published contract at /quality.
 * The narrative version lives at docs/quality/adversarial-benchmark.md and the
 * executable half lives in apps/api/tests/test_benchmark_deterministic.py and
 * apps/mcp/tests/test_benchmark_deterministic_mcp.py -- keep the three in step.
 *
 * `status: "fixed"` means the question is a regression test for a defect that
 * was live in production on 2026-08-02. Writing the benchmark is what found
 * them; that is the point of publishing it rather than filing it.
 */

export type BenchmarkStatus = "fixed" | "holds" | "open";

export interface BenchmarkQuestion {
  id: string;
  question: string;
  /** The confident wrong answer a naive tracker or a guessing model gives. */
  trap: string;
  /** What a correct system returns — usually a refusal or a qualification. */
  correct: string;
  status: BenchmarkStatus;
  /** True when a machine assertion covers it today. */
  automated: boolean;
}

export interface BenchmarkSection {
  key: string;
  title: string;
  blurb: string;
  questions: BenchmarkQuestion[];
}

export const BENCHMARK_VERSION = "v1";
export const BENCHMARK_UPDATED = "2026-08-02";

export const BENCHMARK: BenchmarkSection[] = [
  {
    key: "ambiguity",
    title: "Ambiguity",
    blurb:
      "“More than one” is not “none”, and it is not “the one”. 981 ambiguous (jurisdiction, identifier) pairs cover 2,459 bills, 726 of them in Texas.",
    questions: [
      {
        id: "tx-hb1-session-disambiguation",
        question: "What happened to Texas HB 1?",
        trap: "Pick a plausible session — usually the newest — and report its status confidently. TX HB 1 resolves to three candidate sessions.",
        correct:
          "Return the candidates and require a session choice. Never a single status, never not_found.",
        status: "holds",
        automated: true,
      },
      {
        id: "search-vs-get-bill-record",
        question:
          "Use the MCP tools to look up Texas HB 1 and tell me its current status.",
        trap: "Two tools had opposite contracts on the same input: get_bill_record refused it as ambiguous while search_legislation returned an arbitrary one of the three labelled “exact”.",
        correct:
          "Surface the multi-session resolution before asserting any status, whichever tool is used.",
        status: "fixed",
        automated: true,
      },
      {
        id: "ambiguity-error-is-a-dead-end",
        question:
          "You returned ambiguous_bill for TX HB 1 — which sessions are the candidates?",
        trap: "The error says “pass session to disambiguate” but ships bill UUIDs, not session identifiers. A model forced to produce a session name invents a plausible one.",
        correct:
          "Return candidate session identifiers, or a labelled path to them. Never emit a session name that did not come from the corpus.",
        status: "open",
        automated: false,
      },
      {
        id: "ambiguous-is-not-not-found",
        question:
          "Your API didn’t resolve this bill — should I delete it from my watchlist?",
        trap: "Conflating ambiguous with not-found tells a consumer to drop a bill the corpus actually holds.",
        correct: "“Held, more than once.” Never “not present”.",
        status: "holds",
        automated: true,
      },
    ],
  },
  {
    key: "adjournment",
    title: "Adjournment",
    blurb:
      "The most common way a bill dies leaves no trace in the action record: the session ends and the bill simply stops. Nothing is filed.",
    questions: [
      {
        id: "adjournment-is-not-recorded-death",
        question:
          "What is the status of a bill that was pending in committee when its session adjourned?",
        trap: "An actions-only tracker calls it pending forever; a guessing model invents a “died” action that was never filed.",
        correct:
          "died_on_adjournment — distinct from dead, because the consumer action differs: voted-down is finished, out-of-clock is a reintroduction candidate.",
        status: "holds",
        automated: false,
      },
      {
        id: "enrolled-survives-sine-die",
        question: "Did Hawaii SB 2135 die when the session adjourned?",
        trap: "A blanket “everything live dies at adjournment” rule overwrites enrolled measures. Its session ended 2026-05-08; it was signed 2026-07-07.",
        correct: "enacted. Refuse to infer death from sine die alone.",
        status: "holds",
        automated: true,
      },
      {
        id: "enrolled-never-resolves",
        question:
          "This bill is enrolled and its session adjourned many months ago — is it law?",
        trap: "The enrolled carve-out had no time bound, so the site asserted “awaiting executive action” indefinitely. 3,274 of 4,918 enrolled bills were in sessions adjourned over 180 days — including 2,192 Texas bills from a session that ended fourteen months earlier.",
        correct:
          "Report the outcome as uncaptured, not pending — and not died_on_adjournment either, which would break the case above.",
        status: "fixed",
        automated: true,
      },
      {
        id: "unknown-session-end-is-not-sine-die",
        question:
          "This session’s end date is blank — does that mean its pending bills are dead?",
        trap: "Treating a missing end date as an expired session invents deaths. Those are overwhelmingly two-year carryover biennia (NY, NJ, IL, MN, WI, DC).",
        correct: "Refuse to infer adjournment from a missing date.",
        status: "holds",
        automated: true,
      },
      {
        id: "sine-die-day-still-counts",
        question:
          "Did a bill acted on during its session’s sine-die date die before that action?",
        trap: "An off-by-one treats the end date as already expired and discards same-day activity.",
        correct: "Sine die is still a legislative day.",
        status: "holds",
        automated: false,
      },
    ],
  },
  {
    key: "derivation",
    title: "Status derivation",
    blurb:
      "42% of actions are unclassified and states disagree on identical wording. Roughly 5% of bills deliberately carry no status at all.",
    questions: [
      {
        id: "state-specific-died-in-committee",
        question:
          "Do bills whose latest action reads “Died in Committee” have the same status in Kansas and Mississippi?",
        trap: "A keyword classifier assigns both failure. Kansas maps that string to failure; Mississippi’s identical wording is deliberately unclassified.",
        correct: "Kansas failure; Mississippi no derived status.",
        status: "holds",
        automated: false,
      },
      {
        id: "passed-both-cannot-be-inferred",
        question: "Which bills have passed_both status?",
        trap: "A tracker synthesises it from chamber actions, or uses it as a tidy catch-all.",
        correct:
          "None. It is never assigned, because no evidence source supports it.",
        status: "holds",
        automated: true,
      },
      {
        id: "filing-convention-decides-the-bucket",
        question:
          "Which states kill the most bills by running out the clock rather than voting them down?",
        trap: "Whether a clock-death lands in died_on_adjournment or dead is decided by whether the clerk files an action — not by what happened. Eleven jurisdictions report zero dead; three report zero died_on_adjournment. Real legislatures are not bimodal.",
        correct:
          "Refuse to rank on the split. Report did_not_pass, the sum, which is unaffected by filing convention.",
        status: "fixed",
        automated: true,
      },
      {
        id: "no-status-is-a-real-answer",
        question: "You returned no status for this bill — is your data broken?",
        trap: "A model reads null as a defect and guesses a status to be helpful.",
        correct:
          "No. Roughly 5% of bills deliberately carry no status; the derivation returns nothing rather than guess.",
        status: "holds",
        automated: false,
      },
    ],
  },
  {
    key: "coverage",
    title: "Coverage",
    blurb:
      "Absence of evidence is not evidence of absence. Coverage is uneven, self-reported, and — until this benchmark was written — silently unreported for degraded jurisdictions.",
    questions: [
      {
        id: "degraded-jurisdiction-emits-no-warning",
        question:
          "Nothing came back for this state — can I conclude it has no such bill?",
        trap: "The coverage warning ranked severity by lifecycle position, where DEGRADED and BLOCKED sit after GREEN. A wholly degraded jurisdiction produced no warning at all, and a blocked session was masked by any healthy sibling.",
        correct:
          "Attach a coverage warning. “Not found in a degraded corpus”, never “the state has no such bill”.",
        status: "fixed",
        automated: true,
      },
      {
        id: "hearings-do-not-exist-here",
        question: "What committee hearings are scheduled next week?",
        trap: "A hearings tool exists and invites a calendar answer, but there are zero hearing records. An empty list labelled “official” reads as “the legislature scheduled none”.",
        correct:
          "State plainly that hearing data is not collected. Never infer a schedule from bill actions.",
        status: "fixed",
        automated: true,
      },
      {
        id: "coverage-before-any-negative-claim",
        question: "Does this state have no legislation about this topic?",
        trap: "An empty result is presented as proof of absence.",
        correct:
          "Check coverage first; distinguish “we don’t hold it” from “it doesn’t exist”.",
        status: "holds",
        automated: false,
      },
      {
        id: "historical-sessions-are-out-of-scope",
        question: "How did this state vote on that topic in 2019?",
        trap: "The corpus is the current session or biennium only — a model will answer from training data.",
        correct: "Say the data is not held here.",
        status: "holds",
        automated: false,
      },
      {
        id: "federal-bills-are-not-here",
        question: "What’s the status of the federal SCAM Act?",
        trap: "The corpus is 50 states plus DC — no Congress. The name is familiar enough to bait a confident answer.",
        correct: "Say this system does not cover federal legislation.",
        status: "holds",
        automated: false,
      },
    ],
  },
  {
    key: "companions",
    title: "Companions and cross-session",
    blurb:
      "Chamber versions diverge and are deliberately kept separate. Only eight states file companions at all.",
    questions: [
      {
        id: "companions-are-separate-bills",
        question: "Isn’t this the same bill as its other-chamber companion?",
        trap: "Merging them invents a single history. New York alone holds 25,332 bills — 12,646 lower and 12,681 upper.",
        correct: "Two linked records, each with its own status.",
        status: "holds",
        automated: false,
      },
      {
        id: "only-eight-states-file-companions",
        question: "Find the companion to this bill.",
        trap: "Only NY, MN, NJ, TN, TX, HI, MD and AL file companions. Elsewhere a model pattern-matches a same-numbered bill in the other chamber.",
        correct: "Report that no companion is recorded.",
        status: "holds",
        automated: false,
      },
      {
        id: "prior-session-target-is-often-absent",
        question: "Show me this bill’s prior-session predecessor.",
        trap: "46,957 prior-session links have a target that is not in the corpus. The identifier is returned with a null target deliberately.",
        correct:
          "The predecessor exists and is not held here — which is still the answer to a multi-year tracking question.",
        status: "holds",
        automated: false,
      },
      {
        id: "number-reuse-across-sessions",
        question: "Track HB 100 in this state across the last two sessions.",
        trap: "Bill numbers are reused; the same number is unrelated legislation in a different session.",
        correct: "Do not narrate two sessions’ HB 100 as one bill’s history.",
        status: "holds",
        automated: false,
      },
    ],
  },
  {
    key: "provenance",
    title: "Provenance",
    blurb:
      "Which claims come from the legislature, and which are ours. Getting this wrong turns an inference into an attributed quote.",
    questions: [
      {
        id: "derived-status-under-an-official-label",
        question:
          "Build an evidence packet for this bill and give me a citation for a story saying it died.",
        trap: "The packet labelled its record “official” over a payload whose status is derived — and died_on_adjournment exists precisely because nothing was filed. The state’s own URL was attached beside it.",
        correct:
          "Label status as derived wherever it appears, and cite the session adjournment date as the evidence — not the bill’s source URL.",
        status: "fixed",
        automated: true,
      },
      {
        id: "status-has-no-as-of-date",
        question: "As of what date is this bill dead, and when did you decide that?",
        trap: "Both look answered and neither is. The status date column is unpopulated corpus-wide, and the web fell back to the last filed action — typically a committee referral months before the session ended.",
        correct:
          "Answer with the session end date; refuse to supply a determination timestamp that is not stored.",
        status: "fixed",
        automated: true,
      },
      {
        id: "truncated-list-vs-complete-list",
        question: "List every vote on this bill.",
        trap: "A list that stops at a cap is indistinguishable from a complete one unless it says so.",
        correct: "Flag truncation explicitly.",
        status: "holds",
        automated: true,
      },
    ],
  },
  {
    key: "identifiers",
    title: "Identifiers and the change feed",
    blurb: "Normalisation, and what the change log does and does not promise.",
    questions: [
      {
        id: "surface-spelling-is-not-a-different-bill",
        question: "Do SB2135, S.B. 2135 and sb 2135 refer to different bills?",
        trap: "Treating surface spelling as identity, or returning not-found for a valid variant.",
        correct: "All normalise to one bill.",
        status: "holds",
        automated: false,
      },
      {
        id: "is-the-change-feed-complete",
        question:
          "I poll the change feed with a stored cursor — is that enough to learn when one of my bills dies?",
        trap: "A flat yes or no. Adjournment deaths do emit events; but a wholesale re-derivation of the status logic deliberately does not, so one maintenance run cannot drown every watchlist in fake changes.",
        correct:
          "Yes for real transitions, with that caveat named. Neither an unqualified “complete” nor an unqualified “deaths are invisible”.",
        status: "holds",
        automated: false,
      },
      {
        id: "commit-visibility-watermark",
        question:
          "I read up to cursor X — can I be sure nothing before it appears later?",
        trap: "Claiming unconditional completeness. Sequence numbers are allocated at insert and visible at commit, so a long writer holds a low number that surfaces after higher ones.",
        correct:
          "Name the safety lag: the feed serves only rows older than it, converting lost data into bounded latency.",
        status: "holds",
        automated: false,
      },
    ],
  },
];

export const BENCHMARK_TOTAL = BENCHMARK.reduce(
  (n, s) => n + s.questions.length,
  0
);
export const BENCHMARK_AUTOMATED = BENCHMARK.reduce(
  (n, s) => n + s.questions.filter((q) => q.automated).length,
  0
);
export const BENCHMARK_FIXED = BENCHMARK.reduce(
  (n, s) => n + s.questions.filter((q) => q.status === "fixed").length,
  0
);
