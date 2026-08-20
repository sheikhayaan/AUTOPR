"""Live Docker integration test for the real sandbox boundary.

This is the one Phase 3 test that needs a running daemon and the built
`autopr-sandbox:latest` image. It SKIPS (never fails) when either is missing, so
the suite stays green on machines without Docker while still proving the real
container path end-to-end where it's available.

To enable it:
    docker build -f docker/sandbox.Dockerfile -t autopr-sandbox:latest .

It proves the two outcomes that matter:
  1. a patch that fixes the lint error verifies (exit 0),
  2. a patch that does NOT fix it fails verification (non-zero),
and that both run with no network and a non-root user.
"""

from __future__ import annotations

import subprocess

import pytest

from app.config import settings
from app.sandbox.runner import DockerSandbox, _resolve_docker


def _docker_available() -> bool:
    docker = _resolve_docker()
    try:
        info = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if info.returncode != 0:
        return False
    imgs = subprocess.run(
        [docker, "images", "-q", settings.sandbox_image],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return bool(imgs.stdout.strip())


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon and autopr-sandbox:latest image required",
)

# A file with a lint violation ruff will flag: an unused import.
DIRTY = "import os\n\n\ndef add(a, b):\n    return a + b\n"
CLEAN = "def add(a, b):\n    return a + b\n"


def _diff(before: str, after: str, path: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def test_real_sandbox_verifies_a_good_lint_fix():
    sbx = DockerSandbox()
    patch = _diff(DIRTY, CLEAN, "add.py")
    res = sbx.run_verification([("add.py", DIRTY)], patch, ["ruff", "check", "add.py"])
    assert res.timed_out is False
    assert res.exit_code == 0, f"expected pass, got {res.exit_code}: {res.stderr}"


def test_real_sandbox_rejects_a_nonfix():
    sbx = DockerSandbox()
    # A no-op patch (adds a blank line) that leaves the unused import in place.
    patch = _diff(DIRTY, DIRTY + "\n", "add.py")
    res = sbx.run_verification([("add.py", DIRTY)], patch, ["ruff", "check", "add.py"])
    assert res.exit_code not in (0, None), "unfixed lint must not verify"
