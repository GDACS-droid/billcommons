import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import { apiGet } from "@/lib/api";
import type { Topic } from "@/lib/types";

const TOPICS_REVALIDATE = 21600;

export const metadata: Metadata = {
  title: "Topic trackers",
  description:
    "Cross-state legislative trackers: every artificial intelligence, data privacy, and cryptocurrency bill in all 50 states + DC, with live status.",
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
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <h1 className="text-2xl font-semibold text-slate-900">Topic trackers</h1>
      <p className="mt-2 max-w-2xl text-sm text-slate-600">
        Curated cross-state slices of the corpus — every bill on a topic,
        across all 50 states and DC, with live status. Updated nightly as
        sessions move.
      </p>

      {!result.ok ? (
        <div className="mt-8">
          <DataUnavailable message="Topics are temporarily unavailable." />
        </div>
      ) : (
        <ul className="mt-8 space-y-4">
          {result.data.data.map((topic) => (
            <li
              key={topic.slug}
              className="rounded-lg border border-slate-200 p-5 transition hover:border-slate-300 hover:shadow-sm"
            >
              <Link
                href={`/topics/${topic.slug}`}
                className="text-lg font-semibold text-slate-900 hover:underline"
              >
                {topic.name}
              </Link>
              <p className="mt-1 text-sm text-slate-600">{topic.description}</p>
              <p className="mt-2 text-xs font-medium text-amber-600">
                {topic.bill_count.toLocaleString("en-US")} bills tracked
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
