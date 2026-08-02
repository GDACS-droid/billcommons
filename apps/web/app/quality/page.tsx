import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";
import {
  BENCHMARK,
  BENCHMARK_AUTOMATED,
  BENCHMARK_FIXED,
  BENCHMARK_TOTAL,
  BENCHMARK_UPDATED,
  BENCHMARK_VERSION,
  type BenchmarkStatus,
} from "@/lib/benchmark";
import { SITE_URL } from "@/lib/config";

export const metadata: Metadata = {
  title: "Data-integrity contract",
  description:
    `${BENCHMARK_TOTAL} adversarial questions Bill Commons must answer honestly — about ambiguity, ` +
    "missing coverage, derived status, and stale sessions. Published as a public quality contract, " +
    "with the deterministic half wired into the test suite.",
  alternates: { canonical: "/quality" },
};

const STATUS_LABEL: Record<BenchmarkStatus, { text: string; className: string }> = {
  fixed: {
    text: "found a real defect",
    className: "bg-amber-100 text-amber-900",
  },
  holds: { text: "holds", className: "bg-emerald-100 text-emerald-800" },
  open: { text: "open", className: "bg-rose-100 text-rose-800" },
};

function Stat({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="surface-card p-5">
      <div className="text-3xl font-semibold tracking-tight text-blue-800">
        {value}
      </div>
      <div className="mt-1 text-sm font-medium text-slate-900">{label}</div>
      <div className="mt-1 text-xs text-slate-600">{detail}</div>
    </div>
  );
}

export default function QualityPage() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Data-integrity contract"
        title="What this system must refuse to say"
        description={
          <>
            <p>
              Most legislative data is judged on coverage and speed. The harder
              question is whether a system will tell you it{" "}
              <strong>doesn&apos;t know</strong> — whether it converts ambiguity,
              missing coverage, and a session that quietly ended into confident
              answers that happen to be wrong.
            </p>
            <p className="mt-3">
              These {BENCHMARK_TOTAL} questions are the ones where a normal bill
              tracker, or a language model working from priors, gets it
              confidently wrong. For most of them the correct answer is a
              refusal, a qualification, or an explicit unknown.
            </p>
          </>
        }
      />

      <div className="mt-8 grid gap-4 sm:grid-cols-3">
        <Stat
          label="Adversarial questions"
          value={String(BENCHMARK_TOTAL)}
          detail={`${BENCHMARK_VERSION}, updated ${BENCHMARK_UPDATED}`}
        />
        <Stat
          label="Enforced automatically"
          value={String(BENCHMARK_AUTOMATED)}
          detail="Run on every test run, not just published"
        />
        <Stat
          label="Found a real defect"
          value={String(BENCHMARK_FIXED)}
          detail="Live in production when this was written"
        />
      </div>

      <div className="mt-8 rounded-lg border-l-[3px] border-amber-500 bg-amber-50 px-4 py-3">
        <p className="text-sm text-slate-800">
          <strong>Writing this found {BENCHMARK_FIXED} real defects</strong>, and
          all of them were in the honesty machinery itself — the coverage warning
          was silently switched off for degraded jurisdictions, evidence packets
          labelled derived conclusions as official record, and a per-state
          mortality table invited a comparison that was really measuring clerical
          filing habits. They are listed below rather than quietly fixed, because
          a quality contract that only ever reports passes is marketing.
        </p>
      </div>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          How this is enforced
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          Every question has a failure signature — the observable symptom, written
          so it can be asserted on rather than eyeballed.{" "}
          <strong>
            {BENCHMARK_AUTOMATED} of {BENCHMARK_TOTAL} are machine-checked
          </strong>{" "}
          and run with the rest of the test suite; a regression fails the build.
          The remainder need an agent transcript to grade, and are checked by hand
          for now. The strongest assertion we have there is provenance: every bill
          number, session name and date in an answer must appear verbatim in a
          recorded tool response.
        </p>
        <p className="mt-3 text-sm text-slate-700">
          The full write-up, including the reasoning behind each question, is in{" "}
          <a
            className="underline"
            href="https://github.com/GDACS-droid/billcommons/blob/main/docs/quality/adversarial-benchmark.md"
          >
            the repository
          </a>
          , alongside{" "}
          <a
            className="underline"
            href="https://github.com/GDACS-droid/billcommons/blob/main/apps/api/tests/test_benchmark_deterministic.py"
          >
            the tests that enforce it
          </a>
          .
        </p>
      </section>

      {BENCHMARK.map((section) => (
        <section key={section.key} className="mt-10">
          <h2 className="text-lg font-semibold text-slate-900">
            {section.title}
          </h2>
          <p className="mt-1 text-sm text-slate-600">{section.blurb}</p>

          <div className="mt-4 space-y-4">
            {section.questions.map((q) => {
              const status = STATUS_LABEL[q.status];
              return (
                <div key={q.id} className="surface-card p-5">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <p className="text-[15px] font-medium text-slate-900">
                      “{q.question}”
                    </p>
                    <div className="flex shrink-0 gap-1.5">
                      {q.automated ? (
                        <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-medium text-blue-900">
                          automated
                        </span>
                      ) : null}
                      <span
                        className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${status.className}`}
                      >
                        {status.text}
                      </span>
                    </div>
                  </div>
                  <dl className="mt-3 space-y-2 text-sm">
                    <div>
                      <dt className="inline font-medium text-slate-900">
                        The trap:{" "}
                      </dt>
                      <dd className="inline text-slate-700">{q.trap}</dd>
                    </div>
                    <div>
                      <dt className="inline font-medium text-slate-900">
                        Correct behaviour:{" "}
                      </dt>
                      <dd className="inline text-slate-700">{q.correct}</dd>
                    </div>
                  </dl>
                </div>
              );
            })}
          </div>
        </section>
      ))}

      <section className="mt-12 border-t border-slate-200 pt-6">
        <h2 className="text-lg font-semibold text-slate-900">
          Found something this misses?
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          The useful contribution to a benchmark like this is a question it
          doesn&apos;t contain — a way to make the system assert something it
          cannot support.{" "}
          <Link href="/feedback" className="underline">
            Send it over
          </Link>
          , or open an issue on the repository. Questions that find a real defect
          get added with the defect named, the way the {BENCHMARK_FIXED} above
          were.
        </p>
      </section>

      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify({
            "@context": "https://schema.org",
            "@type": "TechArticle",
            headline: "Bill Commons data-integrity contract",
            description: `${BENCHMARK_TOTAL} adversarial questions a legislative data system must answer honestly.`,
            url: `${SITE_URL}/quality`,
            dateModified: BENCHMARK_UPDATED,
            version: BENCHMARK_VERSION,
            license: "https://creativecommons.org/licenses/by/4.0/",
          }),
        }}
      />
    </div>
  );
}
