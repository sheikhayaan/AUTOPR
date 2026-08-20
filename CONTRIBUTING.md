# Contributing to AutoPR

Thanks for taking a look. This document describes the local development loop and
the quality gates CI enforces, so you can reproduce a green build before pushing.

## Prerequisites

- **Python 3.11** (the LangGraph/LangChain/Qdrant stack is not yet reliable on 3.12+).
- **[uv](https://docs.astral.sh/uv/)** for dependency management and reproducible installs.
- **Node 20+** for the frontend SPA.
- **Docker** (only needed to run the full stack via Compose, or the fix-verification sandbox).

## Backend development loop

```bash
uv sync --extra dev          # create .venv from the committed uv.lock + dev tools
uv run pytest                # run the test suite (SQLite + fakeredis, fully offline)
uv run ruff check app tests  # lint
uv run ruff format app tests # auto-format
uv run mypy                  # type-check the app package
```

The suite is hermetic: it uses an in-memory SQLite database, a `fakeredis`
server, and a mocked LLM, so no network, Docker, or API keys are required.

## Quality gates (enforced in CI)

Every push and pull request runs `.github/workflows/ci.yml`:

1. **ruff** — lint (`ruff check`) and format check (`ruff format --check`).
2. **mypy** — static type-check of the `app` package.
3. **pytest + coverage** — the full suite with a coverage floor (`--cov-fail-under`).
4. **frontend** — `npm ci && npm run build`.
5. **images** — `docker build` of the app image and the sandbox image (build-only).

Run all of these locally before opening a PR; a green local run should mean a green CI run.

## Running the full stack

```bash
cp .env.example .env          # then fill in AUTOPR_WEBHOOK_SECRET, AUTOPR_GROQ_API_KEY, etc.
docker compose up --build     # postgres + redis + qdrant + api + worker
docker compose up --scale worker=3   # scale workers horizontally
```

## Conventions

- **Phased delivery.** Work lands in numbered phases; each phase adds a decision
  log under `docs/decisions/phase-N.md` recording the choice made, the alternative
  rejected, and any corners deliberately cut.
- **Safety first.** Any code-changing action is **dry-run by default** and always
  **human-gated** — the worker never writes to GitHub without an explicit approval
  through the control plane. Do not weaken this boundary.
- **Never commit secrets.** `.env` is gitignored and holds live API keys. Verify
  `git status` before committing; CI and reviewers will reject a staged `.env`.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected vs. actual behavior, and
relevant log lines (they are single-line JSON — include the `correlation_id`).
