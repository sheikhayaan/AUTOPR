"""CI Monitor agent.

Parses failing GitHub Actions logs and classifies the failure into one of a
fixed taxonomy. The classification + diagnosis is what the Fix Agent scopes its
patch to, so precision here matters — a wrong classification produces a wrong
fix.

Only fires on CI-failure events (enforced by a conditional edge in the graph,
not here). If it cannot confidently diagnose a cause, it says so, and the Fix
Agent is skipped (also enforced by a conditional edge).
"""

from __future__ import annotations

import structlog

from app.agents.common import invoke_llm, message_text, parse_json
from app.agents.llm import get_llm
from app.agents.state import PRState

log = structlog.get_logger()

# Fixed taxonomy from the spec. "unknown" is added so the agent can decline
# rather than guess — declining routes away from the Fix Agent.
FAILURE_TYPES = {"lint", "type_error", "test", "import", "dependency", "unknown"}

SYSTEM_PROMPT = """You are a CI failure triage engineer. Given raw CI logs from
a failed GitHub Actions run, you classify the ROOT CAUSE and extract the
relevant evidence. Return STRICT JSON only.

Schema:
{
  "failure_type": "lint|type_error|test|import|dependency|unknown",
  "diagnosis": "1-2 sentences: what failed and the root cause",
  "excerpt": "the specific log lines that show the failure (verbatim)"
}

Taxonomy:
- lint: style/format violations (flake8, eslint, black --check, ruff).
- type_error: static type checker failures (mypy, pyright, tsc).
- test: an assertion or test case failed (pytest, jest) — real logic failure.
- import: ModuleNotFoundError / ImportError / undefined reference.
- dependency: version conflict, unresolvable requirement, lockfile mismatch.
- unknown: you cannot confidently determine the cause from these logs.

Rules:
- Diagnose the ROOT cause, not a downstream symptom. A test failing because of
  a missing import is `import`, not `test`.
- If the logs are truncated, ambiguous, or show multiple unrelated failures
  with no clear primary, return "unknown". Do not guess."""


def ci_monitor_node(state: PRState) -> dict:
    """LangGraph node. Returns failure_type/diagnosis/excerpt.

    Assumes ci_logs is present (the conditional edge guarantees this node only
    runs on CI-failure events). Defensive fallback to 'unknown' if not.
    """
    logs = state.get("ci_logs", "")
    if not logs.strip():
        log.warning("ci_monitor.no_logs", repo=state["repo"], pr=state["pr_number"])
        return {
            "failure_type": "unknown",
            "failure_diagnosis": "No CI logs available to diagnose.",
            "failure_excerpt": "",
        }

    # Truncate very long logs to the tail (failures are usually at the end).
    if len(logs) > 12000:
        logs = "... (head truncated) ...\n" + logs[-12000:]

    llm = get_llm(temperature=0.0)
    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", f"## CI logs from failed run:\n```\n{logs}\n```\n\nClassify the failure."),
    ]
    response = invoke_llm(llm, messages)
    data = parse_json(message_text(response))

    ftype = str(data.get("failure_type", "unknown")).lower().strip()
    if ftype not in FAILURE_TYPES:
        log.warning("ci_monitor.invalid_type", got=ftype)
        ftype = "unknown"

    log.info(
        "ci_monitor.done",
        repo=state["repo"],
        pr=state["pr_number"],
        failure_type=ftype,
    )
    return {
        "failure_type": ftype,
        "failure_diagnosis": data.get("diagnosis", ""),
        "failure_excerpt": data.get("excerpt", ""),
    }
