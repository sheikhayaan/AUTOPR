"""Tests for the Phase 7 security perimeter.

Covers the auth boundary the ops API grew: a configured bearer token makes
approve/reject reject unauthenticated callers (401), reads stay open unless
explicitly gated, the per-IP fixed-window limiter trips with 429 + Retry-After,
and the fail-fast validator refuses to start on the placeholder webhook secret.

Drives the real FastAPI app the same way test_reviews_api does (in-memory
SQLite + an injected FakeGitHubClient), and unit-tests the limiter and settings
validator directly where that is cleaner and deterministic.

`settings` is the process-wide singleton the dependencies read live, so tests
mutate it via monkeypatch (auto-restored) rather than reconstructing the app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import _PLACEHOLDER_WEBHOOK_SECRET, Settings, settings
from app.main import _mutation_limiter, app, get_db, get_github
from app.models import Base
from app.ratelimit import FixedWindowRateLimiter
from app.routing import store
from app.routing.github import FakeGitHubClient
from app.routing.policy import route

TOKEN = "test-secret-token-abc123"


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


def _seed(client, state: dict | None = None) -> int:
    """Queue a decision the way the router would, on the app's DB."""
    s = client._TestSession()
    try:
        row = store.enqueue(s, route(state or _pr_state()), state or _pr_state())
        return row.id
    finally:
        s.close()


# --- bearer auth on the write path -------------------------------------------
def test_approve_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    did = _seed(client)
    resp = client.post(f"/reviews/{did}/approve")  # no Authorization header
    assert resp.status_code == 401
    assert client._github.calls == []  # never reached GitHub


def test_approve_rejects_bad_token(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    did = _seed(client)
    resp = client.post(
        f"/reviews/{did}/approve",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401
    assert client._github.calls == []


def test_approve_rejects_wrong_scheme(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    did = _seed(client)
    resp = client.post(
        f"/reviews/{did}/approve",
        headers={"Authorization": TOKEN},  # missing the "Bearer " prefix
    )
    assert resp.status_code == 401


def test_approve_succeeds_with_correct_token(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    did = _seed(client)
    resp = client.post(
        f"/reviews/{did}/approve",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "executed"
    assert len(client._github.calls) == 1


def test_reject_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    did = _seed(client)
    resp = client.post(f"/reviews/{did}/reject")
    assert resp.status_code == 401


def test_reject_succeeds_with_correct_token(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    did = _seed(client)
    resp = client.post(
        f"/reviews/{did}/reject",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_empty_token_leaves_write_path_open(client, monkeypatch):
    # The documented dev no-op: empty AUTOPR_API_TOKEN => no auth required, so
    # the existing unauthenticated tests (and local demos) keep working.
    monkeypatch.setattr(settings, "api_token", "")
    did = _seed(client)
    resp = client.post(f"/reviews/{did}/approve")
    assert resp.status_code == 200


# --- read gating -------------------------------------------------------------
def test_reads_open_by_default_even_with_token(client, monkeypatch):
    # require_auth_for_reads defaults False: reads need no token even when a
    # write token is configured, so the read-only dashboard demos freely.
    monkeypatch.setattr(settings, "api_token", TOKEN)
    for path in ("/stats", "/jobs", "/reviews", "/reviews/pending"):
        assert client.get(path).status_code == 200, path


def test_reads_gated_when_flag_set(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(settings, "require_auth_for_reads", True)
    assert client.get("/stats").status_code == 401
    ok = client.get("/stats", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


# --- rate limiting (via the mutation endpoint) -------------------------------
def test_mutation_rate_limit_trips_429(client, monkeypatch):
    # Isolate the limiter: auth off (empty token), tiny limit, fresh buckets.
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(_mutation_limiter, "limit", 2)
    _mutation_limiter.reset()
    try:
        # A missing id 404s, but the rate-limit dependency runs first and counts,
        # so we don't need real rows to exercise the limiter.
        r1 = client.post("/reviews/999999/approve")
        r2 = client.post("/reviews/999999/approve")
        r3 = client.post("/reviews/999999/approve")
        assert (r1.status_code, r2.status_code) == (404, 404)
        assert r3.status_code == 429
        assert "Retry-After" in r3.headers
    finally:
        _mutation_limiter.reset()


# --- limiter unit (deterministic, injected clock) ----------------------------
def test_fixed_window_allows_up_to_limit_then_blocks():
    rl = FixedWindowRateLimiter(limit=3, window_s=60.0)
    assert rl.allow("k", now=100.0) == (True, 0)
    assert rl.allow("k", now=101.0) == (True, 0)
    assert rl.allow("k", now=102.0) == (True, 0)
    allowed, retry = rl.allow("k", now=103.0)
    assert allowed is False
    assert retry >= 1
    # Once the window fully elapses, the key gets a fresh budget.
    assert rl.allow("k", now=161.0) == (True, 0)


def test_fixed_window_keys_are_independent():
    rl = FixedWindowRateLimiter(limit=1, window_s=60.0)
    assert rl.allow("a", now=0.0)[0] is True
    assert rl.allow("a", now=1.0)[0] is False
    assert rl.allow("b", now=1.0)[0] is True  # distinct key, own budget


# --- fail-fast webhook-secret validator --------------------------------------
def test_placeholder_secret_refused_when_not_insecure():
    # Init kwargs outrank env/.env in pydantic-settings, so this exercises the
    # validator regardless of the test env's AUTOPR_ALLOW_INSECURE=1.
    with pytest.raises(ValidationError):
        Settings(webhook_secret=_PLACEHOLDER_WEBHOOK_SECRET, allow_insecure=False)


def test_placeholder_secret_allowed_when_insecure():
    s = Settings(webhook_secret=_PLACEHOLDER_WEBHOOK_SECRET, allow_insecure=True)
    assert s.allow_insecure is True


def test_real_secret_passes_without_insecure():
    s = Settings(webhook_secret="a-real-secret-value", allow_insecure=False)
    assert s.webhook_secret == "a-real-secret-value"


# --- CORS allowlist parsing --------------------------------------------------
def test_cors_origin_list_parses_and_trims():
    s = Settings(cors_origins=" http://a ,http://b , ", allow_insecure=True)
    assert s.cors_origin_list == ["http://a", "http://b"]


def test_cors_wildcard_collapses_to_star():
    s = Settings(cors_origins="http://a,*", allow_insecure=True)
    assert s.cors_origin_list == ["*"]
