# Phase 9 — Decisions Log

Making the system **observable and operable**. Phases 6–8 made it secure and
durable; this phase makes it possible to *watch* — to answer "is it up, is it
ready, what is it doing, and can I follow one webhook all the way through?"
without attaching a debugger. The core was already emitting structured events;
what was missing was a single logging setup shared by both processes, a trace id
that survives the jump across the queue, a readiness signal distinct from
liveness, and a metrics surface. This phase adds exactly those, and no more —
correlation ids rather than a full tracing backend, pull-at-scrape gauges rather
than a metrics pipeline, because that is the proportionate weight for a
single-operator showcase.

The through-line: **one honest signal per operational question.** *Is the
process alive?* `/healthz`. *Can it actually serve — are its dependencies up?*
`/readyz`. *What has it done and how fast?* `/metrics`. *What happened to this
one request?* a `correlation_id` that appears in every log line it touched, in
both processes. Each is cheap, each is unambiguous, and none pretends to more
fidelity than it has.

---

## 1. **One logging configuration, two entrypoints** — `app/observability.py`

**Chosen.** A single `configure_logging(*, json_logs)` builds the structlog
processor chain, and both the API (at import, before it serves) and the worker
(first line of `main()`) call it. Production emits one JSON object per line
(`JSONRenderer`); flipping `AUTOPR_LOG_JSON=false` locally gives the colourised
`ConsoleRenderer`. `merge_contextvars` is first in the chain so context-bound
values (the correlation id) are merged into every event.

**Why.** Before this, the API configured logging inline and the worker did its
own thing — two drifting setups for one system, and only one of them structured.
Centralizing means the API and worker produce byte-compatible log lines, so a log
aggregator ingests both with one parser and a query spans both processes. JSON in
production is not aesthetic: unstructured logs are `grep`-and-hope, whereas
one-object-per-line is queryable (`jq 'select(.correlation_id=="…")'`) and
ingestable by any pipeline. The console renderer stays a dev convenience behind a
flag so local runs are readable without giving up the structured default.

**Rejected — configure per module / inline.** The drift this replaces; two
formats, no shared correlation semantics. **Rejected — stdlib `logging` +
JSON formatter.** structlog was already the codebase's logger and gives
contextvars-based binding (Decision 2) for free; swapping to stdlib would be a
lateral move that loses the context machinery this phase depends on.

---

## 2. A **correlation id** rides contextvars in-process and the job payload across the queue

**Chosen.** `app/observability.py` binds a short id into `structlog.contextvars`.
The API middleware mints one per request (or honours an inbound `X-Request-ID`)
and echoes it back in that header; `app.ingest` reads the bound id and stamps it
onto the Redis stream message as a `correlation_id` field; the worker's
`_handle_message` binds that id for the life of the job (minting a fresh one if a
message arrives without it) and clears it in a `finally`. So the webhook that
enqueued a job and the worker log lines that processed it share one id.

**Why.** The system is two processes joined by a queue; a failure is only
diagnosable if you can follow one PR from the webhook through ingestion, the
stream, the worker, the graph, and the routing decision. An id bound in
contextvars threads through every `log.*` call with zero plumbing at the call
sites, and carrying it as a stream field is the only way it survives the process
boundary — the worker is a different process with a fresh context, so the id has
to travel *with the message*. Honouring an inbound `X-Request-ID` means an
upstream proxy or the operator's `curl` can supply a trace id and have it flow
through our logs, which is what makes this compose with anything in front of it.
Clearing in `finally` is load-bearing: the worker reuses one context across jobs,
so a leaked id would silently mislabel the next job's logs.

**Rejected — full distributed tracing (OpenTelemetry spans).** The correct answer
at larger scale, but a tracing backend, exporter, and span plumbing is
disproportionate weight for two processes and 1–50 requests; a correlation id
delivers the "follow one request across the boundary" property for a fraction of
the cost. Noted as the scale-out path. **Rejected — thread the id through
function signatures.** Every function on the path grows a parameter it only
forwards; contextvars exist precisely to avoid that.

