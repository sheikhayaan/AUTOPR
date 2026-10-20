"""Measure the three things phase 17 changed, against the real code.

Run: ``python scripts/bench_optimisations.py``

Each section answers one question with a number, and each number comes from
running the actual implementation — not a stand-in:

1. **Worker concurrency.** The same jobs through the real `Worker`, real
   `JobQueue`, real `process_job`. Serial vs pooled; the ratio is the throughput
   win. The handler sleeps, because what is being measured is *scheduling*, not
   the model.
2. **The GitHub HTTP client.** Old style (`httpx.post`, which constructs a
   `Client` per call) vs the process-wide pooled client. On this machine
   `httpx.Client()` costs ~280 ms to construct — it builds an SSL context and a
   connection pool — and that cost was paid four to seven times per job.
3. **Quota query cost.** 200k rows, the real `quota_reason` (three `COUNT`s),
   with and without the phase-17 indexes, including the query plan. This is the
   one that gets worse with age: a sequential scan's cost is a function of
   history, not of load.

Nothing here touches the network or a real database.
"""

from __future__ import annotations

import http.server
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("AUTOPR_ALLOW_INSECURE", "true")

import httpx  # noqa: E402

from app.http import build_client, reset_shared_client  # noqa: E402

# --- 1. worker concurrency -------------------------------------------------


def bench_worker(jobs: int = 8, job_seconds: float = 0.10, concurrency: int = 4) -> dict:
    """Serial vs pooled, over the real Worker/JobQueue/process_job path."""
    import fakeredis
    from sqlalchemy import create_engine, insert, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import app.worker as worker_mod
    from app.models import Base, PRJob
    from app.queue import JobQueue

    def run(conc: int, tmpdir: Path) -> float:
        # A *file* database with NullPool, so every thread gets its own connection.
        # The first version of this bench used one in-memory SQLite shared across
        # threads (StaticPool), which is not a concurrency model at all: two
        # threads commit and roll back through one DBAPI connection, so a claim
        # can be undone by another thread's rollback and a job silently loses its
        # handler run ("expected 8 jobs, got 7" — a harness artifact that looked
        # exactly like a product bug). `busy_timeout` makes concurrent writers wait
        # instead of erroring, the way Postgres would.
        engine = create_engine(
            f"sqlite+pysqlite:///{tmpdir / f'bench-{conc}.db'}",
            future=True,
            connect_args={"check_same_thread": False, "timeout": 15},
            poolclass=NullPool,
        )
        Base.metadata.create_all(engine)  # every table: process_job writes job_results
        Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        original = worker_mod.SessionLocal
        worker_mod.SessionLocal = Session  # type: ignore[assignment]
        try:
            redis = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
            queue = JobQueue(redis, "bench:jobs", "bench:workers")
            queue.ensure_group()

            with Session() as s:
                for i in range(jobs):
                    s.execute(
                        insert(PRJob.__table__).values(
                            dedup_key=f"bench-{conc}-{i}",
                            repo="acme/app",
                            pr_number=i,
                            commit_sha="0" * 40,
                            author="a",
                            event="pull_request",
                            installation_id=1,
                            status="QUEUED",
                            attempts=0,
                        )
                    )
                s.commit()
                ids = [r[0] for r in s.execute(text("SELECT id FROM pr_jobs ORDER BY id"))]

            calls: list[int] = []

            def handler(session, job):
                calls.append(job.id)
                time.sleep(job_seconds)  # stand-in for provider latency
                return "ok"

            for jid in ids:
                queue.publish({"job_id": jid, "correlation_id": "bench"})

            w = worker_mod.Worker(queue, name=f"bench-{conc}", handler=handler, concurrency=conc)
            t0 = time.perf_counter()
            for _ in range(50):
                if w.run_once(block_ms=50) == 0:
                    break
            elapsed = time.perf_counter() - t0
            if sorted(calls) != sorted(ids):
                raise SystemExit(f"expected each of {jobs} jobs exactly once, got {len(calls)}")
            if w._pool is not None:
                w._pool.shutdown(wait=True)
            return elapsed
        finally:
            worker_mod.SessionLocal = original  # type: ignore[assignment]

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        serial_s = run(1, tmpdir)
        pooled_s = run(concurrency, tmpdir)
    return {
        "jobs": jobs,
        "job_seconds": job_seconds,
        "serial_s": serial_s,
        "pooled_s": pooled_s,
        "concurrency": concurrency,
        "throughput_ratio": serial_s / pooled_s if pooled_s else 0.0,
    }


