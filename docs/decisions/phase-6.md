# Phase 6 — Decisions Log

Turning a working tree into a **repository**. Phases 1–5 built and tested the
system; nothing yet made it reproducible, version-controlled, or continuously
verified. The single largest gap for a portfolio artifact was blunt: the working
tree was **not a git repository at all** — no history, no remote, no CI. This
phase closes that. It adds nothing to the runtime; it makes the existing runtime
*cloneable, installable byte-for-byte, and gated on every push*. Each entry states
what was chosen, the rejected alternative, and why.

This phase is deliberately right-sized for the project's stated scale (a showcase
serving ~1–50 requests, not a fleet). Where the earlier plan reached for
real-backend service containers and a full frontend test job in CI, this pass
defers those to the phase that actually earns them (Postgres to Phase 8, the
TypeScript frontend suite to Phase 10) rather than paying their weight before the
code under them exists. Those deferrals are listed in Corners, not hidden.

---

## 1. Version control from the first commit, and **commit the lockfile**

**Chosen.** `git init`, a `.gitignore` that excludes the live `.env`, the SQLite
DB (`*.db`/`*.sqlite`), `node_modules/`, build output, and coverage artifacts —
and then **un-ignore `uv.lock` so it is committed**. A verification step
(`git ls-files | grep -E '\.env$|\.db$'`) runs before the initial commit and must
return nothing.

**Why.** A portfolio artifact that cannot be cloned and installed to the exact
same dependency graph is not a production repo, it is a snapshot. The lockfile is
what makes `uv sync --frozen` deterministic: CI, a reviewer's laptop, and the
Docker image all resolve to the *same* 100 packages, not "whatever was latest that
day." Committing it is the difference between "works on my machine" and
"reproducible build." The `.env` exclusion is the hard safety invariant of the
whole project — the live Groq key lives only there and must never enter history —
so the check is a *gate*, not a comment: staged secrets are caught before the
commit, not after a push that can never truly be undone.

**Rejected — ignore the lockfile / ship a `requirements.txt`.** An unpinned repo
re-resolves on every install; a hand-maintained `requirements.txt` drifts from the
real closure and silently omits transitive pins. Both surrender reproducibility,
which is the one claim a lockfile exists to make. **Rejected — commit `.env.example`
only and rely on memory for exclusions.** Relying on a human to not `git add .` the
real `.env` is exactly the failure mode a `.gitignore` + pre-commit check exists to
remove.

---

## 2. `uv` is the single source of truth for dependencies **and** the CI toolchain

**Chosen.** Dependencies, the lock, dev tooling, and every CI command run through
`uv` (`uv lock`, `uv sync --frozen --extra dev`, `uv run --frozen --extra dev …`).
The build backend stays `hatchling`; `uv` owns resolution and execution.

**Why.** One tool that resolves, locks, installs, and runs collapses a class of
"the CI environment differs from mine" bugs: the `--frozen` flag makes CI *refuse
to run* if the lock and `pyproject.toml` have drifted, so a forgotten `uv lock`
fails loudly instead of resolving something new in CI. `uv` is also fast enough
(cold resolve here was ~1.7s) that there is no speed tax for the determinism.

**Rejected — pip + venv, or Poetry.** pip has no first-class lock; Poetry locks but
is slower and adds its own resolver semantics on top of PEP 621 metadata we already
write. Given the project already committed to `uv`'s `pyproject.toml`, adding a
second packaging tool would be two sources of truth, not one.

**Subtlety worth recording:** every CI step uses `uv run --frozen --extra dev`, not
a bare `uv run`. A bare `uv run` re-syncs the environment to the *default*
dependency group and would prune the dev tools (ruff, mypy, pytest) that a prior
`uv sync --extra dev` installed — the lint step would then fail with "command not
found." Pinning `--extra dev` on each invocation keeps the toolchain present and
`--frozen` keeps it from touching the lock. This is a real uv footgun; the flags
are load-bearing, not decoration.

---

## 3. `fastembed` is an **optional `rag` extra** with a lazy import, not a core dependency

**Chosen.** The RAG embedder (`fastembed`, which drags in `onnxruntime`,
`tokenizers`, `huggingface-hub`, `pillow` — a heavy ML stack) moved out of the
default dependency set into an opt-in `[project.optional-dependencies].rag`. The
import in `app/agents/rag.py` is guarded:

