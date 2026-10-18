"""Ingestion service: the idempotent path from a verified webhook to a queued job.

This is deliberately separate from the FastAPI route so it can be unit-tested
directly and reused by the crash-reconcile logic.

The two-step "insert row, then enqueue" has a crash window: what if the process
dies after the INSERT commits but before the XADD? The row would sit forever in
PENDING and never be worked. We close that window from three sides:
  * a failed publish is recorded on the row (`last_error`) and repaired when
    GitHub retries the delivery (see the duplicate branch of `ingest`);
  * the worker runs `reconcile_pending` at startup AND on an interval, which
    re-publishes any PENDING row;
  * re-publishing is harmless, because the worker is idempotent.
This is a lightweight transactional-outbox pattern: Postgres is the source of
truth for "this job exists", Redis is just the delivery mechanism.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import CursorResult

import structlog
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import execute_for_rowcount, insert_ignore_duplicates, supports_insert_returning
from app.models import JobStatus, PRJob
from app.observability import current_correlation_id
from app.queue import JobQueue

log = structlog.get_logger()
# Stamped on a job row when the XADD raised. Used to tell "this PENDING row was
# never enqueued" apart from "another request is enqueueing it right now".
_PUBLISH_FAILED_PREFIX = "publish failed"


@dataclass(frozen=True)
class PRPayload:
    repo: str
    pr_number: int
    commit_sha: str
    author: str
    event: str = "pull_request"
    # CI-fix track only: inline failure evidence pulled from the webhook payload
    # (check_run.output / workflow_run fields). Deliberately excluded from
    # dedup_key — logs are context, not identity; two deliveries of the same
    # failing check are the same job regardless of log text.
    ci_logs: str = ""

    @property
    def dedup_key(self) -> str:
        """Deterministic idempotency key.

        Same repo + PR + head SHA + event => same key => unique-constraint
        collision on redelivery. We hash so the key is a bounded fixed length
        regardless of repo/author length.
        """
        raw = f"{self.event}|{self.repo}|{self.pr_number}|{self.commit_sha}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_pull_request_event(event: str, payload: dict) -> PRPayload | None:
    """Extract the fields we care about from a GitHub PR webhook payload.

    Returns None for events we don't act on (e.g. ping) so the caller can
    200-ack them without creating a job.
    """
    if event == "ping":
        return None
    pr = payload.get("pull_request")
    if not pr:
        return None
    repo = (payload.get("repository") or {}).get("full_name")
    head = pr.get("head") or {}
    author = (pr.get("user") or {}).get("login", "unknown")
    if not repo or pr.get("number") is None or not head.get("sha"):
        return None
    return PRPayload(
        repo=repo,
        pr_number=int(pr["number"]),
        commit_sha=str(head["sha"]),
        author=str(author),
        event=event,
    )


# --- CI-fix track (Phase 5) --------------------------------------------------
# A check_run / workflow_run webhook is the trigger for the CI-fix track. We act
# only on a *completed* run that *failed*, and only when it is associated with a
# PR — the outward action is a PR comment, so a failure with no PR has nowhere to
# land. The inline `output`/summary text GitHub already includes in the payload
# becomes the initial ci_logs the CI Monitor diagnoses, so the common case needs
# no second API round-trip. (Downloading the full zipped Actions log archive is a
# deliberate non-goal here — see docs/decisions/phase-5.md.)

# Check conclusions we treat as "there is a failure worth trying to fix".
# Explicitly NOT: success, neutral, cancelled, skipped, stale.
_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})


def _first_pr_number(objs: list | None) -> int | None:
    """First associated PR number from a check_run/workflow_run `pull_requests`."""
    for item in objs or []:
        n = (item or {}).get("number")
        if n is not None:
            return int(n)
    return None


def _clip(text: str, limit: int = 12_000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n... (truncated) ..."


def _check_run_logs(cr: dict) -> str:
    """Assemble diagnosable log text from a check_run's inline output block."""
    output = cr.get("output") or {}
    parts = [
        f"check: {cr.get('name', '?')}",
        f"conclusion: {cr.get('conclusion', '?')}",
        (output.get("title") or "").strip(),
        (output.get("summary") or "").strip(),
        (output.get("text") or "").strip(),
    ]
    return _clip("\n".join(p for p in parts if p))


