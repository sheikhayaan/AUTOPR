"""Tests for the router node — decide, then act-or-queue.

Covers the paths that matter:
  * auto: a low-risk review is posted immediately via the (fake) GitHub client
    and recorded as EXECUTED (not left PENDING);
  * gated: a verified fix / elevated-risk review is queued for a human and NO
    GitHub call happens;
  * redelivery: re-running the same job does NOT post twice (idempotency via the
    ReviewDecision ledger).

The router takes a `session_factory` (zero-arg callable -> Session), so tests
pass the conftest `Session` sessionmaker. A separate `db` session is used for
assertions; both share the StaticPool engine, so committed rows are visible.
"""

from __future__ import annotations

from app.models import DecisionStatus
from app.routing.github import FakeGitHubClient
from app.routing.router import router_node
from app.routing.store import list_pending


def _pr_state(risk: str) -> dict:
    return {
        "repo": "o/r",
        "pr_number": 9,
        "commit_sha": "cafe",
        "changed_files": [{"path": "a.py", "patch": "+x"}],
        "review_findings": [],
        "risk_score": risk,
        "summary": "s",
    }


def _ci_state() -> dict:
    return {
        "repo": "o/r",
        "pr_number": 9,
        "commit_sha": "cafe",
        "failure_type": "lint",
        "failure_diagnosis": "unused import",
        "proposed_fix": "--- a/a.py\n+++ b/a.py\n@@\n-import os\n",
        "fix_verified": True,
        "risk_score": "low",
    }


# --- auto path ---------------------------------------------------------------
def test_low_risk_review_auto_posts_and_is_not_pending(db, Session):
    gh = FakeGitHubClient()
    out = router_node(_pr_state("low"), github=gh, session_factory=Session)
    assert out["action_taken"] == "executed"
    assert out["approval_required"] is False
    # A comment was actually posted through the client.
    assert len(gh.calls) == 1 and gh.calls[0]["op"] == "comment"
    # Nothing left for a human, but the action is recorded as EXECUTED.
    assert list_pending(db) == []


