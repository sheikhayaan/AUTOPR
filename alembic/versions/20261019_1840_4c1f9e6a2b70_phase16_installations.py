"""phase 16: installations (tenancy) + tenant columns

Revision ID: 4c1f9e6a2b70
Revises: 7398d8b4ca56
Create Date: 2026-10-19 18:40:00.000000+00:00

Adds the tenancy boundary: an `installations` row per GitHub App installation,
plus a nullable `installation_id` on the two tables that cause outward effects.

`installation_id` is nullable on purpose, and that is the whole upgrade story:
every row written before this migration belongs to the single-operator instance,
which has no installation. NULL says exactly that. A backfill would have to
guess an installation for rows that never had one; leaving NULL keeps the
distinction "operator work" vs "tenant work" visible forever, so a query can
ask for one and never accidentally get the other.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4c1f9e6a2b70"
down_revision: str | None = "7398d8b4ca56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "installations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("installation_id", sa.Integer(), nullable=False),
        sa.Column("account_login", sa.String(255), nullable=False),
        sa.Column("account_type", sa.String(32), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("repository_selection", sa.String(32), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "installation_id", name="uq_installations_installation_id"
        ),
    )
    op.create_index(
        "ix_installations_installation_id",
        "installations",
        ["installation_id"],
        unique=False,
    )

    for table in ("pr_jobs", "review_decisions"):
        op.add_column(table, sa.Column("installation_id", sa.Integer(), nullable=True))
        op.create_index(
            f"ix_{table}_installation_id", table, ["installation_id"], unique=False
        )


def downgrade() -> None:
    for table in ("pr_jobs", "review_decisions"):
        op.drop_index(f"ix_{table}_installation_id", table_name=table)
        op.drop_column(table, "installation_id")
    op.drop_index("ix_installations_installation_id", table_name="installations")
    op.drop_table("installations")
