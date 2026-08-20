"""Verification policy: how each diagnosed failure is *proved fixed*.

The Fix Agent emits a patch. Before we trust it, we re-run — inside a sandbox —
the check that would have caught the original failure. This module is the pure,
Docker-free half of that: it decides *what command* re-checks each failure type,
and *how to read* the resulting exit code. Keeping it pure means the whole
policy is unit-testable without a daemon (see tests/test_sandbox_policy.py).

Design points:
- Not every failure is auto-verifiable. `dependency` fixes need `pip install`,
  which needs network — and the sandbox runs with `--network none` on purpose.
  `unknown` never reaches here (the graph gates it). Both map to None ("no
  automated verification; escalate to a human"), which is an honest answer, not
  a failure.
- Checks are scoped to the changed files where it matters (mypy on the whole
  repo would surface unrelated pre-existing errors and fail a good fix). pytest
  runs the whole suite by design — a fix that greens the target test but breaks
  another is not a fix.
"""

from __future__ import annotations

from dataclasses import dataclass

# Sentinel exit code emitted by the sandbox entrypoint when `git apply` rejects
# the patch. Distinct from any verifier tool's exit code so we can tell
# "the patch didn't even apply" apart from "the patch applied but didn't fix it".
PATCH_FAILED_EXIT = 3

# Failure types the CI Monitor can emit. A subset is auto-verifiable.
VERIFIABLE = frozenset({"lint", "type_error", "test", "import"})
# These are real diagnoses but not safely auto-verifiable offline.
NON_VERIFIABLE = frozenset({"dependency", "unknown"})


def verification_command(
    failure_type: str, changed_paths: list[str] | None = None
) -> list[str] | None:
    """Return the shell argv that re-checks `failure_type`, or None if the
    failure is not auto-verifiable.

    The command runs from the repo root *after* the patch has been applied. It
    must exit 0 iff the diagnosed problem is resolved.
    """
    paths = [p for p in (changed_paths or []) if p.endswith(".py")]

    if failure_type == "lint":
        # ruff is fast and its exit code is 0 exactly when there are no lint
        # violations. Scope to changed files: a clean fix shouldn't be blocked
        # by unrelated pre-existing lint elsewhere in the repo.
        return ["ruff", "check", *(paths or ["."])]

    if failure_type == "type_error":
        # mypy over the whole repo would fail on unrelated pre-existing type
        # debt. Scope to the files the PR touched so we verify *this* fix.
        return ["mypy", "--ignore-missing-imports", *(paths or ["."])]

    if failure_type == "test":
        # Whole suite, on purpose: a patch that greens the target test but
        # reddens another has not fixed the build.
        return ["pytest", "-q", "-p", "no:cacheprovider"]

    if failure_type == "import":
        # An ImportError re-surfaces the moment pytest tries to collect. Collect
        # -only is cheap and re-triggers every module import without running
        # tests.
        return ["pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]

    # dependency / unknown / anything unexpected -> not auto-verifiable.
    return None


@dataclass(frozen=True)
class Verdict:
    """The interpreted outcome of a sandbox run."""

    verified: bool
    reason: str  # short machine-ish label for logs/routing
    detail: str  # human-facing one-liner


def interpret(exit_code: int | None, timed_out: bool) -> Verdict:
    """Turn a raw sandbox result into a pass/fail verdict with a reason.

    - timed_out            -> not verified (a fix that hangs is not a fix)
    - PATCH_FAILED_EXIT    -> not verified, and specifically "patch didn't apply"
    - exit 0               -> verified
    - any other exit code  -> not verified (the check still fails)
    """
    if timed_out:
        return Verdict(False, "timeout", "Verification timed out in the sandbox.")
    if exit_code == PATCH_FAILED_EXIT:
        return Verdict(False, "patch_apply_failed", "The proposed patch did not apply cleanly.")
    if exit_code == 0:
        return Verdict(True, "passed", "Fix verified: the check now passes.")
    return Verdict(False, "check_failed", f"The check still fails (exit {exit_code}).")
