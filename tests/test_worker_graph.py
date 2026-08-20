"""The integration-seam vertical slice: webhook-shaped job -> graph -> disposition.

This is the test that proves Phases 1–5 actually run as ONE pipeline. It drives
`process_job` (the exactly-once worker core) with the real `make_graph_handler`,
a compiled graph, a `FakeGitHubReader` (canned diff + repo snapshot), a
`FakeGitHubClient` (records posts), a `FakeSandbox` (canned verdict) for the CI
track, and the in-memory DB — with the LLMs mocked. No infra, no network, no
Groq key.

What it pins down:
  PR-review track:
  * low-risk PR  -> review auto-posted once + JobResult recorded (job DONE);
  * high-risk PR -> review QUEUED for a human, nothing posted;
  * redelivery before the job was marked DONE -> the comment is still posted
    exactly once (router-level idempotency, the seam's load-bearing property).
  CI-fix track (Phase 5):
  * sandbox-verified fix -> PROPOSE_FIX QUEUED for a human (never auto-promoted),
    verified against the whole-repo snapshot, nothing posted;
  * unverified fix -> ESCALATE after the bounded retry loop exhausts.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app.agents import ci_monitor, code_reviewer, fix_agent, test_generator
from app.agents.graph import build_graph
from app.config import settings
from app.models import DecisionStatus, JobResult, JobStatus, PRJob, ReviewDecision
from app.routing.github import FakeGitHubClient, FakeGitHubReader
from app.routing.store import list_pending
from app.sandbox.runner import FakeSandbox, SandboxResult
from app.worker import make_graph_handler, process_job


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def _review_json(risk: str) -> str:
    return (
        '{"findings": [{"file": "app/foo.py", "line": 2, "severity": "error",'
        ' "message": "division by zero"}], "risk_score": "' + risk + '",'
        ' "summary": "Adds a function that always divides by zero."}'
    )


def _seed_job(db, *, sha: str = "abc123", pr: int = 7) -> int:
    job = PRJob(
        dedup_key=f"k-{sha}-{pr}",
        repo="octocat/hello-world",
        pr_number=pr,
        commit_sha=sha,
        author="octocat",
        event="pull_request",
        status=JobStatus.QUEUED,
        attempts=0,
    )
    db.add(job)
    db.commit()
    return job.id


def _mock_graph_llms(risk: str):
    """Patch both agents on the PR-review track to return canned output."""
    return (
        patch.object(code_reviewer, "get_llm", return_value=object()),
        patch.object(code_reviewer, "invoke_llm", return_value=_Resp(_review_json(risk))),
        patch.object(test_generator, "get_llm", return_value=object()),
        patch.object(test_generator, "invoke_llm", return_value=_Resp("def test_foo():\n    pass")),
    )


def _make_handler(Session, gh, files):
    reader = FakeGitHubReader(files=files)
    graph = build_graph(rag=None, sandbox=None, github=gh, session_factory=Session)
    return make_graph_handler(graph, reader, github=gh, session_factory=Session)


DIFF = [{"path": "app/foo.py", "patch": "+def foo():\n+    return 1/0"}]


def test_low_risk_pr_auto_posts_and_records_result(db, Session):
    gh = FakeGitHubClient()
    handler = _make_handler(Session, gh, DIFF)
    job_id = _seed_job(db)

    p1, p2, p3, p4 = _mock_graph_llms("low")
    with p1, p2, p3, p4:
        done = process_job(db, job_id, handler=handler)

    assert done is True
    # The review was posted exactly once.
    assert len(gh.calls) == 1 and gh.calls[0]["op"] == "comment"
    # The job completed and its side effect is recorded (exactly-once ledger).
    job = db.get(PRJob, job_id)
    assert job.status == JobStatus.DONE
    result = db.execute(select(JobResult).where(JobResult.job_id == job_id)).scalar_one()
    assert "action=executed" in result.summary
    # Nothing left for a human.
    assert list_pending(db) == []


def test_high_risk_pr_is_queued_not_posted(db, Session):
    gh = FakeGitHubClient()
    handler = _make_handler(Session, gh, DIFF)
    job_id = _seed_job(db, sha="deadbeef", pr=8)

    p1, p2, p3, p4 = _mock_graph_llms("high")
    with p1, p2, p3, p4:
        process_job(db, job_id, handler=handler)

    # No outward action; the review is waiting for a maintainer.
    assert gh.calls == []
    pending = list_pending(db)
    assert len(pending) == 1
    assert pending[0].action == "comment_review"
    assert pending[0].status == DecisionStatus.PENDING
    # The job still completed — "the work" was disposing of the result.
    assert db.get(PRJob, job_id).status == JobStatus.DONE


def test_redelivery_before_done_does_not_double_post(db, Session):
    # Simulate a crash AFTER the handler posted but BEFORE the job was marked
    # DONE: the handler runs a second time on redelivery. The router's ledger
    # dedup must keep the comment to exactly one.
    gh = FakeGitHubClient()
    handler = _make_handler(Session, gh, DIFF)

    job = PRJob(
        dedup_key="k-redeliver",
        repo="octocat/hello-world",
        pr_number=9,
        commit_sha="cafef00d",
        author="octocat",
        event="pull_request",
        status=JobStatus.PROCESSING,
        attempts=1,
    )
    db.add(job)
    db.commit()

    p1, p2, p3, p4 = _mock_graph_llms("low")
    with p1, p2, p3, p4:
        handler(db, job)  # first (pre-crash) run
        handler(db, job)  # redelivery re-run

    assert len(gh.calls) == 1  # posted exactly once
    rows = db.execute(select(ReviewDecision)).scalars().all()
    assert len(rows) == 1 and rows[0].status == DecisionStatus.EXECUTED


def test_handler_fetches_changed_files_from_reader(db, Session):
    # The seam's input contract: the graph reviews what the reader returns.
    gh = FakeGitHubClient()
    reader = FakeGitHubReader(files=DIFF)
    graph = build_graph(rag=None, github=gh, session_factory=Session)
    handler = make_graph_handler(graph, reader, github=gh, session_factory=Session)
    job_id = _seed_job(db, sha="feed", pr=10)
    job = db.get(PRJob, job_id)

    p1, p2, p3, p4 = _mock_graph_llms("low")
    with p1, p2, p3, p4:
        summary = handler(db, job)

    assert reader.calls == [{"op": "list_files", "repo": "octocat/hello-world", "pr_number": 10}]
    assert "files=1" in summary


# --- CI-fix track (Phase 5) --------------------------------------------------

CI_SNAPSHOT = [
    ("app/foo.py", "def foo():\n    return 1 / 0\n"),
    ("tests/test_foo.py", "from app.foo import foo\n\n\ndef test_foo():\n    assert foo() == 1\n"),
]
FIX_DIFF = (
    "--- a/app/foo.py\n"
    "+++ b/app/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def foo():\n"
    "-    return 1 / 0\n"
    "+def foo():\n"
    "+    return 1\n"
)


def _ci_json(ftype: str = "test") -> str:
    return (
        '{"failure_type": "' + ftype + '", "diagnosis": "test_foo asserts foo()==1 '
        'but foo divides by zero", "excerpt": "E assert ZeroDivisionError"}'
    )


def _mock_ci_llms(ftype: str = "test", diff: str = FIX_DIFF):
    """Patch the CI-track agents (ci_monitor + fix_agent) to canned output."""
    return (
        patch.object(ci_monitor, "get_llm", return_value=object()),
        patch.object(ci_monitor, "invoke_llm", return_value=_Resp(_ci_json(ftype))),
        patch.object(fix_agent, "get_llm", return_value=object()),
        patch.object(fix_agent, "invoke_llm", return_value=_Resp(diff)),
    )


def _seed_ci_job(db, *, sha: str = "ci0001", pr: int = 11, logs: str = "pytest\nE assert") -> int:
    job = PRJob(
        dedup_key=f"ci-{sha}-{pr}",
        repo="octocat/hello-world",
        pr_number=pr,
        commit_sha=sha,
        author="github-actions",
        event="check_run",
        event_context=logs,
        status=JobStatus.QUEUED,
        attempts=0,
    )
    db.add(job)
    db.commit()
    return job.id


def _make_ci_handler(Session, gh, sandbox):
    reader = FakeGitHubReader(files=DIFF, snapshot=CI_SNAPSHOT)
    graph = build_graph(rag=None, sandbox=sandbox, github=gh, session_factory=Session)
    return make_graph_handler(graph, reader, github=gh, session_factory=Session), reader


def test_ci_verified_fix_is_queued_as_propose_fix(db, Session):
    # A sandbox-PASSED fix. It still changes code, so it is ALWAYS human-gated:
    # queued as propose_fix, nothing posted (Phase 3 decision #6 / Phase 4 #2).
    gh = FakeGitHubClient()
    sandbox = FakeSandbox(SandboxResult(exit_code=0, stdout="1 passed", stderr="", timed_out=False))
    handler, reader = _make_ci_handler(Session, gh, sandbox)
    job_id = _seed_ci_job(db)

    p1, p2, p3, p4 = _mock_ci_llms("test")
    with p1, p2, p3, p4:
        done = process_job(db, job_id, handler=handler)

    assert done is True
    assert gh.calls == []  # never auto-promotes a code change
    pending = list_pending(db)
    assert len(pending) == 1
    assert pending[0].action == "propose_fix"
    assert pending[0].status == DecisionStatus.PENDING
    # The fix was verified against the WHOLE-REPO snapshot, not just the diff.
    assert len(sandbox.calls) == 1
    assert sandbox.calls[0]["repo_files"] == CI_SNAPSHOT
    # The fix agent .strip()s its diff before proposing it; that stripped patch
    # is exactly what the sandbox was asked to apply.
    assert sandbox.calls[0]["patch"] == FIX_DIFF.strip()
    # The job completed and the ledger records the CI track's outcome.
    job = db.get(PRJob, job_id)
    assert job.status == JobStatus.DONE
    result = db.execute(select(JobResult).where(JobResult.job_id == job_id)).scalar_one()
    assert "ci-fix" in result.summary and "verified=True" in result.summary


def test_ci_unverified_fix_escalates_after_bounded_retries(db, Session):
    # The sandbox keeps FAILING the check. The graph's bounded retry loop feeds
    # the failure back to the fix agent, then escalates to a human — it does not
    # loop forever, and it does not post.
    gh = FakeGitHubClient()
    sandbox = FakeSandbox(SandboxResult(exit_code=1, stdout="1 failed", stderr="", timed_out=False))
    handler, reader = _make_ci_handler(Session, gh, sandbox)
    job_id = _seed_ci_job(db, sha="ci9999", pr=12)

    p1, p2, p3, p4 = _mock_ci_llms("test")
    with p1, p2, p3, p4:
        process_job(db, job_id, handler=handler)

    assert gh.calls == []
    pending = list_pending(db)
    assert len(pending) == 1
    assert pending[0].action == "escalate"
    assert pending[0].status == DecisionStatus.PENDING
    # The retry ran the sandbox exactly max_fix_attempts times (bounded loop).
    assert len(sandbox.calls) == settings.max_fix_attempts


def test_ci_handler_snapshots_repo_and_carries_inline_logs(db, Session):
    # The CI input contract: the worker snapshots the tree at the head SHA and
    # threads event_context into the graph as ci_logs. Proof the logs are
    # threaded: ci_monitor SHORT-CIRCUITS to failure_type="unknown" on empty
    # logs *before* the (mocked) LLM runs — so a summary of "failure=test" is
    # only reachable if the real logs reached the node.
    gh = FakeGitHubClient()
    sandbox = FakeSandbox(SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False))
    handler, reader = _make_ci_handler(Session, gh, sandbox)
    job_id = _seed_ci_job(db, sha="snapsha", pr=13, logs="unique-log-marker\nE assert")
    job = db.get(PRJob, job_id)

    p1, p2, p3, p4 = _mock_ci_llms("test")
    with p1, p2, p3, p4:
        summary = handler(db, job)

    assert {"op": "snapshot", "repo": "octocat/hello-world", "ref": "snapsha"} in reader.calls
    assert {"op": "list_files", "repo": "octocat/hello-world", "pr_number": 13} in reader.calls
    assert summary.startswith("ci-fix") and "failure=test" in summary
