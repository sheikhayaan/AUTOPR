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

* **Bounded retries + escalation.** ``attempts`` counts executions. A job that has
  already run ``max_attempts`` times is flipped to DEAD (the human queue) on its
  next delivery, without running again. The message is ACK'd so it stops being
  redelivered.

* **Reconcile on startup and periodically.** Closes the insert->XADD crash
  window: any job left in PENDING (committed but never published) is
  re-published. Runs at boot and then every ``reconcile_interval_s`` so a
  stranded row is repaired within a minute, not at the next restart.
"""

from __future__ import annotations

import os
import signal
import socket
import time
from collections import OrderedDict

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.rag import grounding_scope
from app.config import settings
from app.db import SessionLocal, assert_schema_ready, engine, insert_ignore_duplicates
from app.ingest import claim_and_publish
from app.models import JobResult, JobStatus, PRJob
from app.observability import (
    bind_correlation_id,
    clear_correlation_id,
    configure_logging,
    new_correlation_id,
)
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


# Repos@shas already embedded in Qdrant by this process. Bounded because a long
# uptime over many PRs would otherwise grow without limit; an evicted entry at
# worst re-embeds an unchanged tree (chunk ids are deterministic, so re-ingest is
# an idempotent upsert, not a duplicate).
_INDEXED: OrderedDict[tuple[str, str], None] = OrderedDict()
_INDEXED_MAX = 128


def _index_snapshot(rag, repo: str, ref: str, snapshot: list[tuple[str, str]]) -> None:
    """Ingest a repo snapshot into RAG, once per (repo, sha). Never raises.

    Embedding a whole tree is the expensive part of grounding, and redeliveries
    (or several jobs on the same commit) would otherwise pay for it repeatedly.
    A failure here must not fail the job: grounding is an enhancement.
    """
    key = (repo, ref)
    if key in _INDEXED:
        _INDEXED.move_to_end(key)
        return
    if not snapshot:
        return
    try:
        with grounding_scope(repo):
            n = rag.ingest_repo(snapshot)
        log.info("worker.rag_indexed", repo=repo, ref=ref[:8], chunks=n)
    except Exception as exc:  # noqa: BLE001 - grounding is best-effort
        log.warning("worker.rag_index_failed", repo=repo, ref=ref[:8], error=repr(exc))
        return
    _INDEXED[key] = None
    while len(_INDEXED) > _INDEXED_MAX:
        _INDEXED.popitem(last=False)


def make_graph_handler(graph, reader, github=None, session_factory=None, rag=None):
    """Build the job handler that drives the LangGraph pipeline.

    This is the integration seam: it turns a persisted `PRJob` into the
    `PRState` the graph expects, runs the graph, and returns a one-line summary
    for the exactly-once `JobResult` ledger. The collaborators (compiled `graph`,
    GitHub `reader`, `github` write client, `session_factory`, `rag`) are built
    once at worker startup and closed over here, so `process_job` still sees the
    simple `handler(session, job) -> str` contract.

    Both tracks are wired, selected by `job.event`:
      * PR-review (pull_request / opened / synchronize): fetch the PR's changed
        files and run `code_reviewer -> test_generator -> router`.
      * CI-fix (check_run / workflow_run): additionally carry the inline failure
        logs captured at ingest (`job.event_context`) and a whole-repo snapshot
        at the head SHA, so `ci_monitor -> fix_agent -> fix_verifier -> router`
        can diagnose, patch, and *prove the patch in the sandbox* before the
        router gates it on a human. The graph's own `_entry_router` picks the
        track from the `ci_event`/`ci_logs` keys set below.

    `rag` (optional, from `build_rag`) grounds the agents in this repo's code. It
    is ingested from the snapshot we already downloaded on the CI track — one
    index per (repo, sha), memoized — and retrieval is scoped to `job.repo` for
    the duration of the run, so a shared collection never mixes repositories.
    When `rag` is None every agent takes its ungrounded path.

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
            # 3. The snapshot is already in hand, so it doubles as the RAG corpus
            #    (chunk + embed once per repo@sha; see _index_snapshot).
            snapshot = reader.snapshot_repo(job.repo, job.commit_sha)
            state["repo_snapshot"] = snapshot
            if rag is not None:
                _index_snapshot(rag, job.repo, job.commit_sha, snapshot)

        # 4. Run the pipeline. `_entry_router` sends CI jobs to ci_monitor and
        #    everything else to code_reviewer. The router (bound with github +
        #    session_factory) is the only node that acts outward or queues a human.
        #    Grounding is scoped to this repo so one collection can serve many.
        if rag is not None:
            with grounding_scope(job.repo):
                result = graph.invoke(state)
        else:
            result = graph.invoke(state)

        # 5. Summarize for the ledger, per track, so the operator can see at a
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

    # Escalate BEFORE incrementing, so `max_attempts` means exactly that many
    # executions (the previous `attempts > max_attempts` ordering gave N+1 runs).
    if job.attempts >= settings.max_attempts:
        job.status = JobStatus.DEAD
        job.last_error = (job.last_error or "") + " | exceeded max attempts"
        session.commit()
        log.error("worker.escalated_to_human", job_id=job_id, attempts=job.attempts)
        return True  # ack — it's escalated, not retried

    job.attempts += 1
    job.status = JobStatus.PROCESSING
    session.commit()

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

    Only PENDING rows are swept: that status means "committed, never confirmed on
    the stream". QUEUED rows are deliberately left alone — they were published,
    and a message that is merely in flight (or in another consumer's PEL) must
    not be published a second time. Genuinely abandoned deliveries are recovered
    by XAUTOCLAIM instead, which is the mechanism designed for exactly that.

    Re-publishing is safe even if the original message does exist: the worker's
    exactly-once JobResult makes the extra delivery a no-op. Returns the number
    re-enqueued.
    """
    stranded = (
        session.execute(select(PRJob).where(PRJob.status == JobStatus.PENDING)).scalars().all()
    )
    count = 0
    for job in stranded:
        # Same compare-and-set the webhook uses, so a reconcile pass racing a
        # live delivery cannot double-publish (and neither can two workers
        # reconciling at once). Re-enqueued out of band, so mint a fresh trace id.
        if claim_and_publish(session, queue, job, correlation_id=new_correlation_id()):
            count += 1
    if count:
        log.info("worker.reconciled", reenqueued=count)
    return count


def _handle_message(queue: JobQueue, msg: StreamMessage, handler) -> None:
    job_id = int(msg.fields["job_id"])
    # Bind the correlation id the API stamped on this job (see app.ingest), so
    # every worker log line for this job shares the id the webhook logged under —
    # one trace across the process boundary. Fall back to a fresh id for a job
    # enqueued without one (e.g. an older message).
    correlation_id = msg.fields.get("correlation_id") or new_correlation_id()
    bind_correlation_id(correlation_id)
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
        clear_correlation_id()


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
        if settings.reclaim_idle_ms < 300_000:
            log.warning(
                "worker.reclaim_window_may_be_short",
                reclaim_idle_ms=settings.reclaim_idle_ms,
                detail="a job slower than this window can be reclaimed and run "
                "concurrently by another worker;",
            )
        last_heartbeat = 0.0
        last_reconcile = time.monotonic()
        while not self._stop:
            self.run_once(block_ms=2_000)
            now = time.monotonic()
            # Periodic reconcile: a PENDING row whose publish failed must not wait
            # for the next restart to be enqueued (the webhook's 503 tells GitHub
            # to retry, but a retry landing on the dedup key only repairs it if
            # ingest sees PENDING — this covers the case where no retry arrives).
            if now - last_reconcile >= settings.reconcile_interval_s:
                try:
                    with SessionLocal() as session:
                        reconcile_pending(session, self.queue)
                except Exception as exc:  # noqa: BLE001 - never kill the loop
                    log.warning("worker.reconcile_failed", error=repr(exc))
                last_reconcile = now
            if now - last_heartbeat > 30:
                log.debug("worker.heartbeat", pending=self.queue.pending_count())
                last_heartbeat = now
        log.info("worker.stopped", consumer=self.name)


def main() -> None:
    configure_logging(json_logs=settings.log_json)
    # Alembic owns the schema; refuse to run un-migrated rather than fail per job.
    assert_schema_ready(engine)

    # Assemble the pipeline collaborators once. The graph is compiled a single
    # time and reused across jobs; the router is bound with the write client and
    # SessionLocal so it can post (dry-run by default) and durably queue/dedup.
    # RAG is opt-in (settings.rag_enabled): when it cannot be built (no 'rag'
    # extra, Qdrant down) build_rag returns None and the agents run ungrounded,
    # rather than the worker refusing to start.
    from app.agents.graph import build_graph
    from app.agents.rag import build_rag
    from app.routing.github import get_github_client, get_github_reader

    reader = get_github_reader()
    github = get_github_client()
    rag = build_rag(settings.qdrant_url, log=log) if settings.rag_enabled else None
    graph = build_graph(rag=rag, sandbox=None, github=github, session_factory=SessionLocal)
    handler = make_graph_handler(
        graph, reader, github=github, session_factory=SessionLocal, rag=rag
    )

    queue = JobQueue(make_client())
    Worker(queue, handler=handler).run_forever()


if __name__ == "__main__":
    main()
