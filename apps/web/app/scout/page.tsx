import type { Metadata } from "next";
import ScoutExperience from "@/components/scout/ScoutExperience";

export const metadata: Metadata = {
  title: "Scout",
  description:
    "Evidence-first research of official Florida legislative sources, with source-level provenance.",
  alternates: { canonical: "/scout" },
  robots: { index: false, follow: false },
};

/**
 * This route intentionally remains available when the navigation flag is off:
 * the page gives an accurate service-state message and never suggests that a
 * hidden feature is usable. The API remains the authority for creation.
 */
export default function ScoutPage() {
  return <ScoutExperience enabled={process.env.NEXT_PUBLIC_SCOUT_ENABLED === "true"} />;
}
