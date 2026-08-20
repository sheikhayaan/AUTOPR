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

from collections.abc import Iterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, engine
from app.ingest import ingest, parse_event
from app.models import Base, DecisionStatus, JobStatus, PRJob, ReviewDecision
from app.queue import JobQueue, make_client
from app.routing import store
from app.routing.github import get_github_client
from app.routing.router import execute_decision
from app.security import verify_signature

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
    # Create tables (Phase 1 convenience; real migrations = Alembic, noted in
    # the decisions log as deferred).
    Base.metadata.create_all(bind=engine)
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

# The dashboard (Vite dev server / static build) is a separate origin. In dev it
# also proxies /api -> here, but we keep permissive CORS so the built SPA can be
# served from anywhere in a demo. This API takes no cookies/credentials, so
# allow_origins="*" with credentials off is safe.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    response: Response,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    db: Session = Depends(get_db),
    queue: JobQueue = Depends(get_queue),
) -> dict:
    # 1. Verify signature over the RAW body.
    raw = await request.body()
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

    # 3. Idempotent persist + enqueue. Duplicates are a no-op.
    result = ingest(db, queue, pr)
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
        "last_error": getattr(row, "last_error", None),
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
    }


@app.get("/reviews/pending")
def list_pending_reviews(db: Session = Depends(get_db)) -> dict:
    """List routing decisions awaiting human approval, oldest first."""
    rows = store.list_pending(db)
    return {"count": len(rows), "pending": [_decision_view(r) for r in rows]}


@app.post("/reviews/{decision_id}/approve")
def approve_review(
    decision_id: int,
    response: Response,
    db: Session = Depends(get_db),
    github=Depends(get_github),
) -> dict:
    """Approve a queued decision and carry out its GitHub action.

    Idempotent-ish: approving an already-executed decision is a no-op that
    returns the existing result rather than posting twice. A failed GitHub call
    marks the decision FAILED (not executed) so it can be retried.
    """
    row = store.get(db, decision_id)
    if row is None:
        response.status_code = 404
        return {"detail": "not found"}
    if row.status == DecisionStatus.EXECUTED:
        return {"status": "already_executed", "decision": _decision_view(row)}
    if row.status == DecisionStatus.REJECTED:
        response.status_code = 409
        return {"detail": "decision was rejected"}

    store.mark_approved(db, row)
    # Reconstruct the minimal state the executor needs from the stored row.
    state = {"repo": row.repo, "pr_number": row.pr_number, "commit_sha": row.commit_sha}
    decision = {"action": row.action, "body": row.body, "title": row.title}
    result = execute_decision(github, decision, state)
    if result.ok:
        store.mark_executed(db, row, result.url)
        log.info("reviews.approved_executed", id=row.id, url=result.url)
        return {"status": "executed", "url": result.url, "decision": _decision_view(row)}
    store.mark_failed(db, row, result.detail)
    response.status_code = 502
    return {"status": "action_failed", "detail": result.detail, "decision": _decision_view(row)}


@app.post("/reviews/{decision_id}/reject")
def reject_review(
    decision_id: int,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    """Reject a queued decision. No GitHub action is taken, ever."""
    row = store.get(db, decision_id)
    if row is None:
        response.status_code = 404
        return {"detail": "not found"}
    if row.status == DecisionStatus.EXECUTED:
        response.status_code = 409
        return {"detail": "already executed; cannot reject"}
    store.mark_rejected(db, row)
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


@app.get("/stats")
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
        },
    }


@app.get("/jobs")
def list_jobs(db: Session = Depends(get_db), limit: int = 50) -> dict:
    """Recent PR jobs (newest first) with their exactly-once result summary."""
    limit = max(1, min(limit, 200))
    rows = (
        db.execute(select(PRJob).order_by(PRJob.created_at.desc(), PRJob.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return {"count": len(rows), "jobs": [_job_view(j) for j in rows]}


@app.get("/reviews")
def list_reviews(
    db: Session = Depends(get_db),
    status: str | None = None,
    limit: int = 50,
) -> dict:
    """Routing-decision history (newest first), optionally filtered by status.

    Complements ``/reviews/pending`` (oldest-first work queue) with the full
    ledger the dashboard's history view renders. An unknown ``status`` yields an
    empty list rather than a 4xx — the UI passes enum values it got from us.
    """
    limit = max(1, min(limit, 200))
    stmt = (
        select(ReviewDecision)
        .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
        .limit(limit)
    )
    if status:
        try:
            wanted = DecisionStatus(status)
        except ValueError:
            return {"count": 0, "reviews": []}
        stmt = (
            select(ReviewDecision)
            .where(ReviewDecision.status == wanted)
            .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
            .limit(limit)
        )
    rows = db.execute(stmt).scalars().all()
    return {"count": len(rows), "reviews": [_decision_view(r) for r in rows]}
