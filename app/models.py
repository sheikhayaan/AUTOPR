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

    # Which installation (tenant) owns this work. NULL = the single-operator
    # deployment: a PAT-driven instance has no installations, and every row it
    # ever wrote predates tenancy. Keeping NULL meaningful means existing
    # databases upgrade without a backfill guess, and tenancy queries can be
    # written as `installation_id == X` (strict) without silently matching
    # operator rows.
    installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

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
    # Tenant owner of the outward action. Approving a decision must use *this*
    # installation's token, never another's — the reason it is materialised on
    # the row instead of looked up from the repo name at approval time.
    installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

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


class Installation(Base):
    """One GitHub App installation — i.e. one tenant.

    Created from the `installation` webhook (or healed from a PR payload, which
    carries the same object) and the reason a stranger can install this without
    a human on our side: the row *is* the onboarding. `deleted_at` and
    `suspended_at` are kept separate because GitHub distinguishes them and they
    mean different things to an operator: suspended is reversible (billing,
    abuse review), deleted is final and must stop all work immediately.

    Not a cache of GitHub's data for its own sake — it is the tenancy boundary.
    Every write resolves a token through an active row here, so revoking a row
    revokes the credential's reach.
    """

    __tablename__ = "installations"
    __table_args__ = (UniqueConstraint("installation_id", name="uq_installations_installation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_login: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    account_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # Repository selection the installer chose ("all" or "selected"). Informational:
    # the real authority is GitHub's, enforced per-call by the installation token.
    repository_selection: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    @property
    def is_active(self) -> bool:
        """Only an active row may mint tokens or have its work processed."""
        return self.deleted_at is None and self.suspended_at is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Installation {self.installation_id} {self.account_login!r}>"
