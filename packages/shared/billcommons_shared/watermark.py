"""The change-feed "commit safety lag" -- single source of truth for every
reader of `bill_events`: `/changes` (billcommons_api.routers.changes),
the per-jurisdiction Atom feeds (routers/feeds.py, via routers/changes' own
re-export), the webhooks API (routers/webhooks.py) and the webhook
dispatcher (workers/webhooks/dispatch_webhooks.py). `workers/alerts/
send_alerts.py` carries its own literal copy rather than importing this --
see that script's docstring for why (it runs straight from the working tree
without a deploy step and deliberately does not depend on billcommons_shared)
-- and is kept in sync by a contract test instead, the same
duplicate-value-plus-contract-test shape that file already uses for its
topic-membership SQL.

WHY the lag exists at all (unchanged since migration 0005): `seq` is
allocated at INSERT and becomes visible at COMMIT, so a writer that holds a
transaction open can own a LOW seq that only appears after HIGHER ones are
already visible. A consumer that advanced past it would never see it -- a
permanently missed change, reported as "nothing to report", which is the
single worst failure a monitoring source can have. So every reader here only
serves events old enough that any transaction which could hold a lower seq
has necessarily finished, bounding the damage to LATENCY instead of
CORRECTNESS. This is only sound while every writing transaction is shorter
than this window.

EMPIRICAL BASIS (2026-08-04): a pre-ship measurement (spec §8's own
"Pre-ship measurement (D14)" ship gate) run by the project orchestrator
directly against prod -- this repo's own tooling has no prod DB write/
measurement access, see CLAUDE.md -- found, across all 452,013 bill_events
rows: max observed seq-vs-changed_at visibility skew = 98.2s, p99.99 = 97.1s
(method: running-max window over seq ordering). The previous value (120s)
held, but with only ~22s of margin. 240s is roughly 2.4x the observed max.
That measurement is asserted here, not independently re-derived by whichever
agent last edited this constant -- if it is ever in question, re-run the
D14 method against the current corpus size before trusting this value
further.
"""
from __future__ import annotations

COMMIT_SAFETY_LAG_SECONDS = 240