def test_low_risk_auto_records_executed_row(db, Session):
    gh = FakeGitHubClient()
    router_node(_pr_state("low"), github=gh, session_factory=Session)
    from sqlalchemy import select

    from app.models import ReviewDecision

    rows = db.execute(select(ReviewDecision)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == DecisionStatus.EXECUTED
    assert rows[0].action == "comment_review"
    assert rows[0].result_url  # the fake url was persisted


def test_auto_post_is_idempotent_on_redelivery(db, Session):
    # The load-bearing seam property: a redelivered job re-runs the whole graph,
    # so the router runs twice — but the comment must be posted exactly once.
    gh = FakeGitHubClient()
    first = router_node(_pr_state("low"), github=gh, session_factory=Session)
    second = router_node(_pr_state("low"), github=gh, session_factory=Session)
    assert first["action_taken"] == "executed"
    assert second["action_taken"] == "already_executed"
    assert len(gh.calls) == 1  # NOT posted twice
    from sqlalchemy import select

    from app.models import ReviewDecision

    assert len(db.execute(select(ReviewDecision)).scalars().all()) == 1


# --- gated paths -------------------------------------------------------------
def test_high_risk_review_is_queued_not_posted(db, Session):
    gh = FakeGitHubClient()
    out = router_node(_pr_state("high"), github=gh, session_factory=Session)
    assert out["action_taken"] == "queued_for_approval"
    assert out["approval_required"] is True
    assert gh.calls == []  # NO outward action
    pending = list_pending(db)
    assert len(pending) == 1
    assert pending[0].action == "comment_review"
    assert pending[0].status == DecisionStatus.PENDING


def test_verified_fix_is_queued_not_posted(db, Session):
    gh = FakeGitHubClient()
    out = router_node(_ci_state(), github=gh, session_factory=Session)
    assert out["action_taken"] == "queued_for_approval"
    assert gh.calls == []
    pending = list_pending(db)
    assert len(pending) == 1 and pending[0].action == "propose_fix"


def test_gated_queue_is_idempotent_on_rerun(db, Session):
    # Re-processing the same commit (redelivery / retry) must not double-queue.
    gh = FakeGitHubClient()
    router_node(_ci_state(), github=gh, session_factory=Session)
    router_node(_ci_state(), github=gh, session_factory=Session)
    assert len(list_pending(db)) == 1


def test_no_action_when_nothing_diagnosable(db, Session):
    gh = FakeGitHubClient()
    state = {
        "repo": "o/r",
        "pr_number": 9,
        "commit_sha": "cafe",
        "failure_type": "unknown",
        "failure_diagnosis": "",
        "proposed_fix": "",
        "fix_verified": False,
    }
    out = router_node(state, github=gh, session_factory=Session)
    assert out["action_taken"] == "none"
    assert gh.calls == []
    assert list_pending(db) == []


# --- gate preservation: a lower risk score on redelivery must NOT bypass HITL --
# The policy is re-evaluated on every delivery and the LLM is not deterministic,
# so the same commit can score `medium` once and `low` later. The ledger records
# the *first* disposition; a later auto-safe score must not execute work an
# earlier delivery escalated to a human, and must never override a rejection.
def test_risk_downgrade_does_not_bypass_the_human_gate(db, Session):
    gh = FakeGitHubClient()
    first = router_node(_pr_state("medium"), github=gh, session_factory=Session)
    assert first["action_taken"] == "queued_for_approval"

    # Redelivery of the same repo|pr|sha|action, but the model now says low.
    second = router_node(_pr_state("low"), github=gh, session_factory=Session)

    assert second["action_taken"] == "already_queued"
    assert second["decision_id"] == first["decision_id"]
    assert gh.calls == []  # NOT posted without approval
    pending = list_pending(db)
    assert len(pending) == 1
    assert pending[0].status == DecisionStatus.PENDING


def test_rejected_decision_is_never_auto_executed(db, Session):
    from app.routing import store

    gh = FakeGitHubClient()
    first = router_node(_pr_state("medium"), github=gh, session_factory=Session)
    store.mark_rejected(db, store.get(db, first["decision_id"]))

    second = router_node(_pr_state("low"), github=gh, session_factory=Session)

    assert second["action_taken"] == "already_rejected"
    assert gh.calls == []
    row = store.get(db, first["decision_id"])
    assert row.status == DecisionStatus.REJECTED


def test_auto_already_posted_is_not_requeued_by_a_higher_risk_rerun(db, Session):
    # The mirror case: the low-risk run posted, so a later high-risk score must
    # not create a second decision for an action that already happened.
    gh = FakeGitHubClient()
    first = router_node(_pr_state("low"), github=gh, session_factory=Session)
    assert first["action_taken"] == "executed"

    second = router_node(_pr_state("high"), github=gh, session_factory=Session)

    assert second["action_taken"] == "already_executed"
    assert len(gh.calls) == 1
    assert list_pending(db) == []


def test_auto_path_retries_a_failed_post(db, Session):
    # A failed post leaves the row FAILED; a redelivery must be allowed to retry
    # it (that is a retry of the *same* auto decision, not a gate bypass).
    from app.routing.github import ActionResult

    class FailingGitHub(FakeGitHubClient):
        def post_issue_comment(self, repo, pr_number, body):
            return ActionResult(ok=False, kind="comment", detail="boom 502")

    failed = router_node(_pr_state("low"), github=FailingGitHub(), session_factory=Session)
    assert failed["action_taken"] == "action_failed"

    gh = FakeGitHubClient()
    retried = router_node(_pr_state("low"), github=gh, session_factory=Session)
    assert retried["action_taken"] == "executed"
    assert len(gh.calls) == 1


def test_gated_redelivery_reopens_a_failed_auto_row(db, Session):
    """FAILED must not become a black hole when the next score is gated.

    Delivery 1 scores low, the post fails -> row FAILED. Delivery 2 scores medium
    -> human-gated. `list_pending` only shows PENDING, so if the row stayed FAILED
    the decision would be invisible in the review queue and executed by nobody.
    """
    from app.routing.github import ActionResult

    class FailingGitHub(FakeGitHubClient):
        def post_issue_comment(self, repo, pr_number, body):
            return ActionResult(ok=False, kind="comment", detail="boom 502")

    failed = router_node(_pr_state("low"), github=FailingGitHub(), session_factory=Session)
    assert failed["action_taken"] == "action_failed"

    gh = FakeGitHubClient()
    gated = router_node(_pr_state("medium"), github=gh, session_factory=Session)

    assert gated["action_taken"] == "queued_for_approval"
    assert gh.calls == []
    pending = list_pending(db)
    assert len(pending) == 1  # visible to the operator again
    assert pending[0].id == failed["decision_id"]
    assert pending[0].last_error is None


# --- pure-run safety (no persistence) ----------------------------------------
def test_router_without_factory_still_decides():
    # No factory: decides + auto-acts but skips persistence (and can't dedup).
    gh = FakeGitHubClient()
    out = router_node(_pr_state("low"), github=gh, session_factory=None)
    assert out["action_taken"] == "executed"
    assert "decision_id" not in out
    assert len(gh.calls) == 1


def test_router_defaults_to_fake_client_when_none():
    # No github passed and no factory: must not raise, must not touch the network.
    out = router_node(_pr_state("low"))
    assert out["action_taken"] == "executed"
