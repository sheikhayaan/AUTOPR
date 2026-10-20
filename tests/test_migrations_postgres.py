"""The migrations, applied and rolled back on a real Postgres.

Migration reversibility was verified before this file existed, but only on SQLite
and in CI. SQLite's `ALTER TABLE` support is thin enough that a downgrade can pass
there while failing on Postgres — the backend this project actually deploys — and
enum/dropped-index/`timestamptz` handling all differ. Running the real
`upgrade head -> downgrade base -> upgrade head` cycle against a real server is
the only way to know the rollback works where it matters.

Each test gets its own database, so the cycle cannot be contaminated by —
or contaminate — the concurrency tests' schema.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


@pytest.fixture()
def fresh_database(postgres_url: str):  # noqa: ANN001, ANN201 - pytest fixtures
    """Create an empty database on the real server, and drop it afterwards."""
    admin = create_engine(postgres_url, future=True, isolation_level="AUTOCOMMIT")
    name = f"autopr_migr_{uuid.uuid4().hex[:10]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield _with_database(str(postgres_url), name)
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AUTOPR_DATABASE_URL": url,
        "AUTOPR_ALLOW_INSECURE": "1",  # alembic imports app.config, which validates secrets
    }
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _tables(url: str) -> set[str]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            ).scalars()
            return set(rows)
    finally:
        engine.dispose()


def _indexes(url: str, table: str) -> set[str]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = :t"
                ),
                {"t": table},
            ).scalars()
            return set(rows)
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_on_postgres(fresh_database: str) -> None:
    """The full cycle, on real Postgres, ending where it started."""
    up1 = _alembic(fresh_database, "upgrade", "head")
    assert up1.returncode == 0, f"upgrade failed:\n{up1.stdout}\n{up1.stderr}"

    tables = _tables(fresh_database)
    assert {"pr_jobs", "job_results", "review_decisions", "installations"} <= tables
    # The covering index the quota counter relies on (phase 17) is a real
    # Postgres index, not just a declaration in the model.
    assert "ix_review_decisions_status_created_at" in _indexes(fresh_database, "review_decisions")
    assert {"ix_pr_jobs_repo_created_at", "ix_pr_jobs_installation_created_at"} <= _indexes(
        fresh_database, "pr_jobs"
    )

    down = _alembic(fresh_database, "downgrade", "base")
    assert down.returncode == 0, f"downgrade failed:\n{down.stdout}\n{down.stderr}"
    leftover = _tables(fresh_database)
    assert leftover == set(), f"downgrade left tables behind on Postgres: {sorted(leftover)}"

    up2 = _alembic(fresh_database, "upgrade", "head")
    assert up2.returncode == 0, f"re-upgrade failed:\n{up2.stdout}\n{up2.stderr}"
    assert _tables(fresh_database) == tables


def test_migrations_are_idempotent_when_already_at_head(fresh_database: str) -> None:
    """`upgrade head` twice in a row is a no-op, not an error.

    Deployment scripts run it on every boot; a second run must not fail, and must
    not attempt to re-create what exists.
    """
    first = _alembic(fresh_database, "upgrade", "head")
    assert first.returncode == 0, first.stderr
    second = _alembic(fresh_database, "upgrade", "head")
    assert second.returncode == 0, f"second upgrade failed:\n{second.stdout}\n{second.stderr}"
    assert {"pr_jobs", "job_results"} <= _tables(fresh_database)


def test_the_worker_schema_matches_the_models_on_postgres(fresh_database: str) -> None:
    """Alembic's schema and the ORM's metadata agree, column for column.

    Catches the classic drift: a column added to `models.py` without a migration.
    Compared on Postgres because the type mapping is what differs per backend
    (SQLite ignores lengths, renders `timestamptz` as TEXT, and so on).
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.models import Base

    assert _alembic(fresh_database, "upgrade", "head").returncode == 0

    engine = create_engine(fresh_database, future=True)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    # Alembic reports incidental differences (index name casing, server defaults)
    # as noise on some versions; only structural drift is a failure.
    structural = [d for d in diff if not str(d).startswith(("add_index", "remove_index"))]
    assert structural == [], f"models and migrations have drifted: {structural}"
