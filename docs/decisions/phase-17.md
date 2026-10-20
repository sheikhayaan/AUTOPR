# Phase 17 — throughput, latency, and the claim that makes concurrency safe

**Status:** accepted
**Measured on:** 2026-10-20, Windows 11, Python 3.11, SQLite (in-memory for the queue
bench, on-disk for the query bench). Re-runnable: `python scripts/bench_optimisations.py`.

---

## The problem, stated as a rate

The worker handled **one message at a time**. Throughput was `1 / service_time`
regardless of how much machine was idle, so one slow job — a provider stall, a
60-second sandbox run — held up every other delivery behind it. Adding CPU did
nothing; adding workers was the only lever, and that multiplied memory (each job can
hold a 25 MB repo snapshot).

Two suspects were cheaper to fix than to scale around:

| Symptom | Cause found by reading the code |
|---|---|
| A single slow job stalls the queue | `Worker.run_once` called `_handle_message` in a `for` loop |
| Every GitHub call felt slow, end to end | `httpx.Client` built per call (or `httpx.get`, which does the same), so the SSL context and pool were rebuilt 4–7 times per job |
| Quota checks slowed as history grew | `COUNT(*) … WHERE created_at >= ?` with no index on `created_at` |

---

## The order of work, and why

**Concurrency is a correctness change before it is a speed change.** Two deliveries of
one job both read "not DONE" and both post, so running the loop in parallel *as it was*
would have doubled outward effects instead of doubling throughput. The claim ships
first, with a negative control, and concurrency only after it.

### 1. The claim (`process_job`)

A conditional `UPDATE … WHERE id = ? AND status IN (QUEUED, PENDING)` — or
`PROCESSING` past the reclaim window — decides ownership in the database. Only the
worker whose UPDATE touched a row runs the handler; everyone else ACKs, because the
winner owns the side effect. `attempts` is incremented in the same statement, so the
retry cap counts executions even under parallel delivery.

`reclaim_idle_ms` already exceeds one job's worst case, so a *healthy* job is never
stolen, while a dead worker's lease is.

### 2. The release — a hole this phase opened and then closed

The claim alone turned the existing failure path into a black hole. A handler that
raised left the row in `PROCESSING`: `reconcile_pending` only sweeps `PENDING`, and a
redelivery *inside* the lease window loses the claim and is ACK'd — so the job sat
`PROCESSING` forever with its message acknowledged. Nothing retried it, nothing
reported it. `tests/test_worker_crash.py::test_retries_capped_then_escalated` went red
the moment the claim landed, which is the only reason it was found before production.

The failure path now **hands the row back** with a second conditional UPDATE, guarded on
`updated_at == claim_stamp` so a worker that has since re-claimed the row keeps it.
Attempts are kept — they were committed with the claim, and the cap depends on them.

### 3. The naive-datetime trap

The claim's `WHERE` clause compares `updated_at` with a timezone-aware cutoff. SQLAlchemy's
default session synchronisation (`synchronize_session="auto"`) evaluates that clause **in
Python** against objects already in the identity map — and SQLite returns *naive*
datetimes even for a `timezone=True` column. Result:
`TypeError: can't compare offset-naive and offset-aware datetimes`, raised **inside a
worker thread**, where `ThreadPoolExecutor` swallows it as an unhandled thread exception
and the job silently never runs. The suite stayed green; only a
`PytestUnhandledThreadExceptionWarning` hinted at it.

Fix: `execution_options(synchronize_session=False)` — the database decides the claim, and
the row is reloaded afterwards anyway. Pinned by
`test_claim_does_not_compare_datetimes_in_python`, which pre-loads the row into the
identity map (the state that used to explode).

### 4. Bounded concurrency

`worker_concurrency` (default 4) messages are dispatched to a process-local
`ThreadPoolExecutor`; the cycle waits for its batch, so memory stays bounded by
`worker_concurrency × snapshot_max_total_bytes`. The pool is created lazily (a worker
that never dispatches has no threads) and drained on shutdown (`shutdown(wait=True)`), so
a stop doesn't abandon a half-run job. `concurrency == 1` is byte-for-byte the old
serial path.

The ceiling is memory and the provider's per-key rate limit, not CPU: a worker that
exceeds Groq's limit just collects 429s, which is slower than queueing.

### 5. One HTTP client per process

`httpx.Client` is thread-safe and pools keep-alive connections. The credential is **not**
on the client: tokens are per-tenant and travel in per-request headers, which is the only
reason a shared pool is safe in a multi-tenant deployment. `test_client_carries_no_credential`
fails the moment someone "simplifies" this by setting `headers=` or `auth=` on the client.

