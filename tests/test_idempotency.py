"""Idempotency + concurrency tests — Phase 1 acceptance criteria (a) and (c).

(a) Duplicate webhook delivery => exactly one job, exactly one enqueue.
(c) Two webhooks racing for the same PR => exactly one job.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import RedisError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.ingest import ingest
from app.models import Base, JobStatus, PRJob


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


def test_publish_failure_is_repaired_by_the_retried_delivery(
    db, queue, sample_payload, monkeypatch
):
    """A 503'd delivery must not strand the job until a worker restart.

    Sequence: row commits PENDING, XADD raises (Redis blip), the webhook returns
    503, GitHub retries. The retry collides on the dedup key, so without a repair
    branch the job is never enqueued. The publish-failure marker is what lets the
    retry tell this state apart from a concurrent race-loser.
    """
    real_publish = queue.publish

    def dead_redis(_fields):
        raise RedisError("redis down")

    monkeypatch.setattr(queue, "publish", dead_redis)
    with pytest.raises(RedisError):
        ingest(db, queue, sample_payload)

    job = db.execute(select(PRJob)).scalar_one()
    assert job.status == JobStatus.PENDING
    assert job.last_error.startswith("publish failed")
    assert _stream_len(queue) == 0

    # GitHub retries the delivery once Redis is back.
    monkeypatch.setattr(queue, "publish", real_publish)
    retried = ingest(db, queue, sample_payload)

    assert retried.created is False
    assert retried.job_id == job.id
    db.refresh(job)
    assert job.status == JobStatus.QUEUED
    assert job.last_error is None
    assert _stream_len(queue) == 1  # enqueued exactly once, by the retry


def test_duplicate_of_a_queued_job_is_not_republished(db, queue, sample_payload):
    """The repair must not fire for a job another delivery already enqueued."""
    ingest(db, queue, sample_payload)
    ingest(db, queue, sample_payload)
    assert _stream_len(queue) == 1


def test_different_sha_creates_new_job(db, queue, sample_payload):
    """Sanity: a genuinely new head SHA on the same PR is NOT deduped."""
    from dataclasses import replace

    ingest(db, queue, sample_payload)
    updated = replace(sample_payload, commit_sha="f" * 40)
    r = ingest(db, queue, updated)

    assert r.created is True
    assert _job_count(db) == 2
    assert _stream_len(queue) == 2


def test_concurrent_same_pr_creates_one_job(tmp_path, queue, sample_payload):
    """(c) Two webhooks race for the same PR head SHA.

    Each thread gets its own connection, as two real requests would. (The shared
    `Session` fixture uses a StaticPool — one connection — and `rowcount` is then
    the result of whichever statement ran last on it, so both threads can read
    0 or both read 1. That is a harness artifact, not app behaviour, and it is
    why this test builds its own file DB.)

    The unique constraint on dedup_key must yield exactly one job row; the
    compare-and-set in `claim_and_publish` must yield exactly one stream entry;
    and exactly one caller sees created=True.

    Note: SQLite serialises writes, so this validates the constraint-driven logic
    rather than true Postgres lock contention (flagged in the decisions log).
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'race.db'}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(bind=engine)
    RaceSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    start = threading.Barrier(2)  # release both threads into ingest() together

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

    assert sorted(results) == [False, True]  # exactly one INSERT winner

    check = RaceSession()
    try:
        assert _job_count(check) == 1
        job = check.execute(select(PRJob)).scalar_one()
        assert job.status == JobStatus.QUEUED  # not stranded in PENDING
    finally:
        check.close()
    assert _stream_len(queue) == 1
    engine.dispose()
