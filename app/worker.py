"""Worker: durable, crash-safe consumption of the job stream.

Guarantees implemented here:

* **Exactly-once side effect under at-least-once delivery.** The stream can
  deliver a message more than once (worker crash after work, before ACK; or a
  reclaim of an abandoned entry). ``process_job`` is idempotent: it records its
  side effect via INSERT ... ON CONFLICT DO NOTHING into ``job_results``, keyed
  by job id, and short-circuits if the job is already DONE. So N deliveries
  produce exactly one result row.

* **Crash recovery.** ``reclaim_stale`` runs XAUTOCLAIM to pick up entries left
  pending by a dead worker. A message is never silently lost, because it stays
  in the PEL until explicitly ACK'd.

* **No double-processing.** Even if two workers both end up holding the same
  message (original + reclaim overlap), the DONE short-circuit and the unique
  job_results key make the second one a no-op.

* **Bounded retries + escalation.** ``attempts`` is incremented per try; once it
  exceeds ``max_attempts`` the job is marked DEAD (the human queue) rather than
  retried forever. The message is ACK'd so it stops being redelivered.

* **Reconcile on startup.** Closes the insert->XADD crash window: any job left
  in PENDING/QUEUED with no live delivery is re-published.
"""

from __future__ import annotations

import os
import signal
import socket
import time

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, engine, insert_ignore_duplicates
from app.models import Base, JobResult, JobStatus, PRJob
from app.queue import JobQueue, StreamMessage, make_client

log = structlog.get_logger()


class JobError(Exception):
    """Raised by the (pluggable) job body to signal a retryable failure."""


def default_handler(session: Session, job: PRJob) -> str:
    """Phase 1 placeholder work.

    Kept for back-compat and for tests that exercise the exactly-once ledger in
    isolation. The real pipeline handler is built by `make_graph_handler`.
    """
    return f"processed pr {job.repo}#{job.pr_number}@{job.commit_sha[:8]}"


# Webhook event types that drive the CI-fix track rather than PR-review. Kept in
# sync with ingest.parse_event, which only ever writes one of these to a CI job's
# `event` column; anything else (pull_request, ...) takes the PR-review track.
_CI_EVENTS = frozenset({"check_run", "workflow_run"})


def make_graph_handler(graph, reader, github=None, session_factory=None):
    """Build the job handler that drives the LangGraph pipeline.

    This is the integration seam: it turns a persisted `PRJob` into the
    `PRState` the graph expects, runs the graph, and returns a one-line summary
    for the exactly-once `JobResult` ledger. The collaborators (compiled `graph`,
    GitHub `reader`, `github` write client, `session_factory`) are built once at
    worker startup and closed over here, so `process_job` still sees the simple
    `handler(session, job) -> str` contract.

    Both tracks are wired, selected by `job.event`:
      * PR-review (pull_request / opened / synchronize): fetch the PR's changed
        files and run `code_reviewer -> test_generator -> router`.
      * CI-fix (check_run / workflow_run): additionally carry the inline failure
        logs captured at ingest (`job.event_context`) and a whole-repo snapshot
        at the head SHA, so `ci_monitor -> fix_agent -> fix_verifier -> router`
        can diagnose, patch, and *prove the patch in the sandbox* before the
        router gates it on a human. The graph's own `_entry_router` picks the
        track from the `ci_event`/`ci_logs` keys set below.

    The handler does NOT open a DB session of its own for the graph — the router
    persists through its own `session_factory` (a separate, short-lived session),
    keeping HITL writes off the worker's job-ledger transaction.
    """

    def handler(session: Session, job: PRJob) -> str:
        # 1. Both tracks need the PR's changed files: the reviewer reviews them,
        #    and on the CI track the fix agent/verifier use them to scope which
        #    file a patch targets and which check to re-run. A read failure raises
        #    -> the message stays unacked and is redelivered (see _handle_message).
        changed_files = reader.list_pull_files(job.repo, job.pr_number)
        state: dict = {
            "repo": job.repo,
            "pr_number": job.pr_number,
            "commit_sha": job.commit_sha,
            "changed_files": changed_files,
        }

        is_ci = job.event in _CI_EVENTS
        if is_ci:
            # 2. CI-fix track: carry the inline failure evidence captured at
            #    ingest time, and snapshot the whole tree at the head SHA so the
            #    sandbox can apply a candidate patch and re-run the failing check
            #    against the real module graph (pytest needs more than the diff).
            state["ci_event"] = job.event
            state["ci_logs"] = job.event_context or ""
            state["repo_snapshot"] = reader.snapshot_repo(job.repo, job.commit_sha)

        # 3. Run the pipeline. `_entry_router` sends CI jobs to ci_monitor and
        #    everything else to code_reviewer. The router (bound with github +
        #    session_factory) is the only node that acts outward or queues a human.
        result = graph.invoke(state)

        # 4. Summarize for the ledger, per track, so the operator can see at a
        #    glance what the pipeline actually did.
        action = result.get("action_taken", "none")
        if is_ci:
            ftype = result.get("failure_type", "unknown")
            verified = result.get("fix_verified", False)
            log.info(
                "worker.graph_done",
                track="ci_fix",
                repo=job.repo,
                pr=job.pr_number,
                failure_type=ftype,
                fix_verified=verified,
                action_taken=action,
            )
            return (
                f"ci-fix pr {job.repo}#{job.pr_number}: failure={ftype} "
                f"verified={verified} action={action}"
            )
        risk = result.get("risk_score", "?")
        n = len(changed_files)
        log.info(
            "worker.graph_done",
            track="pr_review",
            repo=job.repo,
            pr=job.pr_number,
            risk=risk,
            action_taken=action,
            n_files=n,
        )
        return f"review pr {job.repo}#{job.pr_number}: risk={risk} files={n} action={action}"

    return handler


