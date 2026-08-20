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

    store.mark_approved(db, row)
    assert store.get(db, row.id).status == DecisionStatus.APPROVED

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
