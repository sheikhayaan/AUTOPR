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
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import execute_for_rowcount
from app.models import DecisionStatus, ReviewDecision, utcnow
from app.routing.policy import RoutingDecision

log = structlog.get_logger()


def _installation_id_from(state: dict) -> int | None:
    """Read the tenant id out of graph state, tolerating junk.

    This value becomes the authorization boundary for who may approve the row,
    so a malformed one must degrade to NULL ("no tenant") rather than raise
    inside the router or, worse, coerce something truthy out of a string like
    "None". `bool` is rejected explicitly because `isinstance(True, int)` is
    true in Python and would enrol a decision to tenant 1.
    """
    raw = state.get("installation_id")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            return None
        return value if value > 0 else None
    return None


def enqueue(session: Session, decision: RoutingDecision, state: dict) -> ReviewDecision:
    """Persist a pending decision idempotently; return the stored row.

    If a row already exists for this (repo, pr, commit, action) it is returned
    unchanged — we never re-queue or overwrite a decision a human may already
    have acted on.
    """
    return enqueue_tracked(session, decision, state)[0]


def enqueue_tracked(
    session: Session, decision: RoutingDecision, state: dict
) -> tuple[ReviewDecision, bool]:
    """Like `enqueue`, but also reports whether *this* call created the row.

    The distinction is load-bearing for the auto path: a freshly created row is
    ours to execute, whereas an existing row may belong to a human gate (PENDING
    queued at higher risk on an earlier delivery, or APPROVED in flight) and must
    never be executed by a later, lower-risk redelivery. See
    `app.routing.router.router_node`.
    """
    key = decision.dedup_key(state)
    existing = session.execute(
        select(ReviewDecision).where(ReviewDecision.dedup_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        log.info("hitl.enqueue_duplicate", dedup_key=key, id=existing.id)
        return existing, False

    row = ReviewDecision(
        dedup_key=key,
        repo=state.get("repo", "?"),
        pr_number=int(state.get("pr_number", 0)),
        commit_sha=state.get("commit_sha", "?"),
        # Tenancy is part of the row, not a lookup performed later: it is what
        # decides who may approve this action. NULL is a real value meaning
        # "single-operator install", so it must be written explicitly rather
        # than left to a default that no tenant can ever address.
        installation_id=_installation_id_from(state),
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
        return row, False
    log.info("hitl.enqueued", id=row.id, action=row.action, risk=row.risk)
    return row, True


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


def requeue_failed(session: Session, decision_id: int) -> bool:
    """Re-open a FAILED decision to PENDING (conditional UPDATE, FAILED -> PENDING).

    Used when a later delivery is human-gated but the row it collided with was
    left FAILED by an earlier *auto* post attempt. FAILED means no side effect
    landed, and PENDING is what `list_pending` shows — without this the decision
    would be invisible in the review queue and never executed by anyone.
    """
    requeued = (
        execute_for_rowcount(
            session,
            update(ReviewDecision)
            .where(
                ReviewDecision.id == decision_id,
                ReviewDecision.status == DecisionStatus.FAILED,
            )
            .values(status=DecisionStatus.PENDING, last_error=None, updated_at=utcnow()),
        )
        > 0
    )
    session.commit()
    if requeued:
        log.info("hitl.requeued_failed", id=decision_id)
    return requeued


def claim_approval(session: Session, decision_id: int) -> tuple[ReviewDecision | None, str]:
    """Atomically claim a PENDING decision for execution — the approve CAS.

    The approve endpoint must not read-then-post: two concurrent approvals (a
    double-click, a retried request, two operators) would both pass a bare
    `if status == EXECUTED` check and post twice. Instead we do a single
    conditional UPDATE — PENDING -> APPROVED — and only the caller that changed a
    row wins. Everyone else gets back the current state and does nothing.

    Returns `(row, outcome)` where outcome is one of:
      * ``"claimed"``          — we own the execution; go do the GitHub call.
      * ``"already_executed"`` — done previously; caller should return success.
      * ``"rejected"``         — a human declined it; never execute.
      * ``"in_progress"``      — another approver holds the claim (or a previous
                                 approver crashed mid-post — the API's startup
                                 sweep clears those to FAILED so they can be
                                 retried).
      * ``"not_found"``        — no such decision.
    """
    now = utcnow()
    won = (
        execute_for_rowcount(
            session,
            update(ReviewDecision)
            .where(
                ReviewDecision.id == decision_id,
                ReviewDecision.status == DecisionStatus.PENDING,
            )
            .values(status=DecisionStatus.APPROVED, updated_at=now),
        )
        > 0
    )
    session.commit()

    row = session.get(ReviewDecision, decision_id)
    if row is None:
        return None, "not_found"
    if won:
        log.info("hitl.claimed", id=row.id)
        return row, "claimed"
    if row.status == DecisionStatus.EXECUTED:
        return row, "already_executed"
    if row.status == DecisionStatus.REJECTED:
        return row, "rejected"
    if row.status == DecisionStatus.FAILED:
        # A prior execution failed. Re-approving is a deliberate retry, so claim
        # it the same way we would a fresh PENDING row.
        retried = (
            execute_for_rowcount(
                session,
                update(ReviewDecision)
                .where(
                    ReviewDecision.id == decision_id,
                    ReviewDecision.status == DecisionStatus.FAILED,
                )
                .values(status=DecisionStatus.APPROVED, updated_at=now),
            )
            > 0
        )
        session.commit()
        if retried:
            log.info("hitl.claim_retry", id=decision_id)
            return session.get(ReviewDecision, decision_id), "claimed"
        return session.get(ReviewDecision, decision_id), "in_progress"
    return row, "in_progress"


def sweep_interrupted(session: Session) -> int:
    """Clear claims left APPROVED by a crashed approver back to FAILED.

    Called once at API startup: at that moment no approve request from this
    process is in flight, so an APPROVED row is provably abandoned. We mark it
    FAILED with an explicit error rather than guessing whether the GitHub call
    landed — the operator then decides ([re-]approve or reject). Without this, a
    crash between the claim and `mark_executed` would strand the row forever.
    """
    rows = session.execute(
        select(ReviewDecision).where(ReviewDecision.status == DecisionStatus.APPROVED)
    ).scalars()
    count = 0
    for row in rows:
        row.status = DecisionStatus.FAILED
        row.last_error = "interrupted during approval; re-approve or reject"
        count += 1
    if count:
        session.commit()
        log.warning("hitl.interrupted_claims_swept", count=count)
    return count


def mark_rejected(session: Session, row: ReviewDecision) -> ReviewDecision:
    row.status = DecisionStatus.REJECTED
    session.commit()
    log.info("hitl.rejected", id=row.id)
    return row


def reject_pending(session: Session, decision_id: int) -> tuple[ReviewDecision | None, str]:
    """Atomically reject a PENDING decision. Mirrors `claim_approval`'s CAS.

    Rejecting must not race an in-flight approval: if an approver already claimed
    the row (PENDING -> APPROVED) and is posting to GitHub, flipping it to
    REJECTED would produce a decision that is rejected *and* executed. So only a
    genuine PENDING -> REJECTED transition wins.

    Returns `(row, outcome)` with outcome in {``"rejected"``, ``"already_executed"``,
    ``"in_progress"``, ``"not_found"``}.
    """
    won = (
        execute_for_rowcount(
            session,
            update(ReviewDecision)
            .where(
                ReviewDecision.id == decision_id,
                ReviewDecision.status == DecisionStatus.PENDING,
            )
            .values(status=DecisionStatus.REJECTED, updated_at=utcnow()),
        )
        > 0
    )
    session.commit()

    row = session.get(ReviewDecision, decision_id)
    if row is None:
        return None, "not_found"
    if won:
        log.info("hitl.rejected", id=row.id)
        return row, "rejected"
    if row.status == DecisionStatus.EXECUTED:
        return row, "already_executed"
    return row, "in_progress"


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