```python
try:
    from fastembed import TextEmbedding
except ImportError:
    TextEmbedding = None
```

and `RepoRAG.__init__` raises a clear, actionable error
(`"… install it with: uv sync --extra rag"`) if constructed without the extra.

**Why.** RAG is already *optional at runtime* — the worker builds the graph with
`rag=None` and the agents degrade cleanly to ungrounded prompts (Phases 2–3). It
was inconsistent for an optional-at-runtime feature to be a mandatory-at-install
dependency that forces a ~200 MB ONNX runtime into every environment and every
Docker layer, including CI and the API container that never embed anything. Making
it an extra means the default install and image stay lean (aligned with the
"not heavy, 1–50 req" scope), while `uv sync --extra rag` still gets the full
capability. The lazy import keeps `import app.agents.rag` working without the extra
so the module is importable (and its non-embedding logic testable) everywhere; the
existing `test_rag.py` monkeypatches `TextEmbedding` with a stub, so the suite
never needs the real dependency or a model download.

**Rejected — keep `fastembed` in core.** Forces the heavy stack on every install
for a feature most deployments run disabled. **Rejected — delete RAG / vendor a
lighter embedder.** RAG is a genuine capability of the system and a talking point;
removing it to dodge a dependency-weight problem that an extra already solves is
throwing away function. **Rejected — a module-level hard import guarded by a
feature flag.** A bare top-level `import fastembed` fails collection the moment the
package is pruned (exactly the failure this phase hit); the try/except is the
minimal fix that keeps the symbol module-level for the test's monkeypatch.

---

## 4. A coverage **floor of 78%** (measured 81.4%) — a real gate with cross-platform headroom

**Chosen.** CI runs `pytest --cov=app --cov-fail-under=78`. Measured line+branch
coverage is **81.4%** on the dev machine.

**Why.** A coverage gate is only meaningful if it *fails* when coverage rots, and
only *trustworthy* if it does not fail spuriously. The ~3-point gap between the
floor (78) and the measured value (81.4) is deliberate: coverage is measured on
Windows locally but enforced on Linux in CI, and a handful of lines are
platform-branched (e.g. the Windows `PROGRAMFILES` lookup in `sandbox/runner.py`),
so the Linux number can differ by a point or two. A floor set *at* the measured
value would risk a red badge on the very first push for a reason that has nothing
to do with code quality — the worst possible first impression for a portfolio repo.
78 is low enough to absorb that variance and high enough that a genuine regression
(deleting a tested module, shipping a large untested feature) trips it.

