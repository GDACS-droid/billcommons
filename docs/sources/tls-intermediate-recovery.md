# TLS intermediate recovery (AIA chasing)

**Status:** implemented 2026-07-25. Code: `packages/shared/billcommons_shared/aia.py`,
integrated at `workers/ingest/billcommons_ingest/fulltext.py::FullTextFetcher._get`.

## The problem

Three state legislature hosts present **only their leaf certificate** and omit the
intermediate CA that links it to a trusted root:

| Host | Jurisdiction | Documents | Bills with text before the fix |
|---|---|---|---|
| `legislature.mi.gov` | MI | 18,977 | **0 of 3,884** |
| `billstatus.ls.state.ms.us` | MS | 13,057 | **0 of 4,006** |
| `www.cga.ct.gov` | CT | 4,931 | **7 of 1,283** |

Every fetch failed with:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate (_ssl.c:1010)
```

Reproducible from outside our infrastructure, so it is not our CA bundle:

```
$ openssl s_client -connect legislature.mi.gov:443 -servername legislature.mi.gov </dev/null
 0 s:C = US, ST = Michigan, ... CN = *.legislature.mi.gov
Verify return code: 21 (unable to verify the first certificate)
```

Browsers do not show this, because on a verification failure they fetch the missing
intermediate from the URL in the leaf's Authority Information Access extension
("AIA chasing"). Python's `ssl` module does not. So these sites look perfectly fine
in Chrome and are completely unfetchable to `httpx` — which is why the condition
survived so long: it presented as an ordinary transient network error on hosts that
a human spot-checking in a browser would confirm as working.

The recovered intermediates are all from mainstream CAs — DigiCert Global G2 TLS RSA
SHA256 2020 CA1 (MI), GlobalSign RSA OV SSL CA 2018 (MS), Go Daddy Secure Certificate
Authority G2 (CT). Nothing exotic; three ordinary server misconfigurations.

## Why this was invisible

The failure did not merely lose those three states. It deadlocked the entire crawl.
See `docs/architecture/ARCHITECTURE.md` and the queue notes, but in short: the
permanently-failing jobs could not accumulate `attempts` (the claim's increment was
discarded by the failure handler's rollback), so they never dead-lettered; they
therefore sat in `queued` forever; and because the top-up gate compared *total*
queued against its floor, 1,215 undying jobs held the queue "full" so no new work
was ever enqueued. Throughput was exactly zero for an hour while Railway reported
the worker `Online` and its logs showed it claiming ~45 jobs/minute.

## What the fix does — and does not — do

`aia.build_repaired_ssl_context(host)`:

1. Reads the leaf certificate from the host over an **unverified** handshake. The
   leaf is treated as untrusted data; only its AIA extension is parsed.
2. Downloads the `caIssuers` certificate named there (DER, PEM and PKCS#7 all occur
   in the wild). Walks upward at most `MAX_CHAIN_DEPTH` (4) hops.
3. **Stops before any self-signed certificate.** A root recovered over the network is
   never a trust-anchor candidate — that would let a server nominate its own anchor.
4. **Independently verifies** leaf + recovered intermediates against the certifi
   roots, with hostname and validity checked (`cryptography.x509.verification`).
   If that fails, returns `None` and the original TLS error stands.
5. Only then builds a context of certifi roots + the recovered intermediates.

The narrowing in `is_missing_issuer_error` is deliberate and load-bearing: **only**
`unable to get local issuer certificate` is repairable. Expired, self-signed,
hostname-mismatched and revoked certificates keep failing, and are never even
probed.

### The sharp edge, stated plainly

Python's `ssl` module has **no API for supplying untrusted chain certificates** to
client-side verification. Anything added via `load_verify_locations` becomes a trust
*anchor*. So a naive implementation of AIA chasing is strictly equivalent to
`verify=False` against an active MITM: the attacker serves their own leaf, points
AIA at their own "intermediate", and that intermediate gets installed as an anchor
that validates their forged leaf.

Step 4 is what closes this. Because the recovered material must already chain to a
shipped root before anything is installed, the only certificate that can ever become
an anchor is a genuine CA intermediate the shipped roots already vouch for.

**Residual limitation:** once installed, that intermediate is an anchor for the
returned context, so name/path-length constraints above it and its revocation status
stop being enforced for that one host's client. The alternative — a hand-pinned
bundle per broken host — trades that for permanent manual upkeep and silent breakage
on every CA rotation. We chose recovery plus the verification gate. Anyone revisiting
this should weigh those two, not assume the gate can be dropped.

## robots.txt: a compliance gap this was masking

`RobotsCache` treats an unfetchable robots.txt as allow-all, which is correct
robots semantics (absence of a file is not a disallow) — but a **broken cert chain
also makes robots.txt unfetchable**, so all three hosts were being crawled under a
fallback rather than their actual rules.

CT genuinely publishes one:

```
User-agent: *
Disallow: /cgi-bin/   /asp/cgabillstatus/   /asp/menu/   /audio/
Disallow: /html/      /images/  /js/  /legpics/  /Misc/  /spincludes/
```

So `FullTextFetcher` now invalidates the cached robots verdict for a host the moment
it repairs that host's TLS, re-reads it over the working connection, and re-checks
the URL before fetching. Verified consequence: **0 of CT's 4,931 documents fall under
a disallowed path**, so honouring the real file costs no coverage — but we are now
honouring it, rather than having happened to be lucky.

## Coverage consequence

Before this fix, MI, MS and CT could never satisfy SPEC GREEN criterion #5
(≥80% of obtainable full text) — their obtainable text was 100% unreachable. Any
convergence ETA computed before 2026-07-25 was measured against a denominator
containing ~9,200 permanently unreachable bills and was wrong for that reason,
independent of crawl rate.

These three states are **not** full-text limitations like DC and TN
(`docs/sources/dc-tn-fulltext-limitations.md`), whose sources are robots-blocked.
The text here was always publicly available and sanctioned; only our TLS client
could not reach it.
