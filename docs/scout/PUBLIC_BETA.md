# Scout staged public beta — 2026-09-02

## Current state

- Public URL: `https://billcommons.org/scout`
- Source revision: `8d5dbec3f35e3aaa657a05f158a82a54c2238cc5`
- Railway API: `5828ea8b-6a85-4ca6-b453-f76e604ad374`, image
  `sha256:851cfa07ad06bcb1c2ecd71017beb9743e2ee9ff9ffb7deb7b054e4d093ca1da`
- Railway Scout worker: `e71aa668-4dc1-4d57-9045-3dd51f3a568d`, image
  `sha256:53036350e01ca04efc1ce1b0e39e619ef68c50a12a0c459cb60ba982105198c8`
- Vercel: `dpl_2ajAm7J2TBkdnZjQsqsp1PMC3wfj`
- Schema: `0025`

## Staged evidence

- Public route, authenticated UI submission, evidence/provenance controls, desktop,
  mobile, cache reuse, mobile Beta disclosure: pass.
- Private repair canary: completed, 3 distinct findings, 5 direct requests, zero
  browser use, duplicate cache reuse.
- Real Solari lifecycle during rollout: official Florida §43.16, 7.884s, 1 action,
  replay available, cleanup confirmed. At the documented Starter browser rate of
  $0.10/hour, measured browser runtime is approximately $0.00022.
- Live public-surface checks: anonymous 401; cross-owner job/evidence 404; hostile
  origin 403; oversized authenticated query 422.
- Database after observation: 5 completed jobs; 0 queued/running; 1 released browser
  session; 0 unreleased or cleanup-failed sessions; 13 sources; 12 findings; 70 events;
  7 retained raw blobs.
- Monitoring: 7/7 service checks and 5/5 read-path checks pass; no API 5xx or Scout
  worker error pattern; explicit reaper found zero candidates/staging orphans.

## Rollback and repair record

The first deployed screenshot exposed a material provenance defect: two Florida House
analyses discovered through `flsenate.gov` were labeled as Senate analyses and looked
duplicative. Public API admission was set false and the prior dark Vercel artifact
`dpl_E3q37kAuv37CPMebU2Lme3hmviz5` was promoted; `/scout` returned 404. After the
evidence-derived provenance/cache/UI repair and private canary, the fixed artifacts
above were re-enabled. This is direct rollback evidence, not a hypothetical command.

## Review and residuals

The second canonical multi-model pass returned SHIP from eight available families.
The ninth configured image reviewer was unavailable because its endpoint rejected image
input, so the tool's overall status remained HALT despite no second-pass BLOCK verdict.
Vercel Web Analytics is enabled and reports existing data; the deployed project-specific
analytics script loads. Headless automation queued Scout events but did not independently
observe their provider ingestion, so custom-event delivery remains an observability
qualification rather than a claimed pass. Durable server-side Scout events and service
monitoring are healthy.

