/**
 * Renders the API's `highlight` field safely.
 *
 * `highlight` is PLAIN TEXT (never HTML) with matched fragments wrapped in
 * sentinel tokens ('⟦H⟧' / '⟦/H⟧' -- see billcommons_api.search
 * HIGHLIGHT_START_SENTINEL/HIGHLIGHT_STOP_SENTINEL on the API side). Source
 * titles/descriptions/document text come from upstream jurisdictions and are
 * not sanitized, so this field must never be passed to
 * dangerouslySetInnerHTML or any other raw-HTML sink -- we split on the
 * sentinels and render everything as plain React text/elements instead.
 */
import type { ReactNode } from "react";

const HIGHLIGHT_START_SENTINEL = "⟦H⟧";
const HIGHLIGHT_STOP_SENTINEL = "⟦/H⟧";

export function renderHighlight(highlight: string): ReactNode[] {
  const parts = highlight.split(HIGHLIGHT_START_SENTINEL);
  const nodes: React.ReactNode[] = [];
  // First part (before any start sentinel) is always plain text.
  nodes.push(parts[0]);
  for (let i = 1; i < parts.length; i++) {
    const [marked, ...restParts] = parts[i].split(HIGHLIGHT_STOP_SENTINEL);
    const rest = restParts.join(HIGHLIGHT_STOP_SENTINEL);
    nodes.push(<mark key={i}>{marked}</mark>);
    if (rest) nodes.push(rest);
  }
  return nodes;
}
