"""Shared pytest fixtures.

Every test runs against an isolated in-memory SQLite DB and a fakeredis
server, so the suite is green today without Docker. The same code paths run
against real Postgres/Redis in Compose — the only branch is the ON CONFLICT
construct in app.db, which behaves identically for our purposes.

Fidelity caveats (also in the decisions log):
  * fakeredis implements XADD/XREADGROUP/XACK/XAUTOCLAIM, which is what we
    exercise, but it is not byte-for-byte Redis. The reclaim test asserts on
    observable behaviour (PEL count, redelivery) rather than internals.
  * SQLite serialises writes, so the "race" test proves unique-index arbitration
    but cannot reproduce true Postgres row-lock contention. The race test uses the
    file-backed ``race_engine`` fixture (one connection per thread) rather than the
    shared-connection in-memory one, because a shared connection shares the
    transaction and there is then no concurrency to observe.
"""

from __future__ import annotations

# --- Test environment setup --------------------------------------------------
# AUTOPR_ALLOW_INSECURE MUST be set before any `app.*` import: constructing
# Settings runs the fail-fast secret validator at import time, and CI has no
# .env (so the webhook secret would be the shipped placeholder). This opts into
# the documented local/dev/test escape hatch the validator carves out.
import os

os.environ.setdefault("AUTOPR_ALLOW_INSECURE", "1")

import fakeredis
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.config import settings as _settings
from app.models import Base
from app.queue import JobQueue

# Force a test-safe security posture, overriding whatever a developer's local
# .env supplies (a real AUTOPR_API_TOKEN there would otherwise make the existing
# unauthenticated approve/reject tests 401). We mutate the process-wide settings
# singleton that app.main already imported, so the change is visible to the app:
# auth is a no-op, reads are open, and the per-IP rate limiter is off — every
# TestClient request shares the client IP "testclient", so a live limiter would
# accumulate across unrelated tests and flake. Tests that exercise auth or rate
# limiting opt back in locally via monkeypatch (see test_auth.py).
_settings.api_token = ""
_settings.require_auth_for_reads = False
_settings.rate_limit_enabled = False


@pytest.fixture()
def engine():
    # StaticPool = one shared connection for the whole engine. Required for an
    # in-memory SQLite DB used across threads: the default SingletonThreadPool
    # gives each thread its OWN empty database, so tables created here would be
    # invisible to the concurrency test's worker threads. Real Postgres shares
    # state across connections natively, so this branch is test-harness-only.
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture()
def db(Session):
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def race_engine(tmp_path):
    """Engine for the concurrency test: one *connection* per thread.

    The shared in-memory ``engine`` fixture above cannot model a race. StaticPool
    hands both threads the same DBAPI connection, so both sessions run inside one
    transaction: whichever thread commits first ends the other's transaction and
    closes its cursors, and ON CONFLICT has nothing to arbitrate between. That
    produced a flaky test in *both* directions (~10% of runs reported two winners
    or zero winners) with production code that was correct.

    So: a real on-disk file, one connection per thread, and BEGIN IMMEDIATE so
    the second writer waits on the lock instead of deadlocking — the same shape
    as Postgres blocking on a conflicting unique index. It is still SQLite, so it
    exercises index arbitration rather than row-level lock contention; that
    distinction is in the ADR.
    """
    eng = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'race.db'}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 15},
        poolclass=NullPool,
    )

    @event.listens_for(eng, "connect")
    def _let_us_drive_transactions(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        # Take transaction control away from pysqlite so the "begin" hook below
        # can issue BEGIN IMMEDIATE (pysqlite would otherwise emit a bare BEGIN,
        # which takes the write lock lazily and can deadlock two writers).
        dbapi_conn.isolation_level = None

    @event.listens_for(eng, "begin")
    def _begin_immediate(conn):  # type: ignore[no-untyped-def]
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def redis_client():
    # A single shared fakeredis server instance so multiple clients/consumers
    # see the same stream (mirrors a real shared Redis).
    server = fakeredis.FakeServer()
    return fakeredis.FakeStrictRedis(server=server)


@pytest.fixture()
def queue(redis_client):
    q = JobQueue(redis_client, stream="test:jobs", group="test-workers")
    q.ensure_group()
    return q


@pytest.fixture()
def sample_payload():
    from app.ingest import PRPayload

    return PRPayload(
        repo="octocat/hello-world",
        pr_number=42,
        commit_sha="deadbeefcafebabe0000000000000000deadbeef",
        author="octocat",
        event="pull_request",
    )
