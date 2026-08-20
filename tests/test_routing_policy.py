"""Tests for the pure routing policy — the disposition decision matrix.

`route()` is a total pure function, so this is the highest-value Phase 4 test:
it pins down exactly when the system acts alone versus when a human must sign
off. A wrong edge here is the difference between "posts a helpful comment" and
"pushes a code change nobody approved."
"""

from __future__ import annotations

import pytest

from app.routing.policy import Action, risk_rank, route


# --- risk ordering -----------------------------------------------------------
def test_risk_rank_orders_ascending():
    assert risk_rank("trivial") < risk_rank("low") < risk_rank("medium") < risk_rank("high")


def test_unknown_risk_ranks_as_high():
    # Anything unrecognized is treated as the most cautious level.
    assert risk_rank("chernobyl") == risk_rank("high")


# --- PR-open track: risk-thresholded auto vs. gated --------------------------
def _pr_state(risk: str) -> dict:
    return {
        "repo": "o/r",
        "pr_number": 3,
        "commit_sha": "c0ffee",
        "changed_files": [{"path": "a.py", "patch": "+x"}],
        "review_findings": [],
        "risk_score": risk,
        "summary": "a summary",
    }


@pytest.mark.parametrize("risk", ["trivial", "low"])
def test_low_risk_review_is_auto(risk):
    d = route(_pr_state(risk))
    assert d.action is Action.COMMENT_REVIEW
    assert d.requires_approval is False
    assert d.reason == "low_risk_auto"


@pytest.mark.parametrize("risk", ["medium", "high"])
def test_elevated_risk_review_is_gated(risk):
    d = route(_pr_state(risk))
    assert d.action is Action.COMMENT_REVIEW
    assert d.requires_approval is True


def test_review_body_contains_findings_and_risk():
    state = _pr_state("high")
    state["review_findings"] = [{"file": "a.py", "line": 2, "severity": "error", "message": "boom"}]
    d = route(state)
    assert "boom" in d.body
    assert "high" in d.body


# --- CI-failure track: verified fix is ALWAYS gated --------------------------
def _ci_state(**over) -> dict:
    base = {
        "repo": "o/r",
        "pr_number": 4,
        "commit_sha": "beef",
        "failure_type": "lint",
        "failure_diagnosis": "unused import",
        "proposed_fix": "--- a/a.py\n+++ b/a.py\n@@\n-import os\n",
        "risk_score": "low",  # even a 'low' risk code change must be gated
    }
    base.update(over)
    return base


def test_verified_fix_requires_approval_even_when_low_risk():
    d = route(_ci_state(fix_verified=True))
    assert d.action is Action.PROPOSE_FIX
    assert d.requires_approval is True
    assert "diff" in d.body


def test_unverified_fix_escalates():
    d = route(_ci_state(fix_verified=False, verification_reason="check_failed"))
    assert d.action is Action.ESCALATE
    assert d.requires_approval is True


def test_not_verifiable_escalates():
    d = route(
        _ci_state(
            fix_verified=False, failure_type="dependency", verification_reason="not_verifiable"
        )
    )
    assert d.action is Action.ESCALATE
    assert d.requires_approval is True


def test_undiagnosable_ci_is_no_action():
    # unknown type AND no diagnosis -> nothing worth saying.
    d = route(
        _ci_state(failure_type="unknown", failure_diagnosis="", proposed_fix="", fix_verified=False)
    )
    assert d.action is Action.NONE
    assert d.is_actionable is False


# --- track inference ---------------------------------------------------------
def test_ci_track_inferred_from_failure_type():
    # Presence of failure_type routes to the CI disposition even without ci_logs.
    d = route(_ci_state(fix_verified=True))
    assert d.action is Action.PROPOSE_FIX


def test_pr_track_when_no_ci_signal():
    d = route(_pr_state("low"))
    assert d.action is Action.COMMENT_REVIEW


def test_dedup_key_is_stable_and_scoped():
    d = route(_pr_state("low"))
    state = _pr_state("low")
    k1 = d.dedup_key(state)
    k2 = d.dedup_key(state)
    assert k1 == k2
    assert "o/r" in k1 and "c0ffee" in k1 and Action.COMMENT_REVIEW.value in k1
