"""The routing decision — pure, side-effect-free, the crown jewel of Phase 4.

`route(state)` looks at the finished pipeline state and returns exactly one
`RoutingDecision`: what action the system wants to take, whether that action
requires human approval before it happens, and the human-readable body it would
post. It performs no I/O — no GitHub call, no DB write, no LLM. That is the whole
point: the *disposition rules* are a pure function you can exhaust in a unit
test, separate from the messy business of actually calling GitHub.

The two tracks converge here:

  PR-open track  -> we have review_findings + risk_score + summary.
     trivial/low risk  -> COMMENT_REVIEW, auto (safe: a comment changes no code)
     medium/high risk  -> COMMENT_REVIEW, human-gated (a maintainer signs off
                          before the bot speaks on a risky change)

  CI-failure track -> we have a fix verdict.
     fix_verified              -> PROPOSE_FIX, ALWAYS human-gated (it changes
                                  code; Phase 3 decision #6 forbids auto-promote)
     unverified / escalated    -> ESCALATE, human-gated (a human must look)
     nothing diagnosable       -> NONE (don't spam a PR with "I got nothing")

The approval flag is the human-in-the-loop gate. `requires_approval=False` means
the router may act immediately; `True` means the action is queued and a human
approves it through the ops API before it fires.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.agents.state import PRState


class Action(str, Enum):
    """What the system wants to do with a finished pipeline result."""

    COMMENT_REVIEW = "comment_review"  # post the code review as a PR comment
    PROPOSE_FIX = "propose_fix"  # promote a verified fix (open PR / comment)
    ESCALATE = "escalate"  # flag for a human; system will not act
    NONE = "none"  # nothing actionable


# Risk levels in ascending order of concern. The index is the comparison key.
RISK_ORDER = ("trivial", "low", "medium", "high")


def risk_rank(risk: str) -> int:
    """Ordinal for a risk label; unknown labels rank as high (most cautious)."""
    try:
        return RISK_ORDER.index(risk)
    except ValueError:
        return len(RISK_ORDER) - 1  # treat anything unrecognized as 'high'


@dataclass(frozen=True)
class RoutingDecision:
    """The disposition of one pipeline run. Immutable and fully self-describing.

    `requires_approval` is the human-in-the-loop gate. `dedup_key` makes the
    downstream persistence idempotent (a re-run of the same commit's same action
    collides instead of double-posting).
    """

    action: Action
    requires_approval: bool
    risk: str
    reason: str  # machine-ish label for logs/routing (e.g. "low_risk_auto")
    title: str  # short human summary of the decision
    body: str  # the content that would be posted, if any

    @property
    def is_actionable(self) -> bool:
        return self.action is not Action.NONE

    def dedup_key(self, state: Mapping[str, Any]) -> str:
        return "|".join(
            (
                state.get("repo", "?"),
                str(state.get("pr_number", 0)),
                state.get("commit_sha", "?"),
                self.action.value,
            )
        )


# --- body renderers ----------------------------------------------------------
# Kept here (not in the agents) because they format a *decision*, not model
# output. Plain markdown; no secrets; safe to post verbatim.


def _render_review_body(state: PRState) -> str:
    findings = state.get("review_findings", []) or []
    summary = state.get("summary", "") or "(no summary produced)"
    risk = state.get("risk_score", "medium")
    lines = [
        "## 🤖 AutoPR review",
        "",
        f"**Risk:** `{risk}`",
        "",
        summary.strip(),
    ]
    if findings:
        lines += ["", "### Findings"]
        for f in findings:
            sev = str(f.get("severity", "info")).lower()
            icon = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(sev, "🔵")
            loc = f"`{f.get('file', '?')}:{f.get('line', '?')}`"
            lines.append(f"- {icon} {loc} — {f.get('message', '').strip()}")
    else:
        lines += ["", "_No blocking issues found._"]
    return "\n".join(lines)


def _render_fix_body(state: PRState) -> str:
    diag = state.get("failure_diagnosis", "") or "(no diagnosis)"
    ftype = state.get("failure_type", "unknown")
    fix = state.get("proposed_fix", "") or ""
    return "\n".join(
        [
            "## 🤖 AutoPR proposed fix (sandbox-verified)",
            "",
            f"**Failure type:** `{ftype}`",
            "",
            f"**Diagnosis:** {diag.strip()}",
            "",
            "This patch was applied to a snapshot of the repo and the matching "
            "check passed inside an isolated sandbox. A maintainer must approve "
            "before it is applied to the real branch.",
            "",
            "```diff",
            fix.strip(),
            "```",
        ]
    )


def _render_escalation_body(state: PRState) -> str:
    ftype = state.get("failure_type", "unknown")
    diag = state.get("failure_diagnosis", "") or "(no diagnosis)"
    reason = state.get("verification_reason", "") or "n/a"
    out = state.get("verification_output", "") or ""
    parts = [
        "## 🤖 AutoPR could not auto-fix this — human needed",
        "",
        f"**Failure type:** `{ftype}`",
        "",
        f"**Diagnosis:** {diag.strip()}",
        "",
        f"**Why not auto-fixed:** `{reason}`",
    ]
    if out.strip():
        parts += [
            "",
            "<details><summary>Sandbox output</summary>",
            "",
            "```",
            out.strip()[-1500:],
            "```",
            "</details>",
        ]
    return "\n".join(parts)


# --- the decision ------------------------------------------------------------


def _route_ci(state: PRState) -> RoutingDecision:
    """CI-failure track disposition (a diagnosis, maybe a verified fix)."""
    risk = state.get("risk_score", "medium")

    if state.get("fix_verified"):
        # A proven fix. It still changes code -> never auto-promoted.
        return RoutingDecision(
            action=Action.PROPOSE_FIX,
            requires_approval=True,
            risk=risk,
            reason="verified_fix_needs_promotion",
            title="Verified fix ready for maintainer approval",
            body=_render_fix_body(state),
        )

    ftype = state.get("failure_type", "unknown")
    if ftype == "unknown" and not state.get("failure_diagnosis"):
        # Couldn't even diagnose — nothing useful to say.
        return RoutingDecision(
            action=Action.NONE,
            requires_approval=False,
            risk=risk,
            reason="no_diagnosis",
            title="No actionable CI diagnosis",
            body="",
        )

    # Diagnosed but not fixed (not_verifiable, sandbox_error, retries exhausted,
    # or no fix produced). Put it in front of a human with the evidence.
    return RoutingDecision(
        action=Action.ESCALATE,
        requires_approval=True,
        risk=risk,
        reason=state.get("verification_reason", "unfixed"),
        title="CI failure needs human attention",
        body=_render_escalation_body(state),
    )


def _route_pr(state: PRState) -> RoutingDecision:
    """PR-open track disposition (a review + a risk score)."""
    risk = state.get("risk_score", "medium")
    from app.config import settings

    # Auto-post only at or below the configured risk ceiling. Default ceiling is
    # 'low', so medium/high always wait for a human.
    ceiling = risk_rank(settings.auto_comment_max_risk)
    # Hand-off mode never auto-posts: AutoPR holds no write credential, so every
    # review is routed to a human who acts on their own GitHub account.
    auto = risk_rank(risk) <= ceiling and not settings.handoff_mode
    return RoutingDecision(
        action=Action.COMMENT_REVIEW,
        requires_approval=not auto,
        risk=risk,
        reason="low_risk_auto" if auto else "elevated_risk_needs_approval",
        title=("Auto-posting low-risk review" if auto else "Review held for maintainer approval"),
        body=_render_review_body(state),
    )


def route(state: PRState) -> RoutingDecision:
    """Decide the disposition of a finished pipeline run. Pure, total function.

    Track is inferred from what the pipeline produced: a CI-failure run carries
    a `failure_type`; a PR-open run carries a `risk_score` from the reviewer.
    """
    # CI-failure track leaves failure_type in the state; PR track never does.
    if state.get("failure_type") is not None or state.get("ci_logs") or state.get("ci_event"):
        return _route_ci(state)
    return _route_pr(state)
