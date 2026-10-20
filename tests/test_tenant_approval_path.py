"""The claim a customer actually cares about: "I install it, my PR gets a review,
and *I* can approve the queued action from the PR thread."

Every other tenancy test starts from a `ReviewDecision` it built itself, which is
exactly how a real defect hid: `enqueue_tracked` did not persist `installation_id`
and the worker's graph state did not carry it, so every queued decision landed
with a NULL owner. `_decision_for` then (correctly) refused NULL rows for tenants,
and a tenant's `/autopr approve` answered "no such decision" forever — while the
suite stayed green, because no test ever walked from worker state to approval.

These tests walk that path. Each assertion is paired with the failure it prevents:

* state carries the tenant            -> decision is addressable by that tenant
* a missing tenant stays NULL          -> and is then *not* approvable by anyone's thread
* the queue notice is posted           -> the tenant learns an id exists at all
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, DecisionStatus, Installation, ReviewDecision
from app.routing.github import FakeGitHubClient
from app.routing.router import router_node


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _factory(session: Session):
    """A session factory that hands back the same session (one connection)."""
    return lambda: session


def _state(**overrides) -> dict:
    """The dict the worker builds before graph entry."""
    state = {
        "repo": "acme/api",
        "pr_number": 7,
        "commit_sha": "c" * 40,
        # A medium risk review always requires approval, so this exercises the
        # gated path rather than the auto-post path.
        "risk_score": "medium",
        "summary": "Touches auth middleware.",
        "review_findings": [{"file": "app/auth.py", "line": "12", "message": "wide change"}],
        "changed_files": [{"path": "app/auth.py", "patch": "@@ -1 +1 @@"}],
    }
    state.update(overrides)
    return state


def test_queued_decision_is_owned_by_the_tenant(db_session: Session, monkeypatch) -> None:
    """The router must persist the tenant from state — not drop it on the floor."""
    from app.config import settings as live

    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)

    out = router_node(
        _state(installation_id=4242),  # type: ignore[arg-type]
        github=FakeGitHubClient(),
        session_factory=_factory(db_session),
    )

    assert out["action_taken"] == "queued_for_approval"
    row = db_session.get(ReviewDecision, out["decision_id"])
    assert row is not None
    assert row.status is DecisionStatus.PENDING
    assert row.installation_id == 4242, (
        "the tenant must travel from worker state onto the decision row, or no "
        "tenant can ever approve their own queued work"
    )


def test_single_operator_decision_stays_tenantless(db_session: Session, monkeypatch) -> None:
    """No installation in state must mean NULL, not a fabricated owner."""
    from app.config import settings as live

    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)

    out = router_node(
        _state(),  # no installation_id: a single-operator install
        github=FakeGitHubClient(),
        session_factory=_factory(db_session),
    )

    row = db_session.get(ReviewDecision, out["decision_id"])
    assert row is not None
    assert row.installation_id is None
    # And it stays unaddressable from a tenant thread, which is the guard that
    # makes the NULL meaningful rather than accidental.
    from app.commands import _decision_for

    assert _decision_for(db_session, row.id, "acme/api", 4242) is None


def test_junk_installation_id_degrades_to_tenantless(db_session: Session, monkeypatch) -> None:
    """A malformed id must not raise, and must not invent tenant 1 from `True`."""
    from app.config import settings as live

    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)

    for junk in (True, "not-a-number", -5, 0, {}):
        out = router_node(
            _state(installation_id=junk),  # type: ignore[arg-type]
            github=FakeGitHubClient(),
            session_factory=_factory(db_session),
        )
        row = db_session.get(ReviewDecision, out["decision_id"])
        assert row is not None
        assert row.installation_id is None, f"junk {junk!r} became a tenant"


def test_tenant_is_told_what_to_approve(db_session: Session, monkeypatch) -> None:
    """A queued decision is invisible to a tenant unless the PR says so."""
    from app.config import settings as live

    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)

    client = FakeGitHubClient()
    out = router_node(
        _state(installation_id=99),  # type: ignore[arg-type]
        github=client,
        session_factory=_factory(db_session),
    )

    assert out["action_taken"] == "queued_for_approval"
    posted = [c for c in client.calls if c["op"] == "comment"]
    assert len(posted) == 1, (
        "the tenant has no dashboard, so a queued action that is never announced "
        "cannot be approved by anybody"
    )
    body = posted[0]["body"]
    assert f"/autopr approve {out['decision_id']}" in body
    assert "/autopr status" in body


def test_single_operator_is_not_notified(db_session: Session, monkeypatch) -> None:
    """Operator installs keep their existing behaviour: dashboard, no PR chatter."""
    from app.config import settings as live

    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)

    client = FakeGitHubClient()
    router_node(
        _state(),
        github=client,
        session_factory=_factory(db_session),
    )
    assert [c for c in client.calls if c["op"] == "comment"] == []


def test_redelivery_does_not_renotify(db_session: Session, monkeypatch) -> None:
    """At-least-once delivery must not spam the thread with the same notice."""
    from app.config import settings as live

    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)

    client = FakeGitHubClient()
    state = _state(installation_id=77)
    first = router_node(state, github=client, session_factory=_factory(db_session))
    second = router_node(state, github=client, session_factory=_factory(db_session))

    assert first["decision_id"] == second["decision_id"]
    posted = [c for c in client.calls if c["op"] == "comment"]
    assert len(posted) == 1, "a redelivery posted a second notice"


def test_tenant_can_approve_end_to_end(db_session: Session, monkeypatch) -> None:
    """The whole claim, once: queue -> announce -> a maintainer approves -> executed."""
    from app.commands import handle_issue_comment
    from app.config import settings as live

    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)
    monkeypatch.setattr(live, "github_comment_commands", True)

    installation_id = 555
    db_session.add(
        Installation(
            installation_id=installation_id,
            account_login="acme",
            account_type="Organization",
            account_id=1,
            target_type="Organization",
            repository_selection="selected",
        )
    )
    db_session.commit()

    out = router_node(
        _state(installation_id=installation_id),
        github=FakeGitHubClient(),
        session_factory=_factory(db_session),
    )
    decision_id = out["decision_id"]

    class _Auth:
        """Stands in for GitHub's permission API: only this bit is stubbed."""

        def __init__(self) -> None:
            self.client_calls = 0

        def collaborator_permission(self, iid: int, repo: str, login: str) -> str:
            return "write" if (iid, repo) == (installation_id, "acme/api") else "none"

        def client(self, iid: int):
            self.client_calls += 1
            assert iid == installation_id, "acted with another tenant's credential"
            return FakeGitHubClient()

    auth = _Auth()
    payload = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {"body": f"/autopr approve {decision_id}", "user": {"login": "maintainer"}},
        "repository": {"full_name": "acme/api"},
        "installation": {"id": installation_id},
    }

    outcome = handle_issue_comment(db=db_session, payload=payload, auth=auth)  # type: ignore[arg-type]

    assert outcome["status"] == "executed", outcome
    assert outcome["decision_id"] == decision_id
    row = db_session.get(ReviewDecision, decision_id)
    assert row is not None
    assert row.status is DecisionStatus.EXECUTED


