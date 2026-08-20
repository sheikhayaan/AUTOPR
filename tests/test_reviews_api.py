"""End-to-end tests for the human-in-the-loop ops API.

Drives the real FastAPI app: list pending, approve (which fires the GitHub
action through an injected fake), and reject. Proves the safety contract at the
HTTP boundary — a queued decision only reaches GitHub via an explicit approve,
approving twice doesn't double-post, and a rejected decision can never execute.

Infra-free: in-memory SQLite (StaticPool, one shared DB) + a FakeGitHubClient
injected via dependency override, so no network and no Postgres.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app, get_db, get_github
from app.models import Base, DecisionStatus
from app.routing import store
from app.routing.github import ActionResult, FakeGitHubClient
from app.routing.policy import route


def _pr_state(risk: str = "high", sha: str = "c0mm1t") -> dict:
    return {
        "repo": "o/r",
        "pr_number": 7,
        "commit_sha": sha,
        "changed_files": [{"path": "a.py", "patch": "+x"}],
        "review_findings": [{"file": "a.py", "line": 1, "severity": "error", "message": "x"}],
        "risk_score": risk,
        "summary": "s",
    }


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
    gh = FakeGitHubClient()

    def _db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_github] = lambda: gh

    c = TestClient(app)
    c._TestSession = TestSession  # type: ignore[attr-defined]
    c._github = gh  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _seed(client, state: dict):
    """Queue a decision the way the router would, on the app's DB."""
    s = client._TestSession()
    try:
        row = store.enqueue(s, route(state), state)
        return row.id
    finally:
        s.close()


# --- listing -----------------------------------------------------------------
def test_pending_starts_empty(client):
    resp = client.get("/reviews/pending")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "pending": []}


def test_pending_lists_queued_decision(client):
    _seed(client, _pr_state())
    resp = client.get("/reviews/pending")
    body = resp.json()
    assert body["count"] == 1
    item = body["pending"][0]
    assert item["repo"] == "o/r"
    assert item["action"] == "comment_review"
    assert item["status"] == "pending"
    assert "x" in item["body"]  # the finding is carried in the stored body


# --- approve -----------------------------------------------------------------
def test_approve_executes_github_action(client):
    did = _seed(client, _pr_state())
    resp = client.post(f"/reviews/{did}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "executed"
    # The action actually went through the (fake) client exactly once.
    assert len(client._github.calls) == 1
    assert client._github.calls[0]["op"] == "comment"
    # And it's no longer pending.
    assert client.get("/reviews/pending").json()["count"] == 0


def test_approve_twice_does_not_double_post(client):
    did = _seed(client, _pr_state())
    client.post(f"/reviews/{did}/approve")
    second = client.post(f"/reviews/{did}/approve")
    assert second.status_code == 200
    assert second.json()["status"] == "already_executed"
    assert len(client._github.calls) == 1  # NOT posted a second time


def test_approve_missing_returns_404(client):
    resp = client.post("/reviews/999999/approve")
    assert resp.status_code == 404


def test_approve_failure_marks_failed_and_502(client):
    # A GitHub failure must not be swallowed: 502, decision marked FAILED (so it
    # can be retried), and no false "executed".
    did = _seed(client, _pr_state())

    class FailingGitHub(FakeGitHubClient):
        def post_issue_comment(self, repo, pr_number, body):
            return ActionResult(ok=False, kind="comment", detail="boom 502")

    app.dependency_overrides[get_github] = lambda: FailingGitHub()
    resp = client.post(f"/reviews/{did}/approve")
    assert resp.status_code == 502
    assert resp.json()["status"] == "action_failed"

    s = client._TestSession()
    row = store.get(s, did)
    assert row.status == DecisionStatus.FAILED
    assert "boom" in row.last_error
    s.close()


# --- reject ------------------------------------------------------------------
def test_reject_marks_rejected_and_no_action(client):
    did = _seed(client, _pr_state())
    resp = client.post(f"/reviews/{did}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert client._github.calls == []  # never touched GitHub
    assert client.get("/reviews/pending").json()["count"] == 0


def test_approve_after_reject_is_conflict(client):
    did = _seed(client, _pr_state())
    client.post(f"/reviews/{did}/reject")
    resp = client.post(f"/reviews/{did}/approve")
    assert resp.status_code == 409
    assert client._github.calls == []


def test_reject_after_execute_is_conflict(client):
    did = _seed(client, _pr_state())
    client.post(f"/reviews/{did}/approve")
    resp = client.post(f"/reviews/{did}/reject")
    assert resp.status_code == 409
