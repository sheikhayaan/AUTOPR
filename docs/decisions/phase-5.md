# Phase 5 — Decisions Log

Closing the CI-fix integration seam. The CI-failure *graph track*
(`ci_monitor → fix_agent → fix_verifier → router`) and its routing were already
built and unit-tested in Phases 3–4, but nothing produced its input: no webhook
reached it, and the verifier had no repo tree to apply a patch to. This phase is
the last-mile plumbing that makes a real `check_run`/`workflow_run` failure flow
end-to-end — parse the webhook, carry the failure evidence, snapshot the repo,
drive the track, and land a **sandbox-verified fix queued for a human**. It is
deliberately thin: the interesting, safety-critical logic already exists and is
tested; this phase wires it and proves the wiring. Each entry states what was
chosen, the rejected alternative, and why.

The prior seam (PR-review track) was closed in `phase-4.md`'s "Integration seam
closed" section; this is its CI-track counterpart, and it reuses every boundary
that section established (the GitHub read boundary, the `session_factory`, the
ReviewDecision idempotency spine).

---

## 1. Act only on a *completed, failed, PR-associated* CI run

**Chosen.** `parse_ci_event` returns a job (rather than a 200-ack no-op) only when
**all** of these hold: `action == "completed"`, the `conclusion` is one of
`{failure, timed_out, action_required}`, there is a head SHA, **and** the run is
associated with at least one pull request. Everything else — `success`,
`neutral`, `cancelled`, `skipped`, an in-progress run, a failure with no PR — is
ignored.

**Why.** Two independent reasons converge on "must have a PR." First, the whole
outward action of this track is a **PR comment** (a proposed fix or an
escalation); a failing check with no PR has nowhere to land, so acting on it would
be dead work that can only error. Second, it bounds blast radius: the system only
ever speaks on changes a human already opened for review. The conclusion filter is
the same "fail closed" instinct as the rest of the system — we act on a *narrow
allowlist* of failure conclusions, not a denylist, so a novel conclusion string
GitHub adds later is ignored rather than misinterpreted as a failure to fix.

**Rejected — act on any non-success conclusion.** A denylist (`!= "success"`)
would sweep in `neutral`, `skipped`, `stale`, and `cancelled` — none of which is a
failure a fix could address — and would fire the whole expensive track (snapshot,
LLM diagnose, sandbox) on runs that were merely skipped. Conservative allowlisting
keeps the track's cost aligned with genuine failures.

---

## 2. Carry the failure logs inline on the job row (`event_context`), not via a second fetch

