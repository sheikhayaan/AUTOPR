"""Tests for comment-driven approval — the tenant's only approval channel.

The security-relevant assertions here are negative ones: a commenter without
write access must not be able to fire anything, and a decision that belongs to a
different repo or a different installation must not be addressable at all. Both
are tested, then re-tested with the guard removed (see the negative controls in
the phase-16 notes) — a passing test that cannot fail proves nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.commands import APPROVER_PERMISSIONS, parse_command
from app.models import Base, DecisionStatus, ReviewDecision


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


# --- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "verb", "decision_id"),
    [
        ("/autopr approve 42", "approve", 42),
        ("/autopr approve #42", "approve", 42),
        ("/autopr reject 7", "reject", 7),
        ("/autopr status", "status", None),
        ("/autopr help", "help", None),
        ("/autopr", "help", None),
        ("/AutoPR APPROVE 9", "approve", 9),
        ("/autopr-bot approve 9", "approve", 9),
        ("\n\n  /autopr approve 42  \n", "approve", 42),
    ],
)
def test_commands_parse(body, verb, decision_id):
    cmd = parse_command(body)
    assert cmd is not None and cmd.verb == verb and cmd.decision_id == decision_id


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Looks good to me",
        "approve 42",  # no prefix: not addressed to us
        "//autopr approve 42",
        "/autopr please approve everything",
        "> /autopr approve 42\n\nWait, is that right?",  # quoted prose, not a command
    ],
)
def test_non_commands_are_ignored(body):
    assert parse_command(body) is None


def test_quoted_command_inside_prose_is_not_executed():
    """A reviewer quoting the command while asking about it must not fire it."""
    body = "If I wrote:\n\n    /autopr approve 42\n\nwould that post the comment?"
    assert parse_command(body) is None


def test_approve_without_an_id_parses_but_carries_none():
    cmd = parse_command("/autopr approve")
    assert cmd is not None and cmd.verb == "approve" and cmd.decision_id is None


# --- the permission model -----------------------------------------------------


def test_triage_and_read_are_not_approvers():
    """Triage can manage issues but cannot merge, so it cannot authorise writes."""
    assert "triage" not in APPROVER_PERMISSIONS
    assert "read" not in APPROVER_PERMISSIONS
    assert "none" not in APPROVER_PERMISSIONS
    for allowed in ("admin", "maintain", "write"):
        assert allowed in APPROVER_PERMISSIONS


# --- handler behaviour (with a stub auth and stub client) ----------------------


class StubAuth:
    """Stands in for AppAuth: records what it was asked, answers from a table."""

    def __init__(self, permission: str = "write", installed: int | None = 100) -> None:
        self.permission = permission
        self.installed = installed
        self.permission_calls: list[tuple[int, str, str]] = []
        self.clients: dict[int, StubClient] = {}

    def collaborator_permission(self, installation_id: int, repo: str, login: str) -> str:
        self.permission_calls.append((installation_id, repo, login))
        return self.permission

    def client(self, installation_id: int) -> StubClient:
        return self.clients.setdefault(installation_id, StubClient())

    def repo_installation(self, repo: str) -> int | None:
        return self.installed


class StubClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    def post_issue_comment(self, repo: str, pr_number: int, body: str):
        from app.routing.github import ActionResult

        self.comments.append((repo, pr_number, body))
        return ActionResult(ok=True, kind="comment", url="https://example/comment")


def _comment_payload(
    *,
    body: str = "/autopr approve 1",
    login: str = "maintainer",
    user_type: str = "User",
    repo: str = "acme/api",
    pr: int = 7,
    installation_id: int | None = 100,
    action: str = "created",
    is_pr: bool = True,
) -> dict:
    payload = {
        "action": action,
        "issue": {"number": pr, **({"pull_request": {"url": "u"}} if is_pr else {})},
        "comment": {"body": body, "user": {"login": login, "type": user_type}},
        "repository": {"full_name": repo},
    }
    if installation_id is not None:
        payload["installation"] = {"id": installation_id}
    return payload


def _decision(
    db: Session,
    *,
    decision_id_hint: int | None = None,
    repo: str = "acme/api",
    installation_id: int | None = 100,
    status: DecisionStatus = DecisionStatus.PENDING,
) -> ReviewDecision:
    row = ReviewDecision(
        dedup_key=f"{repo}|{decision_id_hint}|{installation_id}",
        repo=repo,
        pr_number=7,
        commit_sha="a" * 40,
        action="comment_review",
        risk="medium",
        reason="needs_human",
        title="Add retry",
        body="Looks fine",
        status=status,
        installation_id=installation_id,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def active_installation(db):
    from app.tenancy import upsert

    upsert(
        db,
        {
            "id": 100,
            "account": {"login": "acme", "type": "Organization", "id": 1},
            "target_type": "Organization",
            "repository_selection": "selected",
        },
    )
    return 100


def _handle(db, payload, auth):
    from app.commands import handle_issue_comment

    return handle_issue_comment(db=db, payload=payload, auth=auth)


def test_approve_executes_for_a_maintainer(db, active_installation):
    row = _decision(db)
    auth = StubAuth(permission="write")
    out = _handle(db, _comment_payload(body=f"/autopr approve {row.id}"), auth)
    assert out["status"] == "executed"
    db.refresh(row)
    assert row.status == DecisionStatus.EXECUTED


def test_reader_cannot_approve_and_is_told_nothing(db, active_installation):
    """Silence is deliberate: a reply would reveal which ids exist."""
    row = _decision(db)
    auth = StubAuth(permission="read")
    out = _handle(db, _comment_payload(body=f"/autopr approve {row.id}", login="drive-by"), auth)
    assert out == {"status": "ignored", "reason": "insufficient_permission"}
    db.refresh(row)
    assert row.status == DecisionStatus.PENDING
    assert auth.clients == {}  # no client was even built, so no write could occur


def test_non_collaborator_is_refused(db, active_installation):
    row = _decision(db)
    auth = StubAuth(permission="none")
    out = _handle(db, _comment_payload(body=f"/autopr approve {row.id}"), auth)
    assert out["reason"] == "insufficient_permission"
    db.refresh(row)
    assert row.status == DecisionStatus.PENDING


def test_decision_from_another_repo_is_not_addressable(db, active_installation):
    """Tenant/PR isolation: a valid id from elsewhere must not be reachable."""
    other = _decision(db, repo="other/repo")
    auth = StubAuth(permission="admin")
    out = _handle(db, _comment_payload(body=f"/autopr approve {other.id}", repo="acme/api"), auth)
    assert out["status"] == "replied"  # polite "no such decision"
    db.refresh(other)
    assert other.status == DecisionStatus.PENDING


def test_decision_from_another_installation_is_not_addressable(db, active_installation):
    """Two tenants can both have decision #N in their own queues; ids are global."""
    foreign = _decision(db, installation_id=999, repo="victim/repo")
    auth = StubAuth(permission="admin")
    out = _handle(
        db,
        _comment_payload(body=f"/autopr approve {foreign.id}", repo="victim/repo"),
        auth,
    )
    # Same repo, but created by a different installation: still not addressable.
    assert out["status"] == "replied"
    db.refresh(foreign)
    assert foreign.status == DecisionStatus.PENDING