**Rejected — gate at 80 (or at the measured 81).** Too tight against
Windows↔Linux variance; trades a real safety margin for a rounder number.
**Rejected — no coverage gate.** Then coverage is a vanity metric that only ever
drifts down. **Rejected — chase 95%+.** The uncovered lines cluster at the
real-I/O boundaries (the Docker sandbox runner, the live GitHub HTTP client, the
worker's Redis loop) that are honestly tested by *integration*, not unit, tests —
and those need a daemon/network CI can't cheaply provide here. Padding those with
mock-heavy unit tests would raise the number while testing the mocks, not the code.
See Corner 1.

---

## 5. Lint and type strictness calibrated to catch bugs, not to fight the framework

**Chosen.** `ruff` (lint + format) with `select = [E, F, I, W, UP, B, C4, SIM]` and
exactly two ignores — `B008` (FastAPI's `Depends()` in a default *is* the idiom)
and `UP042` (the enums intentionally mix in `str` for SQLAlchemy storage; converting
to `StrEnum` changes `str()` semantics and risks ORM serialization). `mypy` with
`check_untyped_defs`, `no_implicit_optional`, `warn_redundant_casts`. Both gate CI.

**Why.** The point of the gate is to catch defects, and enabling it immediately did:
`mypy` surfaced a real latent bug — a tenacity `before_sleep` hook dereferenced
`retry_state.outcome` (an `Optional`) without a guard, which would have thrown
inside the retry path — and the `BaseMessage.content` handling (langchain types it
`str | list`) was silently assuming `str`, which would break on any provider that
returns content parts. Both are now fixed with a guard and a `message_text()`
helper. That is the gate paying for itself on day one. The two ignores are recorded
*with reasons* rather than blanket-disabled, so a reviewer sees judgment, not
laziness — each is a case where the rule is wrong *for this code*, not a rule we
couldn't be bothered to satisfy.

**Rejected — `mypy --strict`.** `--strict` turns on `disallow_any_generics`,
`disallow_untyped_calls`, etc., which fight the LangChain/LangGraph/redis-py
surfaces (many are `Any`-typed upstream) and would drown the real findings in
hundreds of third-party-shaped complaints. `ignore_missing_imports=true` plus the
targeted strictness flags catch our bugs without litigating theirs. **Rejected —
lint with default ruff rules only.** The `B`, `SIM`, `UP`, `C4` families are where
the value is (bug-bear checks, simplifications, modernizations); the `E`/`W`
defaults alone are just whitespace.

---

## 6. CI is three independent jobs; the image builds are **build-only**

**Chosen.** `.github/workflows/ci.yml` runs three parallel jobs on push/PR:
`backend` (ruff → mypy → pytest+coverage), `frontend` (`npm ci && npm run build`),
and `images` (`docker build` of the app image *and* the sandbox image). The image
job **builds but does not push**. `concurrency` cancels superseded runs on the same
ref; `permissions: contents: read` scopes the token minimally.

**Why.** Three jobs fail *independently and in parallel*, so a frontend break and a
type error surface in one run instead of serially, and the log points straight at
the culprit. Building both images in CI proves the `Dockerfile` and the hardened
`sandbox.Dockerfile` still build on every change — a Dockerfile only discovered to
be broken at deploy time is a classic production trap. Keeping the image job
*build-only* is the right call for this phase: publishing images needs a registry,
credentials, and tagging strategy that belong to the deploy phase, and a portfolio
CI should not push artifacts on every branch push. Least-privilege token +
cancel-in-progress are cheap correctness/cost wins.

**Rejected — one monolithic job.** Serializes unrelated checks and muddies which
one failed. **Rejected — push images from CI now.** Premature: no registry or
secret story yet, and it would publish an image per push. Deferred to Phase 12
(GHCR on tag). **Rejected — a `services: postgres + redis` job now.** See Corner 1.

---

## Corners cut (flagged, deferred)

1. **No real-backend (Postgres + Redis) integration job in CI yet.** The suite runs
   fully offline — SQLite (with the dual `ON CONFLICT` path unit-tested logically),
   `fakeredis`, and a mocked LLM. The earlier plan slated a `services:` job running
   the suite against real Postgres/Redis containers *in this phase*; it is deferred
   to **Phase 8**, where Alembic and the Postgres migration path are introduced —
   that is the phase where a real Postgres actually earns its place in CI. Deferring
   it here avoids paying container-spin-up weight to test a path no code exercises
   yet. Honest cost: the Postgres `ON CONFLICT` branch is verified by logic and
   local runs, not yet by a live Postgres in CI.

2. **Frontend CI is build-only (no lint/test).** The `frontend` job runs
   `npm ci && npm run build` — it proves the SPA compiles and bundles, but there is
   no ESLint/Prettier check and no test run yet, because the frontend is still
   JavaScript with no test suite. Both arrive in **Phase 10** (the TypeScript
   migration + Vitest/RTL), which then wires `tsc --noEmit`, lint, and `npm test`
   into this same job. Until then, a green frontend job means "it builds," not "it
   is verified."

3. **Image jobs build but never push or scan.** No registry publish and no image
   vulnerability scan (Trivy/Grype) run yet. Publishing to GHCR on tag is **Phase
   12**; a scan step is a natural add there. For now CI proves the images *build*,
   which is the property that breaks most often under dependency churn.

4. **The coverage floor is a whole-repo line/branch count.** It does not enforce
   per-module minimums, so a well-covered module could mask a poorly-covered one.
   The uncovered lines are concentrated (and knowingly so) at the Docker/Redis/HTTP
   I/O boundaries; a per-package coverage policy is a refinement, not a gap that
   changes the risk picture at this scale.

5. **Branch protection / required status checks are not configured in this phase.**
   That is a GitHub *repository-settings* action, and it is gated on the push
   actually happening under the operator's account (the one outward step this phase
   hands to the user rather than performing). Once the repo is pushed and CI has run
   green once, marking the `backend` job a required check on `main` is a two-click
   follow-up, noted here so it is not forgotten.
