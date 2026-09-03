"""Observability: structured logging, request correlation, and Prometheus metrics.

One module for the three cross-cutting concerns that make the system operable in
production, so the API and the worker share exactly one setup:

* ``configure_logging()`` — structlog configured identically for both processes:
  single-line JSON in production (one event per line, greppable and ingestable),
  or a pretty console renderer locally. Called once at process start.

* **Correlation ids** — a short id bound into structlog's contextvars so every
  log line emitted while handling one webhook→worker flow carries the same
  ``correlation_id``. The API mints it (or honours an inbound ``X-Request-ID``);
  ``app.ingest`` propagates it to the worker in the job payload; the worker binds
  it while processing. This is what turns scattered log lines into a trace.

* **Prometheus metrics** — HTTP request counters/latency updated by middleware,
  plus gauges the ``/metrics`` endpoint fills from the database at scrape time.
  Defined at import (once) so repeated app imports in tests don't double-register.
"""

from __future__ import annotations

import uuid

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# --- Correlation id ----------------------------------------------------------
# The header the API reads an inbound id from and echoes back, so a caller (or a
# load balancer) can supply its own trace id and have it flow through our logs.
CORRELATION_ID_HEADER = "X-Request-ID"
_CORRELATION_KEY = "correlation_id"


def new_correlation_id() -> str:
    """A short, log-friendly id. 12 hex chars is plenty to disambiguate at this scale."""
    return uuid.uuid4().hex[:12]


def bind_correlation_id(correlation_id: str) -> None:
    """Bind the id into structlog's contextvars for the current context/task.

    Every subsequent ``log.<level>(...)`` in this request (API) or job (worker)
    then includes ``correlation_id`` without threading it through call sites.
    """
    structlog.contextvars.bind_contextvars(**{_CORRELATION_KEY: correlation_id})


def clear_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars(_CORRELATION_KEY)


def current_correlation_id() -> str | None:
    """The id bound in this context, if any — so producers can propagate it.

    Used by ``app.ingest`` to stamp the id onto the job payload, carrying the
    API request's trace across the queue into the worker.
    """
    return structlog.contextvars.get_contextvars().get(_CORRELATION_KEY)


# --- Logging -----------------------------------------------------------------
def configure_logging(*, json_logs: bool) -> None:
    """Configure structlog for this process. Idempotent.

    ``merge_contextvars`` is first so context-bound values (the correlation id)
    are merged into every event. Production uses ``JSONRenderer`` (one line per
    event); local dev can flip to the colourised ``ConsoleRenderer``.
    """
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# --- Prometheus metrics ------------------------------------------------------
# HTTP-level, updated by the API middleware. `path` is the *route template*
# (e.g. /reviews/{decision_id}/approve), never the raw URL, so ids don't explode
# label cardinality.
REQUEST_COUNT = Counter(
    "autopr_http_requests_total",
    "HTTP requests by method, route template, and status code.",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "autopr_http_request_duration_seconds",
    "HTTP request latency by method and route template.",
    ["method", "path"],
)

# Domain gauges, filled from the DB at scrape time (see app.main /metrics). These
# describe pipeline state, which is what an operator actually watches.
JOBS_GAUGE = Gauge("autopr_jobs", "PR jobs by status.", ["status"])
REVIEWS_GAUGE = Gauge("autopr_reviews", "Review decisions by status.", ["status"])
QUEUE_DEPTH = Gauge("autopr_queue_depth", "Length of the Redis jobs stream (XLEN).")


def render_metrics() -> tuple[bytes, str]:
    """Serialize the registry for the /metrics response."""
    return generate_latest(), CONTENT_TYPE_LATEST
