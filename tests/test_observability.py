"""Phase 9 observability tests.

Executable form of the phase-9 decision log. Each asserts a property that, if
silently reverted, would blind an operator in production:

  * ``/readyz`` reflects *real* dependency health (DB + Redis), distinct from the
    always-200 liveness of ``/healthz``.
  * ``/metrics`` exposes both the middleware HTTP counters and the DB-derived
    pipeline gauges, and honours the enable toggle.
  * A correlation id is minted (or an inbound one honoured), echoed in the
    response header, threaded into the JSON log line, and carried across the
    queue into the worker — one trace across the process boundary.
"""

from __future__ import annotations

import json

import fakeredis
import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.ingest import ingest
from app.main import app, get_db
from app.models import Base
from app.observability import (
    bind_correlation_id,
    clear_correlation_id,
    configure_logging,
    current_correlation_id,
)
from app.queue import JobQueue, StreamMessage


@pytest.fixture()
def healthy_client():
    """A TestClient wired to a live in-memory DB and a fakeredis-backed queue.

    We set ``app.state.redis``/``app.state.queue`` directly rather than through
    the lifespan: lifespan's ``make_client()`` would try to reach a real Redis
    (absent in tests) and degrade to ``None``, which is the *unhealthy* path.
    Here we want the healthy baseline, so we inject working fakes and restore
    whatever was there afterwards.
    """
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

    fake = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    q = JobQueue(fake, stream="test:jobs", group="test-workers")
    q.ensure_group()

    prev_redis = getattr(app.state, "redis", None)
    prev_queue = getattr(app.state, "queue", None)
    app.state.redis = fake
    app.state.queue = q
    app.dependency_overrides[get_db] = _db
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        app.state.redis = prev_redis
        app.state.queue = prev_queue
        engine.dispose()


# --- /readyz: deep readiness -------------------------------------------------
def test_readyz_ok_when_db_and_redis_healthy(healthy_client):
    resp = healthy_client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "redis": "ok"}


def test_readyz_503_when_redis_never_connected(healthy_client):
    """Redis unreachable at startup => queue/redis are None => not ready.

    A 503 here with a green /healthz is the signal "process alive, dependency
    down" — the whole reason readiness is separate from liveness.
    """
    app.state.redis = None
    resp = healthy_client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "unavailable"


def test_readyz_503_when_redis_ping_fails(healthy_client):
    """A Redis that is present but unresponsive (ping raises) is not ready."""

    class _DeadPing:
        def ping(self):
            raise ConnectionError("Error 111 connecting to redis:6379.")

    app.state.redis = _DeadPing()
    resp = healthy_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"].startswith("error:")


def test_readyz_503_when_db_down(healthy_client):
    """DB probe failure flips readiness even if Redis is fine."""

    class _DeadDB:
        def execute(self, *a, **k):
            raise RuntimeError("database is down")

    app.dependency_overrides[get_db] = lambda: _DeadDB()
    resp = healthy_client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["database"].startswith("error:")
    assert body["checks"]["redis"] == "ok"


# --- /metrics: Prometheus exposition -----------------------------------------
def test_metrics_exposes_http_and_domain_series(healthy_client):
    # Drive one request through the middleware so the HTTP counter has a sample
    # for a known route template.
    healthy_client.get("/healthz")

    resp = healthy_client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    # HTTP middleware metrics, labelled by *route template* (not raw path).
    assert "autopr_http_requests_total" in body
    assert 'path="/healthz"' in body
    # Domain gauges refreshed from the DB at scrape time.
    assert "autopr_jobs" in body
    assert "autopr_reviews" in body
    assert "autopr_queue_depth" in body


def test_metrics_404_when_disabled(healthy_client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", False)
    resp = healthy_client.get("/metrics")
    assert resp.status_code == 404


# --- Correlation id ----------------------------------------------------------
def test_response_carries_minted_correlation_id(healthy_client):
    resp = healthy_client.get("/healthz")
    cid = resp.headers.get("X-Request-ID")
    assert cid is not None
    # 12 lowercase hex chars (see new_correlation_id).
    assert len(cid) == 12
    int(cid, 16)  # raises if not hex


def test_inbound_correlation_id_is_honoured(healthy_client):
    resp = healthy_client.get("/healthz", headers={"X-Request-ID": "feedfacecafe"})
    assert resp.headers.get("X-Request-ID") == "feedfacecafe"


def test_configure_logging_emits_single_line_json_with_correlation_id(capsys):
    """Production logging is one JSON object per line, with the bound trace id.

    Single-line JSON is what makes the logs greppable and ingestable; the
    merged correlation id is what makes scattered lines a trace.
    """
    configure_logging(json_logs=True)
    bind_correlation_id("abc123def456")
    try:
        structlog.get_logger().info("test.event", foo="bar")
    finally:
        clear_correlation_id()

    out = capsys.readouterr().out.strip()
    assert "\n" not in out  # exactly one line
    record = json.loads(out)  # and it is valid JSON
    assert record["event"] == "test.event"
    assert record["foo"] == "bar"
    assert record["correlation_id"] == "abc123def456"
    assert record["level"] == "info"


def test_ingest_stamps_current_correlation_id_onto_the_stream(db, queue, sample_payload):
    """The API's trace id must ride the job payload across the queue boundary."""
    bind_correlation_id("trace0000beef")
    try:
        ingest(db, queue, sample_payload)
    finally:
        clear_correlation_id()

    entries = queue.client.xrange(queue.stream)
    assert len(entries) == 1
    _id, raw = entries[0]
    fields = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }
    assert fields["correlation_id"] == "trace0000beef"


def test_worker_binds_incoming_correlation_id_and_clears_it(
    Session, queue, sample_payload, monkeypatch
):
    """The worker binds the id carried on the message (so its logs join the
    trace) and clears it afterwards (so it never leaks into the next job)."""
    from app import worker as worker_module

    db = Session()
    job_id = ingest(db, queue, sample_payload).job_id
    db.close()

    # _handle_message opens its own session via app.db.SessionLocal; point that
    # at the in-memory test DB (StaticPool shares the one connection, so the row
    # committed above is visible).
    monkeypatch.setattr(worker_module, "SessionLocal", Session)

    captured: dict[str, str | None] = {}

    def handler(session, job):
        captured["cid"] = current_correlation_id()
        return "ok"

    msg = StreamMessage(id="0-1", fields={"job_id": str(job_id), "correlation_id": "beadfeed1234"})
    worker_module._handle_message(queue, msg, handler)

    assert captured["cid"] == "beadfeed1234"
    assert current_correlation_id() is None  # cleared in the finally


def test_worker_mints_correlation_id_when_message_has_none(
    Session, queue, sample_payload, monkeypatch
):
    """An older/out-of-band message with a blank correlation_id still gets a
    trace id, rather than logging under an empty one."""
    from app import worker as worker_module

    db = Session()
    job_id = ingest(db, queue, sample_payload).job_id
    db.close()

    monkeypatch.setattr(worker_module, "SessionLocal", Session)

    captured: dict[str, str | None] = {}

    def handler(session, job):
        captured["cid"] = current_correlation_id()
        return "ok"

    msg = StreamMessage(id="0-1", fields={"job_id": str(job_id), "correlation_id": ""})
    worker_module._handle_message(queue, msg, handler)

    assert captured["cid"]  # non-empty
    assert len(captured["cid"]) == 12
