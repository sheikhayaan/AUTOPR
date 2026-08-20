"""Tests for graph routing logic — the conditional-edge decisions.

Routing is pure functions over state, so we assert on them directly without
running any LLM. This is the highest-value place to test the orchestration:
a wrong edge sends the wrong agent (or an expensive one) down the wrong path.
"""

from __future__ import annotations

from app.agents.graph import (
    _after_ci_monitor,
    _after_fix_verifier,
    _entry_router,
    build_graph,
    is_ci_failure_event,
)


def test_entry_routes_pr_event_to_reviewer():
    state = {"repo": "o/r", "pr_number": 1, "commit_sha": "x", "changed_files": []}
    assert _entry_router(state) == "code_reviewer"


def test_entry_routes_ci_logs_to_monitor():
    state = {"repo": "o/r", "pr_number": 1, "ci_logs": "boom"}
    assert _entry_router(state) == "ci_monitor"


def test_entry_routes_ci_event_to_monitor():
    state = {"repo": "o/r", "pr_number": 1, "ci_event": "workflow_run"}
    assert _entry_router(state) == "ci_monitor"


def test_is_ci_failure_event():
    assert is_ci_failure_event({"ci_logs": "x"}) is True
    assert is_ci_failure_event({"ci_event": "check_run"}) is True
    assert is_ci_failure_event({"changed_files": []}) is False


def test_after_ci_monitor_unknown_goes_to_router():
    # An undiagnosable failure still reaches the disposition layer (router),
    # which decides whether to escalate or stay silent — it never dead-ends.
    assert _after_ci_monitor({"failure_type": "unknown"}) == "router"


def test_after_ci_monitor_missing_type_goes_to_router():
    # Defensive: absent failure_type must not route to the Fix Agent.
    assert _after_ci_monitor({}) == "router"


def test_after_ci_monitor_diagnosed_goes_to_fix():
    for ftype in ("lint", "type_error", "test", "import", "dependency"):
        assert _after_ci_monitor({"failure_type": ftype}) == "fix_agent"


def test_after_fix_verifier_verified_goes_to_router():
    # A verified fix does NOT auto-promote; it flows to the router, which gates
    # promotion behind a human.
    assert _after_fix_verifier({"fix_verified": True}) == "router"


def test_after_fix_verifier_retryable_within_budget_loops():
    state = {"fix_verified": False, "verification_reason": "check_failed", "fix_attempts": 0}
    assert _after_fix_verifier(state) == "fix_agent"


def test_after_fix_verifier_exhausted_budget_goes_to_router():
    # Same retryable reason, but the attempt budget is spent -> escalate (router).
    from app.config import settings

    state = {
        "fix_verified": False,
        "verification_reason": "check_failed",
        "fix_attempts": settings.max_fix_attempts,
    }
    assert _after_fix_verifier(state) == "router"


def test_after_fix_verifier_non_retryable_goes_to_router():
    # not_verifiable won't improve on retry -> straight to router (escalate).
    state = {"fix_verified": False, "verification_reason": "not_verifiable", "fix_attempts": 0}
    assert _after_fix_verifier(state) == "router"


def test_graph_compiles_with_all_nodes():
    g = build_graph(rag=None)
    nodes = set(g.get_graph().nodes.keys())
    for expected in ("code_reviewer", "test_generator", "ci_monitor", "fix_agent", "router"):
        assert expected in nodes


def test_graph_compiles_with_session_factory(Session):
    # The seam binds a session_factory into the router; compiling with one must
    # still produce the same node set (router is bound, not added twice).
    g = build_graph(rag=None, github=None, session_factory=Session)
    assert "router" in set(g.get_graph().nodes.keys())
