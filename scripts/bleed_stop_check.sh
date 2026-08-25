#!/usr/bin/env bash
# 2026-08-21 bleed-stop verification harness.
#
# Fires 4 fake IPs (one /24, mimicking the scraper's rotate-within-a-small-
# block shape) x up to 100 rounds each at /api/v1/bills?per_page=1 and
# asserts at least one 429, with a Retry-After header and a `docs` field in
# the body. Exits non-zero on any assertion failure.
#
# The local boot sets the SUBNET bucket tight (200/minute) and the per-IP/
# heavy/heavy-subnet buckets generous (1000/minute) -- 4 IPs each individually
# stay well under 300 (default) and 1000 (heavy), so the 429 this proves can
# ONLY come from the subnet bucket added by this fix, not the pre-existing
# per-IP one.
#
# Requests within a round (one per IP) fire CONCURRENTLY: sequentially, a
# live-DB round trip is slow enough (~0.5s) that 200 requests can take
# longer than the 60s fixed window itself, resetting the bucket before it
# ever trips. Firing the round's 4 IPs in parallel keeps wall time close to
# one request's latency per round instead of four.
#
# Usage:
#   ./scripts/bleed_stop_check.sh                 # boots the API locally
#   BILLCOMMONS_API_URL=http://localhost:8000 ./scripts/bleed_stop_check.sh
#   BILLCOMMONS_BLEED_CHECK_LIVE=1 ./scripts/bleed_stop_check.sh   # PROD -- do not run casually
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IPS=(203.0.113.10 203.0.113.11 203.0.113.12 203.0.113.13)  # one /24, RFC5737 TEST-NET-3
ROUNDS=100
PATH_UNDER_TEST="/api/v1/bills?per_page=1"

UVICORN_PID=""
cleanup() {
  if [[ -n "$UVICORN_PID" ]]; then
    kill "$UVICORN_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Verify round 8155c04, finding #5: only the local-boot branch below pins
# the settings that isolate the SUBNET bucket as the sole possible cause of
# a 429 (generous per-IP/heavy, tight subnet). Targeting a URL we don't
# control (live prod, or an already-running BILLCOMMONS_API_URL) proves
# only "some bucket fired somewhere" -- loudly say so, rather than let that
# read as the same isolating proof the local-boot run gives.
ISOLATING_PROOF=1
if [[ "${BILLCOMMONS_BLEED_CHECK_LIVE:-0}" == "1" ]]; then
  API_URL="https://api.billcommons.org"
  ISOLATING_PROOF=0
  echo "WARNING: targeting LIVE ($API_URL) -- this run does NOT isolate the" >&2
  echo "WARNING: subnet bucket. A 429 here only proves SOME bucket fired," >&2
  echo "WARNING: possibly the pre-existing per-IP one, not this fix." >&2
elif [[ -n "${BILLCOMMONS_API_URL:-}" ]]; then
  API_URL="$BILLCOMMONS_API_URL"
  ISOLATING_PROOF=0
  echo "WARNING: targeting an externally-provided BILLCOMMONS_API_URL" >&2
  echo "WARNING: ($API_URL) -- this run does NOT isolate the subnet bucket." >&2
  echo "WARNING: A 429 here only proves SOME bucket fired, not this fix." >&2
else
  API_URL="http://127.0.0.1:8123"
  echo "Booting the API locally on $API_URL ..." >&2
  (
    cd "$REPO_ROOT"
    # Verify round d1357cd, finding #5: pin the per-IP default too -- a
    # generous but UNPINNED default reads whatever the settings module's
    # own default happens to be, which drifted (450/minute, see
    # settings.py) without this harness moving in lockstep. Only the
    # subnet bucket may fire here.
    export BILLCOMMONS_API_RATE_LIMIT_DEFAULT="1000/minute"
    export BILLCOMMONS_API_RATE_LIMIT_SUBNET="200/minute"
    export BILLCOMMONS_API_RATE_LIMIT_HEAVY="1000/minute"
    export BILLCOMMONS_API_RATE_LIMIT_HEAVY_SUBNET="1000/minute"
    exec "$REPO_ROOT/.venv/bin/uvicorn" billcommons_api.app:create_app --factory \
      --host 127.0.0.1 --port 8123 --log-level warning
  ) &
  UVICORN_PID=$!
  healthy=0
  for _ in $(seq 1 30); do
    if curl -sf "$API_URL/api/v1/health" >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
  done
  if [[ "$healthy" != "1" ]]; then
    echo "FAIL: local API never became healthy at $API_URL/api/v1/health after 30s" >&2
    exit 1
  fi
fi

echo "Target: $API_URL$PATH_UNDER_TEST" >&2

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"; cleanup' EXIT

saw_429=0
hit_headers=""
hit_body=""
sent=0
for _ in $(seq 1 "$ROUNDS"); do
  pids=()
  for idx in "${!IPS[@]}"; do
    ip="${IPS[$idx]}"
    (
      status=$(curl -s -o "$WORKDIR/body.$idx" -D "$WORKDIR/headers.$idx" -w "%{http_code}" \
        -H "X-Real-Ip: $ip" "$API_URL$PATH_UNDER_TEST")
      echo "$status" > "$WORKDIR/status.$idx"
    ) &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    # `|| true`: under `set -e`, an unguarded `wait` propagates a
    # background job's non-zero exit (e.g. a single transient curl
    # failure) and aborts the whole script -- this loop's job is to
    # collect the round's responses and look for a 429 among them, not to
    # treat one flaky request as fatal.
    wait "$pid" || true
  done
  sent=$((sent + ${#IPS[@]}))
  for idx in "${!IPS[@]}"; do
    status="$(cat "$WORKDIR/status.$idx" 2>/dev/null || echo "")"
    if [[ "$status" == "429" ]]; then
      saw_429=1
      hit_headers="$WORKDIR/headers.$idx"
      hit_body="$WORKDIR/body.$idx"
      break
    fi
  done
  if [[ "$saw_429" == "1" ]]; then
    break
  fi
done

if [[ "$saw_429" != "1" ]]; then
  echo "FAIL: never saw a 429 across $sent requests -- limiter is a no-op" >&2
  exit 1
fi

if ! grep -qi '^Retry-After:' "$hit_headers"; then
  echo "FAIL: 429 response missing Retry-After header" >&2
  exit 1
fi
# Compact match, no \s -- Starlette's JSONResponse serializes with
# separators=(",", ":") (no space after the colon), and \s is a GNU-grep
# extension not guaranteed on every grep.
if ! grep -q '"code":"rate_limited"' "$hit_body"; then
  echo "FAIL: 429 body missing error.code=rate_limited: $(cat "$hit_body")" >&2
  exit 1
fi
if ! grep -q '"docs"' "$hit_body"; then
  echo "FAIL: 429 body missing 'docs' field: $(cat "$hit_body")" >&2
  exit 1
fi

if [[ "$ISOLATING_PROOF" == "1" ]]; then
  echo "OK: 429 seen after $sent requests, with Retry-After + docs field" >&2
else
  echo "OK (live, non-isolating): 429 seen after $sent requests, with" >&2
  echo "Retry-After + docs field -- but this does NOT prove the SUBNET" >&2
  echo "bucket specifically fired. Re-run without BILLCOMMONS_API_URL /" >&2
  echo "BILLCOMMONS_BLEED_CHECK_LIVE for the isolating proof." >&2
fi
