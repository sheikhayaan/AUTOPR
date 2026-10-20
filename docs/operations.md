# Operations runbook

Practical procedures for running AutoPR: bring-up, secrets, migrations, scaling,
observing, and what to do when a dependency fails. Commands assume the repo root
and `uv`-managed Python 3.11.

---

## Before you deploy: read the measured numbers

The README's latency table is produced by `python scripts/bench_api.py`, which drives
the real ASGI app with `fakeredis` and in-memory SQLite. It is **in-process**: no
socket, no TLS, no network. Treat the p50s as a floor, and budget for a cold start
(≈1–3 s on the first webhook after boot) unless you keep a machine warm.

If those numbers matter to a capacity decision, re-run the bench on the host class you
intend to use and compare against [`assets/bench_result.json`](assets/bench_result.json)
— the histogram in the README is drawn from that file, so a fresh run both re-measures
and re-draws.

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

## 8. Perimeter: exposing the API to the internet

GitHub must reach `/webhook`, and nothing else needs to be reachable. Two shapes
are supported, and the second is the one to prefer:

| Shape | Exposure | Notes |
|---|---|---|
| VPS with a proxy | whole app behind nginx | Set `AUTOPR_REQUIRE_AUTH_FOR_READS=true`, or only route `/webhook` |
| **Loopback + tunnel** | `uvicorn --host 127.0.0.1`, tunnel ingress routes `/webhook` only | Read endpoints then have no internet path at all |

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000   # never 0.0.0.0 without a proxy
```

Behind a proxy or CDN, the per-IP limits are only as good as the client identity
behind them. `X-Forwarded-For` is client-writable, so it is *ignored* unless the
socket peer is a declared trusted proxy — honouring it unconditionally would let
one attacker mint a fresh rate-limit bucket per request:

```bash
AUTOPR_TRUSTED_PROXIES=127.0.0.1            # nginx on the same host
AUTOPR_TRUSTED_PROXIES=172.16.0.0/12        # a platform's egress range (CIDRs work)
```

Leave it empty for a direct-to-internet deployment: the socket peer *is* the
client, and the header is then correctly disregarded.

**Three settings decide whether a stranger can affect you:**

| Setting | Default | Set it when |
|---|---|---|
| `AUTOPR_API_TOKEN` | *required* (boot fails when empty) | always — it is the approval credential |
| `AUTOPR_REQUIRE_AUTH_FOR_READS` | `false` | the instance is reachable from the internet |
| `AUTOPR_ALLOWED_REPOS` | empty (allow all) | the bot is installed on a **public** repo |

The allowlist is a *cost* control, not an authentication control: forging a webhook
is impossible without the secret, but on a public repo a stranger opening a fork PR
produces a genuinely signed delivery — and therefore four LLM calls on your key.
With an allowlist, that delivery is answered `{"status":"ignored","reason":"repo_not_allowed"}`
before it touches the database, and the denial is logged with the repo name.

---

## 9. Public demo (read-only, for strangers)

Wanted: a URL anyone can open that shows real pipeline output without letting
them spend your money or touch a repository. One flag, three enforced
preconditions, no new endpoints:

```bash
AUTOPR_PUBLIC_MODE=true
AUTOPR_ALLOWED_REPOS=demo/repo        # else boot fails
AUTOPR_GITHUB_DRY_RUN=true            # else boot fails
AUTOPR_QUOTA_JOBS_PER_REPO_PER_HOUR=10 # else boot fails
AUTOPR_QUOTA_JOBS_TOTAL_PER_DAY=100    # else boot fails
AUTOPR_TRUSTED_PROXIES=127.0.0.1       # so limits are per visitor, not per proxy
```

| Behaviour | Private (default) | Public demo |
|---|---|---|
| `GET /jobs`, `/reviews/pending`, `/stats` | open | open — that is the demo |
| `POST /reviews/{id}/approve`, `/reject` | token required | **403 for everyone**, token included |
| GitHub writes | per `dry_run` | impossible (dry-run enforced) |
| New work from an unlisted repo | allowed | `ignored`, no row, no spend |
| New work past the quota | unlimited by default | `429` + `Retry-After` |

The 403 is server-side, so a `curl` is refused exactly like the hidden button, and
the dashboard hides the actions instead of offering a button whose only outcome is
an error. Seed it with `uv run python scripts/seed_demo.py` — publish synthetic
data, not repository history you would not want indexed.

This is a demo, not multi-tenancy: there is one identity for everyone (read), and
the quotas key on the only identities that exist — the repo and the instance.

---

## 10. Multi-tenant: serving strangers as a GitHub App

Single-operator mode has one credential. Multi-tenant mode has one *per customer*, minted on
demand, and that is what makes the difference between a bot you run and a bot people install.

### 10.1 Register the App (once, ~10 minutes)

| Field | Value |
|---|---|
| Webhook URL | `https://your-host/webhook` |
| Webhook secret | the same `AUTOPR_WEBHOOK_SECRET` |
| Permissions | Contents **read**, Pull requests **read**, Issues **write** (no Checks access: CI logs arrive inline in the webhook) |
| Events | `pull_request`, `check_run`, `workflow_run`, `issue_comment`, `installation`, `installation_repositories` |
| Where can it be installed | any account |

