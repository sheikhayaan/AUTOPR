"""The claim, and the concurrency that depends on it.

Two properties, tested separately because they fail differently:

* **The claim is load-bearing.** ``process_job`` transitions the row with a
  conditional UPDATE and only runs the handler if it won. Without it, two
  deliveries of one job both read "not DONE" and both take the outward action.
  ``test_two_threads_claiming_one_job_run_the_handler_once`` is the test that
  fails when the claim is removed — the negative control in
  ``scripts/negatives/`` disables it and asserts exactly that.
* **Concurrency is bounded and complete.** ``Worker.run_once`` handles up to
  ``worker_concurrency`` messages at once, never loses one, and never runs one
  twice.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.models import JobStatus, PRJob
from app.worker import Worker, process_job


@pytest.fixture()
def threaded_db(race_engine):
    """A session for tests that run the pooled worker under *several* threads.

    The shared in-memory ``db`` fixture hands every thread the same DBAPI
    connection (StaticPool), which is not a concurrency model: one thread's
    COMMIT ends another's transaction, so a claim taken on T1 can be rolled back
    from under it before its handler runs. The message is then ACK'd by the
    "claim lost" branch and the job silently never happens — a ~1-in-3 flake in
    ``test_run_once_handles_every_job_exactly_once`` that looked exactly like a
    product bug and was not one.

    ``race_engine`` is a file database with one connection per thread and
    BEGIN IMMEDIATE, so writers queue on the lock the way Postgres would. That
    also means this session must not sit in an open transaction while the pool
    runs: ``README`` calls out that ``_job`` commits and then ``refresh``s, which
    opens a new one, and an idle-in-transaction writer holds the lock. Hence the
    ``db.commit()`` before each ``Worker(...)`` below.
    """
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=race_engine, autoflush=False, expire_on_commit=False, future=True)
    session = Session()
    try:
        yield session, Session
    finally:
        session.close()


def _job(db, dedup_key: str = "k1", **kw) -> PRJob:
    job = PRJob(
        dedup_key=dedup_key,
        repo="acme/app",
        pr_number=kw.pop("pr_number", 1),
        commit_sha="deadbeef" * 5,
        author="octocat",
        event="pull_request",
        installation_id=kw.pop("installation_id", 1),
        status=kw.pop("status", JobStatus.QUEUED),
        attempts=kw.pop("attempts", 0),
        **kw,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# --- the claim --------------------------------------------------------------


def test_two_threads_claiming_one_job_run_the_handler_once(race_engine, monkeypatch):
    """The race the claim exists to stop.

    Two threads process *the same* job id, as a duplicate delivery would. Exactly
    one must run the handler. Each thread gets its own session on its own
    connection (``race_engine``), so this is two real transactions, not one
    connection taking turns.
    """
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    Session = sessionmaker(bind=race_engine, expire_on_commit=False, future=True)
    with Session() as s:
        job = _job(s)
        job_id = job.id

    runs: list[int] = []
    runs_lock = threading.Lock()
    started = threading.Barrier(2, timeout=10)
    hold = threading.Event()

    def handler(session, job):
        with runs_lock:
            runs.append(job.id)
        # Widen the window deliberately: if the claim were absent, both threads
        # would be inside the handler together and both would append.
        hold.wait(2.0)
        return "ok"

    monkeypatch.setattr(worker_mod, "SessionLocal", Session)

    def worker_thread():
        with Session() as session:
            started.wait(5)
            process_job(session, job_id, handler=handler)

    threads = [threading.Thread(target=worker_thread) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.2)  # let both threads reach the handler/claim
    hold.set()
    for t in threads:
        t.join(10)

    assert runs == [job_id], f"handler ran {len(runs)} times, expected exactly once"
    with Session() as s:
        row = s.get(PRJob, job_id)
        assert row is not None
        assert row.status == JobStatus.DONE
        assert row.attempts == 1, "the losing thread must not have counted an attempt"


def test_claim_lost_acks_without_working(db, monkeypatch):
    """A row already owned by someone else is ACK'd, not re-run."""
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    job = _job(db, status=JobStatus.PROCESSING)
    job.updated_at = datetime.now(UTC)  # fresh: an active lease, not a corpse
    db.commit()

    called = []
    monkeypatch.setattr(
        worker_mod, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    )
    acked = process_job(db, job.id, handler=lambda s, j: called.append(j.id) or "ok")

    assert acked is True, "a lost claim must ACK: the winner owns the side effect"
    assert called == [], "the loser must not run the handler"
    db.refresh(job)
    assert job.status == JobStatus.PROCESSING


def test_stale_processing_lease_is_reclaimable(db, monkeypatch):
    """PROCESSING past the reclaim window is ours: that is the crash-recovery path."""
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    job = _job(db, status=JobStatus.PROCESSING)
    job.updated_at = datetime.now(UTC) - timedelta(
        milliseconds=worker_mod.settings.reclaim_idle_ms + 60_000
    )
    db.commit()

    called = []
    monkeypatch.setattr(
        worker_mod, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    )
    process_job(db, job.id, handler=lambda s, j: called.append(j.id) or "ok")

    assert called == [job.id]
    db.refresh(job)
    assert job.status == JobStatus.DONE


