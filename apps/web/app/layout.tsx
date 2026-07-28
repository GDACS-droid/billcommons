import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { Analytics } from "@vercel/analytics/next";
import "./globals.css";
import SiteHeader from "@/components/SiteHeader";
import SiteFooter from "@/components/SiteFooter";
import { SITE_URL } from "@/lib/config";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: "Bill Commons — Free, open legislative search",
    template: "%s | Bill Commons",
  },
  description:
    "Bill Commons is a free, open-source, nonpartisan search engine for state legislation across all 50 states and DC — bill text, sponsors, votes, and status, sourced from official records.",
  openGraph: {
    type: "website",
    siteName: "Bill Commons",
    title: "Bill Commons — Free, open legislative search",
    description:
      "Search state legislation across all 50 states and DC. Free, open-source, nonpartisan.",
  },
  twitter: {
    card: "summary",
    title: "Bill Commons",
    description:
      "Free, open-source, nonpartisan legislative search for all 50 states and DC.",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="flex min-h-screen flex-col antialiased">
        <a href="#main-content" className="skip-link">
          Skip to main content
        </a>
        <SiteHeader />
        <main id="main-content" className="flex-1">
          {children}
        </main>
        <SiteFooter />
        <Analytics />
      </body>
    </html>
  );
}
