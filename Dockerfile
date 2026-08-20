# AutoPR application image (shared by the api and worker services).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first (layer cache) then the app.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

COPY app ./app

# Non-root user — the API/worker never need root.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Default command is the API; the worker service overrides it in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