def _workflow_run_logs(wr: dict) -> str:
    """Assemble the evidence a workflow_run payload carries inline.

    Thinner than check_run on purpose: a workflow_run payload does NOT include
    step logs, so the CI Monitor may return `unknown` and the run escalates to a
    human. That is the honest outcome, not a bug — see the phase-5 corners.
    """
    head_commit = wr.get("head_commit") or {}
    parts = [
        f"workflow: {wr.get('name', '?')}",
        f"conclusion: {wr.get('conclusion', '?')}",
        f"event: {wr.get('event', '?')}",
        (wr.get("display_title") or "").strip(),
        (head_commit.get("message") or "").strip(),
    ]
    return _clip("\n".join(p for p in parts if p))


def parse_ci_event(event: str, payload: dict) -> PRPayload | None:
    """Extract a CI-fix job from a check_run/workflow_run webhook, or None.

    Returns None (a 200-ack no-op) unless ALL of these hold:
      * action == "completed" (only a finished run carries a conclusion),
      * conclusion is a failure we act on (_FAILURE_CONCLUSIONS),
      * there is a head SHA (something to snapshot) AND an associated PR number
        (somewhere to post the proposed fix).
    """
    if payload.get("action") != "completed":
        return None
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None

    if event == "check_run":
        cr = payload.get("check_run") or {}
        if cr.get("conclusion") not in _FAILURE_CONCLUSIONS:
            return None
        sha = cr.get("head_sha")
        pr_number = _first_pr_number(cr.get("pull_requests"))
        author = (cr.get("app") or {}).get("slug") or "ci"
        logs = _check_run_logs(cr)
    elif event == "workflow_run":
        wr = payload.get("workflow_run") or {}
        if wr.get("conclusion") not in _FAILURE_CONCLUSIONS:
            return None
        sha = wr.get("head_sha")
        pr_number = _first_pr_number(wr.get("pull_requests"))
        author = (wr.get("actor") or {}).get("login") or "ci"
        logs = _workflow_run_logs(wr)
    else:
        return None

    if not sha or pr_number is None:
        return None

    return PRPayload(
        repo=repo,
        pr_number=pr_number,
        commit_sha=str(sha),
        author=str(author),
        event=event,
        ci_logs=logs,
    )


def parse_event(event: str, payload: dict) -> PRPayload | None:
    """Dispatch a webhook to the right parser by event type.

    The single entry point the webhook route calls. `check_run`/`workflow_run`
    flow to the CI-fix track; `pull_request` (and anything else) to the PR-review
    parser, which returns None for the non-actionable events (ping, non-PR).
    """
    if event in ("check_run", "workflow_run"):
        return parse_ci_event(event, payload)
    return parse_pull_request_event(event, payload)


@dataclass(frozen=True)
class IngestResult:
    job_id: int
    created: bool  # False => this was a duplicate delivery (no new job, no enqueue)


