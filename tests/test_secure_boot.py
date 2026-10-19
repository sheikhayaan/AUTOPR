"""Guards for the two settings whose absence turns a misconfig into someone else's write.

Both failure modes are silent at request time, which is why they are tested here
rather than trusted to a warning in the logs:

  * an empty ``AUTOPR_API_TOKEN`` makes ``/reviews/{id}/approve`` answer anyone,
    so the human gate becomes decorative;
  * an empty ``AUTOPR_ALLOWED_REPOS`` lets any repository able to deliver a
    signed webhook spend LLM budget — which on a public repo means any stranger
    who opens a fork PR.

The first is enforced at boot (a ``ValueError`` from the settings validator), the
second at ingest (a signed delivery is answered ``ignored`` and creates no row).
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.ingest import repo_is_allowed

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TOKEN = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def _settings(**overrides) -> Settings:
    """Build Settings from explicit values, ignoring the ambient .env."""
    base = {
        "webhook_secret": SECRET,
        "api_token": TOKEN,
        "allow_insecure": False,
        "_env_file": None,  # do not read a developer's .env
    }
    return Settings(**{**base, **overrides})


# --- fail-fast: empty API token ---------------------------------------------


def test_empty_api_token_refuses_to_boot():
    """An unauthenticated approval endpoint must not be a silent default."""
    with pytest.raises(ValueError, match="AUTOPR_API_TOKEN is empty"):
        _settings(api_token="")


def test_whitespace_api_token_is_treated_as_empty():
    """`AUTOPR_API_TOKEN=   ` from a shell heredoc must not slip through."""
    with pytest.raises(ValueError, match="AUTOPR_API_TOKEN is empty"):
        _settings(api_token="   ")


def test_allow_insecure_is_the_documented_escape_hatch():
    """Local/dev/test runs (and the test suite itself) opt out deliberately."""
    s = _settings(api_token="", allow_insecure=True)
    assert s.api_token == ""


def test_placeholder_webhook_secret_still_fails_first():
    """The pre-existing guard is unchanged, and reports its own variable."""
    with pytest.raises(ValueError, match="AUTOPR_WEBHOOK_SECRET is still the placeholder"):
        _settings(webhook_secret="changeme-generate-a-real-secret")


def test_a_fully_configured_settings_object_boots():
    """Control case: both settings present => construction succeeds."""
    s = _settings()
    assert s.api_token == TOKEN
    assert s.allow_insecure is False


# --- allowlist parsing -------------------------------------------------------


def test_allowed_repos_parses_csv_case_insensitively():
    """GitHub logins and repo names are case-insensitive; so is the allowlist."""
    s = _settings(allowed_repos=" Acme/API , psf/requests ,,")
    assert s.allowed_repo_set == frozenset({"acme/api", "psf/requests"})


def test_empty_allowlist_means_allow_all(monkeypatch):
    """Default posture is unchanged: a personal install keeps working."""
    monkeypatch.setattr("app.ingest.settings", _settings(allowed_repos=""))
    assert repo_is_allowed("anyone/anything") is True


def test_allowlist_denies_an_unlisted_repo(monkeypatch):
    monkeypatch.setattr("app.ingest.settings", _settings(allowed_repos="acme/api"))
    assert repo_is_allowed("acme/api") is True
    assert repo_is_allowed("ACME/API") is True
    assert repo_is_allowed("stranger/fork") is False
    # Prefix matching would be a hole: "acme/api-evil" must not match "acme/api".
    assert repo_is_allowed("acme/api-evil") is False


# --- the same deny, end to end through the webhook ---------------------------


@pytest.fixture()
def webhook_client():
    """The real app with fakeredis + in-memory SQLite, plus its session factory."""
    import fakeredis
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.main import app, get_db, get_queue
    from app.models import Base
    from app.queue import JobQueue

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    queue = JobQueue(
        fakeredis.FakeStrictRedis(server=fakeredis.FakeServer()), stream="t:j", group="t:w"
    )
    queue.ensure_group()

    def _db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_queue] = lambda: queue
    client = TestClient(app)  # no `with`: skip lifespan (no real Redis)
    yield client, Session
    app.dependency_overrides.clear()


def _post_signed(client, payload: dict, secret: str):
    """POST a correctly signed delivery, exactly as GitHub would."""
    import hashlib
    import hmac

    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )


def _pr_payload(repo: str, sha: str = "abc1234def") -> dict:
    return {
        "action": "opened",
        "pull_request": {"number": 7, "head": {"sha": sha}, "user": {"login": "stranger"}},
        "repository": {"full_name": repo},
    }


def test_webhook_from_a_disallowed_repo_creates_no_job(webhook_client, monkeypatch):
    """A signed delivery we did not ask for must not create a row or spend budget."""
    from sqlalchemy import func, select

    from app.config import settings as live
    from app.models import PRJob

    client, Session = webhook_client
    monkeypatch.setattr("app.ingest.settings", _settings(allowed_repos="acme/api"))

    resp = _post_signed(client, _pr_payload("stranger/fork"), live.webhook_secret)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "repo_not_allowed"}
    with Session() as s:
        count = s.execute(
            select(func.count()).select_from(PRJob).where(PRJob.repo == "stranger/fork")
        ).scalar_one()
    assert count == 0


def test_webhook_from_an_allowed_repo_still_queues(webhook_client, monkeypatch):
    """The allowlist must not break the happy path it sits in front of."""
    from app.config import settings as live

    client, _Session = webhook_client
    monkeypatch.setattr("app.ingest.settings", _settings(allowed_repos="acme/api"))

    resp = _post_signed(client, _pr_payload("acme/api", sha="feedface01"), live.webhook_secret)

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["job_id"] > 0
