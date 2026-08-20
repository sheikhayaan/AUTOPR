"""SQLAlchemy ORM models.

Two tables carry the Phase 1 correctness story:

* ``PRJob`` — one row per unit of work. Its ``dedup_key`` unique constraint
  is what makes webhook ingestion idempotent: a retried delivery collides on
  the key and does not create a second job.

* ``JobResult`` — one row per *completed side effect*, keyed by job id. The
  worker writes it with INSERT ... ON CONFLICT DO NOTHING. It exists to prove
  exactly-once processing: even if a job is delivered/reclaimed twice, there
  is at most one JobResult row for it. Tests assert on this.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    PENDING = "pending"  # row created, not yet confirmed on the stream
    QUEUED = "queued"  # XADD succeeded, awaiting a worker
    PROCESSING = "processing"  # a worker has claimed it
    DONE = "done"  # completed successfully (side effect committed)
    DEAD = "dead"  # exceeded max attempts -> human queue


class DecisionStatus(str, enum.Enum):
    """Lifecycle of a human-gated routing decision (Phase 4)."""

    PENDING = "pending"  # queued, awaiting a human
    APPROVED = "approved"  # a human approved; action about to fire
    REJECTED = "rejected"  # a human declined; no action taken
    EXECUTED = "executed"  # the approved action was carried out on GitHub
    FAILED = "failed"  # approved but the GitHub action errored


class PRJob(Base):
    __tablename__ = "pr_jobs"
    __table_args__ = (
        # The idempotency guarantee lives here.
        UniqueConstraint("dedup_key", name="uq_pr_jobs_dedup_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Idempotency key. Deterministic function of the webhook content
    # (repo + PR number + head SHA + event). Two identical deliveries produce
    # the same key and therefore collide.
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # PR metadata required by the spec.
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False, default="pull_request")

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=16),
        nullable=False,
        default=JobStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Redis stream entry id (populated after XADD) — useful for tracing.
    stream_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # CI-fix track (Phase 5): inline failure evidence extracted from a
    # check_run/workflow_run webhook, carried here so the worker can drive the
    # CI-fix graph track without a second API round-trip for the logs. NULL for
    # PR-review jobs. See ingest.parse_ci_event.
    event_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    result: Mapped[JobResult | None] = relationship(back_populates="job", uselist=False)


class JobResult(Base):
    """Exactly-once side-effect ledger.

    ``job_id`` is the primary key, so a second attempt to record the result
    of the same job is a no-op (ON CONFLICT DO NOTHING). In Phase 1 the
    "side effect" is symbolic (we just record that processing happened); in
    later phases this generalises to "posted the review comment", etc.
    """

    __tablename__ = "job_results"

    job_id: Mapped[int] = mapped_column(
        ForeignKey("pr_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    job: Mapped[PRJob] = relationship(back_populates="result")


class ReviewDecision(Base):
    """A routing decision that needs (or needed) a human — the HITL queue.

    Phase 4 writes one row here whenever the router decides an action must be
    approved before it fires (any code-changing action; any elevated-risk
    review). A maintainer lists pending rows and approves/rejects them through
    the ops API; only on approval does the outward GitHub action happen.

    ``dedup_key`` (repo|pr|commit|action) is unique, so re-processing the same
    commit — a redelivered webhook, a worker retry — collides here instead of
    queuing the same action twice. This is the same idempotency spine the job
    ledger uses, applied to outward actions.
    """

    __tablename__ = "review_decisions"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_review_decisions_dedup_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(String(512), nullable=False)

    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)

    action: Mapped[str] = mapped_column(String(32), nullable=False)  # Action enum value
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status: Mapped[DecisionStatus] = mapped_column(
        Enum(DecisionStatus, native_enum=False, length=16),
        nullable=False,
        default=DecisionStatus.PENDING,
    )
    # Where the executed action landed (comment/PR url), or the failure detail.
    result_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
