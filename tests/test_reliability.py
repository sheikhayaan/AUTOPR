"""Phase 8 reliability guards.

These are regression tests for the "survives real infrastructure" changes:
SQLite WAL hardening, Redis client timeouts, the LLM request timeout, and the
webhook's graceful degradation when the queue backend is down. They are the
executable form of the phase-8 decision log — each asserts a property that,
if silently reverted, would reintroduce a production failure mode.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import _make_engine
from app.main import app, get_db, get_queue
from app.models import Base, JobStatus, PRJob
from app.security import compute_signature


# --- SQLite hardening --------------------------------------------------------
def test_sqlite_wal_pragmas_applied_on_connect(tmp_path):
    """A file-backed SQLite engine must come up in WAL with the tuned pragmas.

    WAL is what lets the API and worker share one file without "database is
    locked"; busy_timeout absorbs brief contention; synchronous=NORMAL is the
    recommended durability/throughput point under WAL. These are per-connection
    PRAGMAs, so the guarantee is "every connection", not "once at startup".
    """
    db_path = tmp_path / "wal_probe.db"
    engine = _make_engine(f"sqlite+pysqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
            assert conn.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
            # synchronous=NORMAL is enum value 1.
            assert conn.execute(text("PRAGMA synchronous")).scalar_one() == 1
    finally:
        engine.dispose()


def test_in_memory_sqlite_is_unaffected_by_wal():
    """WAL is a no-op on :memory: — proving the test suite's own engine is safe."""
    engine = _make_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as conn:
            # In-memory databases cannot use WAL; SQLite keeps them in "memory"
            # mode. The pragma attempt must not error and must not flip to wal.
            assert conn.execute(text("PRAGMA journal_mode")).scalar_one() == "memory"
    finally:
        engine.dispose()


# --- Redis client robustness -------------------------------------------------
def test_make_client_wires_robustness_kwargs():
    """make_client must apply the four timeout/health kwargs from settings.

    Without socket/connect timeouts a dead Redis makes the webhook hang instead
    of failing fast; without health checks a stale pooled connection surfaces as
    a spurious error. from_url builds the client lazily (no connection), so this
    is safe offline.
    """
    from app.queue import make_client

    client = make_client("redis://localhost:6379/0")
    ck = client.connection_pool.connection_kwargs
    assert ck["socket_timeout"] == settings.redis_socket_timeout_s
    assert ck["socket_connect_timeout"] == settings.redis_connect_timeout_s
    assert ck["health_check_interval"] == settings.redis_health_check_interval_s
    assert ck["retry_on_timeout"] is True


# --- LLM guard ---------------------------------------------------------------
def test_llm_has_request_timeout(monkeypatch):
    """ChatGroq must be built with a bounded request_timeout.

    A hung Groq call would otherwise pin a worker indefinitely. We patch in a
    dummy key (construction validates presence, not validity, and makes no
    network call) and clear the lru_cache so we build a fresh client.
    """
    from app.agents import llm as llm_module

    monkeypatch.setattr(settings, "groq_api_key", "dummy-test-key-not-real")
    llm_module.get_llm.cache_clear()
    try:
        client = llm_module.get_llm()
        assert client.request_timeout == settings.llm_timeout_s
    finally:
        # Don't leak the dummy-key client to other tests via the cache.
        llm_module.get_llm.cache_clear()


# --- Webhook degradation when the queue is down ------------------------------
class _ExplodingQueue:
    """A JobQueue stand-in whose publish fails as a dead Redis would."""

    def publish(self, fields: dict) -> str:
        raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")


@pytest.fixture()
def client_with_dead_queue():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def _db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_queue] = lambda: _ExplodingQueue()
    c = TestClient(app)
    c._TestSession = TestSession  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _signed_pr_request():
    body = json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "number": 7,
                "head": {"sha": "abc123"},
                "user": {"login": "octocat"},
            },
            "repository": {"full_name": "octocat/hello-world"},
        }
    ).encode()
    headers = {
        "X-Hub-Signature-256": compute_signature(settings.webhook_secret, body),
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }
    return body, headers


def test_webhook_returns_503_when_queue_publish_fails(client_with_dead_queue):
    """A runtime Redis outage on publish must be a 503, not a 500.

    503 tells GitHub to retry the delivery; a 500 reads as a permanent bug and
    the event is dropped. The distinction is the whole point of catching
    RedisError on the publish path.
    """
    body, headers = _signed_pr_request()
    resp = client_with_dead_queue.post("/webhook", content=body, headers=headers)
    assert resp.status_code == 503


def test_webhook_503_still_leaves_a_recoverable_pending_row(client_with_dead_queue):
    """The job row is committed PENDING *before* publish, so a publish failure is
    recoverable: the worker's startup reconcile re-enqueues PENDING jobs. Prove
    the row survives the 503 (rather than being rolled back and lost)."""
    body, headers = _signed_pr_request()
    client_with_dead_queue.post("/webhook", content=body, headers=headers)

    s = client_with_dead_queue._TestSession()
    try:
        rows = s.execute(select(PRJob)).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == JobStatus.PENDING
        # And nothing was double-inserted.
        assert s.execute(select(func.count()).select_from(PRJob)).scalar_one() == 1
    finally:
        s.close()
