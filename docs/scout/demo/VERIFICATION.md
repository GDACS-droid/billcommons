# Demo verification record

This sanitized record contains no API key, provider session ID, replay bearer URL,
customer data, cookie, browser connection endpoint, or private database value.

## Product segment: deterministic UI

The first 11.12 seconds show the production-built `/scout` page consuming three
local API fixtures: queued, running, and completed. The completed fixture contains
two linked findings, two retained official sources, one released browser session,
and measured usage. The on-screen bottom rail labels the segment
`DETERMINISTIC PRODUCT FIXTURE | DURABLE JOB EVENTS` throughout.

Runtime assertions executed against the production build at desktop and mobile
viewports:

- the Florida Senate HB 625 → Chapter No. 2026-141 finding rendered;
- the Florida Statutes §43.16 current-law finding rendered;
- the compact `Solari: released · 1 page · 2 actions` summary rendered;
- the terminal status referenced retained source metadata;
- both official-record evidence controls rendered;
- no Next.js development indicator appeared.

This segment proves UI hierarchy and state handling. It does not claim a production
deployment or live API job.

## Live segment: real Solari browser

The final nine seconds use before-and-after screenshots from one post-hardening live
command in the public cookbook example:

```bash
cd examples/bill-commons-scout-py
.venv/bin/python main.py --live
```

Observed on 2026-09-01:

| Field | Result |
|---|---|
| Provider | `solari_browser` |
| Official navigation | Chapter 43 contents → section 43.16 |
| Extracted section | `43.16` |
| Required current-law text | exact paragraph beginning “(d) One judge, or senior judge …” |
| Required history | `s. 1, ch. 2026-141` |
| Runtime | 10,137 ms |
| Pages | 1 |
| Actions | 2 |
| Routed requests | 38 of 48 maximum |
| Recording/replay capability | available |
| Remote cleanup | confirmed |
| Captured HTML SHA-256 | `2e70542918802d1e9e744ea98d8e7ecfc911731cbdca1f98fcd76cc7a9bb7e3c` |

The output exposed no session identifier, WebSocket/CDP endpoint, replay URL,
credential, cookie, or raw provider exception. Both screenshots were visually
inspected at their original 1280×720 resolution and contain only public Florida
Legislature pages. Their public-example SHA-256 values are:

- chapter contents: `7c91920fa2b7f1e398468fcef533a0964baab1cb092eae76100ff446e568005e`
- statute result: `23a1c251cc3ceaf75ed035a763f5cfcaee4f769bd62a75e6bb3a4f42a83cfd3b`

The live command uses one non-retrying session-create request. If creation times out
after an ambiguous provider outcome, cleanup is explicitly *not* reported as
confirmed. Cancellation is re-raised after cleanup, the final route must semantically
equal the exact §43.16 page, and evidence fields must come from one scoped `.Section`
container. Twelve deterministic tests cover those contracts.

Public source and proof:

- [`GDACS-droid/solari-cookbook` challenge branch](https://github.com/GDACS-droid/solari-cookbook/tree/billcommons-scout-challenge/examples/bill-commons-scout-py)
- public two-frame example commit `1233dd2`

## Claim boundary

The Solari command verifies current §43.16 text and its 2026 chapter-law history.
The product case file separately associates that chapter law with HB 625 through an
official Florida Senate bill record. The demo does not claim that the browser command
independently establishes the bill-to-law mapping.

## Media integrity

The MP4 fully decoded with `ffmpeg -v error`.

- H.264, 1440×900, 25 fps, 504 frames, `yuv420p`
- 20.160000 seconds
- 864,333 bytes
- SHA-256 `c783e4b1efb21d712195fd5b7bd6da2012edafeba43051b27265300e7122d472`

Poster SHA-256:
`22ed4db376ea0a2b18d5230cc630842f66de88ce55daa5fcdde0be06ac4f6f2a`

Two-step live-proof image SHA-256:
`21991ca0699009401c438cceb5c40f2e55a9fc6dce98cbbe02abd0046fc883a6`
