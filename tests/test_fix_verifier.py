"""Tests for the Fix Verifier node using a FakeSandbox — no Docker daemon.

We assert on how the node maps a (canned) sandbox result to the state verdict,
and on all the short-circuit paths (no fix, not verifiable, no snapshot, sandbox
error). The real container is exercised separately in test_sandbox_docker.py,
which skips when no daemon is present.
"""

from __future__ import annotations

from app.sandbox.runner import FakeSandbox, SandboxResult
from app.sandbox.verifier import fix_verifier_node

SNAPSHOT = [("app/foo.py", "def foo():\n    return 1\n")]
BASE = {
    "repo": "o/r",
    "pr_number": 1,
    "changed_files": [{"path": "app/foo.py", "patch": "x"}],
    "failure_type": "lint",
    "proposed_fix": "--- a/app/foo.py\n+++ b/app/foo.py\n@@\n-x\n+y\n",
    "repo_snapshot": SNAPSHOT,
}


def _ok():
    return SandboxResult(exit_code=0, stdout="All checks passed", stderr="", timed_out=False)


def _fail(code=1):
    return SandboxResult(exit_code=code, stdout="", stderr="E501 line too long", timed_out=False)


def test_verified_when_sandbox_exits_zero():
    sbx = FakeSandbox(_ok())
    out = fix_verifier_node(dict(BASE), sandbox=sbx)
    assert out["fix_verified"] is True
    assert out["verification_reason"] == "passed"
    # It ran the lint command against our snapshot + patch.
    assert len(sbx.calls) == 1
    assert sbx.calls[0]["command"][0] == "ruff"
    assert sbx.calls[0]["repo_files"] == SNAPSHOT


def test_not_verified_when_check_fails():
    out = fix_verifier_node(dict(BASE), sandbox=FakeSandbox(_fail()))
    assert out["fix_verified"] is False
    assert out["verification_reason"] == "check_failed"
    # Failure output is captured (fed back into the retry prompt).
    assert "E501" in out["verification_output"]


def test_patch_apply_failure_is_distinct_reason():
    from app.sandbox.policy import PATCH_FAILED_EXIT

    res = SandboxResult(
        exit_code=PATCH_FAILED_EXIT, stdout="", stderr="does not apply", timed_out=False
    )
    out = fix_verifier_node(dict(BASE), sandbox=FakeSandbox(res))
    assert out["verification_reason"] == "patch_apply_failed"


def test_timeout_is_not_verified():
    res = SandboxResult(exit_code=None, stdout="", stderr="", timed_out=True)
    out = fix_verifier_node(dict(BASE), sandbox=FakeSandbox(res))
    assert out["fix_verified"] is False
    assert out["verification_reason"] == "timeout"


def test_no_fix_short_circuits_without_sandbox():
    state = dict(BASE)
    state["proposed_fix"] = ""
    sbx = FakeSandbox(_ok())
    out = fix_verifier_node(state, sandbox=sbx)
    assert out["fix_verified"] is False
    assert out["verification_reason"] == "no_fix"
    assert sbx.calls == []  # never touched the sandbox


def test_non_verifiable_failure_type_short_circuits():
    state = dict(BASE)
    state["failure_type"] = "dependency"
    sbx = FakeSandbox(_ok())
    out = fix_verifier_node(state, sandbox=sbx)
    assert out["verification_reason"] == "not_verifiable"
    assert sbx.calls == []


def test_missing_snapshot_short_circuits():
    state = dict(BASE)
    del state["repo_snapshot"]
    sbx = FakeSandbox(_ok())
    out = fix_verifier_node(state, sandbox=sbx)
    assert out["verification_reason"] == "no_snapshot"
    assert sbx.calls == []


def test_sandbox_exception_degrades_to_unverified():
    class _Boom:
        def run_verification(self, *a, **k):
            raise RuntimeError("daemon down")

    out = fix_verifier_node(dict(BASE), sandbox=_Boom())
    assert out["fix_verified"] is False
    assert out["verification_reason"] == "sandbox_error"
    assert "daemon down" in out["verification_output"]
