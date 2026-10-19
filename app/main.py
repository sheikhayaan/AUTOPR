"""FastAPI application: the /webhook ingress + the human-in-the-loop ops API.

Responsibilities:
  * Verify the GitHub HMAC signature; 401 on failure.
  * Idempotently persist PR metadata + enqueue a job.
  * Return 200 immediately — no synchronous processing.
  * (Phase 4) Expose the human-in-the-loop queue: list pending routing
    decisions and approve/reject them. Approval is the only path by which a
    code-changing or elevated-risk action reaches GitHub.

The Redis client, queue, GitHub client, and DB session factory live on
``app.state`` so tests can inject fakes via dependency override.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, assert_schema_ready, engine
from app.ingest import ingest, parse_event, repo_is_allowed
from app.models import DecisionStatus, JobStatus, PRJob, ReviewDecision
from app.observability import (
    CORRELATION_ID_HEADER,
    JOBS_GAUGE,
    QUEUE_DEPTH,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    REVIEWS_GAUGE,
    bind_correlation_id,
    clear_correlation_id,
    configure_logging,
    current_correlation_id,
    new_correlation_id,
    render_metrics,
)
from app.queue import JobQueue, make_client
from app.ratelimit import FixedWindowRateLimiter
from app.routing import store
from app.routing.github import get_github_client
from app.routing.router import execute_decision
from app.security import verify_bearer_token, verify_signature

# Configure structured logging once, at import, before the app serves anything —
# so even lifespan/startup events are emitted in the configured format (JSON in
# production). The worker calls the same function from its entrypoint.
configure_logging(json_logs=settings.log_json)
log = structlog.get_logger()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_queue(request: Request) -> JobQueue:
    queue = request.app.state.queue
    if queue is None:
        # Redis was unreachable at startup; ingestion is unavailable but the
        # read-only dashboard stays up. Fail closed with 503 rather than 500.
        raise HTTPException(status_code=503, detail="job queue unavailable")
    return queue


def get_github(request: Request):
    return request.app.state.github


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The schema belongs to Alembic (`alembic upgrade head`, run as a release step
    # in docker compose / CI); the app never creates tables. Refuse to serve an
    # un-migrated database instead of failing obscurely on the first webhook.
    assert_schema_ready(engine)
    # Clear claims abandoned by a crashed approver: a row sits APPROVED while the
    # process that claimed it is gone, so it would otherwise be stuck "in
    # progress" forever. At startup nothing is in flight, so any APPROVED row is
    # provably stranded; FAILED lets an operator re-approve (retry) or reject it.
    with SessionLocal() as session:
        store.sweep_interrupted(session)
    # Loud warning if the mutating API is left unauthenticated (empty token).
    # Allowed for local dev; must never be the case in a real deployment.
    if not settings.api_token:
        log.warning(
            "startup.no_api_token",
            detail="AUTOPR_API_TOKEN is empty; approve/reject are UNAUTHENTICATED. "
            "Set a token in any real deployment.",
        )
    app.state.redis = None
    app.state.queue = None
    app.state.github = get_github_client()
    # The queue backend (Redis) powers webhook ingestion. The read-only ops
    # dashboard reads only from the database, so a down Redis must not take the
    # whole API offline — we degrade: log it, leave the queue unset, and let
    # /webhook fail closed with 503 (see get_queue) while /stats, /jobs,
    # /reviews*, and /healthz keep serving.
    try:
        client = make_client()
        queue = JobQueue(client)
        queue.ensure_group()
        app.state.redis = client
        app.state.queue = queue
        log.info("startup", stream=queue.stream, group=queue.group)
    except Exception as exc:  # noqa: BLE001 - degrade on any queue-backend failure
        log.warning("startup.queue_unavailable", error=str(exc))
    yield
    if app.state.redis is not None:
        app.state.redis.close()


app = FastAPI(title="AutoPR", version="0.1.0", lifespan=lifespan)

# The dashboard (Vite dev server / static build) is a separate origin. CORS is an
# allowlist (AUTOPR_CORS_ORIGINS) rather than "*" — a literal "*" is still honored
# for a wide-open demo, but the default is the local dev origins. The API sends no
# cookies (allow_credentials=False), so this bounds which sites' JS may call it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Per-request correlation id + Prometheus request metrics.

    * Correlation id: honour an inbound ``X-Request-ID`` (so a proxy/caller can
      supply its own trace id), else mint one. Bind it into structlog contextvars
      for the request's lifetime, echo it back in the response header, and clear
      it after so it never leaks into another request sharing the thread.
    * Metrics: count and time every request, labelled by the *route template*
      (never the raw path) to keep label cardinality bounded.
    """
    inbound = request.headers.get(CORRELATION_ID_HEADER)
    correlation_id = inbound or new_correlation_id()
    bind_correlation_id(correlation_id)
    start = time.perf_counter()
    status = 500  # default if call_next raises before assigning a response
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
    finally:
        # Runs before the return completes (and on exception), so metrics always
        # record with the right status, and the id never leaks to another request.
        elapsed = time.perf_counter() - start
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "<unmatched>"
        REQUEST_COUNT.labels(request.method, path, str(status)).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(elapsed)
        clear_correlation_id()


