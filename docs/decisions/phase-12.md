# Phase 12 — Decisions Log

The last phase. Everything before it made the system correct, safe, observable,
and legible; this one makes it **shippable** — a reproducible image, a registry
to hold it, a place to run it, and a pipeline that gets it there without a human
copying artifacts around. The scope refinement holds: this is a 1–50-request
showcase, so "shippable" means *a real, defensible deploy path*, not a
multi-region autoscaled platform.

One honesty note sets the tone for the whole phase: the CI-fix track's sandbox
verifier needs a Docker-capable host (it runs an untrusted patch in a locked-down
container). A shared cloud VM is exactly where you do **not** get a Docker socket.
So the cloud image is deliberately lean and the sandbox stays a local/Compose
capability — documented, not hidden. The PR-review track, which needs no sandbox,
runs fully in the cloud.

---

## 1. **Multi-stage image installed from the lockfile**, not a single-stage `pip install .`

**Chosen.** A `builder` stage resolves dependencies with `uv sync --frozen` from
the committed `uv.lock`, installs the project non-editable into `/app/.venv`, and
the `runtime` stage copies just that venv plus the code needed to serve and
migrate (`app/`, `alembic/`, `alembic.ini`, `scripts/`). uv is pinned to the
exact version that produced the lock (`uv:0.11.15`), not `:latest`. The runtime
stage carries no build tools and no uv binary, and runs as a non-root uid.

**Why.** The image is the artifact the deploy actually runs, so its two
properties that matter are *reproducibility* and *attack surface*. `--frozen`
against the committed lock means the image resolves to the exact versions CI
tested — the same guarantee Phase 6 bought by committing `uv.lock`, now extended
to the container. Pinning uv itself closes the last moving part (a resolver
change in a future uv could produce a different tree from the same lock). Keeping
build tooling out of the runtime stage and dropping to non-root shrinks what an
attacker who lands in the container can use.

This also fixes three concrete bugs in the previous single-stage Dockerfile that
would have failed the moment anyone built it: it installed from `pyproject.toml`
(unpinned, not the lock); it never copied `alembic/`, so the container's
`alembic upgrade head` release step had nothing to run; and it ran the install
*before* copying `README.md`, which the project build reads via pyproject's
`readme` — the build would have errored.

**Rejected — single-stage `uv pip install .`.** Simpler to read, but ships the
compiler toolchain and uv in the running image and installs unpinned. The
reproducibility and surface wins are worth one extra stage.
**Rejected — `uv:latest`.** Convenient until a uv release changes resolution or
CLI behaviour and a rebuild of an old tag silently differs. A pinned toolchain is
the whole point of a reproducible build.

---

## 2. **Publish to GHCR on a version tag**, built once in CI

**Chosen.** `.github/workflows/release.yml` triggers on `v*` tags, builds the
image once, and pushes it to GitHub Container Registry as both `:<tag>` and
`:latest`, using the workflow's own `GITHUB_TOKEN` (no external registry
credential to manage) and GitHub Actions layer caching.

**Why.** The registry should live next to the code and its identity, and for a
GitHub-hosted repo that is GHCR — auth is the built-in `GITHUB_TOKEN` scoped by
the workflow's `packages: write` permission, so there is no long-lived Docker Hub
password to store or rotate. Tag-triggered (not every push to main) means an
image exists for, and only for, a deliberate release, and its tag *is* the git
tag — the image is traceable to an exact commit. Building once in CI and
deploying that same digest (next decision) removes the "works in CI, differs in
prod" gap that build-on-the-target reintroduces.

**Rejected — Docker Hub.** A second account and a stored credential for no
benefit when the code already lives on GitHub. **Rejected — build on the deploy
host / `fly deploy` building from source.** Rebuilding at deploy time means the
thing you ship isn't the thing CI built and cached; publishing an immutable
tagged image and deploying *that* is the stronger chain.

---

## 3. **Fly.io, with migrations as a release step and secrets in the platform store**

**Chosen.** `fly.toml` defines two process groups off the one image — `app`
(uvicorn, the only HTTP surface, health-checked on `/readyz`) and `worker`
(`python -m app.worker`) — with `release_command = "alembic upgrade head"` so
migrations run once per release in a throwaway machine *before* the new version
takes traffic. All secrets and connection strings come from `fly secrets`
(injected into app, worker, and the release machine); none are in the image or
the manifest. Request concurrency is capped at 40/50 to match the stated scale.

