"""The RAG seam in the worker: ingest once per (repo, sha), scope every run.

RAG was fully implemented but never wired — `worker.main()` built the graph with
`rag=None`, so the reviewer and fix agent always took their ungrounded path. It
is now opt-in via `AUTOPR_RAG_ENABLED`, and these tests pin the seam:

* a CI job ingests the snapshot it already downloaded (no extra network call),
* ingest happens ONCE per (repo, sha) even across redeliveries,
* the run is scoped to its repo (`grounding_scope`) so a shared Qdrant
  collection cannot leak one repository's code into another's review,
* an ingest failure is swallowed: grounding must never fail a job.
"""

from __future__ import annotations

from unittest.mock import patch

from app.agents import ci_monitor, fix_agent
from app.agents import rag as rag_mod
from app.agents.graph import build_graph
from app.models import JobStatus, PRJob
from app.routing.github import FakeGitHubClient, FakeGitHubReader
from app.sandbox.runner import FakeSandbox, SandboxResult
from app.worker import _INDEXED, make_graph_handler, process_job

DIFF = [{"path": "app/foo.py", "patch": "+def foo():\n+    return 1/0"}]
SNAPSHOT = [("app/foo.py", "def foo():\n    return 1 / 0\n")]


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeRAG:
    """Records ingest/retrieve calls and the repo scope in force at the time."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.ingested: list[tuple[str | None, int]] = []
        self.queried: list[str | None] = []
        self.queried_in: list[str | None] = []

    def ingest_repo(self, files):
        if self.fail:
            raise RuntimeError("qdrant exploded")
        self.ingested.append((rag_mod._current_repo.get(), len(files)))
        return len(files)

    def retrieve(self, query, top_k=5):
        self.queried.append(query)
        self.queried_in.append(rag_mod._current_repo.get())
        return []


def _seed_ci_job(db, *, sha: str = "ragsha", pr: int = 21, repo: str = "octocat/hello-world"):
    job = PRJob(
        dedup_key=f"rag-{repo}-{sha}-{pr}",
        repo=repo,
        pr_number=pr,
        commit_sha=sha,
        author="github-actions",
        event="check_run",
        event_context="pytest\nE assert",
        status=JobStatus.QUEUED,
        attempts=0,
    )
    db.add(job)
    db.commit()
    return job.id


def _seed_pr_job(db, *, sha: str = "prsha", pr: int = 22, repo: str = "octocat/hello-world"):
    job = PRJob(
        dedup_key=f"pr-{repo}-{sha}-{pr}",
        repo=repo,
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


def _handler(Session, rag):
    gh = FakeGitHubClient()
    reader = FakeGitHubReader(files=DIFF, snapshot=SNAPSHOT)
    graph = build_graph(rag=rag, sandbox=None, github=gh, session_factory=Session)
    return make_graph_handler(graph, reader, github=gh, session_factory=Session, rag=rag)


def _mock_ci_llms():
    return (
        patch.object(ci_monitor, "get_llm", return_value=object()),
        patch.object(
            ci_monitor,
            "invoke_llm",
            return_value=_Resp('{"failure_type": "test", "diagnosis": "d", "excerpt": "e"}'),
        ),
        patch.object(fix_agent, "get_llm", return_value=object()),
        patch.object(
            fix_agent, "invoke_llm", return_value=_Resp("--- a/app/foo.py\n+++ b/app/foo.py")
        ),
    )


def _run(db, handler, job_id):
    p1, p2, p3, p4 = _mock_ci_llms()
    with p1, p2, p3, p4:
        return process_job(db, job_id, handler=handler)


def test_ci_job_indexes_the_snapshot_once_per_repo_and_sha(db, Session):
    _INDEXED.clear()
    rag = FakeRAG()
    handler = _handler(Session, rag)
    job_id = _seed_ci_job(db)
    assert _run(db, handler, job_id) is True
    # Ingested exactly once, from the snapshot already in hand, in repo scope.
    assert rag.ingested == [("octocat/hello-world", len(SNAPSHOT))]

    # A second job on the same commit (e.g. another check_run) must not re-embed.
    job2 = _seed_ci_job(db, pr=23)
    _run(db, handler, job2)
    assert len(rag.ingested) == 1
    _INDEXED.clear()


def test_each_job_grounds_retrieval_in_its_own_repo(db, Session):
    _INDEXED.clear()
    rag = FakeRAG()
    handler = _handler(Session, rag)
    _run(db, handler, _seed_ci_job(db, sha="s1", pr=31, repo="acme/api"))
    _run(db, handler, _seed_ci_job(db, sha="s2", pr=32, repo="acme/web"))
    assert [repo for repo, _n in rag.ingested] == ["acme/api", "acme/web"]
    # Scope is cleared after the run, so nothing leaks into the next job.
    assert rag_mod._current_repo.get() is None
    _INDEXED.clear()


def test_pr_track_does_not_download_or_index_anything(db, Session):
    _INDEXED.clear()
    rag = FakeRAG()
    handler = _handler(Session, rag)
    job_id = _seed_pr_job(db)
    with (
        patch("app.agents.code_reviewer.get_llm", return_value=object()),
        patch(
            "app.agents.code_reviewer.invoke_llm",
            return_value=_Resp('{"findings": [], "risk_score": "low", "summary": "ok"}'),
        ),
        patch("app.agents.test_generator.get_llm", return_value=object()),
        patch("app.agents.test_generator.invoke_llm", return_value=_Resp("def test_x(): pass")),
    ):
        assert process_job(db, job_id, handler=handler) is True
    assert rag.ingested == []  # PR track has no snapshot to ingest
    _INDEXED.clear()


def test_ingest_failure_never_fails_the_job(db, Session):
    _INDEXED.clear()
    rag = FakeRAG(fail=True)
    handler = _handler(Session, rag)
    job_id = _seed_ci_job(db, sha="failing-rag", pr=41)
    assert _run(db, handler, job_id) is True
    assert db.get(PRJob, job_id).status == JobStatus.DONE
    # A failed index is not memoized, so a later delivery retries it.
    assert ("octocat/hello-world", "failing-rag") not in _INDEXED
    _INDEXED.clear()


def test_rag_disabled_means_no_ingest_no_scope(db, Session):
    """Default deployment: rag=None, agents ungrounded, zero extra work."""
    _INDEXED.clear()
    gh = FakeGitHubClient()
    reader = FakeGitHubReader(files=DIFF, snapshot=SNAPSHOT)
    sandbox = FakeSandbox(SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False))
    graph = build_graph(rag=None, sandbox=sandbox, github=gh, session_factory=Session)
    handler = make_graph_handler(graph, reader, github=gh, session_factory=Session, rag=None)
    job_id = _seed_ci_job(db, sha="norag", pr=51)
    assert _run(db, handler, job_id) is True
    assert _INDEXED == {}
    assert rag_mod._current_repo.get() is None


def test_index_cache_is_bounded():
    """A long uptime over many PRs must not grow the memo forever."""
    from app.worker import _INDEXED_MAX

    _INDEXED.clear()
    rag = FakeRAG()
    from app.worker import _index_snapshot

    for i in range(_INDEXED_MAX + 10):
        _index_snapshot(rag, "acme/api", f"sha{i}", SNAPSHOT)
    assert len(_INDEXED) == _INDEXED_MAX
    # Eviction is oldest-first (LRU-ish), and an evicted key re-ingests.
    assert ("acme/api", "sha0") not in _INDEXED
    before = len(rag.ingested)
    _index_snapshot(rag, "acme/api", "sha0", SNAPSHOT)
    assert len(rag.ingested) == before + 1
    _INDEXED.clear()


def test_handoff_mode_rag_is_still_read_only(db, Session, monkeypatch):
    """RAG reads repo text; enabling it must not create a write path."""
    from app.config import settings

    monkeypatch.setattr(settings, "handoff_mode", True, raising=False)
    _INDEXED.clear()
    rag = FakeRAG()
    gh = FakeGitHubClient()
    reader = FakeGitHubReader(files=DIFF, snapshot=SNAPSHOT)
    graph = build_graph(rag=rag, sandbox=None, github=gh, session_factory=Session)
    handler = make_graph_handler(graph, reader, github=gh, session_factory=Session, rag=rag)
    assert _run(db, handler, _seed_ci_job(db, sha="handoff", pr=61)) is True
    # Grounding changed none of the safety posture: nothing was posted, and the
    # verified fix is still queued for a human rather than promoted.
    assert gh.calls == []
    _INDEXED.clear()
