# Final demo verification

Verified 2026-09-02.

## Product capture

- URL: `https://billcommons.org/scout` (controlled public beta).
- Query: `Research Florida HB 625 and verify its current official staff analyses as of September 2, 2026.`
- Final fresh job reached `completed` in 5.605 seconds by database timestamps.
- Result: three findings, three retained official sources, zero browser pages.
- Evidence control opened the official Florida Senate HB 625 page.
- The final repeat capture returned the retained matching research in 3.231 seconds of
  end-to-end browser time, with no new browser work.
- The MP4 uses 1.8 seconds of that actual browser recording. Three production
  screenshots are held for 8.3 seconds so the durable queue, completed result, and
  official evidence remain readable. Playback speed is unchanged and no progress is
  synthesized.

## Solari capture

- Run date: 2026-09-01; this package was reviewed after midnight on 2026-09-02.
- Public implementation commit: `1233dd2ac0782d91cb234359af021f2f0890ae2a`.
- Official host: `www.leg.state.fl.us`.
- Navigation: Chapter 43 contents → §43.16.
- Runtime: 11.689 seconds; one page; two actions; 38 admitted routed requests.
- Recording/replay: available; capability URL withheld.
- Cleanup: confirmed by the provider lifecycle.
- Extracted HTML SHA-256: `2e70542918802d1e9e744ea98d8e7ecfc911731cbdca1f98fcd76cc7a9bb7e3c`.

## Artifact

- File: `scout-challenge-final.mp4`.
- Duration: 17.800 seconds.
- Video: H.264, 1440×900, 25 fps, no audio track.
- SHA-256: `67fc3226cc436e1b71b7d3947a076b187a028f4b2cb3a63d6a3737fedf586ffe`.
- Contact sheet and final proof frame were generated from the encoded MP4 and
  inspected at original resolution.
- The source WebM is 4.120 seconds; the render script preflights that it exceeds the
  verified 1.8-second trim before encoding.
- `scout-live-cache-proof.png`, decoded from 1.2 seconds in the final MP4, visibly
  contains `Cache: Reused matching research` and the retained result.

No customer identifiers, cookie, Solari key, CDP/WebSocket endpoint, provider session
identifier, or signed replay URL is present in the capture or artifact.

## Production closeout

- Vercel production Web Analytics aggregate: Scout opened 1, job created 3, job
  started 1, job completed 2, evidence opened 2, and cache hit 2. The set covers two
  controlled operator visitor identifiers; it is not organic traction. It is not a
  same-cohort funnel: `scout_job_created` records successful create-endpoint responses,
  including cache/coalesced returns, and one completed run predates the `started` event
  repair.
- Partial/failed and Solari terminal analytics branches are contract-tested but were
  not manufactured in production. The direct production workflow correctly emitted
  no Solari event.
- PostgreSQL includes backend/private-canary jobs that never load web analytics:
  migration `0025`; 8 completed jobs; 22 official/hash/raw-linked sources;
  21 retained findings; 118 durable events; 0 active jobs; 0 unreleased, live, or
  cleanup-failed browser sessions.
- Production health/readiness and the public web route returned HTTP 200. Railway API
  HTTP logs and Vercel production logs contained zero 5xx responses in the four-hour
  closeout window; the Scout worker contained zero error-level log entries.
