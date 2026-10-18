"""One test that drives the WHOLE product the way it is actually used.

Everything else in this suite tests a seam: the HTTP contract, the graph, the
router, the approval API. This file crosses every seam in one run, with no mocks
at the seams themselves (only the LLMs and the sandbox are faked), because that
is the only way to catch the class of bug where each piece is individually right
and the pipeline still does the wrong thing end to end:

  1. GitHub delivers a signed `pull_request` webhook        -> HTTP 200, job QUEUED
  2. The worker claims it off the stream and runs the graph -> job DONE
  3. A high-risk review is gated, NOT posted               -> decision PENDING
  4. The dashboard shows it as awaiting human approval      -> GET /reviews/pending
  5. A human approves                                      -> exactly one comment posted
  6. Approving again is a no-op                            -> already_executed, still one
  7. History shows the executed decision with its result URL -> GET /reviews

Step 6 is the one that matters most: the safety property is "a human decides",
so a second approval must never be able to post twice.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import code_reviewer, test_generator
from app.agents.graph import build_graph
from app.config import settings
from app.main import app, get_db, get_github, get_queue
from app.models import Base, DecisionStatus, JobStatus, PRJob
from app.queue import JobQueue
from app.routing.github import FakeGitHubClient, FakeGitHubReader
from app.security import compute_signature
from app.worker import make_graph_handler, process_job


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def _review_json(risk: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "file": "app/foo.py",
                    "line": 2,
                    "severity": "error",
                    "message": "division by zero",
                }
            ],
            "risk_score": risk,
            "summary": "Adds a function that always divides by zero.",
        }
    )


@pytest.fixture()
def stack():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    queue = JobQueue(
        fakeredis.FakeStrictRedis(server=fakeredis.FakeServer()),
        stream="e2e:jobs",
        group="e2e-workers",
    )
    queue.ensure_group()

    github = FakeGitHubClient()

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_queue] = lambda: queue
    app.dependency_overrides[get_github] = lambda: github
    client = TestClient(app)
    try:
        yield client, Session, queue, github
    finally:
        app.dependency_overrides.clear()


def _deliver(client, *, sha: str = "abc123", number: int = 7) -> dict:
    body = json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "number": number,
                "head": {"sha": sha},
                "user": {"login": "octocat"},
            },
            "repository": {"full_name": "octocat/hello-world"},
        }
    ).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": compute_signature(settings.webhook_secret, body),
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run_worker(Session, queue, risk: str, github: FakeGitHubClient) -> None:
    """Claim the delivered job and process it exactly as the worker loop would."""
    messages = queue.consume("e2e-worker", count=1, block_ms=1000)
    assert messages, "webhook did not reach the stream"
    msg = messages[0]
    job_id = int(msg.fields["job_id"])

    reader = FakeGitHubReader(
        files=[{"path": "app/foo.py", "patch": "+def foo():\n+    return 1/0"}]
    )
    graph = build_graph(rag=None, sandbox=None, github=github, session_factory=Session)
    handler = make_graph_handler(graph, reader, github=github, session_factory=Session)

    db = Session()
    try:
        with (
            patch.object(code_reviewer, "get_llm", return_value=object()),
            patch.object(code_reviewer, "invoke_llm", return_value=_Resp(_review_json(risk))),
            patch.object(test_generator, "get_llm", return_value=object()),
            patch.object(
                test_generator,
                "invoke_llm",
                return_value=_Resp("def test_foo():\n    assert foo() == 1\n"),
            ),
        ):
            assert process_job(db, job_id, handler=handler) is True
    finally:
        db.close()
    queue.ack(msg.id)


def test_webhook_to_approved_review_end_to_end(stack):
    client, Session, queue, github = stack

    # 1. Delivery is accepted and durably enqueued.
    delivered = _deliver(client)
    assert delivered["status"] == "queued"

    # 2. The worker runs the pipeline; a HIGH risk review must be gated.
    _run_worker(Session, queue, "high", github)

    db = Session()
    try:
        job = db.query(PRJob).one()
        assert job.status == JobStatus.DONE
    finally:
        db.close()
    assert github.calls == []  # nothing reached GitHub

    # 3. The decision is waiting for a human.
    pending = client.get("/reviews/pending").json()
    assert pending["count"] == 1
    decision = pending["pending"][0]
    assert decision["action"] == "comment_review"
    assert decision["risk"] == "high"
    assert decision["status"] == DecisionStatus.PENDING.value
    # The draft tests from the generator are in the body a human is approving —
    # the reviewer sees exactly what will be posted.
    assert "Suggested tests" in decision["body"]

    # 4. It shows up on the dashboard as awaiting approval.
    stats = client.get("/stats").json()
    assert stats["reviews"]["pending"] == 1
    assert stats["reviews"]["executed"] == 0

    # 5. Approval performs the outward action exactly once.
    approved = client.post(f"/reviews/{decision['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "executed"
    assert len(github.calls) == 1
    assert github.calls[0]["op"] == "comment"
    assert github.calls[0]["repo"] == "octocat/hello-world"
    assert github.calls[0]["pr_number"] == 7

    # 6. A second approval (double-click, retry, second operator) never posts again.
    again = client.post(f"/reviews/{decision['id']}/approve")
    assert again.json()["status"] == "already_executed"
    assert len(github.calls) == 1

    # 7. History shows the executed decision, with the URL it landed at.
    history = client.get("/reviews").json()
    assert history["total"] == 1
    executed = history["reviews"][0]
    assert executed["status"] == DecisionStatus.EXECUTED.value
    assert executed["result_url"] == "https://fake.github/comment/1"
    assert client.get("/reviews/pending").json()["count"] == 0


def test_redelivery_of_the_same_webhook_does_not_re_post(stack):
    """GitHub retries deliveries constantly; a retry must not double-review."""
    client, Session, queue, github = stack
    _deliver(client)
    dup = _deliver(client)  # same repo/PR/sha
    assert dup["status"] == "duplicate"

    # Exactly one job exists, so one worker pass is all that can ever happen.
    db = Session()
    try:
        assert db.query(PRJob).count() == 1
    finally:
        db.close()

    _run_worker(Session, queue, "low", github)
    assert len(github.calls) == 1
    assert client.get("/reviews/pending").json()["count"] == 0
    assert client.get("/jobs").json()["total"] == 1


def test_rejection_is_terminal(stack):
    client, Session, queue, github = stack
    _deliver(client)
    _run_worker(Session, queue, "high", github)
    decision_id = client.get("/reviews/pending").json()["pending"][0]["id"]

    rejected = client.post(f"/reviews/{decision_id}/reject", json={"reason": "not needed"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert github.calls == []

    # A rejected decision can never later be approved into existence.
    approved = client.post(f"/reviews/{decision_id}/approve")
    assert approved.status_code == 409
    assert github.calls == []