def test_bot_comments_are_ignored_to_prevent_loops(db, active_installation):
    row = _decision(db)
    auth = StubAuth(permission="admin")
    out = _handle(db, _comment_payload(body=f"/autopr approve {row.id}", user_type="Bot"), auth)
    assert out["reason"] == "bot_comment"
    assert auth.permission_calls == []


def test_non_pull_request_issue_is_ignored(db, active_installation):
    out = _handle(db, _comment_payload(is_pr=False), StubAuth())
    assert out["reason"] == "not_a_pull_request"


def test_edited_comments_are_ignored(db, active_installation):
    row = _decision(db)
    out = _handle(
        db, _comment_payload(body=f"/autopr approve {row.id}", action="edited"), StubAuth()
    )
    assert out["reason"] == "not_created"
    db.refresh(row)
    assert row.status == DecisionStatus.PENDING


def test_inactive_installation_does_no_work(db, active_installation):
    from app.tenancy import apply_event

    row = _decision(db)
    apply_event(
        db,
        "suspend",
        {"installation": {"id": 100, "account": {"login": "acme", "type": "Organization"}}},
    )
    auth = StubAuth(permission="admin")
    out = _handle(db, _comment_payload(body=f"/autopr approve {row.id}"), auth)
    assert out["reason"] == "installation_inactive"
    assert auth.permission_calls == []
    db.refresh(row)
    assert row.status == DecisionStatus.PENDING


