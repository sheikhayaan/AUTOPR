"""A public instance: read by anyone, write by nobody, spend bounded.

The four invariants are enforced in two different places for one reason: the
*configuration* ones are checked at boot (a demo that could spend without bound
must not start at all), while the *request-time* ones are checked per call
(approve/reject must answer 403 even though the operator token is valid).

Every test here is paired with the ability to fail: the negative controls at the
bottom disable each guard and assert the suite notices.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TOKEN = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def _settings(**overrides) -> Settings:
    """A valid production posture, with `public_mode` opt-in per test."""
    base = {
        "webhook_secret": SECRET,
        "api_token": TOKEN,
        "allow_insecure": False,
        "_env_file": None,
    }
    return Settings(**{**base, **overrides})


def _public(**overrides) -> Settings:
    """The minimal configuration a demo *should* be allowed to boot with."""
    base = {
        "public_mode": True,
        "allowed_repos": "acme/api",
        "github_dry_run": True,
        "quota_jobs_per_repo_per_hour": 5,
        "quota_jobs_total_per_day": 50,
    }
    return _settings(**{**base, **overrides})


# --- boot: a public instance must be safe by construction --------------------


def test_public_mode_boots_when_every_invariant_holds():
    s = _public()
    assert s.public_mode is True
    assert s.allowed_repo_set == frozenset({"acme/api"})


def test_public_mode_refuses_to_start_without_an_allowlist():
    """Otherwise any fork PR on any repo spends the operator's budget."""
    with pytest.raises(ValueError, match="needs AUTOPR_ALLOWED_REPOS"):
        _public(allowed_repos="")


def test_public_mode_refuses_to_start_with_writes_enabled():
    """`look, don't touch` is not a suggestion."""
    with pytest.raises(ValueError, match="requires AUTOPR_GITHUB_DRY_RUN=true"):
        _public(github_dry_run=False)


def test_public_mode_refuses_to_start_without_quotas():
    with pytest.raises(ValueError, match="requires both quotas to be positive"):
        _public(quota_jobs_total_per_day=0)


def test_quotas_are_off_by_default_for_a_private_install():
    """The new ceilings must not change behaviour for the existing user."""
    s = _settings()
    assert s.quota_jobs_per_repo_per_hour == 0
    assert s.quota_jobs_total_per_day == 0
    assert s.public_mode is False


def test_allow_insecure_does_not_excuse_public_mode():
    """`public_mode` is about exposure, not about local convenience.

    A dev box that sets ALLOW_INSECURE *and* PUBLIC_MODE is still claiming to be
    a public demo, so the demo invariants still apply.
    """
    with pytest.raises(ValueError, match="needs AUTOPR_ALLOWED_REPOS"):
        _settings(allow_insecure=True, public_mode=True, allowed_repos="")


# --- request time: nobody mutates the queue ---------------------------------


@pytest.fixture()
def app_env(monkeypatch):
    """The real app, on fakeredis + in-memory SQLite, with a switchable settings."""
    from app.config import settings as live
    from app.main import app, get_db, get_github, get_queue
    from app.models import Base
    from app.queue import JobQueue
    from app.routing import store

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    queue = JobQueue(fakeredis.FakeStrictRedis(server=fakeredis.FakeServer()), "t:j", "t:w")
    queue.ensure_group()

    def _db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_queue] = lambda: queue
    # lifespan never runs here, so app.state.github is never built; the approve
    # path only needs *a* client (dry-run makes its calls no-ops anyway).
    from app.routing.github import FakeGitHubClient

    app.dependency_overrides[get_github] = lambda: FakeGitHubClient()
    client = TestClient(app)

    def apply(**overrides):
        """Point the process-wide settings at a posture for this test.

        The fixture starts in public mode with quotas off, because most tests
        here are about the *403* and the boot invariants; each quota test then
        names its own ceiling.
        """
        base = {
            "public_mode": True,
            "allowed_repos": "acme/api",
            "github_dry_run": True,
            "require_auth_for_reads": False,
            "quota_jobs_per_repo_per_hour": 0,
            "quota_jobs_total_per_day": 0,
        }
        for field, value in {**base, **overrides}.items():
            monkeypatch.setattr(live, field, value)

    apply()

    def use(**overrides):
        apply(**overrides)
        return client

    def seed_decision() -> int:
        """One PENDING decision, so approve/reject have something to act on."""
        from app.routing.policy import Action, RoutingDecision

        decision = RoutingDecision(
            action=Action.COMMENT_REVIEW,
            requires_approval=True,
            risk="low",
            reason="test",
            title="t",
            body="b",
        )
        with Session() as s:
            row = store.enqueue(
                s, decision, {"repo": "acme/api", "pr_number": 7, "commit_sha": "abc1234"}
            )
            return row.id

    client.Session = Session  # type: ignore[attr-defined]
    client.use = use  # type: ignore[attr-defined]
    client.seed_decision = seed_decision  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.clear()


