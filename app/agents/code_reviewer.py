"""Code Reviewer agent.

Reviews changed files and produces three things (per the spec):
  1. Findings: bugs / code smells with file + line + severity.
  2. A risk score (trivial/low/medium/high) based on change scope, blast
     radius, and test-coverage delta. This score drives Phase 4 routing.
  3. A plain-language summary per the Explainer requirement — folded in here
     as a `summary` field rather than a separate LangGraph node (the spec
     explicitly did NOT want a 5th node that just rephrases output).

Grounded with Qdrant RAG: we retrieve similar functions/patterns from the repo
so the reviewer flags deviations from existing conventions rather than
inventing rules.
"""

from __future__ import annotations

import structlog

from app.agents.common import format_changed_files, invoke_llm, message_text, parse_json
from app.agents.llm import get_llm
from app.agents.state import PRState

log = structlog.get_logger()

VALID_RISK = {"trivial", "low", "medium", "high"}

SYSTEM_PROMPT = """You are a senior software engineer performing a code review.
You review diffs and return STRICT JSON only — no prose outside the JSON.

Assess the change and return this exact schema:
{
  "findings": [
    {"file": "path", "line": <int>, "severity": "info|warning|error",
     "message": "concise description of the bug or smell"}
  ],
  "risk_score": "trivial|low|medium|high",
  "summary": "2-3 sentence plain-language summary a non-expert can understand"
}

Risk scoring rubric:
- trivial: docs/comments/formatting only, no logic change.
- low: small localized logic change, well-tested area, low blast radius.
- medium: touches shared/core logic, or adds meaningful behavior, or reduces
  test coverage, or spans multiple modules.
- high: touches auth/security/data-integrity/migrations, wide blast radius,
  or removes tests / significantly lowers coverage.

Base risk on: scope of change, blast radius (how many callers/modules are
affected), and test-coverage delta. Be conservative: when unsure between two
levels, pick the higher one."""


def _build_user_prompt(state: PRState, rag_context: str) -> str:
    files_str = format_changed_files(state["changed_files"])
    context_block = ""
    if rag_context:
        context_block = (
            f"\n## Existing repo patterns (for grounding — flag deviations):\n{rag_context}\n"
        )
    return (
        f"## PR: {state['repo']}#{state['pr_number']} @ {state['commit_sha'][:8]}\n\n"
        f"## Changed files:\n{files_str}\n"
        f"{context_block}\n"
        "Review the changes and return the JSON schema. Focus on real bugs and "
        "material smells; do not nitpick style the linter would catch."
    )


def code_reviewer_node(state: PRState, rag=None) -> dict:
    """LangGraph node. Returns the state keys this agent updates.

    `rag` is an optional RepoRAG instance; when provided we retrieve grounding
    context. Kept as a param (not a global) so tests can pass None or a fake.
    """
    rag_context = ""
    if rag is not None and state.get("changed_files"):
        # Query with the changed file paths + first patch snippet.
        query = " ".join(f["path"] for f in state["changed_files"][:5])
        try:
            hits = rag.retrieve(query, top_k=3)
            rag_context = "\n---\n".join(f"{h['file_path']}:\n{h['content']}" for h in hits)
        except Exception as exc:  # RAG is best-effort grounding, never fatal
            log.warning("code_reviewer.rag_failed", error=repr(exc))

    llm = get_llm(temperature=0.0)
    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", _build_user_prompt(state, rag_context)),
    ]
    response = invoke_llm(llm, messages)
    data = parse_json(message_text(response))

    # Validate/normalize risk score.
    risk = str(data.get("risk_score", "medium")).lower().strip()
    if risk not in VALID_RISK:
        log.warning("code_reviewer.invalid_risk", got=risk)
        risk = "medium"  # safe default routes to human review in Phase 4

    findings = data.get("findings", [])
    summary = data.get("summary", "")

    log.info(
        "code_reviewer.done",
        repo=state["repo"],
        pr=state["pr_number"],
        risk=risk,
        n_findings=len(findings),
    )
    return {
        "review_findings": findings,
        "risk_score": risk,
        "summary": summary,
        "rag_context": rag_context,
    }
