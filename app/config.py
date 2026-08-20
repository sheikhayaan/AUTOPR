"""Application configuration.

Loaded from environment variables (prefix ``AUTOPR_``) and/or a local
``.env`` file via pydantic-settings. Import ``settings`` anywhere you need
config; it is constructed once at import time.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOPR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Security ---
    webhook_secret: str = "changeme-generate-a-real-secret"

    # --- Datastores ---
    # Default is SQLite so the app + tests run with zero infra. Compose
    # overrides this with a Postgres URL. The code paths are written to work
    # on both (see app.db for the SQLite/Postgres branch on ON CONFLICT).
    database_url: str = "sqlite+pysqlite:///./autopr.db"
    redis_url: str = "redis://localhost:6379/0"

    # --- Redis Streams ---
    stream_name: str = "autopr:jobs"
    consumer_group: str = "autopr-workers"

    # --- Worker behaviour ---
    max_attempts: int = 5
    # Entries idle longer than this (ms) in the pending list are considered
    # abandoned by a dead consumer and are reclaimed via XAUTOCLAIM.
    reclaim_idle_ms: int = 30_000

    # --- Later phases (declared now so config is complete) ---
    qdrant_url: str = "http://localhost:6333"
    groq_api_key: str = ""

    # --- Phase 3: sandboxed fix verification ---
    # The image the verifier runs patches inside. Built from
    # docker/sandbox.Dockerfile (python + git + ruff + mypy + pytest).
    sandbox_image: str = "autopr-sandbox:latest"
    # Path to the docker CLI. Empty => resolve from PATH, then fall back to the
    # Docker Desktop per-user install location (see runner._resolve_docker).
    docker_bin: str = ""
    # Hard ceiling on a single verification run. A fix that hangs is not a fix.
    sandbox_timeout_s: int = 120
    # Container resource caps (defense against a pathological/adversarial patch).
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "1.0"
    sandbox_pids_limit: int = 256
    # How many times the Fix Agent may re-attempt after a failed verification
    # before the run escalates to a human. 1 => one fix + one retry.
    max_fix_attempts: int = 2

    # --- Phase 4: risk-based routing / human-in-the-loop ---
    # GitHub REST base. Override for GitHub Enterprise. No trailing slash needed.
    github_api_url: str = "https://api.github.com"
    # Token for outward actions. Empty => the client runs in dry-run (records
    # intended actions, touches nothing). You must opt in to real posting.
    github_token: str = ""
    # Master safety switch: even with a token, dry_run=True keeps the system
    # from touching real repos. Default True — safe by default.
    github_dry_run: bool = True
    # Highest risk level the system will auto-comment on WITHOUT human approval.
    # trivial/low are safe (a comment changes no code); medium/high wait for a
    # human. Any code-changing action is always gated regardless of this.
    auto_comment_max_risk: str = "low"

    # --- Phase 5: CI-fix track (repo tree snapshot for the sandbox) ---
    # The fix verifier needs a snapshot of the repo to apply the patch to. We
    # fetch it as a single tarball and keep only decodable text files, bounded on
    # both axes so a huge or binary-heavy repo cannot blow up worker memory: a
    # file larger than the byte cap is skipped, and once the file cap is reached
    # we stop collecting. See routing.github.HttpGitHubReader.snapshot_repo.
    snapshot_max_file_bytes: int = 1_000_000
    snapshot_max_files: int = 2_000

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