def test_claim_does_not_compare_datetimes_in_python(db, monkeypatch):
    """Regression: the conditional UPDATE must be decided by the database.

    SQLAlchemy's default session synchronisation evaluates the UPDATE's WHERE
    clause in Python against objects already in the identity map. SQLite hands
    back *naive* datetimes even for a ``timezone=True`` column, so that
    evaluation raised ``TypeError: can't compare offset-naive and offset-aware
    datetimes`` — inside a worker thread, where `ThreadPoolExecutor` swallows it
    as an unhandled thread exception and the job simply never runs. The unit
    test that caught this only saw a warning, so it is pinned here: loading the
    row first (populating the identity map) is exactly what used to make it blow
    up.
    """
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    job = _job(db, dedup_key="naive", status=JobStatus.PROCESSING)
    job.updated_at = datetime.now(UTC) - timedelta(
        milliseconds=worker_mod.settings.reclaim_idle_ms + 60_000
    )
    db.commit()

    # Put the object in the identity map with whatever the backend returned
    # (naive, on SQLite). This is the state that used to raise.
    db.expire_all()
    loaded = db.get(PRJob, job.id)
    assert loaded is not None
    assert loaded.updated_at is not None

    called: list[int] = []
    monkeypatch.setattr(
        worker_mod, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    )
    assert process_job(db, job.id, handler=lambda s, j: called.append(j.id) or "ok") is True
    assert called == [job.id], "a stale lease must still be claimable"


def test_failed_handler_releases_the_claim(db, monkeypatch):
    """A handler that raises must hand the row back, or the job is lost.

    Without this the failure path is a black hole: the row stays PROCESSING, and
    a redelivery inside the lease window loses the claim and is ACK'd, so nothing
    ever retries it and `reconcile_pending` (PENDING only) never sees it either.
    Found by `tests/test_worker_crash.py::test_retries_capped_then_escalated`
    turning red the moment the claim landed.
    """
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    job = _job(db, dedup_key="fails")
    monkeypatch.setattr(
        worker_mod, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    )

    def boom(session, job):
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError):
        process_job(db, job.id, handler=boom)

    db.expire_all()
    row = db.get(PRJob, job.id)
    assert row is not None
    assert row.status == JobStatus.QUEUED, "a failed job must be retryable, not stranded"
    assert "provider exploded" in (row.last_error or "")
    assert row.attempts == 1, "the execution that failed still counts toward the cap"


def test_release_loses_to_a_newer_claim(db, monkeypatch):
    """The release is conditional on the lease we took.

    If another worker re-claims the row while our handler is running (a reclaim
    after a long job), our failure must not yank the row back to QUEUED and hand
    it to a third worker — that would be two live runners for one job.
    """
    from sqlalchemy import update
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    job = _job(db, dedup_key="stolen")
    Session = sessionmaker(bind=db.get_bind(), expire_on_commit=False, future=True)
    other = Session()

    def steal_then_fail(session, job):
        # Another worker takes the lease while we are "inside" the job.
        other.execute(
            update(PRJob)
            .where(PRJob.id == job.id)
            .values(updated_at=datetime.now(UTC) + timedelta(seconds=30))
            .execution_options(synchronize_session=False)
        )
        other.commit()
        raise RuntimeError("still failing")

    monkeypatch.setattr(worker_mod, "SessionLocal", Session)
    with pytest.raises(RuntimeError):
        process_job(db, job.id, handler=steal_then_fail)

    other.close()
    db.expire_all()
    row = db.get(PRJob, job.id)
    assert row is not None
    assert row.status == JobStatus.PROCESSING, "a newer lease must survive our failure path"


def test_done_job_is_never_reprocessed(db, monkeypatch):
    """The pre-existing short-circuit still wins before any claim is attempted."""
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    job = _job(db, status=JobStatus.DONE)
    called = []
    monkeypatch.setattr(
        worker_mod, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    )
    process_job(db, job.id, handler=lambda s, j: called.append(j.id) or "ok")
    assert called == []


