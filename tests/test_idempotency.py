"""Idempotency + concurrency tests — Phase 1 acceptance criteria (a) and (c).

(a) Duplicate webhook delivery => exactly one job, exactly one enqueue.
(c) Two webhooks racing for the same PR => exactly one job.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.ingest import ingest
from app.models import JobStatus, PRJob


def _job_count(session) -> int:
    return session.execute(select(func.count()).select_from(PRJob)).scalar_one()


def _stream_len(queue) -> int:
    return queue.client.xlen(queue.stream)


def test_duplicate_delivery_is_noop(db, queue, sample_payload):
    """(a) GitHub retries a delivery: second call must not create work."""
    first = ingest(db, queue, sample_payload)
    second = ingest(db, queue, sample_payload)

    assert first.created is True
    assert second.created is False  # recognised as duplicate
    assert first.job_id == second.job_id  # same underlying job
    assert _job_count(db) == 1  # one row, not two
    assert _stream_len(queue) == 1  # enqueued exactly once


def test_different_sha_creates_new_job(db, queue, sample_payload):
    """Sanity: a genuinely new head SHA on the same PR is NOT deduped."""
    from dataclasses import replace

    ingest(db, queue, sample_payload)
    updated = replace(sample_payload, commit_sha="f" * 40)
    r = ingest(db, queue, updated)

    assert r.created is True
    assert _job_count(db) == 2
    assert _stream_len(queue) == 2


def test_concurrent_same_pr_creates_one_job(race_engine, queue, sample_payload):
    """(c) Two webhooks race for the same PR head SHA.

    Each thread gets its own connection (as real requests would), so the unique
    index on dedup_key is the thing that decides the winner — exactly one
    created=True, one job row, one stream message.

    A barrier releases both threads into ingest() together so the overlap is
    real rather than hopeful; on the file-backed race engine the loser blocks on
    the write lock (busy timeout) and then sees the committed row.
    """
    RaceSession = sessionmaker(
        bind=race_engine, autoflush=False, expire_on_commit=False, future=True
    )
    start = threading.Barrier(2, timeout=10)

    def worker() -> bool:
        s = RaceSession()
        try:
            start.wait()  # both threads enter ingest() at the same moment
            created = ingest(s, queue, sample_payload).created
            # Assert the loser really did observe the winner's row rather than
            # failing silently; this is the invariant a bare bool cannot express.
            if not created:
                assert (
                    s.execute(select(PRJob).where(PRJob.dedup_key == sample_payload.dedup_key))
                    .scalar_one()
                    .status
                    == JobStatus.QUEUED
                )
            return created
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))

    assert sorted(results) == [False, True]  # exactly one winner

    check = RaceSession()
    try:
        rows = check.execute(select(PRJob)).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == JobStatus.QUEUED
    finally:
        check.close()
    assert _stream_len(queue) == 1
