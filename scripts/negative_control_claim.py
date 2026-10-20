"""Negative controls for the claim: prove each half is load-bearing.

Two guards were added to `process_job`, and each gets its own control:

1. **The claim.** Disabled by making the conditional UPDATE unconditional, so
   every worker that sees a message "wins" it. Two tests must fail.
2. **The release.** Disabled by leaving the status alone on a handler failure, so
   a failed job stays PROCESSING. Two tests must fail (including the pre-existing
   `test_retries_capped_then_escalated`, which is what caught the missing release
   in the first place).

Each control writes a sabotaged copy, runs the targeted tests, asserts they fail,
then restores the file byte-for-byte and asserts the restore. A guard whose
removal changes nothing is not a guard; a sabotage that silently stops matching
(is: after a CRLF checkout) would be a silent pass, so it exits non-zero instead.

Run: ``python scripts/negative_control_claim.py``
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

WORKER = pathlib.Path("app/worker.py")
CONCURRENCY = "tests/test_worker_concurrency.py"
CRASH = "tests/test_worker_crash.py"
MULTIPROCESS = "tests/test_worker_multiprocess.py"


def postgres_available() -> bool:
    """Can the cross-process control run? Same rule as the test fixture.

    A control that cannot run must say so and be *excluded*, never quietly pass:
    "the guard is load-bearing" and "we never checked" are different claims.
    """
    if os.environ.get("AUTOPR_TEST_POSTGRES_URL", "").strip():
        return True
    return importlib.util.find_spec("pgserver") is not None


class Control:
    def __init__(
        self,
        name: str,
        find: str,
        replace: str,
        tests: tuple[str, ...],
        must_fail: tuple[str, ...],
        drop_continuation: str | None = None,
    ) -> None:
        self.name = name
        self.find = find
        self.replace = replace
        self.tests = tests
        self.must_fail = must_fail
        # Some guards span two lines (a `where(...)` continued with `| ...`).
        # Replacing only the first line leaves a syntax error, which shows up as a
        # collection error and no FAILED lines — i.e. a bar that looks red for the
        # wrong reason. This drops the continuation entirely.
        self.drop_continuation = drop_continuation
        # Controls whose tests need a real Postgres; skipped (and reported as
        # skipped) on a machine without one.
        self.requires_postgres = False


CONTROLS = (
    Control(
        name="claim disabled (every delivery wins the row)",
        find="PRJob.status.in_([JobStatus.QUEUED, JobStatus.PENDING])",
        replace="PRJob.id.isnot(None),  # NEGATIVE CONTROL: unconditional claim",
        drop_continuation="| ((PRJob.status == JobStatus.PROCESSING)",
        tests=(CONCURRENCY,),
        must_fail=(
            "test_two_threads_claiming_one_job_run_the_handler_once",
            "test_claim_lost_acks_without_working",
        ),
    ),
    Control(
        name="release disabled (a failed job stays PROCESSING)",
        find=".values(status=JobStatus.QUEUED, last_error=repr(exc))",
        replace=".values(last_error=repr(exc))  # NEGATIVE CONTROL: no release",
        tests=(CONCURRENCY, CRASH),
        must_fail=(
            "test_failed_handler_releases_the_claim",
            "test_retries_capped_then_escalated",
        ),
    ),
    Control(
        name="claim disabled, same test but across processes (needs Postgres)",
        find="PRJob.status.in_([JobStatus.QUEUED, JobStatus.PENDING])",
        replace="PRJob.id.isnot(None),  # NEGATIVE CONTROL: unconditional claim",
        drop_continuation="| ((PRJob.status == JobStatus.PROCESSING)",
        tests=(MULTIPROCESS,),
        must_fail=(
            "test_processes_racing_on_one_job_run_the_handler_once",
            "test_many_processes_over_many_jobs_lose_nothing",
        ),
    ),
)

# The cross-process control is the only one that needs a server.
CONTROLS[-1].requires_postgres = True


def run_tests(tests: tuple[str, ...]) -> tuple[set[str], str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
    )
    failed = {
        line.split("::")[1].split(" ")[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    }
    return failed, proc.stdout


def sabotage(text: str, control: Control) -> str:
    """Replace one line, preserving the file's newline convention."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if control.find in line:
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = indent + control.replace
            if control.drop_continuation is not None:
                for j in range(i + 1, min(i + 4, len(lines))):
                    if control.drop_continuation in lines[j]:
                        lines[j] = indent + "# NEGATIVE CONTROL: continuation dropped"
                        break
                else:
                    raise SystemExit(
                        f"continuation line not found for control {control.name!r} — "
                        "the code changed shape; update this script"
                    )
            newline = "\r\n" if "\r\n" in text else "\n"
            return newline.join(lines) + newline
    raise SystemExit(
        f"sabotage point not found for control {control.name!r} — "
        "the code changed shape; update this script"
    )


def run_control(control: Control) -> bool:
    original = WORKER.read_bytes()
    try:
        WORKER.write_bytes(sabotage(original.decode("utf8"), control).encode("utf8"))
        failed, out = run_tests(control.tests)
    finally:
        WORKER.write_bytes(original)
        restored = WORKER.read_bytes() == original

    print(f"\n[{control.name}]")
    print(f"  restored byte-identically: {restored}")
    if not restored:
        print("  FAIL: could not restore app/worker.py — restore from git now")
        return False
    print(f"  failing tests: {sorted(failed) or 'none'}")
    missing = [name for name in control.must_fail if name not in failed]
    if missing:
        print(f"  FAIL: not load-bearing — these passed anyway: {missing}")
        print(out[-1500:])
        return False
    print(f"  OK: removing the guard breaks {len(control.must_fail)} test(s) that assert it")
    return True


def main() -> int:
    have_pg = postgres_available()
    results: list[bool] = []
    for control in CONTROLS:
        if control.requires_postgres and not have_pg:
            print()
            print(f"[{control.name}]")
            print(
                "  SKIPPED: no Postgres — set AUTOPR_TEST_POSTGRES_URL or install pgserver "
                "(the cross-process half of this control is then unverified here, not passing)"
            )
            continue
        results.append(run_control(control))
    ok = all(results)
    if not ok:
        return 1
    checks = subprocess.run(
        [sys.executable, "-m", "pytest", CONCURRENCY, CRASH, "-q", "--no-header"],
        capture_output=True,
        text=True,
    )
    summary = [line for line in checks.stdout.splitlines() if "passed" in line]
    print(f"\nwith both guards restored: {summary[-1] if summary else checks.stdout[-300:]}")
    return 0 if checks.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