def ingest(session: Session, queue: JobQueue, payload: PRPayload) -> IngestResult:
    """Idempotently create a job and enqueue it exactly once.

    Steps:
      1. INSERT ... ON CONFLICT DO NOTHING on dedup_key.
      2. Look up the row. If *we* created it, claim + XADD it
         (`claim_and_publish`). If it already existed, that is normally the
         duplicate-delivery no-op — UNLESS the row is still PENDING, which means
         it was committed but never confirmed on the stream (a failed publish, or
         a crash in that window). That is the exact state a GitHub retry lands in
         after a Redis blip, so the retry repairs it rather than leaving it for
         the worker's next reconcile pass.

    Concurrency: if two requests race for the same head SHA, the unique
    constraint on dedup_key guarantees exactly one INSERT commits; the loser's
    statement is suppressed and its `created` is False. Only the winner
    enqueues, so there is exactly one stream message per PR head SHA — and
    publishing is itself a compare-and-set (`claim_and_publish`), so even a
    repair path cannot double-publish.

    This assumes each caller has its own connection (one Session per request,
    as in the API and the worker). Two sessions sharing a single connection
    share a transaction, and then nothing — including `ON CONFLICT` — can
    arbitrate between them; the test harness gives each thread its own
    connection for exactly that reason.
    """
    # Ask the INSERT itself who won. With RETURNING, a suppressed insert yields
    # zero rows, which is the arbitration decision rather than a guess derived
    # from cursor state; without it (SQLite < 3.35) we fall back to rowcount.
    winning_id = supports_insert_returning()
    stmt = insert_ignore_duplicates(
        PRJob.__table__,
        returning=PRJob.__table__.c.id if winning_id else None,
        values={
            "dedup_key": payload.dedup_key,
            "repo": payload.repo,
            "pr_number": payload.pr_number,
            "commit_sha": payload.commit_sha,
            "author": payload.author,
            "event": payload.event,
            "event_context": payload.ci_logs or None,
            "status": JobStatus.PENDING.value,
            "attempts": 0,
        },
        index_elements=["dedup_key"],
    )
    result = session.execute(stmt)
    if winning_id:
        # Must be read before commit: committing closes the cursor.
        created = result.first() is not None
    else:
        # Session.execute() is typed to return Result, but a Core INSERT yields a
        # CursorResult, which is what carries rowcount. Cast so mypy sees it.
        rowcount = cast("CursorResult[Any]", result).rowcount
        created = bool(rowcount and rowcount > 0)
    session.commit()

    job = session.execute(select(PRJob).where(PRJob.dedup_key == payload.dedup_key)).scalar_one()

    if not created:
        # Duplicate or race-loser. Normally a pure no-op — but a row still in
        # PENDING means it was never confirmed on the stream (a previous publish
        # failed, or the process died between the INSERT commit and the XADD), so
        # this delivery repairs it. Publishing is gated by `claim_and_publish`'s
        # compare-and-set, so the repair cannot double-publish a job that another
        # in-flight delivery is enqueueing at the same moment.
        if job.status == JobStatus.PENDING and claim_and_publish(session, queue, job):
            log.info("ingest.republished_stranded", job_id=job.id)
        return IngestResult(job_id=job.id, created=False)

    claim_and_publish(session, queue, job)
    return IngestResult(job_id=job.id, created=True)


def claim_and_publish(
    session: Session, queue: JobQueue, job: PRJob, *, correlation_id: str | None = None
) -> bool:
    """Atomically claim the right to publish this job, then XADD it.

    The claim is a conditional UPDATE (PENDING -> QUEUED). Exactly one caller can
    win it, which is what makes publishing exactly-once *regardless of who won
    the INSERT*: a race-loser that sees the row PENDING and a creator that has
    not published yet cannot both reach the stream.

    On a Redis failure the claim is released (back to PENDING) and the error is
    recorded on the row, so a GitHub retry — or the worker's periodic reconcile —
    can pick it up. The exception is re-raised so the webhook can answer 503 and
    GitHub retries instead of dropping the event.
    """
    claimed = (
        execute_for_rowcount(
            session,
            update(PRJob)
            .where(PRJob.id == job.id, PRJob.status == JobStatus.PENDING)
            .values(status=JobStatus.QUEUED),
        )
        > 0
    )
    session.commit()
    if not claimed:
        return False
    # The conditional UPDATE bypassed the ORM, so resync the instance: without
    # this the unit of work still believes status is PENDING and would skip the
    # release-to-PENDING UPDATE in the failure branch below.
    session.refresh(job)

    try:
        msg_id = queue.publish(
            {
                "job_id": job.id,
                "repo": job.repo,
                "pr_number": job.pr_number,
                "commit_sha": job.commit_sha,
                # Carry the API request's trace id into the worker. Empty string
                # (not omitted) so the Redis stream field set is stable; the
                # worker treats "" as "mint a fresh one".
                "correlation_id": correlation_id or current_correlation_id() or "",
            }
        )
    except RedisError as exc:
        job.status = JobStatus.PENDING
        job.last_error = f"{_PUBLISH_FAILED_PREFIX}: {exc!r}"
        session.commit()
        raise

    job.stream_msg_id = msg_id
    job.last_error = None
    session.commit()
    return True