# --- Auth & rate limiting -----------------------------------------------------
# Bearer-token auth on the mutating/ops API, plus a light per-IP rate limit on the
# webhook + mutating endpoints. Both reuse the constant-time compare in
# app.security and the in-process limiter in app.ratelimit.
_webhook_limiter = FixedWindowRateLimiter(settings.rate_limit_webhook_per_min, 60.0)
_mutation_limiter = FixedWindowRateLimiter(settings.rate_limit_mutations_per_min, 60.0)
app.state.webhook_limiter = _webhook_limiter
app.state.mutation_limiter = _mutation_limiter


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Enforce ``Authorization: Bearer <token>`` when a token is configured.

    Empty AUTOPR_API_TOKEN => no-op (dev convenience) with a loud startup warning;
    when set, a missing or wrong token is a 401.
    """
    token = settings.api_token
    if not token:
        return
    if not verify_bearer_token(token, authorization):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def require_api_token_for_reads(authorization: str | None = Header(default=None)) -> None:
    """Gate read endpoints behind the token only when require_auth_for_reads is set.

    Default off: the read-only dashboard demos without a token, while the write
    path stays protected whenever a token exists.
    """
    if not settings.require_auth_for_reads:
        return
    require_api_token(authorization)


def _rate_limit(limiter: FixedWindowRateLimiter):
    """Build a dependency enforcing ``limiter`` per client IP (429 on trip)."""

    def dep(request: Request) -> None:
        if not settings.rate_limit_enabled:
            return
        allowed, retry_after = limiter.allow(_client_ip(request) or "unknown")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    return dep


_webhook_rl = _rate_limit(_webhook_limiter)
_mutation_rl = _rate_limit(_mutation_limiter)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """Deep readiness: are our backing services actually reachable *right now*?

    Distinct from ``/healthz`` (liveness — "the process is up"). ``/readyz`` probes
    the database and Redis and returns 503 unless both answer. An orchestrator
    uses liveness to decide *restart* and readiness to decide *send traffic*; a
    503 here with a green ``/healthz`` says "alive, but a dependency is down"
    (e.g. Redis unreachable — ingestion can't enqueue) rather than "crashed".
    """
    checks: dict[str, str] = {}
    ok = True

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must not itself raise
        checks["database"] = f"error: {exc}"
        ok = False

    client = request.app.state.redis
    if client is None:
        checks["redis"] = "unavailable"  # never connected at startup
        ok = False
    else:
        try:
            client.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc}"
            ok = False

    response.status_code = 200 if ok else 503
    return {"status": "ready" if ok else "not_ready", "checks": checks}


def _refresh_domain_gauges(db: Session, queue: JobQueue | None) -> None:
    """Set the pipeline-state gauges from the DB (and Redis) at scrape time.

    Pull-at-scrape rather than tracked incrementally: at 1–50 req these queries
    are trivial, and reading the source of truth on demand can't drift out of
    sync with the tables the way hand-maintained counters would.
    """
    job_counts = _count_by_status(db, PRJob.status)
    for js in JobStatus:
        JOBS_GAUGE.labels(js.value).set(job_counts.get(js.value, 0))
    decision_counts = _count_by_status(db, ReviewDecision.status)
    for ds in DecisionStatus:
        REVIEWS_GAUGE.labels(ds.value).set(decision_counts.get(ds.value, 0))
    if queue is not None:
        # A metrics scrape must never 500 on a degraded Redis; XLEN is best-effort.
        with suppress(Exception):
            QUEUE_DEPTH.set(queue.client.xlen(queue.stream))


@app.get("/metrics")
def metrics(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    """Prometheus exposition. HTTP counters/latency come from the middleware;
    pipeline gauges are refreshed from the database here, at scrape time."""
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics disabled")
    _refresh_domain_gauges(db, request.app.state.queue)
    payload, content_type = render_metrics()
    return PlainTextResponse(content=payload, media_type=content_type)


async def _read_body_with_limit(request: Request, limit: int) -> bytes:
    """Buffer the raw request body, refusing more than `limit` bytes.

    The HMAC is computed over the raw bytes, so we must read them — but we must
    not read an unbounded amount first. A caller without a valid signature can
    still send us a body (we only reject it *after* reading), so an unlimited
    `await request.body()` is pre-auth memory amplification. The rate limiter
    bounds request count, not size. We check the declared Content-Length first
    (cheap reject) and then enforce the same cap while streaming, which also
    covers chunked bodies that declare nothing.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="payload too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/webhook", dependencies=[Depends(_webhook_rl)])
