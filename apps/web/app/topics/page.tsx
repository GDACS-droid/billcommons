import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import PageHeader from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import type { Topic } from "@/lib/types";

// Short, and deliberately matched to TOPIC_LIST_REVALIDATE in [slug]/page.tsx:
// these two share one Data Cache entry, and a long TTL on it is what made a
// newly shipped topic 404 on its own hub while appearing in this list.
const TOPICS_REVALIDATE = 300;

export const metadata: Metadata = {
  title: "Topic trackers",
  description:
    "Cross-state legislative trackers: every artificial intelligence, data privacy, youth online safety, platform accountability, cybersecurity, cryptocurrency, and local government bill in all 50 states + DC, with live status.",
  alternates: { canonical: "/topics" },
};

interface TopicsEnvelope {
  data: Topic[];
}

export default async function TopicsPage() {
  const result = await apiGet<TopicsEnvelope>("/api/v1/topics", undefined, {
    revalidate: TOPICS_REVALIDATE,
  });

  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Research tools"
        title="Topic trackers"
        description={
          <p>
        Curated cross-state slices of the corpus — every bill on a topic,
        across all 50 states and DC, with live status. Updated nightly as
        sessions move.
          </p>
        }
      />

      {!result.ok ? (
        <div className="mt-8">
          <DataUnavailable message="Topics are temporarily unavailable." />
        </div>
      ) : (
        <ul className="space-y-3">
          {result.data.data.map((topic) => (
            <li
              key={topic.slug}
              className="rounded-md border border-slate-200 bg-white p-5 transition-colors hover:border-slate-300 hover:bg-slate-50"
            >
              <Link
                href={`/topics/${topic.slug}`}
                className="text-lg font-semibold tracking-tight text-blue-800 hover:text-blue-700 hover:underline"
              >
                {topic.name}
              </Link>
              <p className="mt-1 text-sm text-slate-600">{topic.description}</p>
              <p className="mt-3 text-xs font-medium tabular-nums text-slate-500">
                {topic.bill_count.toLocaleString("en-US")} bills tracked
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