def test_missing_app_credentials_does_nothing(db, active_installation):
    row = _decision(db)
    out = _handle(db, _comment_payload(body=f"/autopr approve {row.id}"), None)
    assert out["reason"] == "no_app_credentials"
    db.refresh(row)
    assert row.status == DecisionStatus.PENDING


def test_status_lists_only_this_tenants_pending_items(db, active_installation):
    mine = _decision(db)
    _decision(db, repo="other/repo")
    auth = StubAuth(permission="write")
    out = _handle(db, _comment_payload(body="/autopr status"), auth)
    assert out["status"] == "replied"
    reply = auth.client(100).comments[-1][2]
    assert f"#{mine.id}" in reply
    assert "other/repo" not in reply


def test_reject_marks_rejected_and_writes_nothing_to_github(db, active_installation):
    row = _decision(db)
    auth = StubAuth(permission="maintain")
    out = _handle(db, _comment_payload(body=f"/autopr reject {row.id}"), auth)
    assert out["status"] == "rejected"
    db.refresh(row)
    assert row.status == DecisionStatus.REJECTED
    # No comment was posted to the PR by the action itself.
    assert auth.client(100).comments == []


def test_already_executed_decision_is_not_executed_twice(db, active_installation):
    """The CAS must hold across channels: comment + dashboard racing."""
    row = _decision(db, status=DecisionStatus.EXECUTED)
    auth = StubAuth(permission="admin")
    out = _handle(db, _comment_payload(body=f"/autopr approve {row.id}"), auth)
    assert out["status"] == "replied"
    assert "already executed" in auth.client(100).comments[-1][2]
    db.refresh(row)
    assert row.status == DecisionStatus.EXECUTED


def test_help_is_answered_without_touching_the_queue(db, active_installation):
    auth = StubAuth(permission="write")
    out = _handle(db, _comment_payload(body="/autopr"), auth)
    assert out["status"] == "replied"
    reply = auth.client(100).comments[-1][2]
    assert "/autopr approve" in reply


def test_read_access_cannot_list_the_queue(db, active_installation):
    """`status` shows unposted draft reviews, so it needs the same bar as approve.

    A drive-by commenter on a public repo can see merged code, but not proposals
    a maintainer has not released yet — that would leak the review before the
    maintainer sees it.
    """
    _decision(db)
    auth = StubAuth(permission="read")
    out = _handle(db, _comment_payload(body="/autopr status"), auth)
    assert out["reason"] == "insufficient_permission"
    assert auth.clients == {}


def test_permission_check_uses_the_delivering_installation(db, active_installation):
    """The authority question must be asked of the tenant's own installation."""
    row = _decision(db)
    auth = StubAuth(permission="write")
    _handle(db, _comment_payload(body=f"/autopr approve {row.id}", login="octocat"), auth)
    assert auth.permission_calls == [(100, "acme/api", "octocat")]