async def webhook(
    request: Request,
    response: Response,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    db: Session = Depends(get_db),
    queue: JobQueue = Depends(get_queue),
) -> dict:
    # 1. Verify signature over the RAW body.
    raw = await _read_body_with_limit(request, settings.max_webhook_bytes)
    if not verify_signature(settings.webhook_secret, raw, x_hub_signature_256):
        log.warning("webhook.invalid_signature", gh_event=x_github_event)
        response.status_code = 401
        return {"detail": "invalid signature"}

    # 2. Parse. Non-actionable events (ping, non-PR) are acked without work.
    import json

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        response.status_code = 400
        return {"detail": "invalid json"}

    pr = parse_event(x_github_event or "pull_request", payload)
    if pr is None:
        return {"status": "ignored", "event": x_github_event}

    # A signed delivery from a repo we were not asked to serve still costs four
    # LLM calls. Refuse it before the INSERT so no row, no stream message and no
    # spend happen — and log the repo, so an operator can tell "someone is
    # spamming us" apart from "my allowlist is wrong".
    if not repo_is_allowed(pr.repo):
        log.warning(
            "webhook.repo_not_allowed",
            repo=pr.repo,
            pr=pr.pr_number,
            # `event` would collide with structlog's own positional event kwarg.
            gh_event=pr.event,
            correlation_id=current_correlation_id(),
        )
        return {"status": "ignored", "reason": "repo_not_allowed"}

    # 3. Idempotent persist + enqueue. Duplicates are a no-op.
    from redis.exceptions import RedisError

    try:
        result = ingest(db, queue, pr)
    except RedisError as exc:
        # The job row is committed PENDING before the publish (see app.ingest),
        # so a publish failure is recoverable: the worker's startup reconcile
        # re-enqueues PENDING jobs. Surface 503 (not 500) so GitHub retries the
        # delivery rather than treating it as a permanent bug.
        log.error("webhook.enqueue_failed", error=str(exc), repo=pr.repo, pr=pr.pr_number)
        response.status_code = 503
        return {"detail": "queue temporarily unavailable"}
    log.info(
        "webhook.ingested",
        job_id=result.job_id,
        created=result.created,
        repo=pr.repo,
        pr=pr.pr_number,
        sha=pr.commit_sha[:8],
    )
    # 200 either way — a duplicate is a successful no-op, not an error.
    return {
        "status": "queued" if result.created else "duplicate",
        "job_id": result.job_id,
    }


