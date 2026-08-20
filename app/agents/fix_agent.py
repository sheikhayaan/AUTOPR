"""Fix Agent.

Generates a SCOPED patch that addresses the specific cause the CI Monitor
diagnosed. Grounded with RAG so the fix uses the repo's real symbols/imports/
conventions instead of hallucinated ones.

Scope discipline is the whole point: the patch must address only the diagnosed
failure. We instruct the model hard on this and attach the diagnosis + excerpt
as the contract. The patch is emitted as a unified diff string in
`state["proposed_fix"]` — it is NOT applied here. Applying/committing is a
later-phase, human-gated action.

Guardrail: if failure_type is "unknown" this node should not run (enforced by a
conditional edge). We also refuse to touch "dependency" fixes autonomously
beyond suggesting the change, since bumping deps has wide blast radius.
"""

from __future__ import annotations

import structlog

from app.agents.common import format_changed_files, invoke_llm, message_text
from app.agents.llm import get_llm
from app.agents.state import PRState

log = structlog.get_logger()

SYSTEM_PROMPT = """You are a software engineer writing a MINIMAL, SCOPED fix for
a specific CI failure. You are given the diagnosis, the failing log excerpt, and
the changed files.

Output ONLY a unified diff (git diff format) that fixes the diagnosed problem.
No prose, no markdown fences.

Hard rules:
- Fix ONLY the diagnosed failure. Do not refactor, rename, reformat, or "improve"
  anything unrelated. A reviewer must see a tiny, obvious diff.
- Use symbols, imports, and conventions that actually exist in the repo context
  provided. Do not invent APIs.
- If the correct fix is not determinable from the given information, output the
  single line: NO_FIX_POSSIBLE
- Keep the diff limited to the smallest set of lines that resolves the cause."""


def _build_user_prompt(state: PRState, rag_context: str) -> str:
    files_str = format_changed_files(state["changed_files"])
    context_block = ""
    if rag_context:
        context_block = f"\n## Repo context (use these real symbols):\n{rag_context}\n"
    # On a retry, the previous patch failed sandbox verification. Feed the
    # captured output back so the model corrects course instead of repeating
    # the same diff.
    feedback_block = ""
    prior_output = state.get("verification_output", "")
    if state.get("fix_attempts", 0) >= 1 and prior_output:
        feedback_block = (
            "\n## Your previous patch FAILED verification. Do not repeat it.\n"
            "Fix the problem it revealed. Sandbox output:\n"
            f"```\n{prior_output[-2000:]}\n```\n"
        )
    return (
        f"## Diagnosed failure: {state.get('failure_type')}\n"
        f"## Diagnosis: {state.get('failure_diagnosis')}\n"
        f"## Failing log excerpt:\n```\n{state.get('failure_excerpt', '')}\n```\n\n"
        f"## Changed files:\n{files_str}\n"
        f"{context_block}"
        f"{feedback_block}\n"
        "Produce the minimal unified diff that fixes this. If not possible, "
        "output NO_FIX_POSSIBLE."
    )


def fix_agent_node(state: PRState, rag=None) -> dict:
    """LangGraph node. Returns {'proposed_fix': <diff or ''>, 'fix_attempts': n}."""
    ftype = state.get("failure_type", "unknown")
    attempts = state.get("fix_attempts", 0) + 1
    if ftype == "unknown":
        log.info("fix_agent.skipped_unknown", repo=state["repo"], pr=state["pr_number"])
        return {"proposed_fix": "", "fix_attempts": attempts}

    rag_context = ""
    if rag is not None:
        query = state.get("failure_diagnosis", "") or " ".join(
            f["path"] for f in state.get("changed_files", [])[:3]
        )
        try:
            hits = rag.retrieve(query, top_k=4)
            rag_context = "\n---\n".join(f"{h['file_path']}:\n{h['content']}" for h in hits)
        except Exception as exc:
            log.warning("fix_agent.rag_failed", error=repr(exc))

    llm = get_llm(temperature=0.0)
    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", _build_user_prompt(state, rag_context)),
    ]
    response = invoke_llm(llm, messages)
    fix = message_text(response).strip()

    if fix.startswith("```"):
        lines = fix.split("\n")
        fix = "\n".join(lines[1:-1]) if len(lines) > 2 else fix

    if fix == "NO_FIX_POSSIBLE" or not fix:
        log.info("fix_agent.no_fix", repo=state["repo"], pr=state["pr_number"])
        return {"proposed_fix": "", "fix_attempts": attempts}

    log.info(
        "fix_agent.done",
        repo=state["repo"],
        pr=state["pr_number"],
        failure_type=ftype,
        n_chars=len(fix),
        attempt=attempts,
    )
    return {"proposed_fix": fix, "fix_attempts": attempts}
