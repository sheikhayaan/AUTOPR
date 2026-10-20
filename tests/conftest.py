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
# This MUST happen before any `app.*` import.
#
# Two problems are solved at once:
#
# 1. Settings construction runs the fail-fast secret validator. CI has no .env
#    (so the webhook secret is the shipped placeholder), which would raise on
#    import. AUTOPR_ALLOW_INSECURE opts into the documented dev/test escape hatch.
#
# 2. Hermeticity. pydantic-settings gives real environment variables precedence
#    over `.env`, so pinning the safety-relevant fields here makes the suite
#    independent of whatever a developer's local `.env` contains. Without this,
#    a `.env` with AUTOPR_HANDOFF_MODE=true silently flips every routing test to
#    the all-gated path (15 failures), and a real AUTOPR_API_TOKEN 401s the
#    approve/reject tests. Tests that exercise a non-default posture (hand-off
#    mode, auth, rate limiting) opt in locally via monkeypatch — see
#    test_handoff_mode.py, test_auth.py.
import os

os.environ["AUTOPR_ALLOW_INSECURE"] = "1"
os.environ["AUTOPR_HANDOFF_MODE"] = "false"
os.environ["AUTOPR_GITHUB_DRY_RUN"] = "true"
os.environ["AUTOPR_GITHUB_TOKEN"] = ""
os.environ["AUTOPR_API_TOKEN"] = ""
os.environ["AUTOPR_REQUIRE_AUTH_FOR_READS"] = "false"
os.environ["AUTOPR_RATE_LIMIT_ENABLED"] = "false"
os.environ["AUTOPR_LOG_JSON"] = "true"

import fakeredis
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.config import settings as _settings
from app.models import Base
from app.queue import JobQueue

# Belt and braces: force the same posture on the process-wide settings singleton
# that app.main already imported, so the guarantee holds even if a future field
# default or loader change slips past the environment pins above. auth is a
# no-op, reads are open, the rate limiter is off (every TestClient request shares
# the client IP "testclient", so a live limiter would accumulate across unrelated
# tests and flake), no outward writes, and no hand-off mode.
_settings.api_token = ""
_settings.require_auth_for_reads = False
_settings.rate_limit_enabled = False
_settings.handoff_mode = False
_settings.github_dry_run = True
_settings.github_token = ""


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


def normalize_postgres_url(url: str) -> str:
    """Pin the driver in a Postgres URL.

    A bare ``postgresql://`` makes SQLAlchemy reach for psycopg2, which this
    project does not depend on (it uses psycopg 3). Server URLs obtained from a
    test harness commonly arrive in the bare form.
    """
    if url.startswith("postgresql+psycopg://") or url.startswith("postgresql+psycopg2://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory):  # noqa: ANN001, ANN201 - pytest fixtures
    """A real PostgreSQL URL, or skip.

    Two sources, in order:

    1. ``AUTOPR_TEST_POSTGRES_URL`` — what CI uses (a `postgres:16` service
       container). The CI job fails rather than skips, because for CI the whole
       point is to run these.
    2. ``pgserver`` — real PostgreSQL binaries bundled as a Python package, so a
       developer gets a genuine postmaster, real row locks and real MVCC without
       installing anything or starting Docker.

    With neither, the tests skip: they are additive proof, not a gate the rest of
    the suite depends on.
    """
    external = os.environ.get("AUTOPR_TEST_POSTGRES_URL", "").strip()
    if external:
        return normalize_postgres_url(external)

    pgserver = pytest.importorskip(
        "pgserver",
        reason="set AUTOPR_TEST_POSTGRES_URL or `pip install pgserver` to run the Postgres tier",
    )
    data_dir = tmp_path_factory.mktemp("pg") / "data"
    server = pgserver.get_server(str(data_dir), cleanup_mode=None)
    return normalize_postgres_url(str(server.get_uri()))


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
