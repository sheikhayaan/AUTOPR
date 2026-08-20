"""LangGraph state schema for the PR automation pipeline.

The state flows through all agents. Each agent reads what it needs and writes
its outputs. Clean separation: Code Reviewer doesn't see CI logs, CI Monitor
doesn't see test generation, etc.

Risk score drives Phase 4 routing (trivial/low → auto-comment; medium/high →
human queue). The Explainer requirement from the spec is folded into Code
Reviewer's `summary` field rather than a separate node.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class PRState(TypedDict):
    """Shared state object flowing through the LangGraph pipeline."""

    # --- Input (set by worker before graph entry) ---
    repo: str
    pr_number: int
    commit_sha: str
    changed_files: list[dict[str, str]]  # [{"path": ..., "patch": ...}, ...]
    # Optional: CI event data if this is a ci-failure webhook
    ci_logs: NotRequired[str]
    ci_event: NotRequired[str]  # "check_run", "workflow_run", etc.

    # --- Code Reviewer outputs ---
    review_findings: NotRequired[list[dict[str, str]]]
    # Each finding: {"file": ..., "line": ..., "message": ..., "severity": ...}
    risk_score: NotRequired[str]  # "trivial" | "low" | "medium" | "high"
    summary: NotRequired[str]  # Plain-language summary (folding in Explainer)

    # --- Test Generator output ---
    generated_tests: NotRequired[str]  # test code as a string

    # --- CI Monitor outputs (only if ci_logs present) ---
    failure_type: NotRequired[str]  # "lint" | "type_error" | "test" | "import" | "dependency"
    failure_diagnosis: NotRequired[str]  # What failed and why
    failure_excerpt: NotRequired[str]  # Relevant log lines

    # --- Fix Agent output (only if CI Monitor diagnosed a cause) ---
    proposed_fix: NotRequired[str]  # unified diff (empty string = no fix produced)
    fix_attempts: NotRequired[int]  # how many times the Fix Agent has run this run

    # --- Fix Verifier outputs (Phase 3: sandboxed proof the fix works) ---
    # A read-only snapshot of the repo the patch applies to: [(path, content)].
    # Set by the worker before graph entry; without it the verifier can't run.
    repo_snapshot: NotRequired[list[tuple[str, str]]]
    fix_verified: NotRequired[bool]  # did the patch pass its verification check?
    verification_reason: NotRequired[str]  # machine label: passed/check_failed/...
    verification_output: NotRequired[str]  # captured sandbox stdout+stderr (tail)

    # --- Shared RAG context (populated on demand by agents) ---
    rag_context: NotRequired[str]  # similar code snippets from Qdrant

    # --- Phase 4: routing / human-in-the-loop disposition (set by router) ---
    routing_action: NotRequired[str]  # Action enum value the router chose
    routing_reason: NotRequired[str]  # machine label for the decision
    approval_required: NotRequired[bool]  # was a human gate required?
    action_taken: NotRequired[str]  # executed/queued_for_approval/none/...
    action_url: NotRequired[str]  # where an auto action landed (if any)
    decision_id: NotRequired[int]  # ReviewDecision row id when queued