Download the private key once (GitHub never shows it again) and set:

```bash
AUTOPR_GITHUB_APP_ID=123456
AUTOPR_GITHUB_APP_PRIVATE_KEY="$(cat your-app.private-key.pem)"   # or one line with 

AUTOPR_GITHUB_APP_SLUG=your-app-slug
AUTOPR_QUOTA_JOBS_PER_INSTALLATION_PER_DAY=50
```

Then `GET /installations` should answer with `multi_tenant: true` and an `install_url`. The
worker needs these variables too — it is the process that posts comments.

### 10.2 What changes at runtime

| Before | After |
|---|---|
| one `AUTOPR_GITHUB_TOKEN` writes everywhere it can reach | no instance-wide repo credential; each write uses the owning installation's token |
| approval via the ops API with a bearer token | approval via `/autopr approve <id>` in the PR thread; ops API still works for the operator |
| `AUTOPR_ALLOWED_REPOS` decides which repos may create work | the installation decides; the allowlist remains available as an extra filter |
| one global budget | per-repo/hour, per-tenant/day, per-instance/day |

### 10.3 Isolation, precisely

- A decision is addressable only by the installation that created it **and** in the repo it
  belongs to. Ids are global; authority is not.
- A tenant that uninstalls or is suspended stops immediately: `suspend` pauses (reversible),
  `deleted` is terminal and cannot be revived by a replayed `created` event.
- If the App is not installed on a repo, the write is *refused*. There is no PAT fallback in App
  mode, by design: a fallback would be an instance-wide credential one bug away from use.

### 10.4 What the tenant sees

1. A low-risk review is posted automatically (dry-run permitting).
2. Anything code-changing, or above the risk ceiling, is **queued** — and the bot posts a notice
   on the PR with the decision id and the command to approve it. Without that notice a tenant
   could not discover the id, since they have no dashboard.
3. Nothing else appears until a maintainer acts. Silence is not a bug.

### 10.5 Comment commands

```
/autopr status           → what is waiting on this repo
/autopr approve <id>     → execute a queued action
/autopr reject <id>      → decline it (no GitHub write)
/autopr                  → help
```

Authority is `admin`/`maintain`/`write` as reported by
`GET /repos/{repo}/collaborators/{login}/permission` at the moment of the command. Nobody
else gets a reply, not even "no" — replying would let anyone enumerate decision ids.

### 10.6 Uninstalling

GitHub sends `installation: deleted`; the tenant row keeps its history but can no longer mint
tokens or have work processed. To remove a tenant's data as well:

```sql
DELETE FROM review_decisions WHERE installation_id = :id;
DELETE FROM job_results      WHERE job_id IN (SELECT id FROM pr_jobs WHERE installation_id = :id);
DELETE FROM pr_jobs          WHERE installation_id = :id;
DELETE FROM installations    WHERE installation_id = :id;
```

Order matters: children first. Do this deliberately — it is a GDPR-shaped action, not cleanup.

### 10.7 Rotating the App private key

Generate a new key in the App settings, deploy it, then delete the old one. Installation tokens
already minted stay valid for up to an hour; the App JWT is re-signed per burst, so a restart is
enough (`reset_app_auth()` is only needed in tests).

## 11. Going live on GitHub (leaving dry-run)

Dry-run is the default and the safe state. To let approvals post for real:

1. Set `AUTOPR_GITHUB_TOKEN` (a fine-grained PAT / app token with only the needed
   scopes) and `AUTOPR_GITHUB_DRY_RUN=false`.
2. Confirm `AUTOPR_API_TOKEN` is set — the process will not start without it, which
   is the point: an unauthenticated `/approve` is an anonymous approver.
3. Approvals now perform real writes. **Code-changing actions remain
   human-gated regardless** — leaving dry-run does not change the approval
   requirement, only whether an approved action actually reaches GitHub.

Verify on `/stats`: `config.github_dry_run` and `config.live_github` reflect the
live state the dashboard badges surface.
