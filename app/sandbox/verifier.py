"""Fix Verifier node.

Closes the loop the Fix Agent opens: the agent *proposes* a patch, this node
*proves* it. It applies the patch to a snapshot of the repo inside the sandbox,
re-runs the check that matches the diagnosed failure, and writes a verdict back
to the state. The graph then either finishes (verified) or loops back to the Fix
Agent with the failure output as feedback (see graph._after_fix_verifier).

What this node does NOT do: apply the fix to the real repo, push, or comment.
Promotion of a verified fix is a Phase 4 human-gated action. This node's only
authority is to say "this patch does / does not resolve the diagnosed failure."
"""

from __future__ import annotations

import structlog

from app.agents.state import PRState
from app.sandbox import policy
from app.sandbox.runner import DockerSandbox, Sandbox

log = structlog.get_logger()

_OUTPUT_TAIL = 4000  # cap captured logs fed back into the retry prompt


def fix_verifier_node(state: PRState, sandbox: Sandbox | None = None) -> dict:
    """LangGraph node. Verifies state['proposed_fix'] in the sandbox.

    Returns a partial state update with fix_verified / verification_reason /
    verification_output. Never raises: an infra failure (no daemon, image
    missing) is reported as unverified so the run degrades to human review
    rather than crashing the worker.
    """
    repo = state.get("repo", "?")
    pr = state.get("pr_number", 0)
    fix = state.get("proposed_fix", "")

    # Nothing to verify — Fix Agent produced no patch (unknown/no-fix). Not a
    # failure of verification; there's simply nothing to prove.
    if not fix:
        return {
            "fix_verified": False,
            "verification_reason": "no_fix",
            "verification_output": "",
        }

    ftype = state.get("failure_type", "unknown")
    changed = [f.get("path", "") for f in state.get("changed_files", [])]
    command = policy.verification_command(ftype, changed)
    if command is None:
        # e.g. dependency fix: can't verify offline. Honest non-result.
        log.info("verifier.not_verifiable", repo=repo, pr=pr, failure_type=ftype)
        return {
            "fix_verified": False,
            "verification_reason": "not_verifiable",
            "verification_output": (
                f"No offline verification exists for failure_type={ftype!r}; "
                "escalating to human review."
            ),
        }

    snapshot = state.get("repo_snapshot")
    if not snapshot:
        log.warning("verifier.no_snapshot", repo=repo, pr=pr)
        return {
            "fix_verified": False,
            "verification_reason": "no_snapshot",
            "verification_output": "No repo snapshot available to apply the patch.",
        }

    sbx = sandbox or DockerSandbox()
    try:
        result = sbx.run_verification(snapshot, fix, command)
    except Exception as exc:  # daemon down, image missing, etc.
        log.error("verifier.sandbox_error", repo=repo, pr=pr, error=repr(exc))
        return {
            "fix_verified": False,
            "verification_reason": "sandbox_error",
            "verification_output": f"Sandbox could not run: {exc!r}",
        }

    verdict = policy.interpret(result.exit_code, result.timed_out)
    combined = (result.stdout + "\n" + result.stderr).strip()
    log.info(
        "verifier.done",
        repo=repo,
        pr=pr,
        failure_type=ftype,
        verified=verdict.verified,
        reason=verdict.reason,
    )
    return {
        "fix_verified": verdict.verified,
        "verification_reason": verdict.reason,
        "verification_output": combined[-_OUTPUT_TAIL:],
    }
