"""The router: the pipeline's terminal node and the HITL executor.

Two responsibilities, deliberately in one place because they share the
"Action -> GitHub call" mapping:

  router_node(state)   — runs at the end of every graph track. Calls the pure
                         policy, then EITHER acts immediately (auto, no approval
                         needed) OR queues the decision for a human. It is the
                         only node that can cause an outward side effect, and it
                         only does so for actions the policy marked auto-safe.

  execute_decision(...) — carries out one approved decision's GitHub action.
                         Called by router_node for the auto path, and by the
                         ops API's /approve for the human-gated path. One code
                         path for "do the thing", whether a human or the policy
                         authorized it.

Safety invariants:
  * A decision with requires_approval=True is NEVER executed here — it is only
    persisted. Execution waits for an explicit human approve call.
  * The node never raises. A GitHub or DB failure is logged and reflected in the
    returned state; it does not crash the worker mid-run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from app.agents.state import PRState
from app.models import DecisionStatus
from app.routing import policy, store
from app.routing.github import ActionResult, FakeGitHubClient, GitHubClient
from app.routing.policy import Action, RoutingDecision

log = structlog.get_logger()


def execute_decision(
    github: GitHubClient, decision: RoutingDecision | dict, state: Mapping[str, Any]
) -> ActionResult:
    """Perform the outward GitHub action for a decision. Shared by auto + HITL.

    Accepts either a RoutingDecision (auto path, from router_node) or a stored
    ReviewDecision-shaped mapping (HITL path, from the approve endpoint) — both
    expose .action/.body/.title. We normalize to the fields we need.
    """
    action = (
        decision.action if isinstance(decision, RoutingDecision) else Action(decision["action"])
    )
    body = decision.body if isinstance(decision, RoutingDecision) else decision["body"]
    repo = state.get("repo", "?")
    pr = int(state.get("pr_number", 0))

    if action is Action.COMMENT_REVIEW:
        return github.post_issue_comment(repo, pr, body)

    if action in (Action.PROPOSE_FIX, Action.ESCALATE):
        # We surface both as a PR comment. Actually opening a fix PR (a branch +
        # /pulls call) is a follow-on we intentionally keep as a comment for now
        # so a human copies the verified diff — see phase-4 decisions, corner #2.
        return github.post_issue_comment(repo, pr, body)

    return ActionResult(ok=True, kind="noop", detail="no action")


def router_node(
    state: PRState,
    github: GitHubClient | None = None,
    session_factory=None,
) -> dict:
    """LangGraph terminal node. Decide, then act-or-queue. Never raises.

    `github` defaults to a FakeGitHubClient (records, no I/O) so the graph is
    safe to run in tests and offline. `session_factory` is a zero-arg callable
    returning a SQLAlchemy Session (e.g. `SessionLocal`); the node opens a
    short-lived session per call for durability. It is a *factory*, not a live
    session, because the graph is compiled once at worker startup and can't carry
    a per-job session — and it decouples the HITL/ledger write from whatever
    transaction the caller is running. When None (pure unit runs) persistence is
    skipped: the decision is only reflected in the returned state, and the auto
    path posts directly (no dedup).

    Idempotency: the worker is at-least-once (a redelivered job re-runs the whole
    graph), so BOTH paths are deduped through the ReviewDecision ledger when a
    factory is present. The gated path enqueues PENDING; the auto path
    get-or-creates a row and skips the post if it's already EXECUTED.
    """
    decision = policy.route(state)

    base: dict[str, object] = {
        "routing_action": decision.action.value,
        "routing_reason": decision.reason,
        "approval_required": decision.requires_approval,
        "action_taken": "none",
    }

    if not decision.is_actionable:
        log.info("router.no_action", repo=state.get("repo"), reason=decision.reason)
        return base

    if decision.requires_approval:
        # Human-in-the-loop: persist, do NOT act.
        if session_factory is not None:
            try:
                with session_factory() as session:
                    row = store.enqueue(session, decision, dict(state))
                    base["decision_id"] = row.id
            except Exception as exc:  # never crash the run on a store hiccup
                log.error("router.enqueue_failed", error=repr(exc))
                base["action_taken"] = "enqueue_failed"
                return base
        base["action_taken"] = "queued_for_approval"
        log.info(
            "router.queued",
            repo=state.get("repo"),
            action=decision.action.value,
            risk=decision.risk,
        )
        return base

    # Auto path: policy says this is safe to do without a human (low-risk
    # comment). Execute immediately — but dedup through the ledger first so a
    # redelivered job doesn't post the same comment twice.
    gh = github or FakeGitHubClient()

    if session_factory is not None:
        try:
            with session_factory() as session:
                row = store.enqueue(session, decision, dict(state))
                base["decision_id"] = row.id
                if row.status == DecisionStatus.EXECUTED:
                    # A prior delivery already posted this. Idempotent no-op.
                    base["action_taken"] = "already_executed"
                    base["action_url"] = row.result_url or ""
                    log.info("router.auto_already_executed", id=row.id, url=row.result_url)
                    return base
                result = execute_decision(gh, decision, state)
                if result.ok:
                    store.mark_executed(session, row, result.url)
                    base["action_taken"] = "executed"
                else:
                    store.mark_failed(session, row, result.detail)
                    base["action_taken"] = "action_failed"
                base["action_url"] = result.url
                log.info(
                    "router.auto_executed",
                    repo=state.get("repo"),
                    action=decision.action.value,
                    ok=result.ok,
                    url=result.url,
                )
                return base
        except Exception as exc:  # never crash the run on a store/post hiccup
            log.error("router.auto_failed", error=repr(exc))
            base["action_taken"] = "action_failed"
            return base

    # No persistence available (pure unit run): decide + act, but can't dedup.
    result = execute_decision(gh, decision, state)
    base["action_taken"] = "executed" if result.ok else "action_failed"
    base["action_url"] = result.url
    log.info(
        "router.auto_executed_no_store",
        repo=state.get("repo"),
        action=decision.action.value,
        ok=result.ok,
        url=result.url,
    )
    return base