# --- 2. the HTTP client ----------------------------------------------------


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 so the *server* honours keep-alive: with the 1.0 default it closes
    # after every response, reuse is impossible, and both arms would show one
    # connection per call.
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        body = b'{"html_url":"https://example.invalid"}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):  # silence the server
        pass


class _CountingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    connections = 0

    def process_request_thread(self, request, client_address):
        type(self).connections += 1
        super().process_request_thread(request, client_address)


def _once(fn) -> None:
    """Run fn, retrying once on a dropped idle keep-alive socket.

    A loopback server can close an idle connection between requests; real pools
    retry once on a fresh socket. The retry is what makes the measurement stable
    instead of an intermittent ReadError.
    """
    for attempt in range(2):
        try:
            fn()
            return
        except (httpx.ReadError, httpx.RemoteProtocolError):
            if attempt:
                raise


def bench_connection_reuse(n_calls: int = 20) -> dict:
    server = _CountingServer(("127.0.0.1", 0), _CountingHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/repos/x/y/issues/1/comments"
    payload = {"body": "hello"}

    # What a client costs to build, independent of any request.
    builds = []
    for _ in range(3):
        t0 = time.perf_counter()
        client = httpx.Client(timeout=5.0)
        builds.append((time.perf_counter() - t0) * 1000)
        client.close()
    construct_ms = statistics.median(builds)

    # Old path: the module-level helper — build + request + close, every call.
    _CountingServer.connections = 0
    t0 = time.perf_counter()
    for _ in range(n_calls):
        _once(lambda: httpx.post(url, json=payload, timeout=5.0))
    old_ms = (time.perf_counter() - t0) / n_calls * 1000
    old_conns = _CountingServer.connections

    # New path: one pooled client, reused for every call.
    reset_shared_client()
    client = build_client()
    _once(lambda: client.post(url, json=payload))  # establish the connection
    _CountingServer.connections = 0
    t0 = time.perf_counter()
    for _ in range(n_calls):
        _once(lambda: client.post(url, json=payload))
    new_ms = (time.perf_counter() - t0) / n_calls * 1000
    new_conns = _CountingServer.connections
    client.close()
    server.shutdown()

    calls_per_job = 5  # list files, tarball, comment, permission, repo lookup
    return {
        "calls": n_calls,
        "client_construct_ms": construct_ms,
        "old_ms_per_call": old_ms,
        "new_ms_per_call": new_ms,
        "old_connections": old_conns,
        "new_connections": new_conns,
        "speedup": old_ms / new_ms if new_ms else 0.0,
        "saved_per_job_ms": (old_ms - new_ms) * calls_per_job,
    }


# --- 3. quota query cost ---------------------------------------------------


def bench_quota(n_rows: int = 200_000) -> dict:
    """Cost of the real `quota_reason` (three COUNTs) before/after phase 17.

    The quotas are set explicitly: with the default zeros `quota_reason`
    short-circuits and measures nothing at all — the first version of this bench
    did exactly that and reported 0.00 ms for both arms.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine, insert, text
    from sqlalchemy.orm import sessionmaker

    from app.config import settings
    from app.models import Base, PRJob
    from app.quota import quota_reason

    saved = (
        settings.quota_jobs_per_repo_per_hour,
        settings.quota_jobs_per_installation_per_day,
        settings.quota_jobs_total_per_day,
    )
    # High enough that every dimension is evaluated and none refuses.
    settings.quota_jobs_per_repo_per_hour = 10**6
    settings.quota_jobs_per_installation_per_day = 10**6
    settings.quota_jobs_total_per_day = 10**6

    phase17 = {
        "ix_pr_jobs_created_at",
        "ix_pr_jobs_repo_created_at",
        "ix_pr_jobs_installation_created_at",
    }
    results: dict = {}
    try:
        for label in ("unindexed", "indexed"):
            with tempfile.TemporaryDirectory() as tmp:
                engine = create_engine(f"sqlite+pysqlite:///{Path(tmp) / 'bench.db'}", future=True)
                Base.metadata.create_all(engine, tables=[PRJob.__table__])
                if label == "unindexed":
                    with engine.begin() as conn:
                        for ix in PRJob.__table__.indexes:
                            if ix.name in phase17:
                                ix.drop(conn, checkfirst=True)
                now = datetime.now(UTC)
                rows = [
                    {
                        "dedup_key": f"k{i}",
                        "repo": f"acme/repo-{i % 500}",
                        "pr_number": i,
                        "commit_sha": "0" * 40,
                        "author": "a",
                        "event": "pull_request",
                        "installation_id": i % 50,
                        "status": "QUEUED",
                        "attempts": 0,
                        "created_at": now - timedelta(minutes=i % 43_200),
                        "updated_at": now,
                    }
                    for i in range(n_rows)
                ]
                with engine.begin() as conn:
                    for start in range(0, len(rows), 5_000):
                        conn.execute(insert(PRJob.__table__), rows[start : start + 5_000])

                Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
                session = Session()
                quota_reason(session, repo="acme/repo-1", installation_id=1)  # warm
                samples = []
                for _ in range(5):
                    t0 = time.perf_counter()
                    quota_reason(session, repo="acme/repo-1", installation_id=1)
                    samples.append((time.perf_counter() - t0) * 1000)
                plan = [
                    row[-1]
                    for row in session.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT count(*) FROM pr_jobs "
                            "WHERE repo = 'acme/repo-1' "
                            "AND created_at >= '2026-01-01'"
                        )
                    ).fetchall()
                ]
                session.close()
                engine.dispose()
                results[label] = statistics.median(samples)
                results[f"{label}_plan"] = " | ".join(plan)
    finally:
        (
            settings.quota_jobs_per_repo_per_hour,
            settings.quota_jobs_per_installation_per_day,
            settings.quota_jobs_total_per_day,
        ) = saved

    results["rows"] = n_rows
    results["speedup"] = results["unindexed"] / results["indexed"] if results["indexed"] else 0.0
    return results


def main() -> int:
    print("=" * 74)
    print("AUTOPR optimisation benchmark - measured against the real code")
    print("=" * 74)

    w = bench_worker()
    print()
    print("1. worker throughput (real Worker + JobQueue + process_job)")
    print(
        f"   {w['jobs']} jobs x {w['job_seconds'] * 1000:.0f} ms, one at a time : "
        f"{w['serial_s']:.3f} s"
    )
    print(f"   same jobs, concurrency={w['concurrency']:<2}          : {w['pooled_s']:.3f} s")
    print(f"   -> {w['throughput_ratio']:.2f}x throughput")

    c = bench_connection_reuse()
    print()
    print("2. GitHub HTTP client: one per call (old) vs pooled (new)")
    print(f"   httpx.Client() construction alone : {c['client_construct_ms']:.0f} ms")
    print(
        f"   old (httpx.post builds a client)  : {c['old_ms_per_call']:.1f} ms/call, "
        f"{c['old_connections']} connections / {c['calls']} calls"
    )
    print(
        f"   new (pooled, reused)              : {c['new_ms_per_call']:.1f} ms/call, "
        f"{c['new_connections']} connections / {c['calls']} calls"
    )
    print(
        f"   -> {c['speedup']:.1f}x faster per call, ~{c['saved_per_job_ms']:.0f} ms "
        f"saved per job at 5 calls"
    )

    q = bench_quota()
    print()
    print(f"3. quota check over {q['rows']:,} rows (real quota_reason, 3 COUNTs)")
    print(f"   before phase-17 indexes : {q['unindexed']:.1f} ms  [{q['unindexed_plan']}]")
    print(f"   with indexes            : {q['indexed']:.2f} ms  [{q['indexed_plan']}]")
    print(f"   -> {q['speedup']:.0f}x faster")

    print()
    print("Note: (2) is loopback, so it measures client construction and protocol")
    print("overhead, not WAN latency; over the internet the saving is larger.")
    print("(1) uses a sleep, not a model call — it measures scheduling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
