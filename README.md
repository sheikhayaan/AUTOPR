# AutoPR

Multi-agent AI system that automates GitHub PR review, test generation, and
CI-failure auto-fixing.

Build is phased. **Phase 1 (this commit): core infrastructure** — idempotent
GitHub webhook ingestion, a durable Redis Streams job queue, and a crash-safe
worker, wired together with Docker Compose (FastAPI + Postgres + Redis + Qdrant).

## Phase 1 at a glance

```
GitHub ──HMAC──▶ /webhook ──INSERT(dedup_key, ON CONFLICT DO NOTHING)──▶ Postgres
                     │                                                      │
                     └── new row only ──▶ XADD ──▶ Redis Stream ──▶ Worker ─┘
                                                     (consumer group + PEL)
```

* **Idempotent ingestion** — a `dedup_key` unique constraint makes duplicate
  webhook deliveries a no-op. Only the row's creator enqueues, so a PR head SHA
  is enqueued exactly once even under racing deliveries.
* **Durable queue** — Redis Streams consumer groups keep in-flight work in a
  Pending Entries List; a crashed worker's message is recovered via
  `XAUTOCLAIM`, never lost.
* **Exactly-once side effect** — the worker records results in a `job_results`
  ledger keyed by job id, so at-least-once delivery still yields one effect.
* **Bounded retries** — a perpetually failing job is escalated to a `DEAD`
  (human-review) state instead of retrying forever.

See [`docs/decisions/phase-1.md`](docs/decisions/phase-1.md) for the design
rationale, rejected alternatives, and explicitly-flagged corners cut.

## Run the tests (no Docker required)

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"
uv run pytest
```

Tests run against in-memory SQLite + fakeredis. The same code runs against real
Postgres/Redis under Compose.

## Run the stack (needs Docker)

```bash
cp .env.example .env   # set AUTOPR_WEBHOOK_SECRET
docker compose up --build
# scale workers to demo crash-recovery / concurrent consumption:
docker compose up --scale worker=3
```
