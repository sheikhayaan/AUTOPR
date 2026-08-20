"""Durable store for human-gated routing decisions — the HITL queue.

The router persists a `ReviewDecision` here whenever an action needs approval;
the ops API reads and transitions it. Kept as a small function module (not an
ORM-heavy repository) to match the project's style and stay trivially testable
against the same in-memory SQLite the rest of the suite uses.

Idempotency is the load-bearing property: `enqueue` is get-or-create keyed by
(repo|pr|commit|action). A redelivered webhook or a worker retry that re-runs
the graph must not queue the same comment twice. We rely on the unique
constraint rather than a read-then-write, so it's correct under concurrency:
two racers both INSERT, one wins, the loser falls back to SELECT.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DecisionStatus, ReviewDecision
from app.routing.policy import RoutingDecision

log = structlog.get_logger()


def enqueue(session: Session, decision: RoutingDecision, state: dict) -> ReviewDecision:
    """Persist a pending decision idempotently; return the stored row.

    If a row already exists for this (repo, pr, commit, action) it is returned
    unchanged — we never re-queue or overwrite a decision a human may already
    have acted on.
    """
    key = decision.dedup_key(state)
    existing = session.execute(
        select(ReviewDecision).where(ReviewDecision.dedup_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        log.info("hitl.enqueue_duplicate", dedup_key=key, id=existing.id)
        return existing

    row = ReviewDecision(
        dedup_key=key,
        repo=state.get("repo", "?"),
        pr_number=int(state.get("pr_number", 0)),
        commit_sha=state.get("commit_sha", "?"),
        action=decision.action.value,
        risk=decision.risk,
        reason=decision.reason,
        title=decision.title,
        body=decision.body,
        status=DecisionStatus.PENDING,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # Lost an insert race: another worker queued the same decision. Fetch it.
        session.rollback()
        row = session.execute(
            select(ReviewDecision).where(ReviewDecision.dedup_key == key)
        ).scalar_one()
        log.info("hitl.enqueue_race_resolved", dedup_key=key, id=row.id)
        return row
    log.info("hitl.enqueued", id=row.id, action=row.action, risk=row.risk)
    return row


def list_pending(session: Session) -> list[ReviewDecision]:
    """All decisions awaiting a human, oldest first."""
    return list(
        session.execute(
            select(ReviewDecision)
            .where(ReviewDecision.status == DecisionStatus.PENDING)
            .order_by(ReviewDecision.created_at.asc())
        ).scalars()
    )


def get(session: Session, decision_id: int) -> ReviewDecision | None:
    return session.get(ReviewDecision, decision_id)


def mark_approved(session: Session, row: ReviewDecision) -> None:
    row.status = DecisionStatus.APPROVED
    session.commit()


def mark_rejected(session: Session, row: ReviewDecision) -> ReviewDecision:
    row.status = DecisionStatus.REJECTED
    session.commit()
    log.info("hitl.rejected", id=row.id)
    return row


def mark_executed(session: Session, row: ReviewDecision, url: str) -> ReviewDecision:
    row.status = DecisionStatus.EXECUTED
    row.result_url = url
    session.commit()
    log.info("hitl.executed", id=row.id, url=url)
    return row


def mark_failed(session: Session, row: ReviewDecision, detail: str) -> ReviewDecision:
    row.status = DecisionStatus.FAILED
    row.last_error = detail
    session.commit()
    log.warning("hitl.action_failed", id=row.id, detail=detail)
    return row