**Chosen.** A new nullable `PRJob.event_context` TEXT column holds the failure
evidence extracted from the webhook payload at ingest time (`check_run.output`'s
title/summary/text; `workflow_run`'s name/conclusion/commit message). The worker
reads it straight off the row and threads it into the graph as `ci_logs`. It is
`NULL` for PR-review jobs.

**Why.** GitHub already includes a usable diagnosis surface *in the webhook
payload* for `check_run` (the `output` block is where Actions/most checks write
the failing assertion, the ruff violation, the mypy error). Persisting that at
ingest means the common case needs **zero** extra API round-trips to diagnose, and
it keeps the evidence attached to the durable job row — so a redelivery or a
crash-reconcile re-drives the exact same logs, not whatever the API happens to
return later (logs on GitHub can rotate/expire). Reusing the existing `PRJob` row
also means no new table, no join, and the idempotency/reconcile machinery covers
it for free.

**Rejected — a second GitHub API call in the worker to fetch logs.** More network,
more failure surface, and non-deterministic across redeliveries (the fetched log
could differ from the one that triggered the job). **Rejected — a separate
`ci_evidence` table.** A one-to-one satellite table for a single nullable text
blob is ceremony with no payoff; a column on the row it belongs to is simpler and
keeps the read a single `SELECT`.

**Corner (honest cost):** adding a column to an existing table with no migration
tool means the dev SQLite DB must be recreated (`create_all` won't ALTER an
existing table). Alembic is still deferred (carried from `phase-1.md`); for dev we
delete + reseed. Documented, not hidden — see Corners.

---

## 3. Snapshot the repo via the tarball API (one call), not the git-tree + blobs API (N+1)

**Chosen.** `HttpGitHubReader.snapshot_repo(repo, ref)` fetches the whole tree at
the head SHA with a single `GET /repos/{repo}/tarball/{ref}` (which 302-redirects
to a signed codeload URL we follow), then a **pure** module-level helper
`_extract_repo_tarball` decodes it: strip GitHub's `<owner>-<repo>-<sha>/` top
directory, keep only UTF-8-decodable regular files, and apply two caps
(`snapshot_max_file_bytes`, `snapshot_max_files`).

**Why.** The fix verifier needs the **whole** tree, not just the PR's changed
files (decision #4), so we are fetching potentially hundreds of files. The tarball
is *one* request; the alternative (`GET /git/trees/{sha}?recursive=1` then a
`GET .../blobs/{sha}` per file) is one request **per file** — an N+1 that burns
rate limit and latency linearly in repo size. Factoring the decode into a pure,
network-free function is what makes the fiddly parts — the path rewrite, the
binary skip, the two caps — unit-testable from an in-memory `tar.gz` with no
GitHub and no token (`test_github_reader.py`). The caps are defense against a
pathological or binary-heavy repo blowing up worker memory; the byte cap skips
oversize files, the count cap stops collection, and the caller logs the resulting
count so truncation is observable.

**Rejected — git-tree + per-blob fetch.** N+1 API calls, dramatically worse on any
real repo, for no correctness gain. **Rejected — `git clone` in the worker.**
Needs git on the worker host, network egress, and disk, and it fetches history we
never use; the tarball is exactly the working tree at one ref, which is all the
sandbox applies a patch to. **Rejected — snapshot only the changed files.** Would
make `pytest` verification meaningless (see #4).

---

## 4. The snapshot is the *whole tree*, because `pytest`/import checks need it

**Chosen.** The snapshot fed to the sandbox is the entire repo at the head SHA, not
the PR's changed files. `changed_files` is still fetched and passed — the fix agent
scopes its patch to it and the verifier's command-picker uses it to scope
`ruff`/`mypy` — but the *files materialized in the sandbox* are the whole tree.

**Why.** The verification policy (`sandbox/policy.py`) re-runs `pytest` for a
`test` failure and `pytest --collect-only` for an `import` failure. Both import the
whole module graph: a patch that greens the target test but breaks another module,
or fixes one import while leaving a downstream one dangling, must be caught. With
only the changed files present, `pytest` couldn't even import them (their
dependencies would be missing), so every test/import verification would spuriously
fail. Whole-tree is not gold-plating here; it is the minimum for the check to mean
anything. This is the concrete resolution of `phase-3.md` corner #1 ("the repo
snapshot has no live producer yet").

**Rejected — changed-files-only snapshot.** Smaller and cheaper, but it makes the
two most valuable failure types (`test`, `import`) unverifiable — the sandbox would
report `check_failed` on correct patches because imports resolve against a partial
tree. A cheaper snapshot that produces wrong verdicts is worse than no snapshot.

---

## 5. The worker branches on `job.event`; the graph's own entry router picks the track

**Chosen.** `make_graph_handler` inspects `job.event`: for `check_run`/`workflow_run`
it assembles the CI state (`ci_event`, `ci_logs` from `event_context`,
`repo_snapshot` from `reader.snapshot_repo`) in addition to `changed_files`; for
anything else it assembles the PR-review state. It then calls `graph.invoke(state)`
once — the graph's existing `_entry_router` reads the `ci_event`/`ci_logs` keys and
dispatches to `ci_monitor` vs `code_reviewer`. The worker never calls a node
directly.

**Why.** Two layers, each with one job. The worker's job is *I/O assembly* (turn a
durable row into graph input, doing the reads); the graph's job is *control flow*
(which track, which nodes, the bounded retry loop). Keeping the track-selection
predicate in the graph (where it was already written and tested,
`test_graph_routing.py`) means the worker doesn't duplicate routing logic that
could drift out of sync — the worker only has to set the right keys. The two
`_CI_EVENTS` (`check_run`, `workflow_run`) are the single source of truth shared
with `ingest.parse_event`, which only ever writes those two to a CI job's `event`
column.

**Lock-safety (unchanged from the PR seam, restated).** `process_job` commits the
`PROCESSING` status *before* invoking the handler, so the worker's outer session
holds no write lock while the router opens its *separate* `session_factory` session
to persist the ReviewDecision. No SQLite-file self-contention, and the HITL write
commits independently of the job-ledger transaction.

---

## Corners cut (flagged, deferred)

1. **`workflow_run` carries no step logs.** Unlike `check_run.output`, a
   `workflow_run` payload has no failing-assertion text — only the workflow name,
   conclusion, and commit message. So a `workflow_run`-triggered job often gives
   the CI Monitor too little to classify and it returns `unknown`, which routes
   straight to **escalate** (a human looks). That is the *honest* outcome, not a
   bug: we don't guess a failure type from a commit message. `check_run` (the
   richer, more common signal for PR checks) is the primary path.

