"""Tests for the read-only dashboard API (/stats, /jobs, /reviews).

These are projections the operator UI renders; they must never mutate state and
must reflect exactly what the pipeline recorded. Infra-free: in-memory SQLite
(StaticPool) injected via the same dependency-override pattern the ops-API tests
use, no network, no Postgres.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app, get_db
from app.models import Base, JobStatus, PRJob
from app.routing import store
from app.routing.policy import route


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

    def _db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    c = TestClient(app)
    c._TestSession = TestSession  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _add_job(
    client, *, status: JobStatus, sha: str, pr: int, repo: str = "octocat/hello-world"
) -> int:
    s = client._TestSession()
    try:
        job = PRJob(
            dedup_key=f"k-{sha}-{pr}",
            repo=repo,
            pr_number=pr,
            commit_sha=sha,
            author="octocat",
            event="pull_request",
            status=status,
            attempts=1,
        )
        s.add(job)
        s.commit()
        return job.id
    finally:
        s.close()


def _pr_state(risk: str, sha: str, pr: int = 7, repo: str = "octocat/hello-world") -> dict:
    return {
        "repo": repo,
        "pr_number": pr,
        "commit_sha": sha,
        "changed_files": [{"path": "a.py", "patch": "+x"}],
        "review_findings": [{"file": "a.py", "line": 1, "severity": "error", "message": "x"}],
        "risk_score": risk,
        "summary": "s",
    }


def _queue_decision(client, state: dict) -> int:
    s = client._TestSession()
    try:
        row = store.enqueue(s, route(state), state)
        return row.id
    finally:
        s.close()


# --- /stats ------------------------------------------------------------------
def test_stats_empty(client):
    body = client.get("/stats").json()
    assert body["jobs"]["total"] == 0
    assert body["reviews"]["total"] == 0
    # Config surface is always present so the UI can render the dry-run banner.
    assert "github_dry_run" in body["config"]
    assert "auto_comment_max_risk" in body["config"]
    # The web host the dashboard links PRs to, derived from the API base so the
    # frontend never hard-codes github.com.
    assert body["config"]["github_web_url"] == "https://github.com"


def test_stats_counts_by_status(client):
    _add_job(client, status=JobStatus.DONE, sha="a", pr=1)
    _add_job(client, status=JobStatus.DONE, sha="b", pr=2)
    _add_job(client, status=JobStatus.DEAD, sha="c", pr=3)
    _queue_decision(client, _pr_state("high", "d", pr=4))  # PENDING (human-gated)

    body = client.get("/stats").json()
    assert body["jobs"]["total"] == 3
    assert body["jobs"]["done"] == 2
    assert body["jobs"]["dead"] == 1
    assert body["reviews"]["pending"] == 1
    assert body["reviews"]["total"] == 1


# --- /jobs -------------------------------------------------------------------
def test_jobs_lists_newest_first(client):
    _add_job(client, status=JobStatus.DONE, sha="old", pr=1)
    _add_job(client, status=JobStatus.QUEUED, sha="new", pr=2)

    body = client.get("/jobs").json()
    assert body["count"] == 2
    # Newest (highest id / latest created) first.
    assert body["jobs"][0]["pr_number"] == 2
    assert body["jobs"][0]["status"] == "queued"
    assert body["jobs"][1]["status"] == "done"


def test_jobs_limit_is_clamped(client):
    for i in range(3):
        _add_job(client, status=JobStatus.DONE, sha=f"s{i}", pr=i)
    body = client.get("/jobs?limit=2").json()
    assert body["count"] == 2
    # total is pre-paging, so the UI can render a pager.
    assert body["total"] == 3


def test_jobs_paging_walks_the_whole_set(client):
    for i in range(5):
        _add_job(client, status=JobStatus.DONE, sha=f"s{i}", pr=i + 1)
    page1 = client.get("/jobs?limit=2&offset=0").json()
    page2 = client.get("/jobs?limit=2&offset=2").json()
    page3 = client.get("/jobs?limit=2&offset=4").json()
    seen = [j["id"] for page in (page1, page2, page3) for j in page["jobs"]]
    assert len(seen) == 5 and len(set(seen)) == 5  # no overlap, no gap
    assert page3["count"] == 1
    assert all(p["total"] == 5 for p in (page1, page2, page3))


def test_jobs_search_matches_repo_and_sha(client):
    _add_job(client, status=JobStatus.DONE, sha="deadbeef", pr=1, repo="acme/api")
    _add_job(client, status=JobStatus.DONE, sha="cafebabe", pr=2, repo="acme/web")

    body = client.get("/jobs?q=deadbe").json()
    assert body["total"] == 1 and body["jobs"][0]["commit_sha"] == "deadbeef"
    assert client.get("/jobs?q=WEB").json()["total"] == 1  # case-insensitive
    # LIKE metacharacters are literals, not wildcards.
    assert client.get("/jobs?q=%25").json()["total"] == 0


def test_jobs_filter_by_status(client):
    _add_job(client, status=JobStatus.DONE, sha="a", pr=1)
    _add_job(client, status=JobStatus.DEAD, sha="b", pr=2)
    assert client.get("/jobs?status=dead").json()["total"] == 1
    assert client.get("/jobs?status=bogus").json()["total"] == 0


# --- /reviews (history) ------------------------------------------------------
def test_reviews_history_lists_all(client):
    _queue_decision(client, _pr_state("high", "x1", pr=1))
    _queue_decision(client, _pr_state("medium", "x2", pr=2))
    body = client.get("/reviews").json()
    assert body["count"] == 2
    # View carries the fields the UI needs, including timestamps.
    assert set(body["reviews"][0]).issuperset({"id", "risk", "status", "body", "created_at"})


def test_reviews_filter_by_status(client):
    _queue_decision(client, _pr_state("high", "x1", pr=1))
    # Only PENDING exist; filtering to executed yields none.
    assert client.get("/reviews?status=executed").json()["count"] == 0
    assert client.get("/reviews?status=pending").json()["count"] == 1


def test_reviews_unknown_status_is_empty_not_error(client):
    resp = client.get("/reviews?status=bogus")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0 and body["reviews"] == [] and body["total"] == 0


def test_reviews_paging_and_search(client):
    for i in range(4):
        _queue_decision(client, _pr_state("high", f"x{i}", pr=i + 1, repo="acme/api"))
    _queue_decision(client, _pr_state("high", "y9", pr=9, repo="other/svc"))

    page = client.get("/reviews?limit=2&offset=2").json()
    assert page["count"] == 2 and page["total"] == 5
    assert client.get("/reviews?q=acme").json()["total"] == 4
    assert client.get("/reviews?q=other").json()["total"] == 1
    combined = client.get("/reviews?q=acme&status=executed").json()
    assert combined["total"] == 0  # filters compose
