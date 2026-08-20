"""Routing tests for the Phase 3 verify-and-retry cycle.

_after_fix_verifier is a pure function over state; assert the loop terminates
and only retries when it could help. Since Phase 4, every non-retry outcome
flows to the terminal `router` node (which then decides promote-vs-escalate)
rather than straight to END — so "terminate" here means "reach the router".
"""

from __future__ import annotations

from app.agents.graph import _after_fix_verifier, build_graph
from app.config import settings


def test_verified_goes_to_router():
    assert _after_fix_verifier({"fix_verified": True}) == "router"


def test_check_failed_under_budget_retries():
    state = {"fix_verified": False, "verification_reason": "check_failed", "fix_attempts": 1}
    assert _after_fix_verifier(state) == "fix_agent"


def test_check_failed_at_budget_goes_to_router():
    # Bounded: once attempts hit the cap, escalate (via router) instead of
    # looping forever.
    state = {
        "fix_verified": False,
        "verification_reason": "check_failed",
        "fix_attempts": settings.max_fix_attempts,
    }
    assert _after_fix_verifier(state) == "router"


def test_non_retryable_reason_goes_to_router_even_under_budget():
    # sandbox_error / not_verifiable won't improve on retry -> straight to router.
    for reason in ("not_verifiable", "no_fix", "no_snapshot", "sandbox_error"):
        state = {"fix_verified": False, "verification_reason": reason, "fix_attempts": 0}
        assert _after_fix_verifier(state) == "router"


def test_patch_apply_failure_retries():
    state = {"fix_verified": False, "verification_reason": "patch_apply_failed", "fix_attempts": 1}
    assert _after_fix_verifier(state) == "fix_agent"


def test_graph_includes_fix_verifier_node():
    g = build_graph(rag=None, sandbox=None)
    assert "fix_verifier" in set(g.get_graph().nodes.keys())