# --- Phase 4: human-in-the-loop ops API --------------------------------------
# The surface a maintainer uses to review what the pipeline wants to do and
# approve or reject it. Approval is the ONLY path by which a code-changing or
# elevated-risk action reaches GitHub — the pipeline itself never does.


def _pr_review_url(repo: str, pr_number: int) -> str:
    """Deep link to a PR's review screen on the GitHub web UI.

    In hand-off mode this is the primary call to action: the maintainer opens it
    and approves / requests changes / edits the PR under their OWN account —
    AutoPR performs no write. Derived from settings.github_web_url so it is
    correct for github.com and GitHub Enterprise alike.
    """
    return f"{settings.github_web_url}/{repo}/pull/{pr_number}/files"


def _decision_view(row) -> dict:
    """Serialize a ReviewDecision for the API (body included; it's the point)."""
    return {
        "id": row.id,
        "repo": row.repo,
        "pr_number": row.pr_number,
        "commit_sha": row.commit_sha,
        "action": row.action,
        "risk": row.risk,
        "reason": row.reason,
        "title": row.title,
        "body": row.body,
        "status": row.status.value if hasattr(row.status, "value") else row.status,
        "result_url": row.result_url,
        "review_url": _pr_review_url(row.repo, row.pr_number),
        "last_error": getattr(row, "last_error", None),
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
    }


@app.get("/reviews/pending", dependencies=[Depends(require_api_token_for_reads)])
def list_pending_reviews(db: Session = Depends(get_db)) -> dict:
    """List routing decisions awaiting human approval, oldest first."""
    rows = store.list_pending(db)
    return {"count": len(rows), "pending": [_decision_view(r) for r in rows]}


@app.post(
    "/reviews/{decision_id}/approve",
    dependencies=[Depends(require_api_token), Depends(_mutation_rl)],
)
def approve_review(
    decision_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    github=Depends(get_github),
) -> dict:
    """Approve a queued decision and carry out its GitHub action.

    Exactly-once on the happy path: the PENDING -> APPROVED transition is a
    conditional UPDATE (a compare-and-set), so concurrent approvals — a
    double-click, a retried request, two operators — cannot both post. Only the
    caller that changed the row proceeds; everyone else gets 409 (or
    `already_executed` once the winner is done).

    A failed GitHub call marks the decision FAILED (not executed) so it can be
    retried by approving again.
    """
    row, outcome = store.claim_approval(db, decision_id)
    if row is None:
        response.status_code = 404
        return {"detail": "not found"}
    if outcome == "already_executed":
        return {"status": "already_executed", "decision": _decision_view(row)}
    if outcome == "rejected":
        response.status_code = 409
        return {"detail": "decision was rejected"}
    if outcome == "in_progress":
        # Another approver holds the claim (or a crashed one does, until the
        # startup sweep clears it to FAILED). Refusing is what makes a
        # double-submit unable to post twice.
        response.status_code = 409
        return {"detail": "approval already in progress", "decision": _decision_view(row)}

    if settings.handoff_mode:
        # Hand-off mode performs no GitHub write. "Approve" records that the
        # maintainer has taken it to GitHub to act under their own account; we
        # mark it handled and point at the review screen.
        url = _pr_review_url(row.repo, row.pr_number)
        store.mark_executed(db, row, url)
        log.info(
            "audit.review_handed_off",
            decision_id=row.id,
            action=row.action,
            risk=row.risk,
            repo=row.repo,
            pr_number=row.pr_number,
            actor_ip=_client_ip(request),
            outcome="handed_off",
            review_url=url,
        )
        return {"status": "handed_off", "url": url, "decision": _decision_view(row)}

    # Reconstruct the minimal state the executor needs from the stored row.
    state = {"repo": row.repo, "pr_number": row.pr_number, "commit_sha": row.commit_sha}
    decision = {"action": row.action, "body": row.body, "title": row.title}
    result = execute_decision(github, decision, state)
    if result.ok:
        store.mark_executed(db, row, result.url)
        # Audit: an action reached GitHub. Who (IP), what (decision/action/risk),
        # and the outcome — the trail a reviewer needs for a code-changing event.
        log.info(
            "audit.review_approved",
            decision_id=row.id,
            action=row.action,
            risk=row.risk,
            repo=row.repo,
            pr_number=row.pr_number,
            actor_ip=_client_ip(request),
            outcome="executed",
            result_url=result.url,
        )
        return {"status": "executed", "url": result.url, "decision": _decision_view(row)}
    store.mark_failed(db, row, result.detail)
    log.warning(
        "audit.review_approve_failed",
        decision_id=row.id,
        action=row.action,
        repo=row.repo,
        pr_number=row.pr_number,
        actor_ip=_client_ip(request),
        outcome="action_failed",
        detail=result.detail,
    )
    response.status_code = 502
    return {"status": "action_failed", "detail": result.detail, "decision": _decision_view(row)}


