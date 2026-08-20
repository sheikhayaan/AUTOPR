"""LangGraph wiring for the AutoPR pipeline.

Two tracks, selected at entry by event type:

  PR-open / synchronize track:
      code_reviewer -> test_generator -> router -> END

  CI-failure track:
      ci_monitor -> (failure_type != "unknown") ? fix_agent : router
      fix_agent  -> fix_verifier
      fix_verifier -> verified ? router
                    : (retryable & attempts < max) ? fix_agent   (feedback loop)
                    : router  (escalate to human)

Design notes:
- The two tracks are disjoint. A PR-open event never runs CI Monitor/Fix; a
  CI-failure event never runs Reviewer/TestGen. This keeps each run cheap and
  the state clean (agents only see inputs relevant to their track).
- Phase 3 adds a *verify-and-retry* cycle on the CI track: the Fix Agent's patch
  is proven in a sandbox before we trust it, and a failed proof feeds back into
  a bounded re-attempt. The bound (settings.max_fix_attempts) makes the cycle
  terminate — LangGraph allows cycles, so the guard is ours to enforce.
- Phase 4 adds a single terminal `router` node that BOTH tracks end at. It turns
  analysis into disposition: auto-act on safe low-risk output, or queue a
  human-gated decision (any code change, any elevated risk). Every path that
  used to go to END now goes to router -> END, so there is exactly one place
  where the pipeline decides what to do with its result.
- Node functions are bound to their collaborators (rag, sandbox, github) via
  functools.partial at build time, because LangGraph calls nodes with just
  (state). Passing None yields the ungrounded/no-op path used in tests.
- Routing is data-driven off the state, not hardcoded, so it's unit-testable
  without an LLM or a daemon (see tests/test_graph_routing.py).
"""

from __future__ import annotations

from functools import partial
from typing import Literal

from langgraph.graph import END, START, StateGraph

from app.agents.ci_monitor import ci_monitor_node
from app.agents.code_reviewer import code_reviewer_node
from app.agents.fix_agent import fix_agent_node
from app.agents.state import PRState
from app.agents.test_generator import test_generator_node
from app.config import settings
from app.routing.router import router_node
from app.sandbox.verifier import fix_verifier_node

# Verdict reasons where trying again with feedback could plausibly help. A
# `not_verifiable`/`no_fix`/`no_snapshot`/`sandbox_error` result won't improve
# on retry, so those end the run immediately (escalate).
_RETRYABLE_REASONS = frozenset({"check_failed", "patch_apply_failed", "timeout"})


def is_ci_failure_event(state: PRState) -> bool:
    """True if this run was triggered by a CI failure (has logs to diagnose)."""
    return bool(state.get("ci_logs") or state.get("ci_event"))


def _entry_router(state: PRState) -> Literal["code_reviewer", "ci_monitor"]:
    """Pick the track based on event type."""
    return "ci_monitor" if is_ci_failure_event(state) else "code_reviewer"


def _after_ci_monitor(state: PRState) -> Literal["fix_agent", "router"]:
    """Only attempt a fix if CI Monitor produced a confident diagnosis.

    An undiagnosable failure still flows to the router (which decides whether to
    escalate or stay silent) rather than dead-ending — so every run reaches the
    disposition layer.
    """
    if state.get("failure_type", "unknown") == "unknown":
        return "router"
    return "fix_agent"


def _after_fix_verifier(state: PRState) -> Literal["fix_agent", "router"]:
    """Decide the fate of a verification result.

    - verified            -> router (success; router gates promotion on a human)
    - retryable & budget   -> back to fix_agent with the failure as feedback
    - otherwise            -> router (escalate to human via the queue)
    """
    if state.get("fix_verified"):
        return "router"
    reason = state.get("verification_reason", "")
    attempts = state.get("fix_attempts", 0)
    if reason in _RETRYABLE_REASONS and attempts < settings.max_fix_attempts:
        return "fix_agent"
    return "router"


def build_graph(rag=None, sandbox=None, github=None, session_factory=None):
    """Build and compile the pipeline graph.

    `rag` is an optional RepoRAG bound into the agents that use it.
    `sandbox` is an optional Sandbox bound into the fix verifier; None lets the
    verifier construct a real DockerSandbox on demand (production), while tests
    inject a FakeSandbox. `github` is an optional GitHubClient bound into the
    router; None makes the router use a dry-run FakeGitHubClient. `session_factory`
    is an optional zero-arg callable returning a DB Session (e.g. `SessionLocal`)
    bound into the router so it can durably persist human-gated decisions and
    dedup auto-actions; None runs the router in-memory (decides + auto-acts, no
    persistence). Pass rag=None for ungrounded operation.

    The worker binds `session_factory=SessionLocal` when it drives the graph (the
    Phase 4 integration seam); tests pass the in-memory sessionmaker.
    """
    graph = StateGraph(PRState)

    # Nodes. Bind collaborators into the nodes that accept them.
    graph.add_node("code_reviewer", partial(code_reviewer_node, rag=rag))
    graph.add_node("test_generator", partial(test_generator_node, rag=rag))
    graph.add_node("ci_monitor", ci_monitor_node)  # no rag param
    graph.add_node("fix_agent", partial(fix_agent_node, rag=rag))
    graph.add_node("fix_verifier", partial(fix_verifier_node, sandbox=sandbox))
    graph.add_node("router", partial(router_node, github=github, session_factory=session_factory))

    # Entry: route to the correct track.
    graph.add_conditional_edges(
        START,
        _entry_router,
        {"code_reviewer": "code_reviewer", "ci_monitor": "ci_monitor"},
    )

    # PR-open track: reviewer -> test generator -> router -> end.
    graph.add_edge("code_reviewer", "test_generator")
    graph.add_edge("test_generator", "router")

    # CI-failure track: monitor -> (maybe) fix -> verify -> (maybe retry) -> router.
    graph.add_conditional_edges(
        "ci_monitor",
        _after_ci_monitor,
        {"fix_agent": "fix_agent", "router": "router"},
    )
    graph.add_edge("fix_agent", "fix_verifier")
    graph.add_conditional_edges(
        "fix_verifier",
        _after_fix_verifier,
        {"fix_agent": "fix_agent", "router": "router"},
    )

    # The single disposition point. Every track ends here.
    graph.add_edge("router", END)

    return graph.compile()
