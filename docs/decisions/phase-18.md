# Phase 18 — Decisions Log

**Stop reasoning about the process boundary. Cross it and measure.**

Phases 16–17 made the system multi-tenant and measurably faster, and left three
gaps that were openly labelled rather than closed: the claim was proven only
between *threads*, the migration cycle was proven only on SQLite and in a CI job
that merely checked exit codes, and no real GitHub App installation had ever been
performed. This phase attacks all three with the same method: replace the
inference with a run, and where a run is impossible, replace the silence with a
one-command verifier and say so in one sentence.

The through-line: **a guarantee is only as wide as the boundary you crossed to
test it.** Threads share an address space and a transaction; processes share
neither. SQLite serialises writers, so a non-atomic claim passes there almost by
accident. Two container replicas against one Postgres is the deployed shape, so
that is the shape the test has to take.

---

## 1. Cross-process claiming, measured on a real server

`tests/test_worker_multiprocess.py` — 7 tests, real Postgres, real operating
system processes (`spawn`, not `fork`, so no child inherits the parent's sessions
or sockets):

| Test | What it pins |
|---|---|
| 8 processes race one job | the handler runs **once**; 7 processes log `claim_lost` and ACK; `attempts == 1` |
| 4 processes × 8 jobs | every job handled exactly once, work spread over several PIDs, nothing lost |
| failure in process A | the row comes back as `QUEUED` **visible to process B**, which finishes it (`attempts == 2`) |
| stale lease | a job left `PROCESSING` past the window is reclaimed by a different PID |
| **live lease is not stolen** | the negative control: inside the window, `PROCESSING` is not claimable |
| contention 2 and 4 | the same property at two levels, so it is not a lucky scheduling artefact |

The negative control in that list is not decoration. Without it, "the stale lease
was reclaimed" would also pass for a claim that ignores `updated_at` entirely —
such a claim steals live work *and* reclaims dead work, and both tests would be
green. The pair is what makes either meaningful.

A start barrier (`multiprocessing.Barrier`) forces the claims to overlap; without
it, process start-up jitter serialises the contenders and the race becomes
theatre. The handler sleeps 250 ms and records itself in a dedicated
`xproc_runs` table with a serial primary key, so a double run is two rows rather
than a timing coincidence.

### Two harness bugs that looked exactly like product bugs

Both were in the *test*, both were found by the test failing, and both are worth
recording because each would have produced a plausible but false report.

1. **`OnConflictDoNothing object has no attribute constraint_target`.** Swapping
   only the *connection* to Postgres is not enough: `insert_ignore_duplicates`
   branches on `settings.is_sqlite` to pick a dialect, so the harness compiled
   SQLite's `ON CONFLICT` and sent it to a Postgres server. The fixture now points
   `settings.database_url` (and `AUTOPR_DATABASE_URL`, for the spawned children)
   at the server and **asserts `not settings.is_sqlite`** before yielding, so this
   cannot silently regress into a test that is not testing the Postgres path.
2. **`InstallationToken.expires_at` is a monotonic deadline, not a wall clock.**
   The new verifier compared it with `time.time()` and would have reported every
   freshly minted token as *expired* — a confident FAIL on a perfectly healthy
   installation. Caught by reading `fetch_installation_token` while writing the
   fake, and now pinned by `test_an_expired_token_fails` plus a passing healthy
   case.

Neither is a product defect, and neither is presented as one. They are the reason
the Postgres tier asserts on its own preconditions.

## 2. The migration cycle, asserted rather than commanded

CI already ran `upgrade → downgrade → upgrade` and checked that each command
exited 0. That is weaker than it looks: a `downgrade()` that drops nothing also
exits 0. `tests/test_migrations_postgres.py` adds three assertions on the *state*
rather than the exit code, against a throwaway database per test:

- after `downgrade base`, **no tables remain** (only `alembic_version` is ignored),
  which is the only real evidence that every `downgrade()` does its job;
- the covering index the quota counter depends on exists as a Postgres index, not
  just as a declaration in the model;
- `upgrade head` twice in a row is a no-op — deployment scripts run it on every
  boot;
- `compare_metadata(context, Base.metadata)` reports no **structural** drift, which
  is the classic "column added to `models.py`, migration forgotten" bug, checked
  with Postgres type mapping rather than SQLite's permissive one.

Verified locally against a real PostgreSQL 16.2 server (via the `pgserver`
package, which ships actual binaries — no Docker, no emulation) and in CI against
`postgres:16`.

## 3. The installation gap, closed as far as it can be from here

Registering a GitHub App requires an authenticated browser session against
`github.com/settings/apps/new`; there is no API for it and the App Manifest flow
still needs a browser to POST the form. So this repository genuinely cannot
perform a first registration. Three things were done instead of nothing:

1. **The part that *is* verifiable from here was verified.** `tests/test_github_live.py`
   was run against the real API with a real token — 3 passed: `pulls/{n}/files`
   normalisation, the `snapshot_repo` tarball redirect, and the 401 path for a bad
   token. That is the read contract GitHub actually enforces today.
2. **The App chain became one command.** `scripts/verify_installation.py` runs
   private key → JWT (`RS256`, `iss`, 600 s cap, verified locally with the public
   key) → `GET /app` → installation list → installation token (`ghs_`,
   unexpired) → `/installation/repositories` → `repo → installation` →
   collaborator permission → optional comment. Each step is PASS/FAIL with the
   evidence in the detail column.
