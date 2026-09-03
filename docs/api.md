# API reference

Base URL: `http://localhost:8000` (native) or your deployment origin. FastAPI
serves interactive docs at **`/docs`** (Swagger) and **`/redoc`**, generated from
the same code — this page is the narrative companion.

The dashboard calls every endpoint under a `/api` prefix that the Vite dev server
(and any reverse proxy) strips before forwarding; the backend routes themselves
are unprefixed, as listed below.

---

## Authentication

Two independent mechanisms:

- **Webhook — HMAC.** `POST /webhook` must carry
  `X-Hub-Signature-256: sha256=<hmac>` over the raw body, keyed by
  `AUTOPR_WEBHOOK_SECRET`. Verified in constant time; failure is `401`.
- **Ops writes — bearer token.** `approve`/`reject` require
  `Authorization: Bearer <AUTOPR_API_TOKEN>` whenever a token is configured.
  Empty token = unauthenticated dev no-op (with a startup warning). Read
  endpoints are unauthenticated unless `AUTOPR_REQUIRE_AUTH_FOR_READS=true`.

| Failure | Status |
| --- | --- |
| Bad/missing webhook signature | `401` |
| Bad/missing bearer token (when required) | `401` |
| Rate limit exceeded | `429` (+ `Retry-After`) |
| Queue (Redis) unavailable on ingest | `503` |

Rate limits: `AUTOPR_RATE_LIMIT_WEBHOOK_PER_MIN` (default 120) and
`AUTOPR_RATE_LIMIT_MUTATIONS_PER_MIN` (default 30), per client IP.

---

## Operational endpoints

### `GET /healthz` — liveness
Always `200 {"status":"ok"}` if the process is up. No dependency checks.

### `GET /readyz` — deep readiness
Probes DB + Redis. `200` when both answer; `503` otherwise, with a body naming
what failed:
```json
{ "status": "not_ready", "checks": { "database": "ok", "redis": "unavailable" } }
```

### `GET /metrics` — Prometheus exposition
HTTP counters/latency plus pipeline gauges (`autopr_jobs`, `autopr_reviews`,
`autopr_queue_depth`). `404` when `AUTOPR_METRICS_ENABLED=false`.

---

## Ingress

### `POST /webhook`
The GitHub webhook target. Verifies the signature, parses the event, and
idempotently persists + enqueues a job. Non-actionable events are acked without
work.

```bash
BODY='{"action":"opened","pull_request":{"number":7,...},"repository":{"full_name":"octo/repo"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$AUTOPR_WEBHOOK_SECRET" | awk '{print $2}')"
curl -sS -X POST localhost:8000/webhook \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

| Response | Meaning |
| --- | --- |
| `200 {"status":"queued","job_id":N}` | Accepted and enqueued. |
| `200 {"status":"duplicate","job_id":N}` | Same delivery already seen (idempotent no-op). |
| `200 {"status":"ignored","event":…}` | Not an actionable event. |
| `401 {"detail":"invalid signature"}` | HMAC mismatch. |
| `503 {"detail":"queue temporarily unavailable"}` | Redis down — GitHub should retry. |

---

## Human-in-the-loop (the write path)

### `GET /reviews/pending`
The approval work queue, **oldest first**.
```bash
curl -s localhost:8000/reviews/pending | jq
```
```json
{ "count": 4, "pending": [ { "id": 42, "repo": "acme/auth-service", "pr_number": 77,
  "action": "comment_review", "risk": "medium", "title": "…", "body": "…markdown…",
  "status": "pending", "created_at": "…" } ] }
```

### `POST /reviews/{id}/approve`
Approve a decision and carry out its GitHub action. **The only path to a real
GitHub write** (a no-op that only *records* the action while dry-run is on).
```bash
curl -sS -X POST localhost:8000/reviews/42/approve \
  -H "Authorization: Bearer $AUTOPR_API_TOKEN"
```

| Response | Meaning |
| --- | --- |
| `200 {"status":"executed","url":…}` | Action performed (or recorded, in dry-run). |
| `200 {"status":"already_executed",…}` | Idempotent — was already done. |
| `409 {"detail":"decision was rejected"}` | Can't approve a rejected decision. |
| `404` | No such decision. |
| `502 {"status":"action_failed",…}` | Live GitHub call failed; marked `FAILED`, retryable. |

### `POST /reviews/{id}/reject`
Reject a decision. **No GitHub action is ever taken.**
```bash
curl -sS -X POST localhost:8000/reviews/42/reject -H "Authorization: Bearer $AUTOPR_API_TOKEN"
```
`200 {"status":"rejected",…}` · `409` if already executed · `404` if not found.

---

## Dashboard reads

### `GET /stats`
Header KPIs — counts by status plus config the UI needs.
```json
{ "jobs":    { "total": 10, "pending": 1, "queued": 1, "processing": 1, "done": 5, "dead": 1 },
  "reviews": { "total": 8, "pending": 4, "approved": 0, "executed": 2, "rejected": 1, "failed": 1 },
  "config":  { "github_dry_run": true, "live_github": false,
               "auto_comment_max_risk": "low", "github_web_url": "https://github.com" } }
```
`github_web_url` is derived server-side from `AUTOPR_GITHUB_API_URL`
(`api.github.com` → `github.com`, Enterprise hosts strip to their web origin), so
the dashboard's PR links are correct against any host without a hardcoded domain.

### `GET /jobs?limit=50`
Recent jobs (newest first, `limit` clamped to 1–200), each with its exactly-once
result summary, `attempts`, and `last_error`.

### `GET /reviews?status=&limit=50`
Full routing-decision history (newest first), optionally filtered by status
(`pending`/`approved`/`executed`/`rejected`/`failed`). An unknown status yields
an empty list, not a 4xx.

---

## Notes for integrators

- **Correlation id.** Send `X-Request-ID` and it's honoured and echoed back;
  otherwise the API mints one. It appears in every log line for the request and
  the job it spawns, across both processes.
- **Idempotency.** Re-POSTing an identical webhook is safe — the `dedup_key`
  makes it a no-op. Re-approving an executed decision is safe — it returns the
  existing result rather than acting twice.
- **CORS.** Only origins in `AUTOPR_CORS_ORIGINS` may call the API from a browser;
  no cookies are used (`allow_credentials=false`).
