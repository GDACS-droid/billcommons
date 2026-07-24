# billcommons.org DNS + domain runbook

Domain: billcommons.org — registered 2026-07-23 at Vercel (gdacs-droid),
Vercel nameservers. DNS is managed with `vercel dns` / project domain
assignment; no external registrar steps needed.

## Target topology

| Host | Points at | How |
|---|---|---|
| billcommons.org | Vercel project `billcommons-web` | `vercel domains add billcommons.org` on the web project (apex auto-configured on Vercel NS) |
| www.billcommons.org | 308 → apex | add to same project; Vercel auto-redirects www→apex when apex is primary |
| api.billcommons.org | Railway service `api` | `vercel dns add billcommons.org api CNAME <railway-target>.up.railway.app` + add custom domain on Railway service (gives exact CNAME target + TLS) |
| mcp.billcommons.org | Railway service `mcp` | same pattern as api |
| status.billcommons.org | Vercel project `billcommons-web` | add domain to project; Next.js rewrites host → /coverage |

Railway custom domains: Railway dashboard/CLI issues a per-service
`<name>.up.railway.app` target and provisions Let's Encrypt automatically
once the CNAME resolves.

## Exact records (final state)

```
billcommons.org.         ALIAS/A   → Vercel (automatic, project-assigned)
www.billcommons.org.     CNAME     → cname.vercel-dns.com. (automatic)
status.billcommons.org.  CNAME     → cname.vercel-dns.com. (automatic)
api.billcommons.org.     CNAME     → <api-service>.up.railway.app.
mcp.billcommons.org.     CNAME     → <mcp-service>.up.railway.app.
```

API docs live under api.billcommons.org/docs (no separate docs subdomain in
v1). Fill in the concrete railway targets at deploy time; verify with
`dig +short` each host and an HTTPS smoke test before announcing.
