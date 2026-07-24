# API examples

Base URL: `https://api.billcommons.org/api/v1`. Read-only, public, no API
key required (60 req/min/IP anonymous tier per `docs/SPEC.md`). Interactive
OpenAPI docs: `https://api.billcommons.org/docs`. All examples below were
run against the live production API; response bodies are real, with long
arrays trimmed (noted inline).

## Search

### curl

```bash
curl -s "https://api.billcommons.org/api/v1/search?q=education&per_page=2"
```

Bill-number lookup also works directly (normalizes `"HB 123"` / `"HB123"` /
`"H.B. 123"` to the same match):

```bash
curl -s "https://api.billcommons.org/api/v1/search?q=HB%20123&per_page=1"
```

Real response shape (trimmed to one result):

```json
{
  "data": [
    {
      "id": "b6ca8ffe-2f85-4b58-bf91-ba1ddc373bab",
      "jurisdiction_id": "9fc3cba3-dfa8-40e4-94f8-a1a5e2df87d7",
      "session_id": "57d0f30d-17aa-488d-949b-5c40e663f09c",
      "chamber": "lower",
      "identifier": "HB 123",
      "identifier_norm": "HB 123",
      "title": "An Act relating to vehicle rental taxes; ...",
      "short_title": null,
      "bill_type": "bill",
      "status": null,
      "status_date": null,
      "introduced_date": null,
      "latest_action_text": "(H) EFFECTIVE DATE(S) OF LAW SEE CHAPTER",
      "latest_action_date": "2025-07-30",
      "source_url": "https://www.akleg.gov/basis/Bill/Detail/34?Root=HB123#tab5_4",
      "rank": null,
      "highlight": null,
      "match_type": "bill_number"
    }
  ],
  "pagination": { "page": 1, "per_page": 1, "total": 23, "total_pages": 23 },
  "meta": { "source_freshness": null, "api_version": "v1", "request_id": "..." }
}
```

Note `match_type: "bill_number"` — the search endpoint distinguishes the
exact bill-number fast path from FTS/trigram keyword matches. Other
supported query params (per SPEC): `jurisdiction`, `session`, `chamber`,
`status`, `sponsor`, `subject`, `committee`, `date_from`, `date_to`, `sort`,
`page`, `per_page` (max 50).

### Python (httpx)

```python
import httpx

BASE = "https://api.billcommons.org/api/v1"

with httpx.Client(base_url=BASE, timeout=15.0) as client:
    resp = client.get("/search", params={"q": "education", "jurisdiction": "AL", "per_page": 5})
    resp.raise_for_status()
    payload = resp.json()
    for bill in payload["data"]:
        print(bill["identifier"], "-", bill["title"][:80])
    print(f"total={payload['pagination']['total']}")
```

### JavaScript (fetch)

```javascript
const BASE = "https://api.billcommons.org/api/v1";

const res = await fetch(`${BASE}/search?q=education&jurisdiction=AL&per_page=5`);
if (!res.ok) throw new Error(`search failed: ${res.status}`);
const { data, pagination } = await res.json();
for (const bill of data) {
  console.log(bill.identifier, "-", bill.title.slice(0, 80));
}
console.log(`total=${pagination.total}`);
```

## Jurisdictions

```bash
curl -s "https://api.billcommons.org/api/v1/jurisdictions?per_page=1"
```

```json
{
  "data": [
    { "id": "59779d7a-8656-460a-be81-fe76ffb3d778", "name": "Alabama", "abbreviation": "AL", "classification": "state", "openstates_id": null }
  ],
  "pagination": { "page": 1, "per_page": 1, "total": 51, "total_pages": 51 },
  "meta": { "source_freshness": null, "api_version": "v1", "request_id": "..." }
}
```

Fetch one by id — `GET /jurisdictions/{id}` returns the bare object (no
envelope):

```bash
curl -s "https://api.billcommons.org/api/v1/jurisdictions/59779d7a-8656-460a-be81-fe76ffb3d778"
# {"id":"59779d7a-8656-460a-be81-fe76ffb3d778","name":"Alabama","abbreviation":"AL","classification":"state","openstates_id":null}
```

## Bills and subresources

```bash
curl -s "https://api.billcommons.org/api/v1/bills?per_page=1"
```

Fetching one bill's detail plus its subresources:

```bash
BILL_ID=d6165476-41c3-4172-8f10-3a9688ff902f

curl -s "https://api.billcommons.org/api/v1/bills/$BILL_ID"
curl -s "https://api.billcommons.org/api/v1/bills/$BILL_ID/actions"
curl -s "https://api.billcommons.org/api/v1/bills/$BILL_ID/sponsors"
curl -s "https://api.billcommons.org/api/v1/bills/$BILL_ID/versions"
curl -s "https://api.billcommons.org/api/v1/bills/$BILL_ID/documents"
curl -s "https://api.billcommons.org/api/v1/bills/$BILL_ID/votes"
```

Real `/actions` response (trimmed to first two entries):

```json
[
  { "id": "d72122c3-ffa7-43fc-b99c-de956917959f", "bill_id": "d6165476-...", "description": "Senate Hopper", "action_date": "2025-02-12", "classification": "introduction", "order": 0 },
  { "id": "d7df05b0-6660-4437-96df-e295b42cf3f9", "bill_id": "d6165476-...", "description": "Senate Read and Referred", "action_date": "2025-02-13", "classification": "referral-committee", "order": 1 }
]
```

