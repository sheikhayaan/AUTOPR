"""Test Generator agent.

Analyzes modified code and generates unit tests for uncovered logic. Grounded
with Qdrant RAG so generated tests match the repo's existing test conventions
(framework, fixtures, naming, assertion style) rather than a generic template.

Output is test code as a string in `state["generated_tests"]`. We do NOT run or
apply it here — surfacing/verification is later-phase concern. The value is a
reviewable test suggestion attached to the PR.
"""

from __future__ import annotations

import structlog

from app.agents.common import format_changed_files, invoke_llm, message_text
from app.agents.llm import get_llm
from app.agents.state import PRState

log = structlog.get_logger()

SYSTEM_PROMPT = """You are a test engineer. Given a code diff, you write unit
tests that cover the NEW or CHANGED logic — especially edge cases, error
paths, and boundary conditions that are currently untested.

Rules:
- Match the repo's existing test framework and conventions (shown as context
  if available). If none is given, use pytest for Python.
- Only test logic that is actually in the diff. Do not invent tests for code
  you cannot see.
- Prefer a few high-value tests over many trivial ones.
- Output ONLY the test code, ready to save to a file. No prose, no markdown
  fences, no explanation."""


def _build_user_prompt(state: PRState, rag_context: str) -> str:
    files_str = format_changed_files(state["changed_files"])
    context_block = ""
    if rag_context:
        context_block = (
            f"\n## Existing test patterns in this repo (match these conventions):\n{rag_context}\n"
        )
    return (
        f"## Changed files to test:\n{files_str}\n"
        f"{context_block}\n"
        "Write unit tests covering the changed logic. Output only the test code."
    )


def test_generator_node(state: PRState, rag=None) -> dict:
    """LangGraph node. Returns {'generated_tests': <code>}."""
    rag_context = ""
    if rag is not None and state.get("changed_files"):
        # Bias retrieval toward existing test files by querying with "test".
        query = "test " + " ".join(f["path"] for f in state["changed_files"][:5])
        try:
            hits = rag.retrieve(query, top_k=3)
            rag_context = "\n---\n".join(
                f"{h['file_path']}:\n{h['content']}"
                for h in hits
                if "test" in h.get("file_path", "").lower()
            )
        except Exception as exc:
            log.warning("test_generator.rag_failed", error=repr(exc))

    llm = get_llm(temperature=0.0)
    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", _build_user_prompt(state, rag_context)),
    ]
    response = invoke_llm(llm, messages)
    tests = message_text(response).strip()

    # Strip accidental markdown fences if the model added them.
    if tests.startswith("```"):
        lines = tests.split("\n")
        tests = "\n".join(lines[1:-1]) if len(lines) > 2 else tests

    log.info(
        "test_generator.done",
        repo=state["repo"],
        pr=state["pr_number"],
        n_chars=len(tests),
    )
    return {"generated_tests": tests}
