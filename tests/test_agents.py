"""Unit tests for each agent node with a MOCKED LLM.

No network, no Groq key needed. We patch `get_llm` (so no real ChatGroq is
constructed) and `invoke_llm` (to return a canned response) in each agent's
module namespace. This isolates the agent's parsing/validation/routing logic
from the model itself — which is exactly what we can assert on deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch


@dataclass
class FakeResponse:
    """Stand-in for a LangChain AIMessage (only .content is used)."""

    content: str


def _mock_agent_llm(module, response_text: str):
    """Context managers patching get_llm + invoke_llm in an agent module."""
    return (
        patch.object(module, "get_llm", return_value=object()),
        patch.object(module, "invoke_llm", return_value=FakeResponse(response_text)),
    )


BASE_STATE = {
    "repo": "octocat/hello-world",
    "pr_number": 7,
    "commit_sha": "abc123def456",
    "changed_files": [{"path": "app/foo.py", "patch": "+def foo():\n+    return 1/0"}],
}


# --- Code Reviewer -----------------------------------------------------
def test_code_reviewer_parses_output():
    from app.agents import code_reviewer

    payload = (
        '{"findings": [{"file": "app/foo.py", "line": 2, "severity": "error",'
        ' "message": "division by zero"}], "risk_score": "high",'
        ' "summary": "Adds a function that always divides by zero."}'
    )
    p1, p2 = _mock_agent_llm(code_reviewer, payload)
    with p1, p2:
        out = code_reviewer.code_reviewer_node(dict(BASE_STATE), rag=None)

    assert out["risk_score"] == "high"
    assert len(out["review_findings"]) == 1
    assert out["review_findings"][0]["message"] == "division by zero"
    assert "divides by zero" in out["summary"]


def test_code_reviewer_invalid_risk_defaults_to_medium():
    from app.agents import code_reviewer

    payload = '{"findings": [], "risk_score": "catastrophic", "summary": "x"}'
    p1, p2 = _mock_agent_llm(code_reviewer, payload)
    with p1, p2:
        out = code_reviewer.code_reviewer_node(dict(BASE_STATE), rag=None)
    # Unknown risk label -> safe default that routes to human review in Phase 4.
    assert out["risk_score"] == "medium"


def test_code_reviewer_handles_fenced_json():
    from app.agents import code_reviewer

    payload = '```json\n{"findings": [], "risk_score": "low", "summary": "ok"}\n```'
    p1, p2 = _mock_agent_llm(code_reviewer, payload)
    with p1, p2:
        out = code_reviewer.code_reviewer_node(dict(BASE_STATE), rag=None)
    assert out["risk_score"] == "low"


# --- Test Generator ----------------------------------------------------
def test_test_generator_strips_fences():
    from app.agents import test_generator

    payload = "```python\ndef test_foo():\n    assert foo() == 1\n```"
    p1, p2 = _mock_agent_llm(test_generator, payload)
    with p1, p2:
        out = test_generator.test_generator_node(dict(BASE_STATE), rag=None)
    assert out["generated_tests"].startswith("def test_foo")
    assert "```" not in out["generated_tests"]


# --- CI Monitor --------------------------------------------------------
def test_ci_monitor_classifies():
    from app.agents import ci_monitor

    state = dict(BASE_STATE)
    state["ci_logs"] = "ImportError: No module named 'requests'"
    payload = (
        '{"failure_type": "import", "diagnosis": "missing requests module",'
        ' "excerpt": "ImportError: No module named requests"}'
    )
    p1, p2 = _mock_agent_llm(ci_monitor, payload)
    with p1, p2:
        out = ci_monitor.ci_monitor_node(state)
    assert out["failure_type"] == "import"
    assert "requests" in out["failure_diagnosis"]


def test_ci_monitor_empty_logs_returns_unknown_without_llm():
    from app.agents import ci_monitor

    state = dict(BASE_STATE)
    state["ci_logs"] = "   "
    # Should short-circuit to unknown WITHOUT calling the LLM.
    with patch.object(ci_monitor, "invoke_llm") as mock_invoke:
        out = ci_monitor.ci_monitor_node(state)
        mock_invoke.assert_not_called()
    assert out["failure_type"] == "unknown"


def test_ci_monitor_invalid_type_defaults_unknown():
    from app.agents import ci_monitor

    state = dict(BASE_STATE)
    state["ci_logs"] = "some failure"
    payload = '{"failure_type": "cosmic_ray", "diagnosis": "d", "excerpt": "e"}'
    p1, p2 = _mock_agent_llm(ci_monitor, payload)
    with p1, p2:
        out = ci_monitor.ci_monitor_node(state)
    assert out["failure_type"] == "unknown"


# --- Fix Agent ---------------------------------------------------------
def test_fix_agent_skips_on_unknown_without_llm():
    from app.agents import fix_agent

    state = dict(BASE_STATE)
    state["failure_type"] = "unknown"
    with patch.object(fix_agent, "invoke_llm") as mock_invoke:
        out = fix_agent.fix_agent_node(state, rag=None)
        mock_invoke.assert_not_called()
    assert out["proposed_fix"] == ""


def test_fix_agent_generates_patch():
    from app.agents import fix_agent

    state = dict(BASE_STATE)
    state["failure_type"] = "import"
    state["failure_diagnosis"] = "missing import"
    diff = "--- a/app/foo.py\n+++ b/app/foo.py\n@@ -1 +1,2 @@\n+import os\n"
    p1, p2 = _mock_agent_llm(fix_agent, diff)
    with p1, p2:
        out = fix_agent.fix_agent_node(state, rag=None)
    assert "import os" in out["proposed_fix"]


def test_fix_agent_honors_no_fix_possible():
    from app.agents import fix_agent

    state = dict(BASE_STATE)
    state["failure_type"] = "test"
    p1, p2 = _mock_agent_llm(fix_agent, "NO_FIX_POSSIBLE")
    with p1, p2:
        out = fix_agent.fix_agent_node(state, rag=None)
    assert out["proposed_fix"] == ""