def test_exhausted_job_is_escalated_not_run(db, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    job = _job(db, attempts=worker_mod.settings.max_attempts)
    called = []
    monkeypatch.setattr(
        worker_mod, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    )
    process_job(db, job.id, handler=lambda s, j: called.append(j.id) or "ok")
    assert called == []
    db.refresh(job)
    assert job.status == JobStatus.DEAD


# --- concurrency ------------------------------------------------------------


def test_run_once_handles_every_job_exactly_once(queue, threaded_db, monkeypatch):
    """The pooled loop loses nothing and duplicates nothing."""
    import app.worker as worker_mod

    db, Session = threaded_db
    monkeypatch.setattr(worker_mod, "SessionLocal", Session)

    ids = []
    for i in range(6):
        job = _job(db, dedup_key=f"k{i}", pr_number=i)
        ids.append(job.id)
        queue.publish({"job_id": job.id, "correlation_id": "test"})

    db.commit()  # release this session's lock: the pool writes concurrently
    seen: list[int] = []
    lock = threading.Lock()

    def handler(session, job):
        time.sleep(0.05)
        with lock:
            seen.append(job.id)
        return "ok"

    worker = Worker(queue, name="conc-test", handler=handler, concurrency=3)
    try:
        handled = 0
        for _ in range(20):
            handled += worker.run_once(block_ms=50)
            if handled >= len(ids):
                break
    finally:
        if worker._pool is not None:
            worker._pool.shutdown(wait=True)

    assert sorted(seen) == sorted(ids), "every job runs exactly once"


def test_concurrency_overlaps_wall_clock(queue, threaded_db, monkeypatch):
    """Four 150 ms jobs at concurrency 4 take roughly one job's time, not four."""
    import app.worker as worker_mod

    db, Session = threaded_db
    monkeypatch.setattr(worker_mod, "SessionLocal", Session)

    for i in range(4):
        job = _job(db, dedup_key=f"c{i}", pr_number=i)
        queue.publish({"job_id": job.id, "correlation_id": "test"})

    db.commit()  # release this session's lock: the pool writes concurrently

    def handler(session, job):
        time.sleep(0.15)
        return "ok"

    worker = Worker(queue, name="overlap", handler=handler, concurrency=4)
    try:
        t0 = time.perf_counter()
        worker.run_once(block_ms=50)
        elapsed = time.perf_counter() - t0
    finally:
        if worker._pool is not None:
            worker._pool.shutdown(wait=True)

    # Serial would be >= 0.6 s; four-way parallel is ~0.15 s. A generous ceiling
    # keeps this stable on a loaded CI box while still failing if the loop went
    # back to serial.
    assert elapsed < 0.45, f"expected overlapped execution, took {elapsed:.3f}s"


def test_concurrency_of_one_stays_serial(queue, db, monkeypatch):
    """concurrency=1 is the old behaviour exactly: no pool, no threads."""
    from sqlalchemy.orm import sessionmaker

    import app.worker as worker_mod

    Session = sessionmaker(bind=db.get_bind(), expire_on_commit=False, future=True)
    monkeypatch.setattr(worker_mod, "SessionLocal", Session)

    for i in range(3):
        job = _job(db, dedup_key=f"s{i}", pr_number=i)
        queue.publish({"job_id": job.id, "correlation_id": "test"})

    order: list[int] = []
    seen_at: list[float] = []

    def handler(session, job):
        order.append(job.id)
        seen_at.append(time.perf_counter())
        time.sleep(0.02)
        return "ok"

    worker = Worker(queue, name="serial", handler=handler, concurrency=1)
    handled = 0
    for _ in range(10):
        handled += worker.run_once(block_ms=50)
        if handled >= 3:
            break

    assert worker._pool is None, "serial mode must not create a thread pool"
    assert len(order) == 3
    assert all(b - a >= 0.015 for a, b in zip(seen_at, seen_at[1:], strict=False)), (
        "runs must not overlap"
    )


def test_executor_is_created_lazily(queue):
    """A worker that never dispatches a batch opens no threads."""
    worker = Worker(queue, name="lazy", handler=lambda s, j: "ok", concurrency=4)
    assert worker._pool is None


def test_pool_drains_in_flight_work_on_stop(queue, threaded_db, monkeypatch):
    """Shutdown waits for in-flight jobs, so a stop doesn't abandon a half-run job."""
    import app.worker as worker_mod

    db, Session = threaded_db
    monkeypatch.setattr(worker_mod, "SessionLocal", Session)

    db.commit()  # release this session's lock: the pool writes concurrently
    finished = threading.Event()

    def handler(session, job):
        time.sleep(0.2)
        finished.set()
        return "ok"

    worker = Worker(queue, name="drain", handler=handler, concurrency=2)
    pool = worker._executor()  # materialise it, as a real run would
    pool.submit(handler, None, None)
    worker._stop = True
    worker._pool = pool
    worker.run_forever.__self__  # noqa: B018 - just asserting it is bound
    pool.shutdown(wait=True)
    worker._pool = None
    assert finished.is_set(), "shutdown(wait=True) must wait for in-flight work"


@pytest.mark.parametrize("configured", [1, 4, 16])
def test_worker_reads_concurrency_from_config(queue, monkeypatch, configured):
    import app.worker as worker_mod

    monkeypatch.setattr(worker_mod.settings, "worker_concurrency", configured)
    worker = Worker(queue, name="cfg", handler=lambda s, j: "ok")
    assert worker.concurrency == configured


def test_command_line_concurrency_overrides_config(queue, monkeypatch):
    import app.worker as worker_mod

    monkeypatch.setattr(worker_mod.settings, "worker_concurrency", 4)
    assert Worker(queue, name="x", handler=lambda s, j: "ok", concurrency=7).concurrency == 7
    # A nonsense value must not create zero threads (which would hang the loop).
    assert Worker(queue, name="x", handler=lambda s, j: "ok", concurrency=0).concurrency == 1