2. **We never download the full zipped Actions log archive.** GitHub offers
   `GET /repos/{repo}/actions/runs/{id}/logs` (a redirect to a zip of every step's
   log). Fetching + unzipping + selecting the relevant step is a meaningful chunk
   of work and an extra authenticated call; we rely on the inline `output` block
   instead. This bounds diagnosis quality for checks that put their failure detail
   only in the archived logs. Deferred, not forgotten — it is the obvious next
   upgrade to CI-Monitor accuracy.

3. **Snapshot caps can silently truncate a large repo.** A repo exceeding
   `snapshot_max_files` (2000) or with files over `snapshot_max_file_bytes` (1 MB)
   is partially snapshotted. We *log the resulting file count* so truncation is
   observable, but a truncated tree could make `pytest` fail to import a dropped
   module and yield a spurious `check_failed` → escalate. Mitigation: the caps are
   generous for typical service repos, and the failure mode is "escalate to human,"
   never "wrong fix applied." Raising/removing the caps is a config change.

4. **The sandbox image must already contain the repo's dependencies.** Carried from
   `phase-3.md` corner #3: the sandbox runs `--network none`, so `pytest`/`import`
   verification only succeeds if the repo's third-party deps are baked into
   `autopr-sandbox:latest`. A repo needing an uninstalled package surfaces as an
   `ImportError` inside the sandbox → `check_failed` → escalate. Correct (we never
   claim a fix works when we couldn't actually run it) but limits which repos
   verify green out of the box. A per-repo dependency-install step (still offline,
   from a vendored wheel cache) is the real fix and is deferred.

5. **No live smoke test against real GitHub.** The whole track is proven offline —
   `test_ingest_ci.py` (parsing), `test_github_reader.py` (tarball decode via an
   in-memory `tar.gz`), and `test_worker_graph.py` (the vertical slice: seeded
   `check_run` job → graph → `FakeSandbox` verdict → `propose_fix` queued /
   `escalate`, with the LLMs mocked). What is *not* exercised in CI: a real
   `check_run` webhook signature, the real tarball redirect + bytes, and the real
   Docker sandbox (its two tests skip without a daemon). A manual live smoke
   (real token, a real failing PR check) is the same deferred step flagged for the
   PR seam.

6. **A failing check on a branch with no PR is ignored** (decision #1). This is
   intentional, but it means CI failures on the default branch or on a
   pushed-but-unopened branch produce no action. The system is a *PR* assistant by
   design; branch-level CI triage is out of scope.