Real `/sponsors` response (trimmed to first entry — note `person_id` is
`null`: the bulk-CSV source has no legislator-roster file, so sponsors are
captured as free text; see `docs/state-coverage/methodology.md`):

```json
[
  { "id": "07411889-c42f-433b-9340-8e17a37f25ee", "bill_id": "d6165476-...", "person_id": null, "name": "Kay Kirkpatrick", "classification": "primary", "primary": true }
]
```

Real `/votes` response (trimmed — `votes[]` holds member-level records):

```json
[
  {
    "id": "0f93b7a9-db71-47f9-ab62-6151c47381ec",
    "bill_id": "d6165476-...",
    "motion_text": "Senate Vote #148 - 2025-2026 Regular Session",
    "motion_classification": "passage",
    "start_date": "2025-03-04",
    "result": "pass",
    "yes_count": 53,
    "no_count": 1,
    "other_count": 2,
    "votes": [
      { "id": "dc690c0e-...", "person_id": null, "voter_name": "ALBERS", "option": "yes" },
      { "id": "f5f25597-...", "person_id": null, "voter_name": "ANAVITARTE", "option": "yes" }
    ]
  }
]
```

### Python (httpx) — fetch a bill and its full text-in-progress state

```python
import httpx

BASE = "https://api.billcommons.org/api/v1"

with httpx.Client(base_url=BASE, timeout=15.0) as client:
    bill = client.get(f"/bills/{bill_id}").json()
    documents = client.get(f"/bills/{bill_id}/documents").json()
    has_text = any(d["has_extracted_text"] for d in documents)
    print(bill["identifier"], bill["title"][:60], "full_text_available=", has_text)
```

### JavaScript (fetch) — bill + actions timeline

```javascript
const BASE = "https://api.billcommons.org/api/v1";

async function billTimeline(billId) {
  const [bill, actions] = await Promise.all([
    fetch(`${BASE}/bills/${billId}`).then((r) => r.json()),
    fetch(`${BASE}/bills/${billId}/actions`).then((r) => r.json()),
  ]);
  return { bill, actions: actions.sort((a, b) => a.order - b.order) };
}
```

## Coverage

```bash
curl -s "https://api.billcommons.org/api/v1/coverage"
```

Real response shape (one row, trimmed — this is the same data rendered on
`status.billcommons.org`):

```json
{
  "data": [
    {
      "jurisdiction_code": "AK",
      "jurisdiction_name": "Alaska",
      "session_identifier": "2026 Regular Session (34th Legislature, 2nd Session)",
      "session_status": "active",
      "bill_count": 856,
      "full_text_count": 0,
      "full_text_pct": 0.0,
      "last_update": "2026-07-24T05:39:03.862550+00:00",
      "source_name": "Open States / Plural",
      "validation_sample": null,
      "validation_pass_rate": 0.8666666666666667,
      "status": "VALIDATING",
      "known_gaps": [
        "full-text coverage is 0; GREEN deferred until the fulltext pipeline (billcommons_ingest.fulltext) has run for this jurisdiction"
      ]
    }
  ]
}
```

See `docs/state-coverage/methodology.md` for what each field/status means
and why `full_text_count: 0` caps `status` below `GREEN`.

## Health / readiness (no `/api/v1` prefix quirk — these ARE under it)

```bash
curl -s "https://api.billcommons.org/api/v1/health"   # {"status":"ok","database":"ok"}
curl -s "https://api.billcommons.org/api/v1/ready"    # {"ready":true,"database":"ok"}
```

## Error shape

All typed errors share one envelope (`errors.py`):

```bash
curl -s "https://api.billcommons.org/api/v1/bills/not-a-uuid"
```

```json
{
  "error": {
    "code": "validation_error",
    "message": "[{'type': 'uuid_parsing', 'loc': ('path', 'bill_id'), 'msg': 'Input should be a valid UUID, invalid character: found `n` at 1', ...}]",
    "request_id": "9408d06a-eb11-4d58-b1be-e3d70ace4b7a"
  }
}
```

Every response (success or error) carries `meta.request_id` (or
`error.request_id`) for support/log correlation.

## MCP (Streamable HTTP, not REST — included here for completeness)

The MCP server at `https://mcp.billcommons.org/mcp` is a separate protocol
(JSON-RPC over Streamable HTTP, not a plain REST endpoint) — see
`docs/SPEC.md` "MCP" section for the 10 tools and `apps/mcp/server.py` for
the live tool signatures. A raw `curl -X POST` needs the SSE-capable
`Accept` header:

```bash
curl -s -X POST https://mcp.billcommons.org/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"example","version":"0"}}}'
```

Real (trimmed) response confirms the server and its capabilities:

```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}, ...},"serverInfo":{"name":"billcommons","version":"..."}, ...}}
```

In practice, use an MCP client library (the official `mcp` Python SDK, or
any Streamable-HTTP-capable MCP client) rather than hand-rolling the
JSON-RPC handshake — see `apps/mcp/tests/mcp_integration.py` for a working
reference client that lists tools and calls `search_legislation` /
`get_jurisdiction_coverage` against a real deployed endpoint
(`MCP_TEST_URL` env var).