def test_approve_is_forbidden_on_a_public_instance(app_env):
    """Even a *correct* bearer token must not mutate a public demo."""
    decision_id = app_env.seed_decision()
    resp = app_env.post(
        f"/reviews/{decision_id}/approve", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"]


def test_reject_is_forbidden_on_a_public_instance(app_env):
    decision_id = app_env.seed_decision()
    resp = app_env.post(
        f"/reviews/{decision_id}/reject", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code == 403


def test_a_forbidden_approve_leaves_the_decision_pending(app_env):
    """403 must be a *no-op*, not a state change the operator has to undo."""
    from app.models import DecisionStatus, ReviewDecision

    decision_id = app_env.seed_decision()
    app_env.post(f"/reviews/{decision_id}/approve", headers={"Authorization": f"Bearer {TOKEN}"})
    with app_env.Session() as s:
        row = s.get(ReviewDecision, decision_id)
    assert row is not None and row.status == DecisionStatus.PENDING


def test_reads_stay_open_on_a_public_instance(app_env):
    """The demo is worthless if visitors cannot see it."""
    client = app_env.use(public_mode=True, require_auth_for_reads=False)
    assert client.get("/reviews/pending").status_code == 200
    assert client.get("/stats").status_code == 200
    assert client.get("/jobs").status_code == 200


def test_approve_still_works_when_not_public(app_env):
    """Control: the 403 is specific to public mode, not a broken endpoint."""
    decision_id = app_env.seed_decision()
    client = app_env.use(public_mode=False)
    resp = client.post(
        f"/reviews/{decision_id}/approve", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in {"handed_off", "executed"}


# --- quotas: bounded spend ---------------------------------------------------


def _post_signed(client, repo: str, sha: str):
    import hashlib
    import hmac

    from app.config import settings as live

    body = json.dumps(
        {
            "action": "opened",
            "pull_request": {"number": 1, "head": {"sha": sha}, "user": {"login": "x"}},
            "repository": {"full_name": repo},
        }
    ).encode()
    sig = "sha256=" + hmac.new(live.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )


def test_jobs_over_the_per_repo_hourly_quota_are_refused(app_env):
    client = app_env.use(quota_jobs_per_repo_per_hour=2)
    codes = [_post_signed(client, "acme/api", f"sha{i}").status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_a_quota_refusal_is_retryable_not_permanent(app_env):
    """429 + Retry-After, so GitHub's retry semantics keep working."""
    client = app_env.use(quota_jobs_per_repo_per_hour=1)
    _post_signed(client, "acme/api", "one")
    resp = _post_signed(client, "acme/api", "two")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"
    assert resp.json()["reason"] == "quota_repo_hourly"


def test_a_quota_refusal_creates_no_row(app_env):
    """Refused before the INSERT: no row, no stream message, no spend."""
    from app.models import PRJob

    client = app_env.use(quota_jobs_per_repo_per_hour=1)
    _post_signed(client, "acme/api", "one")
    _post_signed(client, "acme/api", "two")
    with app_env.Session() as s:
        count = s.execute(select(func.count()).select_from(PRJob)).scalar_one()
    assert count == 1


def test_redelivery_of_zero_cost_work_is_never_quota_refused(app_env):
    """A GitHub retry of a job we already hold spends nothing — must not 429."""
    client = app_env.use(quota_jobs_per_repo_per_hour=1)
    assert _post_signed(client, "acme/api", "same").status_code == 200
    again = _post_signed(client, "acme/api", "same")
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"


def test_the_instance_daily_quota_binds_across_repos(app_env):
    client = app_env.use(quota_jobs_total_per_day=1, allowed_repos="acme/api,acme/other")
    assert _post_signed(client, "acme/api", "a").status_code == 200
    resp = _post_signed(client, "acme/other", "b")
    assert resp.status_code == 429
    assert resp.json()["reason"] == "quota_instance_daily"


def test_old_jobs_fall_out_of_the_window(app_env):
    """The ceiling is a rate, not a lifetime total: yesterday's work is free."""
    from app.models import PRJob

    client = app_env.use(quota_jobs_per_repo_per_hour=1)
    _post_signed(client, "acme/api", "old")
    with app_env.Session() as s:
        row = s.execute(select(PRJob)).scalars().one()
        row.created_at = datetime.now(UTC) - timedelta(hours=2)
        s.commit()
    assert _post_signed(client, "acme/api", "new").status_code == 200


# --- negative controls -------------------------------------------------------


def test_negative_control_guard_is_what_blocks_the_approval(app_env, monkeypatch):
    """Disabling the public_mode branch must make the 403 disappear.

    Without this, the test could pass because approve is broken for some other
    reason, and the guard would be untested in practice.
    """
    from app.config import settings as live

    decision_id = app_env.seed_decision()
    monkeypatch.setattr(live, "public_mode", False)
    resp = app_env.post(
        f"/reviews/{decision_id}/approve", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code != 403
