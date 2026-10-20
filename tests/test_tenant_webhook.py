"""Tenancy, end to end through the real webhook route.

Unit tests cover the pieces; these cover the claim that matters to a customer:
"if my installation is suspended, nothing of mine runs", and "my work is billed
to me, not to whoever else is on this instance".
"""

from __future__ import annotations

import json

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.security import compute_signature

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TOKEN = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


@pytest.fixture()
def env(monkeypatch):
    """The app under test, with tenancy enabled and a in-memory tenant table."""
    from app.config import settings as live
    from app.main import app, get_db, get_github, get_queue
    from app.models import Base, Installation, PRJob
    from app.queue import JobQueue
    from app.routing.github import FakeGitHubClient

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
    app.dependency_overrides[get_github] = lambda: FakeGitHubClient()
    client = TestClient(app)

    base = {
        "webhook_secret": SECRET,
        "api_token": TOKEN,
        "allow_insecure": True,
        "allowed_repos": "",
        "github_dry_run": True,
        "require_auth_for_reads": False,
        "public_mode": False,
        "quota_jobs_per_repo_per_hour": 0,
        "quota_jobs_total_per_day": 0,
        "quota_jobs_per_installation_per_day": 0,
        "github_app_id": 1234,
        # Not a real key: these tests never sign a JWT, they only check that the
        # tenancy gates fire before any credential would be needed.
        "github_app_private_key": "-----BEGIN RSA PRIVATE KEY-----" + chr(10) + "fake",
        "github_app_slug": "autopr-test",
        "github_comment_commands": True,
    }
    for field, value in base.items():
        monkeypatch.setattr(live, field, value)

    def use(**overrides):
        for field, value in overrides.items():
            monkeypatch.setattr(live, field, value)
        return client

    def deliver(event: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": compute_signature(SECRET, body),
                "Content-Type": "application/json",
            },
        )
        return {"status_code": resp.status_code, "json": resp.json()}

    def pr_payload(
        *, installation_id: int | None = 100, repo: str = "acme/api", sha: str = "a" * 40
    ) -> dict:
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 5,
                "head": {"sha": sha},
                "user": {"login": "contributor"},
            },
            "repository": {"full_name": repo},
        }
        if installation_id is not None:
            payload["installation"] = {
                "id": installation_id,
                "account": {"login": "acme", "type": "Organization", "id": 1},
                "target_type": "Organization",
                "repository_selection": "selected",
            }
        return payload

    install_payload = lambda action, iid=100, login="acme": {  # noqa: E731
        "action": action,
        "installation": {
            "id": iid,
            "account": {"login": login, "type": "Organization", "id": 1},
            "target_type": "Organization",
            "repository_selection": "selected",
        },
    }

    client.deliver = deliver  # type: ignore[attr-defined]
    client.pr_payload = pr_payload  # type: ignore[attr-defined]
    client.install_payload = install_payload  # type: ignore[attr-defined]
    client.use = use  # type: ignore[attr-defined]
    client.Session = Session  # type: ignore[attr-defined]
    client.Installation = Installation  # type: ignore[attr-defined]
    client.PRJob = PRJob  # type: ignore[attr-defined]
    client.auth_headers = {"Authorization": f"Bearer {TOKEN}"}
    yield client
    app.dependency_overrides.clear()


# --- onboarding ---------------------------------------------------------------


def test_installation_event_registers_a_tenant(env):
    out = env.deliver("installation", env.install_payload("created"))
    assert out["json"]["tenancy"] == "active"
    with env.Session() as s:
        rows = list(s.execute(select(env.Installation)).scalars())
    assert [(r.installation_id, r.account_login) for r in rows] == [(100, "acme")]


def test_a_pr_delivery_enrols_the_tenant_without_an_installation_event(env):
    """Webhook ordering must never decide whether a paying tenant works.

    GitHub can deliver `pull_request` before `installation`, or lose the install
    event entirely if our endpoint was down. The payload carries the same object,
    so enrolment happens there too.
    """
    out = env.deliver("pull_request", env.pr_payload())
    assert out["json"]["status"] == "queued"
    with env.Session() as s:
        tenant = s.execute(select(env.Installation)).scalar_one()
        job = s.execute(select(env.PRJob)).scalar_one()
    assert tenant.installation_id == 100
    assert job.installation_id == 100  # the work is attributed to its tenant


# --- the gate that matters ----------------------------------------------------


def test_suspended_tenant_gets_no_work(env):
    env.deliver("installation", env.install_payload("created"))
    env.deliver("installation", env.install_payload("suspend"))
    out = env.deliver("pull_request", env.pr_payload())
    assert out["json"] == {"status": "ignored", "reason": "installation_inactive"}
    with env.Session() as s:
        assert s.execute(select(func.count()).select_from(env.PRJob)).scalar_one() == 0


