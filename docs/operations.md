# Operations runbook

Practical procedures for running AutoPR: bring-up, secrets, migrations, scaling,
observing, and what to do when a dependency fails. Commands assume the repo root
and `uv`-managed Python 3.11.

---

## Deployments at a glance

| Environment | DB | Queue | How | Secrets from |
| --- | --- | --- | --- | --- |
| Local native | SQLite (`./autopr.db`) | optional Redis | `uvicorn` + `npm run dev` | env / `.env` |
| Local Compose | Postgres | Redis | `docker compose up --build` | `.env` (interpolated) |
| Cloud (Fly.io) | Managed Postgres | Upstash Redis | CI → `flyctl deploy` | `fly secrets` |

The cloud manifests and CI/CD are described in
[`decisions/phase-12.md`](decisions/phase-12.md); `fly.toml` is the deployment
config. **The live cloud deploy is a deliberate, gated action** — it is
outward-facing and needs the operator's Fly.io auth.

---

## 1. First bring-up (Compose)

```bash
cp .env.example .env
python -c "import secrets; print('AUTOPR_WEBHOOK_SECRET=' + secrets.token_hex(32))"
python -c "import secrets; print('AUTOPR_API_TOKEN=' + secrets.token_hex(32))"
# paste both into .env, then:
docker compose up --build
```

Compose **refuses to start** unless `AUTOPR_WEBHOOK_SECRET` and `AUTOPR_API_TOKEN`
are set (the `:?` interpolation guards). Order of operations on `up`:

1. Postgres and Redis start and become healthy.
2. The **api** service runs `alembic upgrade head`, then serves uvicorn with a
   30s graceful-shutdown bound.
3. The **worker** waits for the api to be *healthy* (`/readyz` = DB + Redis up)
   before starting, so **only one service migrates** — no concurrent-upgrade
   race. The worker's own `create_all` is then an idempotent no-op.

Verify: `curl -s localhost:8000/readyz` → `{"status":"ready",…}`.

---

## 2. Secrets

- **Where they live.** `.env` locally (gitignored), `fly secrets` in cloud. They
  are **never** baked into an image and never committed. `AUTOPR_GROQ_API_KEY`
  lives only in `.env`.
- **Rotate the webhook secret:** update the GitHub webhook's secret and
  `AUTOPR_WEBHOOK_SECRET` together, then restart the api (`docker compose up -d
  api` / `fly deploy`). Deliveries signed with the old secret will 401 during the
  gap — rotate in a low-traffic window.
- **Rotate the API token:** set a new `AUTOPR_API_TOKEN`, restart, and update any
  operator's saved token (the dashboard reads it from `localStorage`
  `autopr_api_token`, or bake it with `VITE_AUTOPR_API_TOKEN`).
- **Verify nothing leaked before any commit/push:**
  ```bash
  git ls-files | grep -E '\.env$|\.db$'   # must print nothing
  ```

---

## 3. Migrations (Alembic)

The database URL comes from `app.config` (`AUTOPR_DATABASE_URL`) — there is no
second source of truth in `alembic.ini`.

```bash
uv run alembic upgrade head            # apply all migrations
uv run alembic current                 # show current revision
uv run alembic downgrade -1            # roll back one
uv run alembic revision --autogenerate -m "add X"   # create a migration from model changes
```

- In **Compose/cloud** the api container runs `alembic upgrade head` as a release
  step before serving.
- On **SQLite**, migrations run in **batch mode** (copy-and-swap) because SQLite
  can't `ALTER TABLE` in place — this is already configured in `alembic/env.py`.
- CI runs a real-Postgres **up → down → up** round-trip on every push, so a
  migration that isn't reversible fails the build.

---

## 4. Scaling workers

```bash
docker compose up --scale worker=3
```

Safe by construction: all workers share one consumer group, Redis hands each
stream entry to exactly one consumer, and the `job_results` ledger makes
reprocessing idempotent. Adding workers increases throughput and crash
resilience (a dead worker's in-flight jobs are reclaimed via `XAUTOCLAIM`) with
no coordination code.

> Note: the **API** stays a single process by design (1–50 req scale). A
> multi-process API (gunicorn) would need `prometheus_client` multiprocess mode
> to aggregate `/metrics` across workers — see
> [`decisions/phase-9.md`](decisions/phase-9.md).

---

## 5. Observing

```bash
# Liveness vs readiness
curl -s localhost:8000/healthz         # {"status":"ok"} — process is up
curl -s localhost:8000/readyz | jq     # 200 ready / 503 with per-dependency checks

# Metrics (Prometheus text)
curl -s localhost:8000/metrics | grep -E 'autopr_(jobs|reviews|queue)'

# Follow one request across both processes (JSON logs)
docker compose logs -f api worker | jq 'select(.correlation_id=="<id>")'
```

Key log events to know: `webhook.ingested`, `webhook.enqueue_failed`,
`startup.queue_unavailable`, `audit.review_approved`, `audit.review_rejected`.

---

## 6. Failure playbook

| Symptom | Meaning | Action |
| --- | --- | --- |
| `/readyz` 503, `redis: error` | Redis down. Webhook returns 503 (GitHub retries); dashboard reads still serve. | Restart Redis; workers reclaim in-flight jobs automatically; `PENDING` jobs re-enqueue on worker startup. |
| `/readyz` 503, `database: error` | DB unreachable. | Restart/repoint the DB. No data lost — the ingress commits before enqueue. |
| Startup crash: *"AUTOPR_WEBHOOK_SECRET is still the placeholder"* | Fail-fast secret validation. | Set a real secret, or `AUTOPR_ALLOW_INSECURE=1` for local/dev only. |
| Jobs land in `DEAD` | Exceeded `AUTOPR_MAX_ATTEMPTS`; see `last_error` on the job / in `/jobs`. | Fix the root cause; re-drive by re-delivering the webhook (new `dedup_key` if the sha changed). |
| `approve` returns 502 `action_failed` | The live GitHub call failed; decision marked `FAILED`. | Inspect `last_error`; retry approve after fixing (token scope, rate limit, PR state). |
| Dashboard: "Cannot reach the API" | API down or CORS origin not allowlisted. | Check the api process; ensure the dashboard origin is in `AUTOPR_CORS_ORIGINS`. |

---

## 7. Graceful shutdown

The Compose api command sets `--timeout-graceful-shutdown 30`, so a `SIGTERM`
drains in-flight HTTP requests within 30s before the process is forced down. The
worker's signal handler stops the poll loop **between** jobs, so a shutdown
doesn't abandon a job mid-flight; anything genuinely in-flight during a hard kill
is reclaimed via `XAUTOCLAIM` on the next worker's next loop.

---

## 8. Going live on GitHub (leaving dry-run)

Dry-run is the default and the safe state. To let approvals post for real:

1. Set `AUTOPR_GITHUB_TOKEN` (a fine-grained PAT / app token with only the needed
   scopes) and `AUTOPR_GITHUB_DRY_RUN=false`.
2. Confirm `AUTOPR_API_TOKEN` is set (so `approve`/`reject` are authenticated).
3. Approvals now perform real writes. **Code-changing actions remain
   human-gated regardless** — leaving dry-run does not change the approval
   requirement, only whether an approved action actually reaches GitHub.

Verify on `/stats`: `config.github_dry_run` and `config.live_github` reflect the
live state the dashboard badges surface.
