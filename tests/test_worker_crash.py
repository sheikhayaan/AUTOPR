"""Worker crash / recovery tests — Phase 1 acceptance criterion (b).

A worker dies mid-job. The message must NOT be lost and must NOT be processed
twice-with-effect. We prove:

  1. After a "crash" (consume without ack), the message is still pending (PEL).
  2. XAUTOCLAIM moves it to a live worker, which completes it.
  3. The exactly-once ledger (job_results) has exactly ONE row, even though the
     job was delivered twice (crash + reclaim).
  4. Even if the crash happened AFTER the side effect was written but before
     the ack, reprocessing is a safe no-op (still one row).
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.ingest import ingest
from app.models import JobResult, JobStatus, PRJob
from app.worker import process_job


def _result_count(session, job_id) -> int:
    return session.execute(
        select(func.count()).select_from(JobResult).where(JobResult.job_id == job_id)
    ).scalar_one()


def test_crash_before_ack_message_stays_pending_then_reclaimed(Session, queue, sample_payload):
    db = Session()
    job_id = ingest(db, queue, sample_payload).job_id
    db.close()

    # --- Worker A consumes the message but "crashes" before ack/processing ---
    msgs = queue.consume("worker-A", block_ms=100)
    assert len(msgs) == 1
    assert queue.pending_count() == 1  # delivered, unacked => in the PEL
    # (worker-A process dies here: no ack, no state change)

    # --- Worker B reclaims idle entries (min_idle=0 to simulate elapsed time) ---
    reclaimed = queue.reclaim("worker-B", min_idle_ms=0)
    assert len(reclaimed) == 1
    assert reclaimed[0].fields["job_id"] == str(job_id)

    # Worker B processes it to completion and acks.
    dbb = Session()
    assert process_job(dbb, job_id) is True
    queue.ack(reclaimed[0].id)

    # Job is DONE and the side effect happened exactly once.
    job = dbb.get(PRJob, job_id)
    assert job.status == JobStatus.DONE
    assert _result_count(dbb, job_id) == 1
    assert queue.pending_count() == 0  # PEL drained
    dbb.close()


def test_crash_after_side_effect_reprocessing_is_noop(Session, queue, sample_payload):
    """Worst case: work done + committed, THEN crash before ack.

    Redelivery/reclaim will call process_job again. The DONE short-circuit and
    the unique job_results key make it a no-op — no second side effect.
    """
    db = Session()
    job_id = ingest(db, queue, sample_payload).job_id
    db.close()

    # consume() here purely for its side effect: the message moves into worker-A's
    # PEL (simulating worker-A picking it up) so the pending_count check below holds.
    queue.consume("worker-A", block_ms=100)

    # Worker A completes the work + commits (status DONE, one result row)...
    dba = Session()
    assert process_job(dba, job_id) is True
    assert _result_count(dba, job_id) == 1
    dba.close()
    # ...but crashes before ack. The message is still pending.
    assert queue.pending_count() == 1

    # Worker B reclaims and reprocesses the already-DONE job.
    reclaimed = queue.reclaim("worker-B", min_idle_ms=0)
    assert len(reclaimed) == 1
    dbb = Session()
    assert process_job(dbb, job_id) is True  # no-op path
    assert _result_count(dbb, job_id) == 1  # STILL exactly one
    queue.ack(reclaimed[0].id)
    dbb.close()
    assert queue.pending_count() == 0


def test_retries_capped_then_escalated(Session, queue, sample_payload):
    """A perpetually-failing job is escalated to DEAD, not retried forever."""
    from app.config import settings

    db = Session()
    job_id = ingest(db, queue, sample_payload).job_id
    db.close()

    def always_fails(session, job):
        raise RuntimeError("boom")

    # Drive it past max_attempts. process_job increments attempts each call;
    # once attempts > max_attempts it flips to DEAD before running the handler.
    dbx = Session()
    for _ in range(settings.max_attempts + 1):
        try:
            process_job(dbx, job_id, handler=always_fails)
        except RuntimeError:
            dbx.rollback()  # simulate the worker's retry loop leaving it pending
    job = dbx.get(PRJob, job_id)
    assert job.status == JobStatus.DEAD
    assert job.attempts > settings.max_attempts
    dbx.close()


def test_reconcile_reenqueues_stranded_pending_job(Session, queue, sample_payload):
    """Crash in the insert->XADD window: a PENDING row with nothing on the stream.

    reconcile_pending must re-publish it so it isn't stuck forever.
    """
    from app.worker import reconcile_pending

    # Simulate the crash: create the row in PENDING but never enqueue.
    db = Session()
    job = PRJob(
        dedup_key=sample_payload.dedup_key,
        repo=sample_payload.repo,
        pr_number=sample_payload.pr_number,
        commit_sha=sample_payload.commit_sha,
        author=sample_payload.author,
        event=sample_payload.event,
        status=JobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()

    assert queue.client.xlen(queue.stream) == 0  # nothing enqueued yet

    db2 = Session()
    n = reconcile_pending(db2, queue)
    assert n == 1
    assert queue.client.xlen(queue.stream) == 1  # now on the stream
    assert db2.get(PRJob, job_id).status == JobStatus.QUEUED
    db2.close()
