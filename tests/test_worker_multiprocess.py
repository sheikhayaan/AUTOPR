"""Cross-process claiming, against a real Postgres.

Every other concurrency test in this suite runs in **one** process, on SQLite, and
the ADR has said so since phase 17: "cross-process claiming is reasoned about but
only the in-process case is tested". That gap matters because production runs N
worker containers against one Postgres, and a claim that only serialises threads
would double-post reviews while every test stayed green.

This module closes it:

* two *operating-system processes*, not threads, each with its own engine, its
  own connection and its own transaction, racing to claim one row;
* a real Postgres — row locks, MVCC, real `UPDATE ... WHERE status = ...`
  arbitration — not SQLite's single-writer serialisation;
* a start barrier so the claims genuinely overlap instead of accidentally
  serialising through process start-up jitter.

Why Postgres is worth the dependency: on SQLite the claim looks correct even if
it is wrong, because SQLite only admits one writer. Postgres is where a
non-atomic claim actually double-runs.

Requires `pgserver` (bundled Postgres binaries) or `AUTOPR_TEST_POSTGRES_URL`.
Tests skip when neither is present, so the default suite stays hermetic; the CI
job `postgres-concurrency` sets the env var and fails if anything skips.
"""

from __future__ import annotations

# Must precede any `app.*` import: a spawned child re-imports this module in a
# fresh interpreter, and `app.config` validates secrets at import time.
import os

