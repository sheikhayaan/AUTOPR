"""Shared pytest fixtures.

Every test runs against an isolated in-memory SQLite DB and a fakeredis
server, so the suite is green today without Docker. The same code paths run
against real Postgres/Redis in Compose — the only branch is the ON CONFLICT
construct in app.db, which behaves identically for our purposes.

Fidelity caveats (also in the decisions log):
  * fakeredis implements XADD/XREADGROUP/XACK/XAUTOCLAIM, which is what we
    exercise, but it is not byte-for-byte Redis. The reclaim test asserts on
    observable behaviour (PEL count, redelivery) rather than internals.
  * SQLite serialises writes, so the "race" test proves the unique-constraint
    logic is correct but cannot reproduce true Postgres row-lock contention.
"""

from __future__ import annotations

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.queue import JobQueue


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
