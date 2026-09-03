# AutoPR

**A multi-agent system that reviews pull requests and repairs failing CI — safely.**

AutoPR watches a repository's GitHub webhooks and, for every pull request or
failed check, runs a LangGraph agent pipeline that reviews the diff, drafts
tests, or proposes a fix. Anything that would change code, or carries more than
trivial risk, is **never applied automatically** — it is queued for a human to
approve on a dashboard. The system's job is to do the reading, reasoning, and
drafting; the maintainer keeps the final say.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-agents-1C3C3C)
![Redis Streams](https://img.shields.io/badge/Redis-Streams-DC382D?logo=redis&logoColor=white)
![Postgres](https://img.shields.io/badge/Postgres%20%2F%20SQLite-SQLAlchemy%202-4169E1?logo=postgresql&logoColor=white)
![React](https://img.shields.io/badge/React%2018-TypeScript-3178C6?logo=react)
![License](https://img.shields.io/badge/license-MIT-green)

<!-- After the repo is pushed to GitHub, uncomment the live CI badge:
[![CI](https://github.com/<owner>/autopr/actions/workflows/ci.yml/badge.svg)](https://github.com/<owner>/autopr/actions/workflows/ci.yml)
-->

---

## Why this exists (the one-paragraph pitch)

Reviewing PRs and babysitting flaky CI is exactly the kind of high-volume,
pattern-heavy work an LLM is good at — and exactly the kind of work you must
**not** let an LLM do unsupervised, because the blast radius of a wrong automated
commit is unbounded. AutoPR is built around that tension. The interesting
engineering isn't the prompts; it's everything that makes an unreliable, slow,
non-deterministic model safe to put in front of a real repository: **exactly-once
job processing**, a **crash-safe queue**, a **dry-run write boundary**, and a
**human-in-the-loop approval gate** that no agent can bypass.

It is deliberately right-sized for a **single-operator, 1–50-request** workload:
one uvicorn process, SQLite-with-WAL by default (Postgres in Compose/cloud), and
a single Redis stream. The correctness and safety machinery is production-grade;
the horizontal-scale machinery is intentionally *not* built, and every such
trade-off is written down in [`docs/decisions/`](docs/decisions/).

---

## What it does

```
                         GitHub (pull_request / check_run / workflow_run)
                                            │  HMAC-signed webhook
                                            ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  FastAPI ingress  (app/main.py)                                             │
│  • verify signature (constant-time)   • idempotent persist   • enqueue      │
│  • returns 200 immediately — no synchronous LLM work on the request path    │
└───────────────────────────────────────────────────────────────────────────┘
                                            │  Redis Streams  (at-least-once delivery)
                                            ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  Worker  (app/worker.py)                                                    │
│  • consumer group + PEL reclaim (XAUTOCLAIM)   • reconcile-on-startup       │
│  • exactly-once via a job_results ledger       • bounded retry → DEAD       │
└───────────────────────────────────────────────────────────────────────────┘
                                            │  invokes the graph
                                            ▼
┌──────────────────────────────┐        ┌───────────────────────────────────┐
│  Track A — PR review          │        │  Track B — CI fix                  │
│  code_reviewer → test_gen ─┐  │        │  ci_monitor → fix_agent →          │
│                            ▼  │        │      fix_verifier (Docker sandbox) │
│                        router  │        │              └──────► router      │
└──────────────────────────────┘        └───────────────────────────────────┘
                                            │  policy.route() — risk-based disposition
                                            ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  Router + human-in-the-loop                                                 │
│  • ≤ low-risk *comment* → auto-post (dry-run by default)                     │
│  • medium/high risk, OR any code change → queue a ReviewDecision (PENDING)  │
└───────────────────────────────────────────────────────────────────────────┘
                                            │
                        ┌───────────────────┴────────────────────┐
                        ▼                                         ▼
             Operator dashboard (React/TS)          POST /reviews/{id}/approve|reject
             — pending queue, jobs, history         — the ONLY path to a real GitHub write
```

A fuller diagram and a walk-through of both tracks live in
[`docs/architecture.md`](docs/architecture.md).

### Two agent tracks

- **PR review** (`event=pull_request`) — `code_reviewer` reads the changed files
  and writes a review; `test_generator` drafts tests for the change; `router`
  decides whether the review comment is safe to auto-post or needs approval.
- **CI fix** (`event=check_run` / `workflow_run`) — `ci_monitor` classifies the
  failure; `fix_agent` proposes a patch; `fix_verifier` applies it to a snapshot
  of the repo **inside an isolated Docker sandbox** (`--network none`, dropped
  capabilities, resource caps) and re-runs the failing check. A fix is only ever
  *proposed* to a human — a verified patch is still human-gated, always.

---

## Safety invariants (the part that matters)

These hold regardless of configuration, risk score, or model output:

1. **Dry-run by default.** `AUTOPR_GITHUB_DRY_RUN=true` out of the box. The GitHub
   client records the action it *would* take and touches nothing. You must
   deliberately opt into live posting.
2. **Every code-changing action is human-gated.** A proposed fix — even one the
   sandbox verified as passing — is written to the approval queue as `PENDING`
   and applied only when a human approves. No agent, no risk score, and no config
   flag can auto-apply code.
3. **Only ≤ low-risk *comments* auto-post.** The ceiling is
   `AUTOPR_AUTO_COMMENT_MAX_RISK` (default `low`). Unknown/unparseable risk ranks
   as high and fails closed to the human queue.
4. **The webhook is authenticated.** Every delivery is HMAC-verified against
   `AUTOPR_WEBHOOK_SECRET` in constant time; the app **refuses to start** on the
   shipped placeholder secret unless `AUTOPR_ALLOW_INSECURE=1` (local/dev/tests).
5. **The write API is authenticated.** `approve`/`reject` require a bearer token
   (constant-time compared) whenever `AUTOPR_API_TOKEN` is set, and every approval
   emits an `audit.*` log line (actor IP, decision, action, outcome).
6. **Secrets never enter the image or the repo.** `.env` is gitignored; Compose
   and Fly inject secrets at runtime. The Groq API key lives only in `.env`.

---

## Engineering highlights

| Concern | How AutoPR handles it |
| --- | --- |
| **Exactly-once processing** | A `job_results` ledger keyed by `job_id` with `INSERT … ON CONFLICT DO NOTHING` (Postgres *and* SQLite). At-least-once delivery + an idempotent write = effectively-once. |
| **Crash safety** | Redis Streams consumer group; a dead worker's un-acked entries are reclaimed via `XAUTOCLAIM`; `PENDING` rows are re-enqueued by a startup reconcile. |
| **Bounded failure** | Each attempt increments `attempts`; past `AUTOPR_MAX_ATTEMPTS` the job is parked `DEAD` with its last error, never retried forever. |
| **Graceful degradation** | Redis down → the webhook fails closed with **503** (GitHub retries), while the read-only dashboard keeps serving from the database. `/readyz` reports the truth. |
| **Schema migrations** | Alembic, with one source of truth for the URL (`app.config`) and batch mode for SQLite. CI drives a real-Postgres up → down → up round-trip. |
| **Observability** | structlog JSON from both processes; a `correlation_id` that rides contextvars in-process and the job payload across the queue; `/healthz` (liveness) vs `/readyz` (deep) vs `/metrics` (Prometheus). |
| **Security perimeter** | Fail-fast secret validation, constant-time HMAC + bearer checks, per-IP rate limiting, a CORS allowlist, and audit logging on every write. |
| **Typed end-to-end** | The React dashboard is TypeScript; its response models mirror the API serializers, so a field rename on either side is a compile error, not a runtime `undefined`. |

---

## Quick start

**Prerequisites:** Python 3.11 (via [uv](https://docs.astral.sh/uv/)), Node 20+.
Docker Desktop only if you want the full Compose stack or the CI-fix sandbox.

### 1. Run the backend (native, SQLite — zero infra)

```bash
# from the repo root
uv sync --extra dev                     # create .venv from the committed uv.lock

# SQLite + no external services; allow the dev placeholder secret
AUTOPR_ALLOW_INSECURE=1 AUTOPR_LOG_JSON=false \
  uv run uvicorn app.main:app --reload --port 8000
```

The API is now at `http://localhost:8000` (interactive docs at `/docs`). With no
Redis running it logs `startup.queue_unavailable` and the webhook returns 503 —
that's the designed degradation; the dashboard's read APIs work regardless.

### 2. Seed realistic demo data (optional but recommended)

```bash
uv run python scripts/seed_demo.py      # ~10 jobs + a populated review queue
```

Safe to re-run — it owns only the `acme/*` demo rows and leaves real data alone.

### 3. Run the dashboard

```bash
cd frontend
npm ci
npm run dev                             # http://localhost:5173
```

The dev server proxies `/api/*` to the backend on `:8000`, so the browser stays
same-origin. Open **http://localhost:5173** — Overview, Review Queue, and Jobs
should all light up with the seeded data.

### 4. (Alternative) The whole stack in Docker

```bash
cp .env.example .env
# set AUTOPR_WEBHOOK_SECRET and AUTOPR_API_TOKEN (both required by Compose):
#   python -c "import secrets; print(secrets.token_hex(32))"
docker compose up --build
```

Brings up Postgres, Redis, Qdrant, the API (which runs `alembic upgrade head`
first), and the worker. See [`docs/operations.md`](docs/operations.md).

---

## Tests

The backend suite is **hermetic** — in-memory SQLite, `fakeredis`, and a mocked
LLM — so it runs fully offline and fast:

```bash
uv run pytest -q                                    # all backend tests
uv run pytest --cov=app --cov-report=term-missing   # with coverage
```

```bash
cd frontend && npm test                             # Vitest + React Testing Library
```

CI (`.github/workflows/ci.yml`) additionally runs ruff, mypy, a coverage gate, a
real-Postgres Alembic round-trip, the frontend lint/type/test/build, and a
build of both container images.

---

## Configuration

Every setting is an `AUTOPR_`-prefixed environment variable (see
[`.env.example`](.env.example) and [`app/config.py`](app/config.py)). The ones
you're most likely to touch:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTOPR_WEBHOOK_SECRET` | *(placeholder)* | HMAC secret for GitHub deliveries. App refuses to start on the placeholder unless `AUTOPR_ALLOW_INSECURE=1`. |
| `AUTOPR_API_TOKEN` | *(empty)* | Bearer token for `approve`/`reject`. Empty = unauthenticated dev no-op (with a loud warning). |
| `AUTOPR_DATABASE_URL` | `sqlite+pysqlite:///./autopr.db` | SQLite locally; a `postgresql+psycopg://…` URL in Compose/cloud. |
| `AUTOPR_REDIS_URL` | `redis://localhost:6379/0` | Queue backend. |
| `AUTOPR_GITHUB_DRY_RUN` | `true` | Master safety switch. `true` = touch nothing. |
| `AUTOPR_GITHUB_TOKEN` | *(empty)* | Enables live GitHub reads/writes. Only meaningful with dry-run off. |
| `AUTOPR_GROQ_API_KEY` | *(empty)* | Powers the LLM agents (Llama via Groq). Kept only in `.env`. |
| `AUTOPR_CORS_ORIGINS` | local dev origins | Comma-separated allowlist for the dashboard origin. |

---

## Project layout

```
app/
  main.py            FastAPI: /webhook + human-in-the-loop ops API + dashboard reads
  worker.py          Redis-Streams consumer: reclaim, reconcile, drive the graph
  ingest.py          idempotent persist + enqueue (the exactly-once write)
  queue.py           Redis Streams client (consumer groups, PEL, XAUTOCLAIM)
  db.py              SQLAlchemy engine, SQLite WAL pragmas, ON CONFLICT helper
  models.py          PRJob, JobResult (ledger), ReviewDecision + status enums
  config.py          pydantic-settings; fail-fast secret validation
  security.py        constant-time HMAC + bearer verification
  ratelimit.py       in-process per-IP fixed-window limiter
  observability.py   structlog config, correlation id, Prometheus collectors
  agents/            LangGraph graph + nodes (code_reviewer, ci_monitor, fix_agent, …)
  routing/           policy.route() disposition, dry-run GitHub client, approval store
  sandbox/           isolated Docker fix-verification (runner, verifier, policy)
alembic/             migration env + versioned schema
frontend/            React 18 + TypeScript + Vite operator dashboard
docs/                architecture, operations runbook, API reference, decision logs
tests/               25 hermetic test modules (SQLite + fakeredis + mocked LLM)
```

---

## Documentation

- **[Architecture](docs/architecture.md)** — components, data flow, both tracks, the exactly-once and human-gating mechanics.
- **[Operations runbook](docs/operations.md)** — deploy, scale workers, rotate secrets, migrate, observe, and what degrades when a dependency dies.
- **[API reference](docs/api.md)** — every endpoint, auth, and `curl` examples.
- **[Decision logs](docs/decisions/)** — one ADR per phase: the choice made, the alternative rejected, and the corners deliberately cut.

---

## License

[MIT](LICENSE).
