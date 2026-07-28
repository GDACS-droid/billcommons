import type { Metadata } from "next";
import PageHeader from "@/components/PageHeader";
import FeedbackForm from "@/components/FeedbackForm";

export const metadata: Metadata = {
  title: "Feedback",
  description:
    "Report a bug, a data error, or request a feature. Bill Commons is built in the open and feedback goes straight to the maintainer.",
  alternates: { canonical: "/feedback" },
};

export default function FeedbackPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Get in touch"
        title="Feedback"
        description={
          <p>
            Spotted a data error, a bill that looks wrong, or something the
            site should do but doesn&apos;t? Tell us. Messages go straight to
            the maintainer — this is a small project and feedback genuinely
            shapes what gets built next.
          </p>
        }
      />

      <FeedbackForm />

      <p className="mt-8 text-sm text-slate-500">
        Prefer GitHub? Open an issue at{" "}
        <a
          href="https://github.com/GDACS-droid/billcommons/issues"
          className="text-blue-800 underline underline-offset-2 hover:text-blue-700"
        >
          GDACS-droid/billcommons
        </a>
        .
      </p>
    </div>
  );
}