os.environ.setdefault("AUTOPR_ALLOW_INSECURE", "1")
os.environ.setdefault("AUTOPR_HANDOFF_MODE", "false")
os.environ.setdefault("AUTOPR_GITHUB_DRY_RUN", "true")
os.environ.setdefault("AUTOPR_GITHUB_TOKEN", "")
os.environ.setdefault("AUTOPR_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("AUTOPR_LOG_JSON", "true")

import multiprocessing as mp
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, JobStatus, PRJob

# --- Postgres fixture (shared: see `postgres_url` in conftest) ----------------


@pytest.fixture()
def pg(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A clean schema on the real server, with settings pointed at it.

    Drops and recreates on every test: these tests assert on *counts* of handler
    runs, so a leaked row from an earlier test would read as a double-run.

    Pointing the *settings* at Postgres is not optional. `insert_ignore_duplicates`
    branches on `settings.is_sqlite` to pick a dialect, so a test that only swaps
    the connection would compile SQLite's `ON CONFLICT` for a Postgres server and
    fail with `OnConflictDoNothing has no attribute constraint_target` — a harness
    bug that looks exactly like a product bug. The env var covers the spawned
    children, which load their own settings.
    """
    monkeypatch.setenv("AUTOPR_DATABASE_URL", postgres_url)
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", postgres_url)
    engine = create_engine(postgres_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS xproc_runs"))
        conn.execute(text("DROP TABLE IF EXISTS job_results"))
        conn.execute(text("DROP TABLE IF EXISTS review_decisions"))
        conn.execute(text("DROP TABLE IF EXISTS pr_jobs"))
        conn.execute(text("DROP TABLE IF EXISTS installations"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Evidence table for "the handler really ran, and how many times".
        # A serial PK per run means a duplicate run cannot hide behind ON CONFLICT.
        conn.execute(
            text(
                "CREATE TABLE xproc_runs ("
                "  id serial PRIMARY KEY,"
                "  job_id integer NOT NULL,"
                "  pid integer NOT NULL,"
                "  at timestamptz NOT NULL DEFAULT now()"
                ")"
            )
        )
    engine.dispose()
    assert not settings.is_sqlite, "the harness must exercise the Postgres code path"
    return postgres_url


def _engine(url: str):  # noqa: ANN202 - SQLAlchemy Engine
    return create_engine(url, future=True, pool_pre_ping=False)


def _session_factory(url: str):  # noqa: ANN202 - sessionmaker
    return sessionmaker(bind=_engine(url), autoflush=False, expire_on_commit=False, future=True)


# --- handler + child entry points (module level: they must be picklable) ----


def _record_then_sleep(session, job) -> str:  # noqa: ANN001
    """Handler that proves it ran by inserting a row in its *own* transaction.

    The sleep widens the window in which a second process could also run the
    handler, so a double-run shows up as two rows rather than as a timing
    coincidence.
    """
    time.sleep(0.25)
    session.execute(
        text("INSERT INTO xproc_runs (job_id, pid) VALUES (:j, :p)"),
        {"j": job.id, "p": os.getpid()},
    )
    session.commit()
    return "ok"


def _record_fast(session, job) -> str:  # noqa: ANN001
    session.execute(
        text("INSERT INTO xproc_runs (job_id, pid) VALUES (:j, :p)"),
        {"j": job.id, "p": os.getpid()},
    )
    session.commit()
    return "ok"


def _child_claim(url: str, job_id: int, barrier) -> None:  # noqa: ANN001
    """One racing process: join the barrier, then try to claim and run one job."""
    from app.worker import process_job

    Session = _session_factory(url)
    session = Session()
    try:
        barrier.wait(timeout=60)
        process_job(session, job_id, handler=_record_then_sleep)
    finally:
        session.close()


def _child_claim_many(url: str, job_ids: list[int], barrier) -> None:  # noqa: ANN001
    """A process that works through *every* job, racing the others on all of them."""
    from app.worker import process_job

    Session = _session_factory(url)
    session = Session()
    try:
        barrier.wait(timeout=60)
        for job_id in job_ids:
            process_job(session, job_id, handler=_record_fast)
    finally:
        session.close()


def _child_fail(url: str, job_id: int) -> None:
    """Claim a job and fail. The release must be visible to *other* processes."""
    from app.worker import process_job

    Session = _session_factory(url)
    session = Session()

    def boom(_session, _job):  # noqa: ANN001
        raise RuntimeError("cross-process handler failure")

    try:
        process_job(session, job_id, handler=boom)  # raises by design
    finally:
        session.close()


# --- helpers -----------------------------------------------------------------


def _add_job(url: str, dedup_key: str = "xproc", **kw) -> int:
    Session = _session_factory(url)
    with Session() as session:
        job = PRJob(
            dedup_key=dedup_key,
            repo="acme/cross",
            pr_number=kw.pop("pr_number", 1),
            commit_sha="c0ffee" * 6 + "c0",
            author="octocat",
            event="pull_request",
            installation_id=kw.pop("installation_id", 1),
            status=kw.pop("status", JobStatus.QUEUED),
            attempts=kw.pop("attempts", 0),
            **kw,
        )
        session.add(job)
        session.commit()
        return int(job.id)


def _runs(url: str, job_id: int | None = None) -> list[tuple[int, int]]:
    engine = _engine(url)
    with engine.connect() as conn:
        if job_id is None:
            rows = conn.execute(text("SELECT job_id, pid FROM xproc_runs")).all()
        else:
            rows = conn.execute(
                text("SELECT job_id, pid FROM xproc_runs WHERE job_id = :j"), {"j": job_id}
            ).all()
    engine.dispose()
    return [(int(r[0]), int(r[1])) for r in rows]


def _job_row(url: str, job_id: int) -> tuple[str, int, str | None]:
    engine = _engine(url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, attempts, last_error FROM pr_jobs WHERE id = :j"),
            {"j": job_id},
        ).one()
    engine.dispose()
    # The Enum column is native_enum=False, so the *name* ("DONE") is stored;
    # compare on the value ("done") that the rest of the codebase uses.
    return str(row[0]).lower(), int(row[1]), row[2]


def _ctx():
    """spawn, not fork: a forked child would inherit the parent's open sessions
    and sockets, which is precisely the sharing this test exists to rule out."""
    return mp.get_context("spawn")


# --- the tests ---------------------------------------------------------------


def test_processes_racing_on_one_job_run_the_handler_once(pg: str) -> None:
    """The production configuration: N worker processes, one job, one winner.

    Eight processes claim the same row simultaneously. Exactly one may run the
    handler; the other seven must lose the claim and ACK. If the claim were not a
    single conditional UPDATE decided by the database, this is where two reviews
    would be posted to the same PR.
    """
    job_id = _add_job(pg, dedup_key="race")
    ctx = _ctx()
    contenders = 8
    barrier = ctx.Barrier(contenders)
    procs = [
        ctx.Process(target=_child_claim, args=(pg, job_id, barrier), name=f"claim-{i}")
        for i in range(contenders)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)

    assert [p.exitcode for p in procs] == [0] * contenders, "a contender crashed"

    runs = _runs(pg, job_id)
    assert len(runs) == 1, f"handler ran {len(runs)} times across processes, expected 1"
    # ...and it ran in a child, not in the pytest process.
    assert runs[0][1] != os.getpid()

    status, attempts, error = _job_row(pg, job_id)
    assert status == JobStatus.DONE.value
    assert attempts == 1, "only the winner may count an attempt"
    assert error is None or "failed" not in error


def test_many_processes_over_many_jobs_lose_nothing(pg: str) -> None:
    """No lost updates: 4 processes × 8 jobs, every job handled exactly once."""
    job_ids = [_add_job(pg, dedup_key=f"many-{i}", pr_number=i + 1) for i in range(8)]
    ctx = _ctx()
    workers = 4
    barrier = ctx.Barrier(workers)
    procs = [
        ctx.Process(target=_child_claim_many, args=(pg, job_ids, barrier), name=f"bulk-{i}")
        for i in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=180)

    assert [p.exitcode for p in procs] == [0] * workers

    runs = _runs(pg)
    assert len(runs) == len(job_ids), f"expected {len(job_ids)} handler runs, saw {len(runs)}"
    per_job: dict[int, int] = {}
    for job_id, _pid in runs:
        per_job[job_id] = per_job.get(job_id, 0) + 1
    assert per_job == dict.fromkeys(job_ids, 1), f"duplicate or missing runs: {per_job}"

    for job_id in job_ids:
        status, attempts, _ = _job_row(pg, job_id)
        assert status == JobStatus.DONE.value
        assert attempts == 1

    # Work was actually spread over several processes, not funnelled into one.
    assert len({pid for _job, pid in runs}) > 1, "one process did everything; the race was fake"


def test_failed_handler_releases_the_claim_for_another_process(pg: str) -> None:
    """A crash in process A must hand the row back so process B can retry it.

    This is the phase-17 defect seen from the other side of a process boundary:
    when the release was missing, the row stayed PROCESSING and every other
    process's redelivery lost the claim and ACK'd, so the job was stranded until
    the 15-minute reclaim sweep. Here the release has to be *committed* to be
    seen by a different process.
    """
    job_id = _add_job(pg, dedup_key="release")
    ctx = _ctx()
    first = ctx.Process(target=_child_fail, args=(pg, job_id), name="failing")
    first.start()
    first.join(timeout=120)

    assert first.exitcode != 0, "the child was supposed to raise"
    assert _runs(pg, job_id) == [], "a failed handler must not record a run"

    status, attempts, error = _job_row(pg, job_id)
    assert status == JobStatus.QUEUED.value, "the claim was not released for other processes"
    assert attempts == 1, "the failed execution still counts"
    assert error and "cross-process handler failure" in error

    # A second process picks it up and finishes it.
    barrier = ctx.Barrier(1)
    second = ctx.Process(target=_child_claim, args=(pg, job_id, barrier), name="retry")
    second.start()
    second.join(timeout=120)

    assert second.exitcode == 0
    assert len(_runs(pg, job_id)) == 1
    status, attempts, _ = _job_row(pg, job_id)
    assert status == JobStatus.DONE.value
    assert attempts == 2


def test_stale_lease_is_reclaimed_by_another_process(pg: str) -> None:
    """A job left PROCESSING by a dead worker is recoverable *by a different pid*.

    The reclaim path is the crash-recovery guarantee. Under SQLite it is trivially
    true; under Postgres it depends on the conditional UPDATE's timestamp
    comparison being evaluated by the database, in the database's timezone
    handling, against a `timestamptz` column.
    """
    from app.config import settings

    stale = datetime.now(UTC) - timedelta(milliseconds=settings.reclaim_idle_ms + 60_000)
    job_id = _add_job(pg, dedup_key="stale", status=JobStatus.PROCESSING)
    engine = _engine(pg)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pr_jobs SET updated_at = :ts WHERE id = :j"), {"ts": stale, "j": job_id}
        )
    engine.dispose()

    ctx = _ctx()
    barrier = ctx.Barrier(1)
    proc = ctx.Process(target=_child_claim, args=(pg, job_id, barrier), name="reclaimer")
    proc.start()
    proc.join(timeout=120)

    assert proc.exitcode == 0
    assert len(_runs(pg, job_id)) == 1, "the abandoned job was not recovered"
    status, attempts, _ = _job_row(pg, job_id)
    assert status == JobStatus.DONE.value
    assert attempts == 1, "a reclaim is the first attempt on a job that never ran"


def test_a_live_lease_is_not_stolen_by_another_process(pg: str) -> None:
    """The negative control: PROCESSING *inside* the window must not be claimed.

    Without this, `test_stale_lease_is_reclaimed_by_another_process` would pass
    for a degenerate reason — a claim that ignores `updated_at` altogether steals
    live work and reclaims dead work, and both tests would be green.
    """
    import threading

    job_id = _add_job(pg, dedup_key="live", status=JobStatus.PROCESSING)
    ctx = _ctx()
    barrier = ctx.Barrier(1)
    proc = ctx.Process(target=_child_claim, args=(pg, job_id, barrier), name="thief")
    proc.start()

    # Wait for the child to have had every chance to steal it.
    deadline = time.monotonic() + 20
    while proc.is_alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    proc.join(timeout=60)

    assert proc.exitcode == 0
    assert _runs(pg, job_id) == [], "a live lease was stolen by another process"
    status, attempts, _ = _job_row(pg, job_id)
    assert status == JobStatus.PROCESSING.value
    assert attempts == 0, "a lost claim must not count an attempt"
    assert isinstance(threading.active_count(), int)  # keep the import honest


@pytest.mark.parametrize("contenders", [2, 4])
def test_claim_is_arbitrated_by_postgres_for_any_contention(pg: str, contenders: int) -> None:
    """Same property at two contention levels: double-posting must never happen."""
    job_id = _add_job(pg, dedup_key=f"n{contenders}")
    ctx = _ctx()
    barrier = ctx.Barrier(contenders)
    procs = [
        ctx.Process(target=_child_claim, args=(pg, job_id, barrier), name=f"n{contenders}-{i}")
        for i in range(contenders)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)

    assert [p.exitcode for p in procs] == [0] * contenders
    assert len(_runs(pg, job_id)) == 1
    assert _job_row(pg, job_id)[0] == JobStatus.DONE.value
