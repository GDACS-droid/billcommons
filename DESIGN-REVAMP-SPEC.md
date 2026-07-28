# Bill Commons — visual design pass (spec)

Scope: `apps/web` only (Next.js 14 app router + Tailwind). This is a RESTYLE,
not a rebuild: no route changes, no content/copy changes beyond what a style
needs, no new heavy dependencies (next/font with a bundled Google font is
fine), no client-side libraries, light theme only.

## Design direction

Professional, sleek, restrained — a data product a lobbyist or policy team
would trust. Think Stripe-docs / Linear-marketing restraint, NOT a flashy
landing page. The guiding critique (from a designer's AI-vs-agency
comparison): produce **hierarchy, focus, and a clear path** — not a dense
dashboard. Concretely:

1. **One clear primary path.** The homepage's job is the search box. It should
   be the unmistakable focal point above the fold; everything else recedes.
2. **Strong information hierarchy.** One display typeface moment (the hero),
   a disciplined type scale for everything else (e.g. text-sm body,
   text-base/lg leads, 2–3 heading sizes total, consistent tracking).
3. **Generous whitespace.** More vertical rhythm between sections; let cards
   and tables breathe. Never cramped.
4. **Cards with focal emphasis.** Card = one obvious primary element (the
   linked title) + quiet metadata. Subtle borders (slate-200), very subtle
   shadow or none, small radius, consistent padding. Hover: border darkens or
   background tints — no lift/scale animations.
5. **Attention to detail.** Consistent spacing scale, aligned baselines,
   tabular-nums for all counts, consistent icon-free labels (no emoji),
   focus-visible rings for accessibility.

## Palette & type

- Base: existing slate scale is right — keep it.
- Accent: pick ONE accent and use it sparingly (links on hover, primary
  buttons, eyebrows, active states). A deep authoritative blue
  (e.g. blue-700/800 family) fits legislative data. Replace the stray
  amber-600 eyebrow on /services with the same accent. No gradients except
  at most one ≤ 3% opacity wash in the hero; no purple/violet (AI-slop tell).
- Type: add `next/font` Inter (or keep system stack if Inter adds weight
  concerns) with `font-feature-settings` for tabular numbers on stats. The
  hero heading may use a heavier weight + tighter tracking instead of a
  second family.

## Surfaces to touch

- `components/SiteHeader.tsx` — tighten: slimmer bar, clearer active state,
  wordmark treatment ("Bill Commons" set solid, maybe small-caps or weight
  contrast; no logo image). Mobile menu stays functional.
- `components/SiteFooter.tsx` — proper multi-column footer (Product / Data /
  Company columns from existing links only), quiet slate-50 background.
- `app/page.tsx` — hero (bigger focus on search, tighter headline), stat
  cards (tabular numbers, quieter labels), "Building something with this?"
  band (make it a refined callout, not a gray box), active-session cards,
  coverage band.
- `components/SearchBox.tsx` — the hero element: larger, crisp border,
  visible focus ring, button uses the accent.
- `components/StatusBadge.tsx` — keep semantics/colors, refine to a quiet
  pill (smaller text, tighter padding, dot indicator optional).
- List/table pages (`/states`, `/coverage`, `/topics`, `/search`, bill pages,
  `/reports/2026-bill-mortality`) — apply the same card/table treatment:
  row hover states, thin dividers, tabular-nums, consistent page headers
  (eyebrow + h1 + lead paragraph pattern that /services already uses).
- `/docs/*`, `/services`, `/about`, `/methodology` — same page-header
  pattern, better code-block styling on the docs pages (slate-900 blocks,
  small font, copy affordance NOT required).

## Hard constraints

- `npm run build` and `npx tsc --noEmit` must pass.
- Do not touch anything outside `apps/web`.
- Do not change data fetching, routes, metadata/SEO tags, JSON-LD, sitemap,
  llms.txt, or any API code.
- Do not add images, illustrations, or icon libraries. Inline SVG glyphs
  already in the codebase may stay.
- Keep all existing links and content sections — this is styling and layout
  refinement only.
- Accessibility: contrast ≥ AA, focus-visible on all interactive elements.

## Anti-goals (AI-slop tells to avoid)

Gradient-heavy heroes, purple/indigo accents, emoji in UI, oversized rounded
cards with drop shadows everywhere, marketing superlatives, animated
counters, sparkle icons, "AI-powered" badges, dense KPI dashboards.