def test_deleted_tenant_gets_no_work_and_cannot_be_revived_by_a_delivery(env):
    env.deliver("installation", env.install_payload("created"))
    env.deliver("installation", env.install_payload("deleted"))
    # A stray created event (a replay, a GitHub retry) must not re-open the door.
    env.deliver("installation", env.install_payload("created"))
    out = env.deliver("pull_request", env.pr_payload())
    assert out["json"]["reason"] == "installation_inactive"
    with env.Session() as s:
        assert s.execute(select(func.count()).select_from(env.PRJob)).scalar_one() == 0


def test_unsuspend_restores_service(env):
    env.deliver("installation", env.install_payload("created"))
    env.deliver("installation", env.install_payload("suspend"))
    env.deliver("installation", env.install_payload("unsuspend"))
    out = env.deliver("pull_request", env.pr_payload())
    assert out["json"]["status"] == "queued"


# --- billing isolation --------------------------------------------------------


def test_tenant_quota_is_charged_to_the_tenant_that_spent_it(env):
    """Tenant A exhausting its budget must not block tenant B."""
    env.use(quota_jobs_per_installation_per_day=1)
    assert (
        env.deliver("pull_request", env.pr_payload(installation_id=100, sha="a" * 40))["json"][
            "status"
        ]
        == "queued"
    )
    blocked = env.deliver("pull_request", env.pr_payload(installation_id=100, sha="b" * 40))
    assert blocked["status_code"] == 429
    assert blocked["json"]["reason"] == "quota_installation_daily"
    # A different tenant, same instance, is untouched.
    other = env.deliver(
        "pull_request", env.pr_payload(installation_id=200, repo="other/repo", sha="c" * 40)
    )
    assert other["json"]["status"] == "queued"


def test_quota_refusal_creates_no_row(env):
    env.use(quota_jobs_per_installation_per_day=1)
    env.deliver("pull_request", env.pr_payload(sha="a" * 40))
    env.deliver("pull_request", env.pr_payload(sha="b" * 40))
    with env.Session() as s:
        assert s.execute(select(func.count()).select_from(env.PRJob)).scalar_one() == 1


# --- sign-in-free approval ----------------------------------------------------


def test_issue_comment_without_app_credentials_is_a_no_op(env):
    """A dashboard-less instance must not half-handle a command."""
    env.use(github_app_id=None, github_app_private_key="")
    out = env.deliver(
        "issue_comment",
        {
            "action": "created",
            "issue": {"number": 5, "pull_request": {"url": "u"}},
            "comment": {"body": "/autopr approve 1", "user": {"login": "x", "type": "User"}},
            "repository": {"full_name": "acme/api"},
            "installation": {"id": 100},
        },
    )
    assert out["json"] == {"status": "ignored", "reason": "no_app_credentials"}


def test_issue_comment_can_be_switched_off(env):
    env.use(github_comment_commands=False)
    out = env.deliver("issue_comment", {"action": "created"})
    assert out["json"]["reason"] == "comment_commands_disabled"


# --- operator visibility ------------------------------------------------------


def test_installations_endpoint_lists_tenants_with_counts(env):
    env.deliver("installation", env.install_payload("created", 100, "acme"))
    env.deliver("installation", env.install_payload("created", 200, "globex"))
    env.deliver("installation", env.install_payload("suspend", 200, "globex"))
    env.deliver("pull_request", env.pr_payload(installation_id=100))

    resp = env.get("/installations", headers=env.auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["multi_tenant"] is True
    assert body["install_url"].endswith("/autopr-test/installations/new")
    by_id = {row["installation_id"]: row for row in body["installations"]}
    assert by_id[100]["active"] is True and by_id[100]["jobs"] == 1
    assert by_id[200]["active"] is False and by_id[200]["suspended"] is True


def test_stats_advertises_the_install_url(env):
    body = env.get("/stats").json()
    assert body["config"]["multi_tenant"] is True
    assert body["config"]["install_url"].endswith("/autopr-test/installations/new")


# --- downgrade path -----------------------------------------------------------


def test_single_operator_delivery_without_an_installation_still_works(env):
    """A classic webhook (no App) must behave exactly as before tenancy existed."""
    env.use(github_app_id=None, github_app_private_key="")
    out = env.deliver("pull_request", env.pr_payload(installation_id=None))
    assert out["json"]["status"] == "queued"
    with env.Session() as s:
        job = s.execute(select(env.PRJob)).scalar_one()
    assert job.installation_id is None  # operator work, not tenant work