**Why.** Fly runs OCI images directly with a managed Postgres and Upstash Redis a
command away, and its secret store keeps `AUTOPR_WEBHOOK_SECRET`,
`AUTOPR_API_TOKEN`, and `AUTOPR_GROQ_API_KEY` out of both the image and the repo —
the same invariant every earlier phase protected, now enforced by the platform.
`release_command` is the correct home for migrations: it runs exactly once per
deploy regardless of how many machines start, so there's no concurrent-`upgrade`
race, and a failed migration aborts the release before any new machine serves —
strictly better than the Compose pattern of one designated service migrating on
start. Two process groups off one image mirror the Compose topology (api +
worker) without a second artifact to build or version.

**Rejected — bake secrets / connection strings into the image or `fly.toml`.**
Violates the project's hard invariant and makes the image itself a secret.
**Rejected — Kubernetes.** A control plane, ingress, and manifests to defend for
a two-process, single-operator showcase; the operational surface dwarfs the app.
**Rejected — migrate on container start (the Compose pattern) in the cloud.**
Works, but couples migration to boot and needs a "who migrates" rule when
machines scale; `release_command` is purpose-built for exactly this and fails the
release safely.

---

## 4. **CD is tag-triggered and gated on a repo variable**, deploying the built digest

**Chosen.** The same workflow's `deploy` job runs only when the `FLY_APP_NAME`
repository variable is set, `flyctl deploy --image <the GHCR image just pushed>`,
then polls `/readyz` on the public URL as a post-deploy smoke test and fails the
run if it never returns 200.

**Why.** A portfolio repo is cloned and forked; CD that hard-fails on a fork with
no Fly account would paint every fork's Actions red. Gating the deploy job on a
repo *variable* (allowed in a job `if:`) means the build/publish half always runs
and the deploy half is opt-in — present and correct for the owner, invisibly
skipped for everyone else. Deploying the *image already built and pushed* (rather
than rebuilding) keeps the CI→registry→prod chain on one digest. The `/readyz`
smoke test closes the loop: "deployed" should mean "answered a real
deep-readiness probe on the public URL," not merely "flyctl exited 0."

**Rejected — deploy on every push to `main`.** Too eager for an outward-facing
action; releases should be deliberate and tagged. **Rejected — an always-on
deploy job with no gate.** Red CI on every fork, and a confusing failure for
anyone who just wants to build the image.

---

## Corners cut (flagged, deferred)

1. **The CI-fix sandbox does not run in the cloud.** Verifying an untrusted patch
   needs a Docker socket, which a shared cloud VM shouldn't and doesn't give you.
   The cloud image is intentionally lean (no docker CLI); the sandbox verifier is
   a local/Compose capability. The PR-review track runs fully in the cloud; the
   CI-fix track's *verification* step is where you'd add a dedicated
   Docker-capable runner (or Fly Machines API spawning a sibling VM) as the
   scale-out path. Called out here and in `docs/operations.md` rather than
   silently degraded.

2. **Single region, single small VM per group, rolling deploy only.** No
   multi-region, no blue-green/canary, no autoscaling. `min_machines_running = 1`
   with `auto_stop`/`auto_start` is right for a demo's traffic and cost; the
   concurrency cap (40/50) matches the stated scale. The path up is
   `fly scale count`/`fly regions add`, deliberately not taken.

3. **No image signing, SBOM, or vulnerability-scan gate.** The release publishes
   an unsigned image with no `cosign` attestation and no `trivy`/`grype` gate. For
   a portfolio artifact on a trusted base image this is proportionate; supply-chain
   attestation is the obvious hardening step and is noted, not done.

4. **The smoke test is a single readiness probe, and rollback is manual.** A green
   `/readyz` proves the app booted, migrated, and reached its dependencies — not
   that a full webhook→approve transaction works. And a smoke failure fails the CI
   run but does not auto-roll-back; recovery is `fly releases` / `fly deploy` to
   the prior tag by hand. Automatic rollback and a synthetic end-to-end
   transaction are deferred as the next reliability increment.
