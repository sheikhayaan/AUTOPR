"""Tests for the verification policy — the pure, Docker-free half of Phase 3.

Asserts on command selection per failure type and on exit-code interpretation.
No daemon, no subprocess.
"""

from __future__ import annotations

from app.sandbox import policy


# --- verification_command ------------------------------------------------
def test_lint_uses_ruff_scoped_to_changed_py_files():
    cmd = policy.verification_command("lint", ["app/foo.py", "README.md"])
    assert cmd[:2] == ["ruff", "check"]
    assert "app/foo.py" in cmd
    # Non-python paths are dropped from scoping.
    assert "README.md" not in cmd


def test_lint_falls_back_to_dot_without_paths():
    assert policy.verification_command("lint", []) == ["ruff", "check", "."]


def test_type_error_uses_mypy():
    cmd = policy.verification_command("type_error", ["app/foo.py"])
    assert cmd[0] == "mypy"
    assert "app/foo.py" in cmd


def test_test_failure_runs_whole_suite():
    # Whole suite by design: a fix must not red another test.
    cmd = policy.verification_command("test", ["app/foo.py"])
    assert cmd[0] == "pytest"
    assert "--collect-only" not in cmd


def test_import_uses_collect_only():
    cmd = policy.verification_command("import", [])
    assert cmd[0] == "pytest"
    assert "--collect-only" in cmd


def test_dependency_is_not_verifiable_offline():
    # Needs pip install -> needs network -> sandbox has none. Honest None.
    assert policy.verification_command("dependency", ["app/foo.py"]) is None


def test_unknown_is_not_verifiable():
    assert policy.verification_command("unknown", []) is None
    assert policy.verification_command("something_weird", []) is None


# --- interpret -----------------------------------------------------------
def test_interpret_exit_zero_is_verified():
    v = policy.interpret(0, timed_out=False)
    assert v.verified is True
    assert v.reason == "passed"


def test_interpret_nonzero_is_check_failed():
    v = policy.interpret(1, timed_out=False)
    assert v.verified is False
    assert v.reason == "check_failed"


def test_interpret_patch_failed_sentinel():
    v = policy.interpret(policy.PATCH_FAILED_EXIT, timed_out=False)
    assert v.verified is False
    assert v.reason == "patch_apply_failed"


def test_interpret_timeout_beats_exit_code():
    # A hang is never a pass, regardless of any exit code captured.
    v = policy.interpret(0, timed_out=True)
    assert v.verified is False
    assert v.reason == "timeout"
