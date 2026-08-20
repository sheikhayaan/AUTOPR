# Phase 1 — Decisions Log

Core infrastructure: idempotent webhook ingestion, durable queue, crash-safe
worker. Each entry states what was chosen, the rejected alternative, and why.

---

## 1. Queue: Redis Streams + consumer groups (not a LIST, not Celery/RQ)

**Chosen.** `XADD` to a stream; workers read via `XREADGROUP` under a consumer
group; completion is `XACK`; crash recovery is `XAUTOCLAIM`.

**Why.** The spec requires that a worker crashing mid-job does not lose or
double-process the job. Streams give this natively: a delivered-but-unacked
message stays in the consumer's **Pending Entries List (PEL)**. It is neither
gone (as it would be with a LIST) nor reprocessed by the original consumer
until explicitly reclaimed. Another worker calls `XAUTOCLAIM` after an idle
threshold and takes ownership. This is the textbook at-least-once delivery
substrate.

**Rejected — Redis LIST (`RPUSH`/`BLPOP`).** Simpler, but `BLPOP` *removes* the
element atomically on read. If the worker dies after the pop and before
finishing, the job is gone with no record it was ever taken. Recovering that
requires bolting on a separate "processing" list + reaper — i.e.
reimplementing the PEL by hand. Not worth it when Streams give it for free.

**Rejected — Celery / RQ.** Less code up front, but they *hide* the exact
mechanics (acks, visibility timeout, redelivery) that this project exists to
demonstrate and that I need to defend in an interview. They also pull in a
heavy dependency and their own broker semantics. For a portfolio piece whose
whole point is "show the engineering," delegating the interesting part to a
library is the wrong trade.

---

## 2. Idempotency: DB unique constraint + `ON CONFLICT DO NOTHING`
   (not an application-level "check-then-insert")

**Chosen.** A `dedup_key = sha256(event|repo|pr|head_sha)` column with a UNIQUE
constraint. Ingestion does `INSERT ... ON CONFLICT (dedup_key) DO NOTHING`,
then reads back the row. Only the request that actually inserted the row (i.e.
`rowcount == 1`) proceeds to `XADD`. Duplicates and race-losers get
`created=False` and enqueue nothing.

**Why.** This makes idempotency a property enforced by the database under
concurrency, not by hopeful application logic. Two webhooks racing for the same
PR head SHA cannot both win the insert; the constraint serialises them. So the
job is created once and enqueued exactly once, regardless of delivery timing.

**Rejected — `SELECT` then `INSERT` in app code.** Classic TOCTOU race: two
requests both `SELECT` (miss), both `INSERT`, and you get either a duplicate or
an unhandled IntegrityError. The DB constraint is the only race-free place to
enforce uniqueness.

**Dedup key choice.** Keyed on **head SHA**, not just PR number, on purpose: a
new push to the same PR *should* create new work (new code to review). A
literal GitHub redelivery of the same event carries the same head SHA and is
correctly deduped. (GitHub's `X-GitHub-Delivery` UUID would dedup *exact*
redeliveries too, but not "same commit re-sent as a slightly different event";
the content-derived key is the stronger guarantee. Noted as a possible belt-
and-suspenders addition.)

---

## 3. Exactly-once *side effect* via a `job_results` ledger

**Chosen.** At-least-once delivery means `process_job` can run more than once
for the same job (crash-then-reclaim). We make that safe two ways: (a) a
`status == DONE` short-circuit, and (b) the side effect is an
`INSERT ... ON CONFLICT DO NOTHING` into `job_results` keyed by `job_id`. N
deliveries ⇒ at most one result row.

**Why.** True exactly-once *delivery* is impossible in a distributed queue; the
achievable and correct goal is exactly-once *effect* via idempotent
processing. `test_crash_after_side_effect_reprocessing_is_noop` proves the
worst case (work committed, then crash before ack) yields exactly one row.

**Rejected — trusting the ack alone.** If correctness depended only on "we
acked, so it ran once," any crash in the ack window would either lose the work
or double it. The ledger decouples correctness from ack timing.

---

## 4. Reconcile pass for the `INSERT → XADD` crash window

