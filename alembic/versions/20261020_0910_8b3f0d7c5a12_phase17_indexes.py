"""phase 17: indexes for the per-job writes and reads

Revision ID: 8b3f0d7c5a12
Revises: 4c1f9e6a2b70
Create Date: 2026-10-20 09:10:00.000000+00:00

Every webhook runs up to three quota `COUNT(*)`s on `pr_jobs` filtered by
`created_at` (and optionally `repo` or `installation_id`), and every reconcile
pass scans for `status = 'PENDING'`. None of those columns was indexed, so each
delivery did a sequential scan whose cost grew with total history — the classic
"fine in testing, quadratic in production" shape. At 100k rows a single quota
check reads the whole table; at 1M it dominates ingest latency.

These five indexes exist for five specific queries, not speculatively:

1. ``ix_pr_jobs_created_at``            — instance-wide daily quota
2. ``ix_pr_jobs_repo_created_at``       — per-repo hourly quota
3. ``ix_pr_jobs_installation_created_at`` — per-tenant daily quota
4. ``ix_pr_jobs_status``                — reconcile_pending + pending reads
5. ``ix_review_decisions_status_created_at`` — the pending-decisions read

The composites lead with the equality column and end with the range column, so
Postgres can answer each COUNT from the index alone (index-only scan) without
touching the heap.

Cost: five indexes on the two busiest tables. Writes are already one INSERT per
delivery, so the amortised write cost is a few hundred microseconds against a
scan that grows without bound. `CONCURRENTLY` is deliberately not used: this
runs inside Alembic's transaction, and a brief lock on a table that is at most a
few hundred thousand rows is acceptable for a migration an operator schedules.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "8b3f0d7c5a12"
down_revision: str | None = "4c1f9e6a2b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_pr_jobs_created_at", "pr_jobs", ["created_at"])
    op.create_index("ix_pr_jobs_repo_created_at", "pr_jobs", ["repo", "created_at"])
    op.create_index(
        "ix_pr_jobs_installation_created_at",
        "pr_jobs",
        ["installation_id", "created_at"],
    )
    op.create_index("ix_pr_jobs_status", "pr_jobs", ["status"])
    op.create_index(
        "ix_review_decisions_status_created_at",
        "review_decisions",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_decisions_status_created_at", table_name="review_decisions")
    op.drop_index("ix_pr_jobs_status", table_name="pr_jobs")
    op.drop_index("ix_pr_jobs_installation_created_at", table_name="pr_jobs")
    op.drop_index("ix_pr_jobs_repo_created_at", table_name="pr_jobs")
    op.drop_index("ix_pr_jobs_created_at", table_name="pr_jobs")
