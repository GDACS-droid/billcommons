import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import AlertSignup from "@/components/AlertSignup";
import DataUnavailable from "@/components/DataUnavailable";
import JsonLd from "@/components/JsonLd";
import BillListItem from "@/components/BillListItem";
import PageHeader from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { fetchAllPages } from "@/lib/collections";
import { billPath } from "@/lib/billUrl";
import { SITE_URL } from "@/lib/config";
import type { BillSummary, Topic } from "@/lib/types";

const TOPIC_REVALIDATE = 21600;

/**
 * The topic LIST is cached far more briefly than a topic's bills.
 *
 * Vercel's Data Cache persists across deployments, so a 6-hour TTL on the list
 * meant a freshly deployed build could still resolve slugs against a list that
 * predated them -- and `find()` returning undefined here is a 404, not a stale
 * number. Four new topics 404'd for exactly that reason, and which ones 404'd
 * shifted between deploys as different cache entries expired, which is what
 * gave it away.
 *
 * The list is seven rows. Re-fetching it every few minutes costs almost
 * nothing and makes a newly shipped topic reachable rather than pending a
 * cache expiry. The bills BELOW a topic keep the long TTL -- they are the
 * expensive part and staleness there is merely stale, not a missing page.
 */
const TOPIC_LIST_REVALIDATE = 300;

/**
 * How many pages of bills a topic hub renders (50 per page).
 *
 * fetchAllPages defaults to 50 pages, and it issues every page after the first
 * as ONE Promise.all burst. That was harmless while the largest topic held 789
 * bills (16 pages), and stopped being harmless the moment `local-government`
 * arrived with 6,325: 126 simultaneous requests, which trips our own rate
 * limiter, and a silent truncation to 2,500 bills that the page then reported
 * as if it were the whole set.
 *
 * 10 pages is a page a reader will actually scroll, and the true corpus size
 * comes from `topic.bill_count` -- an aggregate, one query -- rather than from
 * counting what we happened to fetch.
 */
const TOPIC_MAX_PAGES = 10;

interface Props {
  params: Promise<{ slug: string }>;
}

/**
 * Prerender every topic hub at build time.
 *
 * Without this, a topic page is rendered on demand and its `/api/v1/topics`
 * fetch is served from Vercel's Data Cache -- which PERSISTS ACROSS
 * DEPLOYMENTS. So on the day four topics were added, /topics listed all seven
 * (that page is prerendered, and a build-time fetch is fresh) while
 * /topics/youth-online-safety returned 404 for six hours, because the
 * on-demand render read a cached list that predated them and `find()` came
 * back undefined. Two surfaces, same endpoint, opposite answers.
 *
 * Prerendering ties a topic hub's existence to the deployment rather than to
 * a cache TTL: ship a topic, deploy, it is there. The list is a handful of
 * curated slugs, so the build cost is negligible.
 */
export async function generateStaticParams() {
  const result = await apiGet<TopicsEnvelope>("/api/v1/topics", undefined, {
    revalidate: TOPIC_LIST_REVALIDATE,
  });
  // Returning [] on a failed build-time fetch leaves every topic to on-demand
  // rendering rather than failing the build -- degraded, not broken.
  return result.ok ? result.data.data.map((t) => ({ slug: t.slug })) : [];
}

interface TopicsEnvelope {
  data: Topic[];
}

async function getTopic(slug: string): Promise<Topic | null | undefined> {
  const result = await apiGet<TopicsEnvelope>("/api/v1/topics", undefined, {
    revalidate: TOPIC_LIST_REVALIDATE,
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
          TOPIC_REVALIDATE,
          { maxPages: TOPIC_MAX_PAGES }
        );

  if (topic === null || !bills.ok) {
    return (
      <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
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
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
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

      <PageHeader
        eyebrow="Topic tracker"
        title={`${topic.name} legislation, all 50 states`}
        description={<p>{topic.description}</p>}
      />
      <p className="mt-3 text-sm tabular-nums text-slate-500">
        <strong className="text-slate-900">
          {topic.bill_count.toLocaleString("en-US")}
        </strong>{" "}
        bills — updated nightly. Membership is matched on bill titles and
        official subject tags; see something missing?{" "}
        <Link href="/about" className="underline">
          Tell us.
        </Link>
      </p>
      {/* Say the list is partial rather than letting the buckets below read as
          the complete set. The counts inside each bucket are counts of what is
          SHOWN, and on a large topic that is a fraction of the corpus. */}
      {topic.bill_count > bills.items.length ? (
        <p className="mt-2 text-sm text-slate-500">
          Showing the{" "}
          <strong className="text-slate-900">
            {bills.items.length.toLocaleString("en-US")}
          </strong>{" "}
          most recently active, across{" "}
          <strong className="text-slate-900">{states.length}</strong>{" "}
          jurisdictions. For the full set, query{" "}
          <code className="rounded bg-slate-100 px-1">
            /api/v1/topics/{slug}
          </code>{" "}
          or filter by state.
        </p>
      ) : (
        <p className="mt-2 text-sm text-slate-500">
          Across{" "}
          <strong className="text-slate-900">{states.length}</strong>{" "}
          jurisdictions.
        </p>
      )}

      <div className="mt-6 max-w-xl">
        <AlertSignup topicSlug={slug} topicName={topic.name} />
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        {states.slice(0, 12).map(([code, count]) => (
          <Link
            key={code}
            href={`/states/${code}`}
          className="rounded-full border border-slate-200 px-3 py-1 text-xs font-medium tabular-nums text-slate-600 transition-colors hover:border-blue-300 hover:bg-blue-50 hover:text-blue-800"
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
            <h2 className="text-lg font-semibold tracking-tight text-slate-950">
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
