# Incident runbook — the API is slow, hanging, or down

Written after 2026-08-02, when the API was unavailable for an unknown number of
hours and nobody noticed. Read the first section before doing anything.

## The three things that made that incident invisible

Internalise these or you will misdiagnose the next one the same way.

1. **`billcommons.org` returning 200 proves nothing.** Next.js serves cached
   pages over a dead API. The website looked completely healthy throughout.
2. **`/api/v1/health` returning 200 proves nothing.** It catches the database
   error and returns **HTTP 200** with `{"status": "degraded", "database":
   "error"}`. A status-code-only check reports UP during exactly this failure.
   **Assert on the body**: `"database":"ok"`.
3. **The crawl-stall monitor staying green proves nothing.** It watches the
   *write* path — extracted text — which was genuinely fine. Read-path health is
   a separate monitor (`read_path_monitor.py`).

Corollary: "Railway says all services Online" is not evidence either. It was
Online for the entire outage.

## 1. Detect and confirm (2 minutes)

```bash
# The authoritative check. Body, not status code.
curl -s --max-time 10 https://api.billcommons.org/api/v1/health

# A real read through the rate-limited path (/health is limiter-exempt,
# so it cannot see a 429 storm).
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 10 \
  'https://api.billcommons.org/api/v1/bills?per_page=1'

# End to end, including Vercel and the Data Cache.
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 15 \
  https://billcommons.org/states/NC

# Or just run the monitor, which does all three:
.venv/bin/python infra/monitoring/read_path_monitor.py
```

Healthy looks like `"database":"ok"` and sub-second responses.

**A request that HANGS is a different failure from one that errors.** Hanging
means requests are queueing for a database connection. Erroring fast is usually
a bad deploy.

## 2. Capture evidence BEFORE restarting

A restart destroys the evidence and the problem usually comes back. Spend two
minutes here.

```bash
# What is the pool doing? 15 backends all "idle in transaction", from ONE
# client address, with transaction ages clustered in 30-second buckets, is the
# signature of pool exhaustion.
.venv/bin/python - <<'EOF'
import json, psycopg
d = json.load(open('/home/alberto/.config/billcommons/railway-pg.json'))
with psycopg.connect(d['DATABASE_PUBLIC_URL'], connect_timeout=20) as c:
    cur = c.cursor()
    cur.execute("""select client_addr::text, state, count(*),
                          max(now()-xact_start) as oldest_xact
                   from pg_stat_activity where backend_type='client backend'
                   group by 1,2 order by 3 desc""")
    for r in cur.fetchall(): print(r)
    cur.execute("""select left(regexp_replace(query,'\\s+',' ','g'),120), state,
                          now()-query_start as dur
                   from pg_stat_activity
                   where state <> 'idle' and pid <> pg_backend_pid()
                   order by query_start limit 10""")
    for r in cur.fetchall(): print(r)
EOF

# Sequential scans are the other classic cause. idx_scan=0 on a child table
# means a foreign key has no index and every lookup is a full scan.
# (Nine such indexes were missing until 2026-08-02.)
```

```bash
npx @railway/cli logs --service api | tail -50
```

If you see `Railway rate limit of 500 logs/sec reached ... Messages dropped`,
the service is in a traceback storm — capture what you can immediately, because
most of it is already gone.

## 3. Mitigate

In order of preference:

1. **Shed the load, don't add capacity.** If a crawler is the source, that is
   the thing to bound. Raising the pool while queries are slow only lets the
   failure consume more database.
2. **Restart only if the process is genuinely wedged** (static routes like
   `/openapi.json` also failing). `npx @railway/cli redeploy --service api --yes`
   re-runs the same deployment; it does not ship new code.
   **A restart that "works" for ten minutes and then degrades again is not a
   fix** — it means the underlying cost per request is too high.
3. **Roll back** if the incident started right after a deploy:
   `npx @railway/cli deployment list --service api`, then redeploy the last
   known-good id.

## 4. Verify recovery — five endpoints, not one

```bash
for p in health "bills?per_page=1" "coverage?per_page=5" "changes?limit=5" \
         "stats/mortality"; do
  printf '%-28s ' "$p"
  curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 20 \
    "https://api.billcommons.org/api/v1/$p"
done
```

Then one MCP tool call, because the MCP server is a separate service and has
stayed healthy while the API was down:

```bash
curl -s --max-time 30 -X POST https://mcp.billcommons.org/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"get_jurisdiction_coverage","arguments":{}}}' | tail -c 300
```

Recovery means **sustained** health. Probe ten times over a minute; a single
200 can be one lucky request through a saturated pool.

## 5. Write it down

Add what happened to `~/.claude/.../memory/billcommons-traction-reality-check.md`
and, if it revealed a general trap, to the failure-archaeology skill. The
specific thing worth recording is **what made it invisible**, not just the fix.

## Current configuration (2026-08-02)

| Setting | API | Workers |
|---|---|---|
| `pool_size` + `max_overflow` | 30 + 20 = 50 | 5 + 10 = 15 |
| `pool_timeout` | 10s | 30s |
| `statement_timeout` | 30s | 300s |
| `connect_timeout` | 10s | 10s |
| `pool_recycle` | 1800s | 1800s |

Env-tunable per service: `BILLCOMMONS_DB_POOL_SIZE`, `_MAX_OVERFLOW`,
`_POOL_TIMEOUT`, `_STATEMENT_TIMEOUT_MS`. Postgres `max_connections` is 100 and
is shared with mcp, three workers, and the ingest job on the box — check the
budget before raising anything.

Pool exhaustion now returns **503 + `Retry-After: 5`**, not a 500 traceback.

## Monitors

| Monitor | Watches | Cadence |
|---|---|---|
| `com.gdacs.billcommons-read-path.timer` | API + website + a real MCP tool call | 2 min |
| `com.gdacs.billcommons-stall-monitor.timer` | full-text crawl (write path) | 10 min |
| `com.gdacs.billcommons-alerts.timer` | nightly topic digests | daily 08:30 |

Both monitors alert to Telegram on a state change, after two consecutive
failures, with a 6-hour re-notify. Silence from a monitor is not health — check
`systemctl --user list-timers 'com.gdacs.billcommons*'` if you have not seen one
fire.

### The read-path monitor must not be counted as usage

The monitor calls a real MCP tool (not just `initialize`, which succeeds
against a dead database). At a 2-minute cadence that is ~720 tool calls a day
landing in `tool_invocations` — the table whose entire purpose is answering
"is anyone using this?". On 2026-08-02 it was **65 of 67 rows** within two
hours of both shipping.

It now sends `x-billcommons-probe`, which
`billcommons_mcp.telemetry.ClientFamilyMiddleware` turns into
`client_family = 'self-probe'`, and `/api/v1/stats/usage` excludes.

**This can break silently.** The tag rides a ContextVar set in ASGI middleware
and read inside the tool; if a future `mcp` release dispatches the request
through a task group that does not copy context, the tag is lost, no error is
raised anywhere, and the only symptom is a usage figure that flatters us. The
in-process test cannot cover that leg (the stack deadlocks on the streaming
response), so **verify it against production after any `mcp` upgrade or change
to `build_app()`**:

```bash
# Should be ~0 untagged coverage calls; every monitor row must be 'self-probe'.
psql "$DATABASE_URL" -c "
  select coalesce(client_family,'(null)') fam, count(*)
    from tool_invocations
   where tool='get_jurisdiction_coverage'
     and occurred_at > now() - interval '30 minutes'
   group by 1;"
```

A NULL family on a 120s cadence means the tagger stopped working.