### 6. Indexes for the queries the product actually runs (phase-17 migration)

Five, each named for the query it serves: instance-wide daily quota, per-repo hourly
quota, per-tenant daily quota, `status` (reconcile + pending reads), and
`(status, created_at)` on `review_decisions`.

---

## Measurements

`python scripts/bench_optimisations.py`, against the real `Worker`, `JobQueue`,
`process_job`, `quota_reason`, and the real client builder.

### Worker throughput — 8 jobs × 100 ms of simulated provider latency

| Configuration | Wall clock | Throughput |
|---|---:|---:|
| Serial (concurrency 1) | 1.02 s | 1× |
| `worker_concurrency=4` | 0.36 s | **2.8–3.1×** (3 runs) |

Below the theoretical 4×, and the gap is informative rather than noise: SQLite serialises
*writers*, and each job does a claim `UPDATE` plus a `DONE` commit, so four threads queue on
the write lock. Postgres with per-row locking would be closer to 4×; that is reasoning, not a
measurement, and it is not claimed as one. The `100 ms` handler is a `sleep`, so this measures
**scheduling**, not the model.

> **A number this document had to correct.** The first harness run reported 3.72×. It used
> one in-memory SQLite connection shared across threads (`StaticPool`) with the comment
> "one shared in-memory DB across threads" — which is not a concurrency model: one connection
> means threads commit and roll back through the same DBAPI transaction, so a claim could be
> undone by another thread's rollback and a job would silently lose its handler run
> ("expected 8 jobs, got 7"). Fixing the harness to a file database with `NullPool` (one
> connection per thread, like any real deployment) gave the 2.8–3.1× above. The lower number
> is the true one; it was flattering before.

### HTTP client — 20 calls to a local keep-alive server

| Strategy | Per call | Connections |
|---|---:|---:|
| `httpx.post` (builds a client per call — the old code) | 291.1 ms | 20 / 20 |
| Pooled client, reused | 7.8 ms | 10 / 20 |

Reuse is partial in this harness (10 connections for 20 calls — the loopback server closes an
idle socket and the measurement retries once), so the 37× is **not** a handshake count: it is
dominated by removing client construction. The connection-reuse benefit is real but smaller,
and it would grow with RTT on a real network.

**`httpx.Client()` construction alone measures 273 ms** on this host: it builds an SSL
context and a connection pool. That cost was paid four to seven times per job — roughly
**1.4 s of pure overhead per job**, dwarfing every other latency component. This is
loopback, so it measures construction and protocol overhead, not WAN latency; over the
internet the saving is larger.

### Quota check — `quota_reason` (three `COUNT`s) over 200,000 rows

| Schema | Median | Query plan |
|---|---:|---|
| Before phase-17 indexes | 117.0 ms | `SCAN pr_jobs` |
| With indexes | 1.10 ms | `SEARCH pr_jobs USING COVERING INDEX ix_pr_jobs_repo_created_at (repo=? AND created_at>?)` |

106× on SQLite. The shape is what matters: a sequential scan's cost is a function of
**history**, so the unindexed version degrades as the table ages even at constant load.

---

## What the negative controls prove

`python scripts/negative_control_claim.py` disables each guard, runs the tests, and
asserts they fail:

| Guard disabled | Tests that fail |
|---|---|
| Claim's conditional `WHERE` | `test_two_threads_claiming_one_job_run_the_handler_once`, `test_claim_lost_acks_without_working` |
| Release on failure | `test_failed_handler_releases_the_claim`, `test_retries_capped_then_escalated`, `test_run_once_handles_every_job_exactly_once` |

Each control restores `app/worker.py` byte-for-byte and verifies the restore; a sabotage
point that no longer matches exits non-zero rather than passing silently.

---

## What is *not* proven here

* **Postgres.** All numbers above are SQLite. The indexes and the claim are
  backend-agnostic by construction, but "index-only scan" on Postgres, real row-lock
  behaviour under two concurrent claims, and `EXPLAIN (ANALYZE)` output have not been
  observed. `tests/test_migrations.py` covers the migration applying to a single head,
  not its performance.
* **Multiple worker processes.** The claim closes the cross-process race by design; only
  the in-process case is tested. Two processes claiming one row concurrently is the
  remaining untested path.
* **A real provider under `worker_concurrency=4`.** The default of 4 is a memory and
  rate-limit judgement, not a measured optimum — the measurement shows concurrency
  *helps*, not how high it should go before 429s dominate.
* **TLS handshake savings over the internet.** The 40× above is dominated by client
  construction on loopback; the WAN figure would need a real network.
