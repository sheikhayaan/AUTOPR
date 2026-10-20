"""Tenancy tests: token minting, per-tenant isolation, lifecycle, and quotas.

Everything here runs against a fake HTTP transport, so the tests exercise the
real code paths (JWT signing with a real RSA key, real GitHub response parsing,
real cache logic) without touching the network. The point of the fake key is that
RS256 signing is the part people get wrong; mocking it away would test nothing.
"""

from __future__ import annotations

import itertools
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Base, PRJob, ReviewDecision
from app.quota import quota_reason
from app.tenancy import apply_event, apply_repositories_event, get, get_active, upsert


@pytest.fixture(scope="module")
def private_key() -> str:
    """A real 2048-bit RSA key in PEM form — what the App portal hands out."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


class FakeTransport(httpx.BaseTransport):
    """A real httpx transport, so the client code under test runs unchanged.

    Faking at the transport layer (rather than patching `httpx.post`) means the
    tests still exercise header construction, JSON encoding and status handling.
    """

    def __init__(self, routes: dict[str, tuple[int, dict]] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str, dict]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), dict(request.headers)))
        # Longest matching fragment wins, so a specific route (one installation's
        # token endpoint) beats a generic one ("access_tokens").
        matches = [(f, r) for f, r in self.routes.items() if f in str(request.url)]
        if matches:
            _, (status, payload) = max(matches, key=lambda item: len(item[0]))
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"message": "Not Found"})

    # Small conveniences so route tables stay readable in each test.
    def respond(self, fragment: str, status: int, payload: dict) -> None:
        self.routes[fragment] = (status, payload)


# --- app auth internals -------------------------------------------------------


def test_app_jwt_is_backdated_and_short_lived(private_key):
    """A JWT valid in the future is refused by GitHub; a long one is a liability."""
    import jwt as pyjwt

    from app.github_app import app_jwt

    now = 1_700_000_000.0
    token = app_jwt(12345, private_key, now=now)
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(now) - 60  # clock skew allowance
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's hard ceiling


def test_private_key_with_escaped_newlines_is_accepted(private_key):
    """Env vars are single-line; a key that works only multi-line breaks every deploy."""
    from app.github_app import normalise_private_key

    flattened = private_key.replace("\n", "\\n")
    assert normalise_private_key(flattened) == private_key


def test_non_pem_private_key_fails_with_an_actionable_message():
    from app.github_app import GitHubAppError, normalise_private_key

    with pytest.raises(GitHubAppError, match="PEM"):
        normalise_private_key("just-some-string")


def test_tokens_are_minted_once_and_reused(private_key, monkeypatch):
    """Caching matters: minting per write would add latency and burn rate limit."""
    from app.github_app import AppAuth

    transport = FakeTransport({"access_tokens": (201, {"token": "ghs_abc"})})
    auth = AppAuth(99, private_key, transport=transport)
    assert auth.installation_token(7) == "ghs_abc"
    assert auth.installation_token(7) == "ghs_abc"
    assert len(transport.calls) == 1

    # A different installation must not receive the first one's token.
    transport.respond("/app/installations/8/access_tokens", 201, {"token": "ghs_other"})
    assert auth.installation_token(8) == "ghs_other"


def test_token_mint_failure_is_a_loud_error(private_key):
    from app.github_app import AppAuth, GitHubAppError

    transport = FakeTransport({"access_tokens": (401, {"message": "Bad credentials"})})
    auth = AppAuth(99, private_key, transport=transport)
    with pytest.raises(GitHubAppError, match="HTTP 401"):
        auth.installation_token(7)


def test_invalidate_drops_a_cached_token(private_key):
    """GitHub can revoke a token early; the cache must be droppable."""
    from app.github_app import AppAuth

    transport = FakeTransport({"access_tokens": (201, {"token": "ghs_1"})})
    auth = AppAuth(99, private_key, transport=transport)
    assert auth.installation_token(5) == "ghs_1"
    auth.invalidate(5)
    transport.respond("access_tokens", 201, {"token": "ghs_2"})
    assert auth.installation_token(5) == "ghs_2"


def test_expiry_parsing_refreshes_before_the_deadline(private_key):
    """A token expiring mid-request is a 401 on a user's PR; refresh early."""
    from app.github_app import AppAuth

    soon = (datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    transport = FakeTransport({"access_tokens": (201, {"token": "t", "expires_at": soon})})
    auth = AppAuth(99, private_key, transport=transport)
    tok = auth.fetch_installation_token(3)
    # 10 minutes out, minus the 60s margin: ~9 minutes of usable life.
    assert 0 < tok.expires_at - time.monotonic() <= 9 * 60 + 1


# --- repository -> installation resolution ------------------------------------


def test_repo_installation_is_resolved_from_github_not_a_local_table(private_key):
    from app.github_app import AppAuth

    transport = FakeTransport({"/repos/acme/api/installation": (200, {"id": 4242})})
    auth = AppAuth(99, private_key, transport=transport)
    assert auth.repo_installation("acme/api") == 4242
    # Second call is served from cache — the worker would otherwise ask per write.
    assert auth.repo_installation("acme/api") == 4242
    assert len(transport.calls) == 1


def test_repo_without_the_app_resolves_to_none(private_key):
    from app.github_app import AppAuth

    transport = FakeTransport({})
    auth = AppAuth(99, private_key, transport=transport)
    assert auth.repo_installation("stranger/repo") is None


def test_tenant_client_refuses_to_write_when_the_app_is_not_installed(private_key, monkeypatch):
    """The critical one: no fallback to an instance-wide token, ever.

    Needs live mode: in a dry run the client answers before resolving anything,
    because a demo should not require an installation to demonstrate the flow.
    """
    from app.github_app import AppAuth, TenantGitHubClient

    monkeypatch.setattr("app.config.settings.github_dry_run", False)

    transport = FakeTransport({})
    auth = AppAuth(99, private_key, transport=transport)
    result = TenantGitHubClient(auth).post_issue_comment("stranger/repo", 1, "hi")
    assert result.ok is False
    assert "not installed" in (result.detail or "")
    # No comment POST was attempted at all.
    assert all(method == "GET" for method, _, _ in transport.calls)


def test_tenant_client_uses_the_owning_installation_token(private_key, monkeypatch):
    from app.github_app import AppAuth, TenantGitHubClient

    monkeypatch.setattr("app.config.settings.github_dry_run", False)

    transport = FakeTransport(
        {
            "/repos/acme/api/installation": (200, {"id": 55}),
            "/app/installations/55/access_tokens": (201, {"token": "ghs_tenant"}),
            "/repos/acme/api/issues/7/comments": (201, {"html_url": "u"}),
        }
    )
    auth = AppAuth(99, private_key, transport=transport)
    result = TenantGitHubClient(auth).post_issue_comment("acme/api", 7, "body")
    assert result.ok is True
    # The comment POST carried the *installation's* token, not the App JWT.
    # httpx lower-cases header names on the wire.
    posted = [h for m, u, h in transport.calls if "/comments" in u]
    assert posted and posted[0].get("authorization") == "Bearer ghs_tenant"


def test_dry_run_makes_no_network_write_even_with_a_tenant_configured(private_key, monkeypatch):
    """Dry run is the default and must mean zero HTTP writes, tenancy or not."""
    from app.github_app import AppAuth, TenantGitHubClient

    monkeypatch.setattr("app.config.settings.github_dry_run", True)
    transport = FakeTransport(
        {
            "/repos/acme/api/installation": (200, {"id": 55}),
            "/app/installations/55/access_tokens": (201, {"token": "ghs_tenant"}),
            "/repos/acme/api/issues/7/comments": (201, {"html_url": "u"}),
        }
    )
    auth = AppAuth(99, private_key, transport=transport)
    result = TenantGitHubClient(auth).post_issue_comment("acme/api", 7, "body")
    assert result.ok is True  # the path ran
    assert transport.calls == []  # but nothing reached the network


# --- installation lifecycle ---------------------------------------------------


def _installation_payload(installation_id: int = 100, login: str = "acme") -> dict:
    return {
        "installation": {
            "id": installation_id,
            "account": {"login": login, "type": "Organization", "id": 1},
            "target_type": "Organization",
            "repository_selection": "selected",
        }
    }


def test_installation_created_is_registered_and_active(db):
    assert apply_event(db, "created", _installation_payload()) == "active"
    row = get(db, 100)
    assert row is not None and row.is_active
    assert row.account_login == "acme"


def test_suspend_stops_work_but_keeps_the_row(db):
    """Suspended is reversible: the audit trail must survive the pause."""
    apply_event(db, "created", _installation_payload())
    assert apply_event(db, "suspend", _installation_payload()) == "suspended"
    assert get(db, 100) is not None  # history preserved
    assert get_active(db, 100) is None  # but no work is permitted


def test_unsuspend_restores_work(db):
    apply_event(db, "created", _installation_payload())
    apply_event(db, "suspend", _installation_payload())
    assert apply_event(db, "unsuspend", _installation_payload()) == "active"
    assert get_active(db, 100) is not None


def test_delete_is_terminal_and_cannot_be_undone_by_a_later_delivery(db):
    """An uninstall must not be re-opened by a stray or replayed delivery."""
    apply_event(db, "created", _installation_payload())
    assert apply_event(db, "deleted", _installation_payload()) == "deleted"
    assert get_active(db, 100) is None
    apply_event(db, "created", _installation_payload())
    assert get_active(db, 100) is None  # still gone


def test_upsert_is_idempotent_and_refreshes_metadata(db):
    upsert(db, _installation_payload()["installation"])
    upsert(db, _installation_payload()["installation"])
    assert db.query(type(get(db, 100))).count() == 1
    renamed = _installation_payload(login="acme-renamed")["installation"]
    upsert(db, renamed)
    assert get(db, 100).account_login == "acme-renamed"


def test_upsert_without_an_id_is_a_no_op(db):
    assert upsert(db, {"account": {"login": "x"}}) is None


def test_repositories_event_is_recorded_without_changing_authority(db):
    apply_event(db, "created", _installation_payload())
    payload = _installation_payload()
    payload["repositories_added"] = [{"full_name": "acme/api"}]
    assert apply_repositories_event(db, "added", payload) == "recorded"
    assert get_active(db, 100) is not None


# --- per-tenant quota ---------------------------------------------------------


# A counter, not a clock: `time.time_ns()` is not unique on Windows (its timer
# granularity is far coarser than a nanosecond), so two rows created back to back
# could collide on `uq_pr_jobs_dedup_key` and fail the test about once in five
# runs. Uniqueness only has to hold inside one test's database.
_JOB_SEQ = itertools.count()


def _job(
    db: Session, installation_id: int | None, repo: str = "acme/api", age_hours: int = 0
) -> None:
    db.add(
        PRJob(
            dedup_key=f"k{next(_JOB_SEQ)}-{installation_id}-{repo}",
            repo=repo,
            pr_number=1,
            commit_sha="a" * 40,
            author="u",
            event="pull_request",
            installation_id=installation_id,
            created_at=datetime.now(UTC) - timedelta(hours=age_hours),
        )
    )
    db.commit()


def test_tenant_quota_blocks_the_noisiest_tenant_only(db, monkeypatch):
    """One tenant exhausting its budget must not deny service to another."""
    monkeypatch.setattr(
        "app.quota.settings",
        Settings(
            _env_file=None,
            allow_insecure=True,
            quota_jobs_per_installation_per_day=2,
            quota_jobs_total_per_day=0,
        ),
    )
    _job(db, 100)
    _job(db, 100)
    assert quota_reason(db, "acme/api", 100) == "quota_installation_daily"
    # A different tenant with no work is unaffected.
    assert quota_reason(db, "other/repo", 200) is None


def test_installation_quota_of_zero_means_unlimited(db, monkeypatch):
    monkeypatch.setattr(
        "app.quota.settings",
        Settings(_env_file=None, allow_insecure=True, quota_jobs_per_installation_per_day=0),
    )
    for _ in range(5):
        _job(db, 100)
    assert quota_reason(db, "acme/api", 100) is None


def test_old_jobs_fall_outside_the_tenant_window(db, monkeypatch):
    monkeypatch.setattr(
        "app.quota.settings",
        Settings(
            _env_file=None,
            allow_insecure=True,
            quota_jobs_per_installation_per_day=1,
        ),
    )
    _job(db, 100, age_hours=25)
    assert quota_reason(db, "acme/api", 100) is None


# --- schema guard -------------------------------------------------------------


def test_tenant_columns_exist_on_the_tables_that_cause_outward_effects():
    """A missing tenant column means approvals cannot be scoped; keep it explicit."""
    assert "installation_id" in PRJob.__table__.columns
    assert "installation_id" in ReviewDecision.__table__.columns