3. **The verifier cannot lie in the easy direction.** 16 tests pin every failure
   mode: a rejected JWT, zero installations, an installation id that is not
   installed, a token that is a `ghp_` PAT instead of a `ghs_` installation token
   (the exact multi-tenant leak App mode exists to prevent), an expired token, a
   repo outside the installation, a repo mapped to a different installation, a
   refused comment, and missing configuration. A comment that was not confirmed is
   a **SKIP**, never a PASS; webhook delivery is **MANUAL**, because only GitHub
   can trigger it — and `Report.failed` treats only FAIL as failure, so MANUAL can
   never be mistaken for verified.

`github-live.yml` runs the verifier weekly once App secrets exist, and — like the
live contract job — **warns that the path is unverified** rather than going green
when they do not. That is the honest shape: an unverifiable claim is a warning, a
skipped claim in a job that has the credentials is an error.

**Residual, stated plainly:** the author has not driven a real installation, so
`docs/install.md` ends with a single command whose success is the user's proof,
not the author's assertion.

## 4. Decisions taken

| Decision | Why |
|---|---|
| Postgres tier is **optional locally, mandatory in CI** | `postgres_url` skips without a server or `pgserver`, so the hermetic suite stays offline; the CI job greps its own pytest output and **fails on `skipped`**, because there a skip means the fixture never saw the URL |
| `pgserver` rather than Docker for developers | real PostgreSQL binaries, no daemon, no container runtime, ~40 MB on demand; `AUTOPR_TEST_POSTGRES_URL` remains the CI path |
| A separate database for the concurrency tier in CI | those tests `DROP`/`TRUNCATE`; the migrated database from the alembic steps must not be disturbed |
| No new runtime dependency | everything added is test- or tooling-side; `psycopg[binary]` was already required |
| The verifier lives in `scripts/`, not `app/` | it needs real credentials and a human to read the output; it is an operator tool, not a code path |

## 5. The delivery leg, exercised by GitHub itself

Registration needs a browser, so this phase cannot create an App. Nothing stopped it from
putting the *receiving half* in front of real signed traffic, which is the half that runs on
every delivery and the half a mistake in is expensive.

Method: a running instance (`uvicorn`, SQLite, real Redis 8.10.2, a 64-hex webhook secret),
exposed through a disposable public tunnel (`ssh -R 80:127.0.0.1:8113 nokey@localhost.run`);
a repository webhook created with `POST /repos/sheikhayaan/AUTOPR/hooks` (201,
`events=["pull_request"]`, `content_type=json`); verdicts read from **GitHub's delivery log**
(`GET /repos/.../hooks/{id}/deliveries`), not from our own logs alone.

| delivery | status we returned | evidence |
|---|---|---|
| `ping`, correct secret | `200` in 0.79 ms | GitHub delivery log |
| `ping`, deliberately wrong secret | **`401`** | GitHub delivery log + `webhook.invalid_signature gh_event=ping` in our log |
| `pull_request` (PR #1, `e07f22c`) | `200` in 1.71 ms | `webhook.ingested created=True job_id=1 pr=1 sha=e07f22c6`; `XLEN autopr:jobs` = 1 with that job's fields |

The wrong-secret row is the part that matters: it is a negative control **signed by GitHub**, and
it means the `200` above is not "the endpoint answers everything" but "the endpoint checks the
HMAC and answers everything it can prove is GitHub".

The first attempt returned `503`. Redis was down, `get_queue` fails closed, and the delivery was
refused instead of being acknowledged into a queue that did not exist. That is the behaviour the
code documents; it was confirmed by accident, which is the most convincing way.

**What this does not prove.** These are *repository* webhook deliveries. Their payloads carry no
`installation` object, so `upsert`/`get_active`, the installation-token mint, and per-tenant
attribution were not exercised by this traffic; `installation_id` being `None` also means the
delivery took the "registration must never depend on the installation event" branch. An App
installation remains unperformed, and the honest reason is not reluctance: creating a GitHub App
has no REST endpoint — the manifest flow requires a logged-in human in a browser — so no script
in this repository can do it. `scripts/verify_installation.py` is what a human with a browser can
run in one command afterwards.

Cleanup, since a proof that leaves residue is not a proof: hook deleted (204, `GET /hooks` → 0),
PR #1 closed and its branch deleted (`GET /branches` → `main` only, 0 open PRs), tunnel, uvicorn
and Redis processes stopped.

## 5. What this phase does not do

- It does not prove the >15-minute-job lease window; the reclaim still *can* run
  a genuinely slow job twice (double spend, never double side effects). Only a
  real slow provider would exercise that.
- It does not measure a real model provider under load. The worker benchmark
  still substitutes a `sleep` for inference, so it measures scheduling.
- It does not make the Postgres tier a gate on a laptop: without a server those 7
  tests skip, and the README says so instead of implying they ran.
- It does not perform a real App installation. The delivery leg is now proven against GitHub's
  own signed traffic (§5); registration and installation-token minting are not, and cannot be
  from here.

Gate state at the end of this phase: **478 passed, 5 skipped** (483 collected) —
of which 10 are the new Postgres tier — **88%** coverage against a 78% gate,
ruff/format/mypy clean, 17 docs guards, figures regenerated, frontend 63 tests
with tsc/eslint/prettier clean.
