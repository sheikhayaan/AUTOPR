# Multi-stage build for the AutoPR application image (shared by the api and
# worker services). Two goals over the naive single-stage build:
#
#   1. Reproducible: install from the committed uv.lock with --frozen, and pin
#      the exact uv release that generated it — not `uv:latest`.
#   2. Slim runtime: build tools and the uv binary stay in the builder stage;
#      the final image carries only the resolved virtualenv and the app sources.
#
# It also copies alembic/ + alembic.ini (the release step runs `alembic upgrade
# head`) and README.md (pyproject's `readme` — the project build reads it), both
# of which the previous single-stage image omitted.

# ---- builder: resolve and install into /app/.venv from the lockfile ----------
FROM python:3.11-slim AS builder

# Use the base image's interpreter (never download a standalone one), so the
# venv's interpreter path is identical in the runtime stage below.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Pin uv to the version that produced uv.lock (see `uv --version`) for a
# byte-for-byte reproducible resolve — not the moving `:latest` tag.
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv

WORKDIR /app

# 1) Dependencies only, from the lockfile. This layer is cached until
#    pyproject.toml / uv.lock change, so app-code edits don't reinstall deps.
#    --no-dev excludes pytest/ruff/mypy; the runtime needs none of them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Project sources, then install the project itself (non-editable) into the
#    same venv. README.md is required because pyproject sets `readme`.
COPY README.md ./
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime: just the venv + the code needed to serve / migrate -------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Non-root: the API/worker never need root.
RUN useradd --create-home --uid 10001 appuser

# The resolved virtualenv from the builder (owned by appuser).
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# Application code + migrations + the demo seed. alembic/ and alembic.ini are
# required by the `alembic upgrade head` release step (Compose command / Fly
# release_command); the previous image shipped neither and would fail to migrate.
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser scripts ./scripts

USER appuser

EXPOSE 8000

# Default command is the API; the worker service/process overrides it. Note the
# CI-fix *sandbox* verification needs a Docker-capable host (docker CLI + socket)
# and is a local/Compose capability, not available in this lean cloud image — see
# docs/decisions/phase-12.md.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
