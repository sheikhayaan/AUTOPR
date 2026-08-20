"""End-to-end /webhook tests through the real FastAPI app.

Proves the HTTP contract: 401 on bad signature, 200 + enqueue on a valid PR
event, and duplicate delivery returning 200 with status "duplicate" (no second
job). Uses a fakeredis-backed queue and an in-memory SQLite DB injected via the
app's dependency overrides, so no infra is needed.
"""

from __future__ import annotations

import json

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.main import app, get_db, get_queue
from app.models import Base, PRJob
from app.queue import JobQueue
from app.security import compute_signature


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    server = fakeredis.FakeServer()
    rds = fakeredis.FakeStrictRedis(server=server)
    queue = JobQueue(rds, stream="test:jobs", group="test-workers")
    queue.ensure_group()

    def _db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    # Bypass the lifespan (which would build a real Redis client) by overriding
    # the two dependencies.
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_queue] = lambda: queue

    # TestClient normally runs lifespan; we don't want the real Redis connect,
    # so drive the app without the startup context.
    c = TestClient(app)
    c._TestSession = TestSession  # type: ignore[attr-defined]
    c._queue = queue  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _pr_body(sha: str = "abc123") -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "number": 7,
                "head": {"sha": sha},
                "user": {"login": "octocat"},
            },
            "repository": {"full_name": "octocat/hello-world"},
        }
    ).encode()


def _headers(body: bytes) -> dict:
    return {
        "X-Hub-Signature-256": compute_signature(settings.webhook_secret, body),
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }


def test_invalid_signature_returns_401(client):
    body = _pr_body()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=deadbeef", "X-GitHub-Event": "pull_request"},
    )
    assert resp.status_code == 401


def test_valid_webhook_returns_200_and_enqueues(client):
    body = _pr_body()
    resp = client.post("/webhook", content=body, headers=_headers(body))
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    # One job row, one stream message.
    s = client._TestSession()
    count = s.execute(select(func.count()).select_from(PRJob)).scalar_one()
    s.close()
    assert count == 1
    assert client._queue.client.xlen("test:jobs") == 1


def test_duplicate_delivery_returns_200_duplicate(client):
    body = _pr_body()
    first = client.post("/webhook", content=body, headers=_headers(body))
    second = client.post("/webhook", content=body, headers=_headers(body))
    assert first.json()["status"] == "queued"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    s = client._TestSession()
    count = s.execute(select(func.count()).select_from(PRJob)).scalar_one()
    s.close()
    assert count == 1
    assert client._queue.client.xlen("test:jobs") == 1  # not enqueued twice


def test_ping_event_ignored(client):
    body = json.dumps({"zen": "hello"}).encode()
    headers = {
        "X-Hub-Signature-256": compute_signature(settings.webhook_secret, body),
        "X-GitHub-Event": "ping",
    }
    resp = client.post("/webhook", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
