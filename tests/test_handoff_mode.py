"""Phase 13: hand-off mode — tokenless reads, zero writes, forced human-gating.

Two layers of proof:
  * unit — the two client/reader factories and the PR router honour the flag;
  * HTTP — approving in hand-off mode records `handed_off` with a review_url and
    never touches GitHub, driving the real FastAPI app.

Infra-free, matching the rest of the suite: in-memory SQLite (StaticPool) + a
FakeGitHubClient injected via dependency override. The `handoff` fixture flips
the process-wide settings singleton and restores it, the same mutate-the-shared-
settings approach conftest and test_auth already use.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.routing.github as gh
import app.routing.policy as policy
from app.config import settings
from app.main import app, get_db, get_github
from app.models import Base, DecisionStatus
from app.routing import store
from app.routing.github import FakeGitHubClient, FakeGitHubReader, HttpGitHubReader
from app.routing.policy import route


@pytest.fixture()
def handoff():
    """Turn hand-off mode on for one test, restoring the prior value after."""
    prev = settings.handoff_mode
    settings.handoff_mode = True
    try:
        yield
    finally:
        settings.handoff_mode = prev


# --- unit: factory + router gates -------------------------------------------
def test_reader_is_anonymous_http_without_token(handoff, monkeypatch):
    # No token, hand-off on => real HTTP reader with an empty token (anonymous),
    # and the header set omits Authorization so public-repo reads work.
    monkeypatch.setattr(settings, "github_token", "", raising=False)
    reader = gh.get_github_reader()
    assert isinstance(reader, HttpGitHubReader)
    assert reader.token == ""
    assert "Authorization" not in reader._headers()


def test_client_never_writes_even_with_token(handoff, monkeypatch):
    # A token AND dry-run off would normally yield the real HTTP client; hand-off
    # must still force the no-op client so AutoPR cannot write.
    monkeypatch.setattr(settings, "github_token", "ghp_secret", raising=False)
    monkeypatch.setattr(settings, "github_dry_run", False, raising=False)
    assert isinstance(gh.get_github_client(), FakeGitHubClient)


def test_pr_review_is_always_gated_in_handoff(handoff, monkeypatch):
    # Even a trivial-risk review (normally auto-posted) must require approval.
    monkeypatch.setattr(settings, "auto_comment_max_risk", "high", raising=False)
    decision = policy._route_pr(
        {"risk_score": "trivial", "summary": "s", "review_findings": []}
    )
    assert decision.requires_approval is True


def test_off_by_default_preserves_existing_paths(monkeypatch):
    # Hand-off off + no token => the fake reader (unchanged behaviour). The
    # `handoff` fixture is intentionally NOT used here.
    monkeypatch.setattr(settings, "handoff_mode", False, raising=False)
    monkeypatch.setattr(settings, "github_token", "", raising=False)
    assert isinstance(gh.get_github_reader(), FakeGitHubReader)


# --- HTTP: approve hands off without writing --------------------------------
def _pr_state(risk: str = "high") -> dict:
    return {
        "repo": "octocat/hello",
        "pr_number": 7,
        "commit_sha": "c0mm1t",
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
    fake_gh = FakeGitHubClient()

    def _db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_github] = lambda: fake_gh

    c = TestClient(app)
    c._TestSession = TestSession  # type: ignore[attr-defined]
    c._github = fake_gh  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _seed(client, state: dict) -> int:
    s = client._TestSession()
    try:
        row = store.enqueue(s, route(state), state)
        return row.id
    finally:
        s.close()


def test_approve_in_handoff_marks_handed_off_without_writing(client, handoff):
    did = _seed(client, _pr_state())
    resp = client.post(f"/reviews/{did}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "handed_off"
    assert body["url"].endswith("/octocat/hello/pull/7/files")
    assert body["decision"]["review_url"].endswith("/octocat/hello/pull/7/files")
    # The whole point: GitHub was never touched.
    assert client._github.calls == []
    # And it leaves the pending queue (marked executed).
    assert client.get("/reviews/pending").json()["count"] == 0
    s = client._TestSession()
    assert store.get(s, did).status == DecisionStatus.EXECUTED
    s.close()


def test_decision_view_exposes_review_url_regardless_of_mode(client):
    # review_url is always serialized (the frontend falls back to it); it points
    # at the PR's files/review screen.
    _seed(client, _pr_state())
    item = client.get("/reviews/pending").json()["pending"][0]
    assert item["review_url"].endswith("/octocat/hello/pull/7/files")


def test_stats_reports_handoff_flag(client, handoff):
    cfg = client.get("/stats").json()["config"]
    assert cfg["handoff_mode"] is True
