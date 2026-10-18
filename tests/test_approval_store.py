"""Tests for the durable HITL decision store.

The store is where a human-gated decision lives between "the pipeline wants to
do X" and "a maintainer approved X". Its load-bearing property is idempotency:
re-running the same commit's same action must NOT create a second row. We also
pin the status lifecycle (pending -> approved/rejected/executed/failed) that the
ops API depends on.
"""

from __future__ import annotations

from app.models import DecisionStatus
from app.routing import store
from app.routing.policy import route


def _pr_state(risk: str = "high") -> dict:
    # 'high' so the review is human-gated (requires_approval=True) and thus a
    # store candidate.
    return {
        "repo": "o/r",
        "pr_number": 7,
        "commit_sha": "c0mm1t",
        "changed_files": [{"path": "a.py", "patch": "+x"}],
        "review_findings": [{"file": "a.py", "line": 1, "severity": "error", "message": "x"}],
        "risk_score": risk,
        "summary": "s",
    }


def test_enqueue_persists_pending_decision(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    assert row.id is not None
    assert row.status == DecisionStatus.PENDING
    assert row.action == "comment_review"
    assert row.repo == "o/r" and row.pr_number == 7 and row.commit_sha == "c0mm1t"
    assert row.body  # the renderable content is stored, not recomputed later


def test_enqueue_is_idempotent_on_same_decision(db):
    d = route(_pr_state())
    first = store.enqueue(db, d, _pr_state())
    second = store.enqueue(db, d, _pr_state())
    assert first.id == second.id
    assert len(store.list_pending(db)) == 1


def test_enqueue_distinct_for_different_commits(db):
    d1 = route(_pr_state())
    s2 = _pr_state()
    s2["commit_sha"] = "different"
    d2 = route(s2)
    store.enqueue(db, d1, _pr_state())
    store.enqueue(db, d2, s2)
    assert len(store.list_pending(db)) == 2


def test_list_pending_excludes_resolved(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    assert len(store.list_pending(db)) == 1
    store.mark_rejected(db, row)
    assert store.list_pending(db) == []


def test_list_pending_is_oldest_first(db):
    a = _pr_state()
    a["commit_sha"] = "aaa"
    b = _pr_state()
    b["commit_sha"] = "bbb"
    ra = store.enqueue(db, route(a), a)
    rb = store.enqueue(db, route(b), b)
    ids = [r.id for r in store.list_pending(db)]
    assert ids == [ra.id, rb.id]


def test_get_returns_row_or_none(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    assert store.get(db, row.id).id == row.id
    assert store.get(db, 999999) is None


def test_status_transitions(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())

    # Approving is a CAS (PENDING -> APPROVED); mark_approved was superseded by it.
    claimed, outcome = store.claim_approval(db, row.id)
    assert outcome == "claimed" and claimed is not None
    assert store.get(db, row.id).status == DecisionStatus.APPROVED
    # A second claim loses, which is what stops a double-click posting twice.
    _, outcome = store.claim_approval(db, row.id)
    assert outcome == "in_progress"

    store.mark_executed(db, row, "https://gh/x/1")
    got = store.get(db, row.id)
    assert got.status == DecisionStatus.EXECUTED
    assert got.result_url == "https://gh/x/1"


def test_mark_failed_records_error(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    store.mark_failed(db, row, "boom: 502")
    got = store.get(db, row.id)
    assert got.status == DecisionStatus.FAILED
    assert got.last_error == "boom: 502"


# --- claim_approval: the approve CAS -----------------------------------------
# Two concurrent approvals must not both post. The first conditional UPDATE wins;
# the second sees a row that is no longer PENDING and stands down.
def test_claim_approval_is_exclusive(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())

    first, outcome = store.claim_approval(db, row.id)
    assert outcome == "claimed"
    assert first.status == DecisionStatus.APPROVED

    second, outcome = store.claim_approval(db, row.id)
    assert outcome == "in_progress"
    assert second.status == DecisionStatus.APPROVED


def test_claim_approval_reports_terminal_states(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    store.mark_executed(db, row, "https://gh/x/1")
    _, outcome = store.claim_approval(db, row.id)
    assert outcome == "already_executed"

    state2 = _pr_state()
    state2["commit_sha"] = "other"
    row2 = store.enqueue(db, route(state2), state2)
    store.mark_rejected(db, row2)
    _, outcome2 = store.claim_approval(db, row2.id)
    assert outcome2 == "rejected"

    _, missing = store.claim_approval(db, 999999)
    assert missing == "not_found"


def test_claim_approval_allows_retry_after_failure(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    store.mark_failed(db, row, "boom")
    again, outcome = store.claim_approval(db, row.id)
    assert outcome == "claimed"
    assert again.status == DecisionStatus.APPROVED


def test_sweep_interrupted_clears_stranded_claims(db):
    # A crash between the claim and mark_executed leaves APPROVED with no process
    # to finish it. The startup sweep turns it into a retryable FAILED.
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    store.claim_approval(db, row.id)

    assert store.sweep_interrupted(db) == 1
    got = store.get(db, row.id)
    assert got.status == DecisionStatus.FAILED
    assert "interrupted" in (got.last_error or "")


# --- reject_pending: the reject CAS ------------------------------------------
def test_reject_pending_only_wins_from_pending(db):
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    _, outcome = store.reject_pending(db, row.id)
    assert outcome == "rejected"
    assert store.get(db, row.id).status == DecisionStatus.REJECTED


def test_reject_pending_refuses_an_in_flight_approval(db):
    # Rejecting while an approver holds the claim would produce a decision that is
    # both rejected and (about to be) executed.
    d = route(_pr_state())
    row = store.enqueue(db, d, _pr_state())
    store.claim_approval(db, row.id)

    _, outcome = store.reject_pending(db, row.id)
    assert outcome == "in_progress"
    assert store.get(db, row.id).status == DecisionStatus.APPROVED