**Chosen.** The ingest path commits the row (`PENDING`) *then* `XADD`s (→
`QUEUED`). If the process dies between those, the row is stranded in `PENDING`
with nothing on the stream. On startup the worker runs `reconcile_pending`,
which re-publishes any `PENDING` job. Idempotent processing makes re-publishing
safe.

**Why.** This is a lightweight **transactional-outbox** pattern: Postgres is
the source of truth for "this job exists"; Redis is only the delivery channel.
The reconcile closes the one gap where the two could disagree.

**Rejected — full outbox with a relay table + poller.** More robust for very
high throughput but heavier than this project needs at Phase 1. Flagged as the
scale-up path. **Rejected — 2-phase / XA across Postgres+Redis.** Operational
complexity far beyond the requirement.

---

## 5. Bounded retries → `DEAD` (human queue), not infinite retry

**Chosen.** `attempts` increments per try; once it exceeds `max_attempts`
(default 5) the job flips to `DEAD` and is acked (stops being redelivered). The
spec's "cap retries and escalate to human queue" — `DEAD` *is* that queue
(query `WHERE status = 'dead'`).

**Rejected — retry forever.** A poison message (malformed payload, permanent
downstream failure) would loop indefinitely, burning the worker and, in later
phases, LLM spend.

---

## 6. Synchronous SQLAlchemy + redis-py (not async) in Phase 1

**Chosen.** The webhook handler does one INSERT + one XADD, both sub-ms. Sync
code is dramatically easier to test deterministically (the crash/race tests
would be far fiddlier with an event loop).

**Rejected — async everywhere.** Real benefit only under high concurrent I/O
we don't have yet. Noted as a revisit if load testing (Phase 6) shows the API
is connection-bound.

---

## Corners cut / simplifying assumptions — be ready to explain these

These are deliberate and known. None affect the correctness the tests prove;
they are scope boundaries for Phase 1.

1. **Tests run on SQLite + fakeredis, not Postgres + real Redis.** Docker isn't
   installed on the dev machine yet (agreed to defer). The application code is
   datastore-agnostic except one branch (`app/db.insert_ignore_duplicates`) that
   picks the SQLAlchemy dialect construct. **Fidelity gaps to state aloud:**
   - The concurrency test uses SQLite with a `StaticPool` (single shared
     connection). SQLite serialises writes, so the test proves the
     *constraint-driven dedup logic* is correct but does **not** reproduce true
     Postgres row-lock contention. On Postgres the same `ON CONFLICT` path holds;
     the guarantee is the constraint, not the engine.
   - fakeredis implements the stream commands we use
     (`XADD/XREADGROUP/XACK/XAUTOCLAIM/XPENDING`) but is not byte-for-byte Redis.
     The crash test asserts on *observable behaviour* (PEL count, redelivery,
     one result row), not Redis internals, so it should translate — but the
     honest statement is "validated against a Redis-compatible fake; real-Redis
     integration run is pending Docker."
   - **What's genuinely solid regardless of engine:** the idempotency logic, the
     exactly-once ledger, the reconcile, and the retry cap. **What needs the
     real-infra run to be airtight:** the true-concurrency claim and
     `XAUTOCLAIM` timing under a real idle clock.

2. **Schema is created via `Base.metadata.create_all`, not Alembic migrations.**
   Fine for Phase 1; a production system needs versioned migrations. Deferred
   intentionally.

3. **`XAUTOCLAIM` runs inline in each worker poll cycle**, not as a dedicated
   reaper process. Simpler and correct for a handful of workers; at large scale
   a single reaper (or per-worker jitter) avoids redundant reclaim scans.

4. **Reclaim idle threshold is a fixed config value (30s).** A real deployment
   would tune this against actual job duration; too low re-runs slow-but-alive
   jobs, too high delays recovery.

5. **No auth/rate-limiting on `/webhook` beyond the HMAC.** HMAC is the security
   boundary GitHub gives us; a public deployment would add IP allow-listing and
   rate limits.

6. **Worker "work" is a placeholder** (`default_handler`). Phase 2 replaces it
   with the LangGraph pipeline. The exactly-once machinery around it is real; the
   thing it wraps is a stub by design at this phase.
