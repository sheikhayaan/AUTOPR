"""Idempotency + concurrency tests — Phase 1 acceptance criteria (a) and (c).

(a) Duplicate webhook delivery => exactly one job, exactly one enqueue.
(c) Two webhooks racing for the same PR => exactly one job.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from app.ingest import ingest
from app.models import PRJob


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


def test_concurrent_same_pr_creates_one_job(Session, queue, sample_payload):
    """(c) Two webhooks race for the same PR head SHA.

    Each thread gets its own Session (as real requests would). The unique
    constraint on dedup_key must ensure exactly one job + one enqueue, with
    the loser reporting created=False.

    Note: SQLite serialises writes, so this validates the constraint-driven
    logic rather than true Postgres lock contention (flagged in decisions log).
    """

    def worker() -> bool:
        s = Session()
        try:
            return ingest(s, queue, sample_payload).created
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))

    assert sorted(results) == [False, True]  # exactly one winner

    check = Session()
    try:
        assert _job_count(check) == 1
    finally:
        check.close()
    assert _stream_len(queue) == 1