@app.post(
    "/reviews/{decision_id}/reject",
    dependencies=[Depends(require_api_token), Depends(_mutation_rl)],
)
def reject_review(
    decision_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    """Reject a queued decision. No GitHub action is taken, ever.

    The transition is a compare-and-set from PENDING, so rejecting cannot
    overwrite an approval that is already in flight (approve claimed it) or one
    that has already fired.
    """
    row, outcome = store.reject_pending(db, decision_id)
    if row is None:
        response.status_code = 404
        return {"detail": "not found"}
    if outcome == "already_executed":
        response.status_code = 409
        return {"detail": "already executed; cannot reject"}
    if outcome == "in_progress":
        response.status_code = 409
        return {"detail": "approval already in progress; cannot reject"}
    log.info(
        "audit.review_rejected",
        decision_id=row.id,
        action=row.action,
        repo=row.repo,
        pr_number=row.pr_number,
        actor_ip=_client_ip(request),
        outcome="rejected",
    )
    return {"status": "rejected", "decision": _decision_view(row)}


# --- Dashboard read API ------------------------------------------------------
# Read-only projections for the operator dashboard. These never mutate state;
# they exist so the UI can render the pipeline's health and history. The write
# path (approve/reject) stays confined to the endpoints above.


def _job_view(job: PRJob) -> dict:
    return {
        "id": job.id,
        "repo": job.repo,
        "pr_number": job.pr_number,
        "commit_sha": job.commit_sha,
        "author": job.author,
        "event": job.event,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "summary": job.result.summary if job.result is not None else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _count_by_status(db: Session, column) -> dict[str, int]:
    """`{status_value: count}` for an enum-typed status column."""
    out: dict[str, int] = {}
    for status, n in db.execute(select(column, func.count()).group_by(column)).all():
        key = status.value if hasattr(status, "value") else str(status)
        out[key] = int(n)
    return out


@app.get("/stats", dependencies=[Depends(require_api_token_for_reads)])
def stats(db: Session = Depends(get_db)) -> dict:
    """Aggregate KPIs for the dashboard header — counts by status + config."""
    jobs = _count_by_status(db, PRJob.status)
    decisions = _count_by_status(db, ReviewDecision.status)
    return {
        "jobs": {
            "total": sum(jobs.values()),
            "pending": jobs.get(JobStatus.PENDING.value, 0),
            "queued": jobs.get(JobStatus.QUEUED.value, 0),
            "processing": jobs.get(JobStatus.PROCESSING.value, 0),
            "done": jobs.get(JobStatus.DONE.value, 0),
            "dead": jobs.get(JobStatus.DEAD.value, 0),
        },
        "reviews": {
            "total": sum(decisions.values()),
            "pending": decisions.get(DecisionStatus.PENDING.value, 0),
            "approved": decisions.get(DecisionStatus.APPROVED.value, 0),
            "executed": decisions.get(DecisionStatus.EXECUTED.value, 0),
            "rejected": decisions.get(DecisionStatus.REJECTED.value, 0),
            "failed": decisions.get(DecisionStatus.FAILED.value, 0),
        },
        "config": {
            "github_dry_run": settings.github_dry_run,
            "live_github": bool(settings.github_token),
            "auto_comment_max_risk": settings.auto_comment_max_risk,
            "github_web_url": settings.github_web_url,
            "handoff_mode": settings.handoff_mode,
        },
    }


def _paged(stmt, offset: int, limit: int):
    """Apply ORDER BY-independent paging (callers order first)."""
    return stmt.offset(max(0, offset)).limit(max(1, min(limit, 200)))


def _like(term: str) -> str:
    """Escape-free LIKE pattern for a user-supplied search term.

    ``%`` and ``_`` in user input are literal characters, not wildcards, so they
    are escaped; the escape character is declared in the WHERE clause.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@app.get("/jobs", dependencies=[Depends(require_api_token_for_reads)])
def list_jobs(
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    status: str | None = None,
) -> dict:
    """PR job history (newest first), paged and searchable.

    ``q`` matches repo / commit sha / event; ``status`` filters the lifecycle
    state. ``total`` is the count *before* paging so the UI can render page
    links without a second round trip.
    """
    stmt = select(PRJob)
    if q:
        pattern = _like(q.strip())
        stmt = stmt.where(
            or_(
                PRJob.repo.ilike(pattern, escape="\\"),
                PRJob.commit_sha.ilike(pattern, escape="\\"),
                PRJob.event.ilike(pattern, escape="\\"),
            )
        )
    if status:
        try:
            stmt = stmt.where(PRJob.status == JobStatus(status))
        except ValueError:
            return {"count": 0, "total": 0, "limit": limit, "offset": offset, "jobs": []}
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        db.execute(_paged(stmt.order_by(PRJob.created_at.desc(), PRJob.id.desc()), offset, limit))
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "total": int(total),
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
        "jobs": [_job_view(j) for j in rows],
    }


@app.get("/reviews", dependencies=[Depends(require_api_token_for_reads)])
def list_reviews(
    db: Session = Depends(get_db),
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
) -> dict:
    """Routing-decision history (newest first), filtered, paged and searchable.

    Complements ``/reviews/pending`` (oldest-first work queue) with the full
    ledger the dashboard's history view renders. An unknown ``status`` yields an
    empty list rather than a 4xx — the UI passes enum values it got from us.
    """
    stmt = select(ReviewDecision)
    if status:
        try:
            wanted = DecisionStatus(status)
        except ValueError:
            return {
                "count": 0,
                "total": 0,
                "limit": limit,
                "offset": offset,
                "reviews": [],
            }
        stmt = stmt.where(ReviewDecision.status == wanted)
    if q:
        pattern = _like(q.strip())
        stmt = stmt.where(
            or_(
                ReviewDecision.repo.ilike(pattern, escape="\\"),
                ReviewDecision.title.ilike(pattern, escape="\\"),
                ReviewDecision.commit_sha.ilike(pattern, escape="\\"),
                ReviewDecision.reason.ilike(pattern, escape="\\"),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        db.execute(
            _paged(
                stmt.order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc()),
                offset,
                limit,
            )
        )
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "total": int(total),
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
        "reviews": [_decision_view(r) for r in rows],
    }
