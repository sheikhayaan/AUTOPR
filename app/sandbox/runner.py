"""The sandbox execution boundary.

`DockerSandbox` applies a proposed patch to a throwaway copy of the repo and
runs a verification command **inside a locked-down container**, then reports the
exit code. Nothing from the patch ever touches the host or the network.

Isolation (defense against a hostile or buggy patch running arbitrary code):
- `--network none`     : no exfiltration, no dependency downloads, no callbacks.
- `--memory/--cpus/--pids-limit` : bounded blast radius; a fork bomb or OOM
  loop dies instead of taking the host down.
- `--read-only` root + `--tmpfs /work` : the container cannot persist anything;
  the writable work area is RAM-backed and vanishes on exit.
- `-v <host_snapshot>:/src:ro` : the repo snapshot + patch go in read-only; the
  container copies them into the tmpfs to work on them.
- runs as the image's non-root `sandbox` user (set in the Dockerfile).
- a hard `--stop-timeout` plus a host-side wait timeout: a hang is killed and
  reported as `timed_out`, which the policy treats as "not verified".

The class is deliberately thin: build argv, run it, capture (exit, out, err,
timed_out). All *interpretation* lives in policy.py so it stays unit-testable
without a daemon.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

from app.config import settings
from app.sandbox.policy import PATCH_FAILED_EXIT

log = structlog.get_logger()

# The entrypoint script run inside the container. It reconstructs the repo in a
# writable tmpfs, applies the patch (exit 3 on reject — the PATCH_FAILED
# sentinel), then execs the verification command so its exit code becomes the
# container's exit code.
#
# Robustness notes for the locked-down environment:
# - HOME/GIT_CONFIG_GLOBAL point at the writable /tmp tmpfs because the root fs
#   is --read-only; git otherwise fails trying to touch ~/.gitconfig.
# - safe.directory '*' avoids git's "dubious ownership" refusal on the
#   root-owned tmpfs mountpoint.
# - The patch is tried at -p1 (git/difflib "a/ b/" prefixes) then -p0 (bare
#   paths), so we accept either diff style the Fix Agent emits.
# - File modes are normalized to 0644 after the copy: /src is a Windows-host
#   bind mount, and Docker Desktop grants every file exec bits (Windows has no
#   Unix modes). Without the normalization, ruff's EXE002 fires on every plain
#   .py file and honest fixes fail — a real Linux checkout would be 0644.
_ENTRY = rf"""
set -e
export HOME=/tmp
export GIT_CONFIG_GLOBAL=/tmp/.gitconfig
cp -r /src/repo/. /work/
find /work -type f -exec chmod 0644 {{}} +
cd /work
git init -q
git config --global --add safe.directory '*' 2>/dev/null || true
if git apply --whitespace=nowarn /src/patch.diff 2>/tmp/apply.err \
   || git apply -p0 --whitespace=nowarn /src/patch.diff 2>>/tmp/apply.err; then
  :
else
  cat /tmp/apply.err >&2
  exit {PATCH_FAILED_EXIT}
fi
exec "$@"
"""


@dataclass(frozen=True)
class SandboxResult:
    """Raw outcome of a sandbox run — interpreted by policy.interpret()."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


@runtime_checkable
class Sandbox(Protocol):
    """What the verifier needs. Lets us swap a fake in tests."""

    def run_verification(
        self,
        repo_files: list[tuple[str, str]],
        patch: str,
        command: list[str],
    ) -> SandboxResult: ...


def _resolve_docker() -> str:
    """Locate the docker CLI.

    Config override wins; then PATH; then the Docker Desktop per-user install
    location (this machine has Docker installed there but not on PATH). Returns
    the string "docker" as a last resort so the error surfaces at call time.
    """
    if settings.docker_bin:
        return settings.docker_bin
    found = shutil.which("docker")
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs"
        / "DockerDesktop"
        / "resources"
        / "bin"
        / "docker.exe",
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Docker"
        / "Docker"
        / "resources"
        / "bin"
        / "docker.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return "docker"


def _write_context(root: Path, repo_files: list[tuple[str, str]], patch: str) -> None:
    """Materialize the read-only build context: repo snapshot + patch file.

    Layout the container expects:
        <root>/repo/<path...>   the repo files
        <root>/patch.diff       the unified diff to apply
    Path traversal is refused — a snapshot path must stay under repo/.
    """
    repo_dir = root / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in repo_files:
        dest = (repo_dir / rel).resolve()
        if not str(dest).startswith(str(repo_dir.resolve())):
            raise ValueError(f"unsafe path in snapshot: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    # git apply expects a trailing newline on the diff.
    (root / "patch.diff").write_text(
        patch if patch.endswith("\n") else patch + "\n", encoding="utf-8"
    )


class DockerSandbox:
    """Runs verification inside a locked-down Docker container."""

    def __init__(self, image: str | None = None) -> None:
        self.image = image or settings.sandbox_image
        self.docker = _resolve_docker()

    def _docker_run_argv(self, ctx_dir: Path, command: list[str]) -> list[str]:
        """Assemble the hardened `docker run` invocation."""
        return [
            self.docker,
            "run",
            "--rm",
            "--network",
            "none",  # no egress at all
            "--memory",
            settings.sandbox_memory,
            "--memory-swap",
            settings.sandbox_memory,  # == memory => no swap
            "--cpus",
            settings.sandbox_cpus,
            "--pids-limit",
            str(settings.sandbox_pids_limit),
            "--read-only",  # immutable root fs
            # RAM-backed, world-writable (mode=1777 like a real /tmp) so the
            # non-root `sandbox` user can populate them. NOTE: passing explicit
            # tmpfs options makes Docker drop its default 1777 mode to a
            # root-owned 0755 — hence the explicit mode=1777, or the non-root
            # user cannot write here.
            "--tmpfs",
            "/work:rw,exec,size=256m,mode=1777",  # workspace
            "--tmpfs",
            "/tmp:rw,size=64m,mode=1777",
            "--cap-drop",
            "ALL",  # no Linux capabilities
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{ctx_dir}:/src:ro",  # context in, read-only
            "-w",
            "/work",
            "--entrypoint",
            "sh",
            self.image,
            "-c",
            _ENTRY,
            "sh",  # $0 for the exec'd command
            *command,  # $@ -> the verification command
        ]

    def run_verification(
        self,
        repo_files: list[tuple[str, str]],
        patch: str,
        command: list[str],
    ) -> SandboxResult:
        ctx = Path(tempfile.mkdtemp(prefix="autopr-sbx-"))
        try:
            _write_context(ctx, repo_files, patch)
            argv = self._docker_run_argv(ctx, command)
            log.info("sandbox.run", image=self.image, command=command)
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=settings.sandbox_timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                log.warning("sandbox.timeout", seconds=settings.sandbox_timeout_s)
                return SandboxResult(
                    exit_code=None,
                    stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                    stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
                    timed_out=True,
                )
            log.info("sandbox.result", exit_code=proc.returncode)
            return SandboxResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                timed_out=False,
            )
        finally:
            shutil.rmtree(ctx, ignore_errors=True)


class FakeSandbox:
    """Test double: returns a canned result and records what it was asked to run.

    Lets the verifier and graph be tested with zero Docker dependency.
    """

    def __init__(self, result: SandboxResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def run_verification(
        self,
        repo_files: list[tuple[str, str]],
        patch: str,
        command: list[str],
    ) -> SandboxResult:
        self.calls.append({"repo_files": repo_files, "patch": patch, "command": command})
        return self.result