---

## 3. `/readyz` is **deep readiness** (DB + Redis), strictly separate from `/healthz` liveness

**Chosen.** `/healthz` stays a static `{"status":"ok"}` — the process answered, it
is alive. `/readyz` probes the database (`SELECT 1`) and Redis (`ping()`) and
returns **200** only if both answer, else **503** with a per-dependency
`checks` body naming what failed. Compose's `api` healthcheck now targets
`/readyz` (graduating from the `/healthz` placeholder flagged in phase-8 corner
#5), so the worker's `api: condition: service_healthy` gate means "truly ready".

**Why.** Liveness and readiness answer different operational questions and drive
different actions: an orchestrator *restarts* on failed liveness but only *stops
sending traffic* on failed readiness. Collapsing them is a classic outage
amplifier — a momentary Redis blip that fails a combined check gets the whole
process killed and restarted, turning a degraded state into downtime. Keeping
`/healthz` trivially true means "the process is wedged" is distinguishable from
"a dependency is down": a green `/healthz` with a red `/readyz` says *alive but
can't serve*, which is exactly the signal you want when Redis is the thing that's
down. The per-dependency `checks` body means the 503 tells you *which* dependency,
not just that something is wrong.

**Note on strictness.** `/readyz` reports Redis-down as *not ready* even though
the API deliberately degrades to a read-only dashboard when Redis is absent
(the webhook fails closed with 503; reads keep serving — see phase-8 Decision 6).
This is intentional: readiness reports the honest full-service state, and in this
single-instance showcase there is no load balancer that would pull the instance
and take the dashboard down with it. The degradation and the readiness signal are
separate concerns — the app stays up (liveness) and serves what it can, while
`/readyz` still tells the truth about the missing dependency.

**Rejected — one combined health endpoint.** The restart-amplification failure
above. **Rejected — `/readyz` ignores Redis (DB-only).** Then it would report
"ready" while ingestion is broken — a readiness check that lies about the
system's actual ability to accept its primary input.

---

## 4. `/metrics` gauges are **pulled from the database at scrape time**, not tracked incrementally

**Chosen.** HTTP request count and latency are updated by the middleware as
requests happen (they are inherently per-request events). The *pipeline* gauges —
jobs by status, review decisions by status, and Redis queue depth — are set in
`_refresh_domain_gauges` when `/metrics` is scraped, by querying the database
(reusing `_count_by_status`) and calling `XLEN`. HTTP metric labels use the
**route template** (`/reviews/{decision_id}/approve`), never the raw URL.

**Why.** Pipeline state already has a source of truth: the `pr_jobs` and
`review_decisions` tables. Hand-maintaining parallel counters (increment on every
state transition, everywhere a job changes status) would duplicate that truth and
*drift* from it — a missed increment on some error path, and the gauge silently
lies forever. Reading the tables at scrape time cannot drift: the gauge is a
projection of the real data, computed fresh. At 1–50 requests these `GROUP BY`
counts are trivial, so the cost that would justify incremental tracking isn't
there. Bounding the HTTP label to the route template is the one non-obvious
metrics rule that matters: labelling by raw path (with PR numbers, decision ids)
would explode Prometheus cardinality — a new time series per id — which is the
canonical way to melt a metrics backend. The template collapses all
`/reviews/{id}/approve` calls onto one series.

**Rejected — incrementally tracked domain counters.** The drift-from-truth
failure above, for no benefit at this scale. **Rejected — label HTTP metrics by
raw path.** Unbounded cardinality; a documented Prometheus anti-pattern.

---

## 5. Metrics are **defined once at import**, and `/metrics` honours an enable toggle

**Chosen.** The Prometheus collectors (`REQUEST_COUNT`, `REQUEST_LATENCY`,
`JOBS_GAUGE`, `REVIEWS_GAUGE`, `QUEUE_DEPTH`) are module-level in
`app/observability.py`, created exactly once when the module is first imported.
`/metrics` is gated on `settings.metrics_enabled` (default on) and 404s when off.

**Why.** prometheus-client registers each collector in a global default registry;
defining a metric twice with the same name raises `Duplicated timeseries`. The
test suite imports `app.main` repeatedly across modules, so anything that created
metrics at call time or per-app-instance would blow up on the second import.
Module scope makes registration happen once regardless of how many times the app
is imported — the same property that makes it safe under `--reload` and multiple
test modules. The enable toggle is there so a deployment that scrapes elsewhere,
or wants the endpoint closed on a public surface, can turn it off by config rather
than code.

**Rejected — create metrics inside the app factory / per request.** Double
registration on re-import; the exact crash above. **Rejected — a private
registry threaded through the app.** More plumbing than this scale needs; the
default registry is fine for a single process, and `generate_latest()` reads it
directly.

---

## 6. The metrics middleware records in a `finally`, with an explicit status default

**Chosen.** `observability_middleware` starts a timer, sets `status = 500` as the
default, and records `REQUEST_COUNT`/`REQUEST_LATENCY` in a `finally` block — so
the metric is emitted even when `call_next` raises before a response exists, and
an uncaught handler exception is counted as a 500 rather than vanishing from the
metrics.

**Why.** The whole value of request metrics is that they cover the *bad* paths.
If metric recording lived only on the success path, the one thing you most want to
alert on — a spike of 500s — would be the thing least likely to be recorded,
because the exception would skip past the recording line. Recording in `finally`
with a pessimistic default means every request, including one that raises,
produces exactly one counted sample with an honest status. The correlation-id
clear lives in the same `finally` for the same reason: it must run on the error
path too, or a failing request leaks its id into the next.

**Rejected — record after a successful `call_next`.** Silently drops the failure
cases from the metrics — the inverse of what request metrics are for.

---

## Corners cut (flagged, deferred)

1. **Correlation ids, not distributed tracing.** There are no spans, no parent/
   child causality, no exporter — just one flat id per request/job. That answers
   "show me every line for this webhook" but not "show me the latency breakdown
   across nodes." At two processes and this scale the id is the right weight;
   OpenTelemetry is the scale-out path (Decision 2), deferred deliberately.

2. **`/metrics` is unauthenticated.** It exposes operational counts (request
   rates, job/decision totals, queue depth) — not secrets or payload content — and
   in a real deployment it sits behind the network boundary where a scraper reaches
   it and the public does not. The `metrics_enabled` toggle (Decision 5) can close
   it entirely. Putting it behind the bearer token would complicate scraping for
   little gain at this scale; flagged rather than done.

3. **Gauges are point-in-time snapshots at scrape.** `JOBS_GAUGE` et al. reflect
   the database *at the moment `/metrics` is hit*, and `QUEUE_DEPTH` is a
   best-effort `XLEN` that is swallowed to `pass` if Redis is unreachable (a
   metrics scrape must never 500 on a degraded dependency — that's what `/readyz`
   is for). Between scrapes the values are whatever Prometheus last pulled; there
   is no push, no event stream. Correct and cheap for periodic scraping; noted so
   the numbers aren't mistaken for a live event feed.

4. **The default global registry, single process.** Metrics use prometheus-client's
   process-global registry. That is exactly right for one uvicorn process (the
   Phase 8 scale decision), but a multi-process server (gunicorn with several
   workers) would need `prometheus_client`'s multiprocess mode to aggregate across
   workers. Tied to the same scale-out boundary as the single-process decision;
   not needed until then.

5. **Graceful shutdown is bounded but minimal.** The Compose `api` command sets
   `--timeout-graceful-shutdown 30` so a SIGTERM drains in-flight requests within a
   bound instead of hanging or cutting them instantly. There is no connection-drain
   coordination with an upstream load balancer (there isn't one) and no worker-side
   in-flight-job draining beyond the existing signal handler that stops the poll
   loop between jobs. Proportionate for a single instance; a rolling-deploy story
   belongs to the cloud phase.
