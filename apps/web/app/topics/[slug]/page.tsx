import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import AlertSignup from "@/components/AlertSignup";
import DataUnavailable from "@/components/DataUnavailable";
import JsonLd from "@/components/JsonLd";
import BillListItem from "@/components/BillListItem";
import { apiGet } from "@/lib/api";
import { fetchAllPages } from "@/lib/collections";
import { billPath } from "@/lib/billUrl";
import { SITE_URL } from "@/lib/config";
import type { BillSummary, Topic } from "@/lib/types";

const TOPIC_REVALIDATE = 21600;

interface Props {
  params: Promise<{ slug: string }>;
}

interface TopicsEnvelope {
  data: Topic[];
}

async function getTopic(slug: string): Promise<Topic | null | undefined> {
  const result = await apiGet<TopicsEnvelope>("/api/v1/topics", undefined, {
    revalidate: TOPIC_REVALIDATE,
  });
  // null = API down (render an error), undefined = topic does not exist (404).
  if (!result.ok) return null;
  return result.data.data.find((t) => t.slug === slug);
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const topic = await getTopic(slug);
  if (!topic) {
    return { title: "Topic tracker" };
  }
  return {
    title: `${topic.name} legislation — all 50 states`,
    description: `${topic.bill_count.toLocaleString("en-US")} state bills on ${
      topic.name.toLowerCase()
    }, tracked across all 50 states + DC with live status. ${topic.description}`,
    alternates: { canonical: `/topics/${slug}` },
  };
}

// Buckets ordered the way a policy reader scans: what became law, what is
// moving, what is waiting, what is gone.
const BUCKETS: { key: string; label: string; statuses: (string | null)[] }[] = [
  { key: "enacted", label: "Enacted", statuses: ["enacted"] },
  {
    key: "advancing",
    label: "Advancing",
    statuses: ["passed_one_chamber", "passed_both", "enrolled"],
  },
  {
    key: "pending",
    label: "Pending",
    statuses: ["introduced", "in_committee", null],
  },
  {
    key: "failed",
    label: "Died or failed",
    statuses: ["dead", "died_on_adjournment", "vetoed", "withdrawn"],
  },
];

export default async function TopicPage({ params }: Props) {
  const { slug } = await params;
  const topic = await getTopic(slug);
  if (topic === undefined) notFound();

  const bills =
    topic === null
      ? { ok: false as const, items: [] as BillSummary[] }
      : await fetchAllPages<BillSummary>(
          `/api/v1/topics/${slug}`,
          {},
          TOPIC_REVALIDATE
        );

  if (topic === null || !bills.ok) {
    return (
      <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
        <DataUnavailable message="This topic tracker is temporarily unavailable." />
      </div>
    );
  }

  const byBucket = BUCKETS.map((bucket) => ({
    ...bucket,
    bills: bills.items.filter((b) =>
      bucket.statuses.includes(b.status ?? null)
    ),
  }));
  const byState = new Map<string, number>();
  for (const bill of bills.items) {
    const code = bill.jurisdiction_abbreviation ?? "??";
    byState.set(code, (byState.get(code) ?? 0) + 1);
  }
  const states = [...byState.entries()].sort((a, b) => b[1] - a[1]);

  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <JsonLd
        data={{
          "@context": "https://schema.org",
          "@type": "CollectionPage",
          name: `${topic.name} legislation — all 50 states`,
          description: topic.description,
          url: `${SITE_URL}/topics/${slug}`,
          isPartOf: { "@type": "WebSite", name: "Bill Commons", url: SITE_URL },
        }}
      />

      <p className="text-xs font-semibold uppercase tracking-wide text-amber-600">
        Topic tracker
      </p>
      <h1 className="mt-1 text-2xl font-semibold text-slate-900">
        {topic.name} legislation, all 50 states
      </h1>
      <p className="mt-2 max-w-2xl text-sm text-slate-600">{topic.description}</p>
      <p className="mt-3 text-sm text-slate-500">
        <strong className="text-slate-900">
          {bills.items.length.toLocaleString("en-US")}
        </strong>{" "}
        bills across{" "}
        <strong className="text-slate-900">{states.length}</strong>{" "}
        jurisdictions — updated nightly. Membership is matched on bill titles
        and official subject tags; see something missing?{" "}
        <Link href="/about" className="underline">
          Tell us.
        </Link>
      </p>

      <div className="mt-6 max-w-xl">
        <AlertSignup topicSlug={slug} topicName={topic.name} />
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        {states.slice(0, 12).map(([code, count]) => (
          <Link
            key={code}
            href={`/states/${code}`}
            className="rounded-full border border-slate-200 px-3 py-1 text-xs font-medium text-slate-600 hover:border-slate-300 hover:text-slate-900"
          >
            {code} · {count}
          </Link>
        ))}
        {states.length > 12 ? (
          <span className="px-2 py-1 text-xs text-slate-400">
            +{states.length - 12} more
          </span>
        ) : null}
      </div>

      {byBucket.map((bucket) =>
        bucket.bills.length === 0 ? null : (
          <section key={bucket.key} className="mt-10">
            <h2 className="text-lg font-semibold text-slate-900">
              {bucket.label}{" "}
              <span className="text-sm font-normal text-slate-500">
                ({bucket.bills.length.toLocaleString("en-US")})
              </span>
            </h2>
            <ul className="mt-4 space-y-3">
              {bucket.bills.map((bill) => (
                <BillListItem
                  key={bill.id}
                  bill={bill}
                  href={
                    bill.jurisdiction_abbreviation && bill.session_identifier
                      ? billPath(
                          bill.jurisdiction_abbreviation,
                          bill.session_identifier,
                          bill.identifier_norm
                        )
                      : undefined
                  }
                />
              ))}
            </ul>
          </section>
        )
      )}
    </div>
  );
}