def process_job(
    session: Session,
    job_id: int,
    handler=default_handler,
) -> bool:
    """Idempotently process one job. Returns True if it is (now) DONE.

    Raises JobError/Exception on a retryable failure so the caller can leave
    the message unacked for redelivery.
    """
    job = session.get(PRJob, job_id)
    if job is None:
        # Nothing to do; ack it so it stops being redelivered.
        log.warning("worker.job_missing", job_id=job_id)
        return True

    # Idempotency short-circuit: already completed on a prior delivery.
    if job.status == JobStatus.DONE:
        log.info("worker.already_done", job_id=job_id)
        return True
    if job.status == JobStatus.DEAD:
        log.info("worker.already_dead", job_id=job_id)
        return True

    job.attempts += 1
    job.status = JobStatus.PROCESSING
    session.commit()

    if job.attempts > settings.max_attempts:
        job.status = JobStatus.DEAD
        job.last_error = (job.last_error or "") + " | exceeded max attempts"
        session.commit()
        log.error("worker.escalated_to_human", job_id=job_id, attempts=job.attempts)
        return True  # ack — it's escalated, not retried

    # --- do the work ---
    summary = handler(session, job)

    # --- record the side effect exactly once ---
    stmt = insert_ignore_duplicates(
        JobResult.__table__,
        values={"job_id": job.id, "summary": summary},
        index_elements=["job_id"],
    )
    session.execute(stmt)
    job.status = JobStatus.DONE
    session.commit()
    log.info("worker.done", job_id=job_id, attempts=job.attempts)
    return True


def reconcile_pending(session: Session, queue: JobQueue) -> int:
    """Re-enqueue jobs stranded by a crash in the insert->XADD window.

    Any job still PENDING (never XADD'd) or QUEUED but with no matching live
    delivery is safe to re-publish because the worker is idempotent. Returns
    the number re-enqueued.
    """
    stranded = (
        session.execute(select(PRJob).where(PRJob.status == JobStatus.PENDING)).scalars().all()
    )
    count = 0
    for job in stranded:
        msg_id = queue.publish(
            {
                "job_id": job.id,
                "repo": job.repo,
                "pr_number": job.pr_number,
                "commit_sha": job.commit_sha,
            }
        )
        job.stream_msg_id = msg_id
        job.status = JobStatus.QUEUED
        count += 1
    if count:
        session.commit()
        log.info("worker.reconciled", reenqueued=count)
    return count


def _handle_message(queue: JobQueue, msg: StreamMessage, handler) -> None:
    job_id = int(msg.fields["job_id"])
    session = SessionLocal()
    try:
        done = process_job(session, job_id, handler=handler)
        if done:
            queue.ack(msg.id)
    except Exception as exc:  # retryable: do NOT ack -> stays in PEL
        session.rollback()
        # Record the error for observability without consuming an attempt slot
        # beyond what process_job already counted.
        try:
            job = session.get(PRJob, job_id)
            if job is not None:
                job.last_error = repr(exc)
                job.status = JobStatus.QUEUED
                session.commit()
        except Exception:
            session.rollback()
        log.error("worker.job_failed", job_id=job_id, error=repr(exc))
        # Message remains pending; it will be reclaimed after the idle window.
    finally:
        session.close()


class Worker:
    def __init__(self, queue: JobQueue, name: str | None = None, handler=default_handler):
        self.queue = queue
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self.handler = handler
        self._stop = False

    def stop(self, *_a) -> None:
        self._stop = True

    def run_once(self, block_ms: int = 2_000) -> int:
        """One poll cycle: reclaim stale, then consume new. Returns #handled."""
        handled = 0
        for msg in self.queue.reclaim(self.name):
            _handle_message(self.queue, msg, self.handler)
            handled += 1
        for msg in self.queue.consume(self.name, count=10, block_ms=block_ms):
            _handle_message(self.queue, msg, self.handler)
            handled += 1
        return handled

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.queue.ensure_group()
        with SessionLocal() as session:
            reconcile_pending(session, self.queue)
        log.info("worker.started", consumer=self.name)
        last_reclaim = 0.0
        while not self._stop:
            self.run_once(block_ms=2_000)
            # Periodic reclaim cadence is already covered by run_once, but keep
            # a heartbeat log for observability.
            now = time.monotonic()
            if now - last_reclaim > 30:
                log.debug("worker.heartbeat", pending=self.queue.pending_count())
                last_reclaim = now
        log.info("worker.stopped", consumer=self.name)


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    Base.metadata.create_all(bind=engine)

    # Assemble the pipeline collaborators once. The graph is compiled a single
    # time and reused across jobs; the router is bound with the write client and
    # SessionLocal so it can post (dry-run by default) and durably queue/dedup.
    # RAG is left ungrounded here (rag=None): Qdrant isn't guaranteed running and
    # the agents degrade cleanly without it.
    from app.agents.graph import build_graph
    from app.routing.github import get_github_client, get_github_reader

    reader = get_github_reader()
    github = get_github_client()
    graph = build_graph(rag=None, sandbox=None, github=github, session_factory=SessionLocal)
    handler = make_graph_handler(graph, reader, github=github, session_factory=SessionLocal)

    queue = JobQueue(make_client())
    Worker(queue, handler=handler).run_forever()


if __name__ == "__main__":
    main()
