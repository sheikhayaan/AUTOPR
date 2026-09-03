"""Ingestion service: the idempotent path from a verified webhook to a queued job.

This is deliberately separate from the FastAPI route so it can be unit-tested
directly and reused by the crash-reconcile logic.

The two-step "insert row, then enqueue" has a crash window: what if the process
dies after the INSERT commits but before the XADD? The row would sit forever in
PENDING and never be worked. We close that window with a reconcile pass on
worker startup (see worker.reconcile_pending) that re-enqueues any PENDING/
QUEUED jobs. This is a lightweight transactional-outbox pattern: Postgres is the
source of truth for "this job exists", Redis is just the delivery mechanism.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.orm import Session

from app.db import insert_ignore_duplicates
from app.models import JobStatus, PRJob
from app.observability import current_correlation_id
from app.queue import JobQueue


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
      2. Look up the row. If *we* created it (rowcount==1), XADD and mark
         QUEUED. If it already existed, we do nothing further — this is the
         duplicate-delivery no-op.

    Concurrency: if two requests race, the unique constraint guarantees only
    one INSERT wins. The loser's `created` is False. Only the winner enqueues,
    so there is exactly one stream message per PR head SHA.
    """
    stmt = insert_ignore_duplicates(
        PRJob.__table__,
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
    session.commit()

    # Session.execute() is typed to return Result, but a Core INSERT yields a
    # CursorResult, which is what carries rowcount. Cast so mypy sees the attribute.
    rowcount = cast("CursorResult[Any]", result).rowcount
    created = bool(rowcount and rowcount > 0)

    job = session.execute(select(PRJob).where(PRJob.dedup_key == payload.dedup_key)).scalar_one()

    if not created:
        # Duplicate or race-loser: someone else owns enqueueing this job.
        return IngestResult(job_id=job.id, created=False)

    msg_id = queue.publish(
        {
            "job_id": job.id,
            "repo": payload.repo,
            "pr_number": payload.pr_number,
            "commit_sha": payload.commit_sha,
            # Carry the API request's trace id into the worker. Empty string (not
            # omitted) so the Redis stream field set is stable; the worker treats
            # "" as "mint a fresh one".
            "correlation_id": current_correlation_id() or "",
        }
    )
    job.stream_msg_id = msg_id
    job.status = JobStatus.QUEUED
    session.commit()
    return IngestResult(job_id=job.id, created=True)