# --- the worker seam ---------------------------------------------------------
# The controls above cover the router and the store. This one covers the step
# before them: the worker turning a `PRJob` into graph state. It exists because
# a negative control found that dropping `installation_id` *there* left the whole
# suite green — the defect was one line, in a closure, with no test on the seam.


def test_worker_state_carries_the_job_tenant() -> None:
    """`job_state` must forward the job's tenant; the router depends on it."""
    from app.models import PRJob
    from app.worker import job_state

    job = PRJob(
        dedup_key="acme/api|7|" + "c" * 40 + "|pull_request",
        repo="acme/api",
        pr_number=7,
        commit_sha="c" * 40,
        author="contributor",
        event="pull_request",
        installation_id=31337,
    )

    state = job_state(job, [{"path": "app/auth.py", "patch": "@@ -1 +1 @@"}])

    assert state["installation_id"] == 31337
    assert state["repo"] == "acme/api"
    assert state["pr_number"] == 7
    assert state["changed_files"] == [{"path": "app/auth.py", "patch": "@@ -1 +1 @@"}]


def test_worker_state_keeps_single_operator_tenantless() -> None:
    """A PAT-driven job has no installation, and the state must say so."""
    from app.models import PRJob
    from app.worker import job_state

    job = PRJob(
        dedup_key="acme/api|7|" + "c" * 40 + "|pull_request",
        repo="acme/api",
        pr_number=7,
        commit_sha="c" * 40,
        author="contributor",
        event="pull_request",
    )

    assert job_state(job, [])["installation_id"] is None


def test_public_mode_blocks_the_operator_api_but_not_the_tenant(
    db_session: Session, monkeypatch
) -> None:
    """Public mode's 403 is about the *shared* operator token, not about tenants.

    The two paths are deliberately different. `POST /reviews/{id}/approve` is
    refused for everyone in public mode because it authenticates with one bearer
    token that every visitor would share. `/autopr approve` is refused for nobody
    who GitHub says has write access, because it authenticates the commenter
    individually. If public mode also closed the comment path, installing the App
    on a public instance would be pointless: work would queue and never clear.
    """
    from app.commands import handle_issue_comment
    from app.config import settings as live

    monkeypatch.setattr(live, "public_mode", True)
    monkeypatch.setattr(live, "handoff_mode", False)
    monkeypatch.setattr(live, "github_dry_run", True)
    monkeypatch.setattr(live, "github_comment_commands", True)

    installation_id = 909
    db_session.add(
        Installation(
            installation_id=installation_id,
            account_login="acme",
            account_type="Organization",
            account_id=1,
            target_type="Organization",
            repository_selection="selected",
        )
    )
    db_session.commit()

    out = router_node(
        _state(installation_id=installation_id),
        github=FakeGitHubClient(),
        session_factory=_factory(db_session),
    )

    class _Auth:
        def collaborator_permission(self, iid: int, repo: str, login: str) -> str:
            return "admin"

        def client(self, iid: int):
            return FakeGitHubClient()

    payload = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {
            "body": f"/autopr approve {out['decision_id']}",
            "user": {"login": "owner"},
        },
        "repository": {"full_name": "acme/api"},
        "installation": {"id": installation_id},
    }

    outcome = handle_issue_comment(db=db_session, payload=payload, auth=_Auth())  # type: ignore[arg-type]
    assert outcome["status"] == "executed", outcome


def test_demo_without_app_credentials_cannot_approve_from_comments(
    db_session: Session, monkeypatch
) -> None:
    """No App credentials means no tenant identity, so the comment path is inert."""
    from app.commands import handle_issue_comment
    from app.config import settings as live

    monkeypatch.setattr(live, "github_comment_commands", True)

    payload = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {"body": "/autopr approve 1", "user": {"login": "someone"}},
        "repository": {"full_name": "acme/api"},
        "installation": {"id": 1},
    }

    outcome = handle_issue_comment(db=db_session, payload=payload, auth=None)
    assert outcome == {"status": "ignored", "reason": "no_app_credentials"}
