import type { Metadata } from "next";
import ScoutExperience from "@/components/scout/ScoutExperience";

export const metadata: Metadata = {
  title: "Scout",
  description:
    "Research Florida official sources and retained California bill-action archives, with source evidence.",
  alternates: { canonical: "/scout" },
  robots: { index: false, follow: false },
};

/**
 * This route intentionally remains available when the navigation flag is off:
 * the page gives an accurate service-state message and never suggests that a
 * hidden feature is usable. The API remains the authority for creation.
 */
type Props = {
  searchParams: Promise<{ job?: string | string[] }>
};

export default async function ScoutPage({ searchParams }: Props) {
  const params = await searchParams;
  const jobId = typeof params.job === "string" ? params.job : params.job ? null : undefined;
  return <ScoutExperience enabled={process.env.NEXT_PUBLIC_SCOUT_ENABLED === "true"} initialJobId={jobId} />;
}
