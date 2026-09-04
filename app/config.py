"""Application configuration.

Loaded from environment variables (prefix ``AUTOPR_``) and/or a local
``.env`` file via pydantic-settings. Import ``settings`` anywhere you need
config; it is constructed once at import time.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The shipped placeholder for the webhook secret. If this exact value survives to
# runtime, no real secret was configured — the model validator below refuses to
# start unless AUTOPR_ALLOW_INSECURE=1 (local/dev/tests).
_PLACEHOLDER_WEBHOOK_SECRET = "changeme-generate-a-real-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOPR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Security ---
    # GitHub webhook HMAC secret. Refuses to start at the placeholder value unless
    # allow_insecure is set (see _enforce_secure_secret).
    webhook_secret: str = _PLACEHOLDER_WEBHOOK_SECRET
    # Bearer token for the mutating/ops API (approve/reject). Empty => auth is a
    # no-op (dev convenience) with a loud startup warning; set it in any real
    # deployment. Compared in constant time (see app.security.verify_bearer_token).
    api_token: str = ""
    # When true, the read endpoints (/stats, /jobs, /reviews*) also require the
    # bearer token. Default false so the read-only dashboard demos without auth,
    # while the write path (approve/reject) is always protected once a token is set.
    require_auth_for_reads: bool = False
    # Comma-separated CORS allowlist. A literal "*" allows any origin (demo). The
    # API sends no cookies (allow_credentials=False), so this bounds which sites'
    # JS may call it rather than guarding a credential.
    cors_origins: str = (
        "http://localhost:5173,http://localhost:4173,http://localhost:3000,http://127.0.0.1:5173"
    )
    # Escape hatch for local/dev/tests: permits the placeholder webhook secret.
    # MUST be false (unset) in production — that is what makes the secret fail-fast.
    allow_insecure: bool = False
    # --- Rate limiting (in-process, per client IP, fixed 60s window) ---
    rate_limit_enabled: bool = True
    rate_limit_webhook_per_min: int = 120
    rate_limit_mutations_per_min: int = 30

    # --- Datastores ---
    # Default is SQLite so the app + tests run with zero infra. Compose
    # overrides this with a Postgres URL. The code paths are written to work
    # on both (see app.db for the SQLite/Postgres branch on ON CONFLICT).
    database_url: str = "sqlite+pysqlite:///./autopr.db"
    redis_url: str = "redis://localhost:6379/0"

    # --- Redis client robustness (Phase 8) ---
    # Bound every Redis call so a dead server or network blip surfaces as a
    # timeout we can convert to a 503, instead of hanging the request thread
    # indefinitely. socket_timeout must exceed the longest blocking read: the
    # worker's consume() blocks up to block_ms (2s in the worker loop, 5s
    # default), so 10s leaves headroom without wedging on a truly dead server.
    redis_socket_timeout_s: float = 10.0
    redis_connect_timeout_s: float = 5.0
    redis_health_check_interval_s: int = 30

    # --- Redis Streams ---
    stream_name: str = "autopr:jobs"
    consumer_group: str = "autopr-workers"

    # --- Observability (Phase 9) ---
    # JSON logs (one event per line) are the production default: greppable and
    # ready for a log pipeline. Flip to false locally for the colourised console
    # renderer. Both the API and the worker read this through configure_logging.
    log_json: bool = True
    # Expose GET /metrics (Prometheus text format). On by default; toggle off if
    # a deployment scrapes elsewhere or wants the endpoint closed.
    metrics_enabled: bool = True

    # --- Worker behaviour ---
    max_attempts: int = 5
    # Entries idle longer than this (ms) in the pending list are considered
    # abandoned by a dead consumer and are reclaimed via XAUTOCLAIM.
    reclaim_idle_ms: int = 30_000

    # --- Later phases (declared now so config is complete) ---
    qdrant_url: str = "http://localhost:6333"
    groq_api_key: str = ""
    # Groq model id for all agents. Empty => the code default (llm.DEFAULT_MODEL).
    # Overridable via AUTOPR_LLM_MODEL since Groq's catalog changes over time.
    llm_model: str = ""
    # Hard per-request ceiling on the Groq call (seconds). A hung LLM must not
    # wedge a worker; tenacity (see agents.common) still retries transient
    # failures, each attempt bounded by this timeout.
    llm_timeout_s: float = 30.0

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

    # --- Phase 13: hand-off mode (tokenless "route to people, they act") -----
    # When true, AutoPR NEVER writes to GitHub — not a comment, not a review,
    # not a push — even if a token happens to be set. It READS the PR (public
    # repos need no token: the reader omits the auth header; private repos still
    # supply a read token), the pipeline generates the review, and every
    # decision is routed to the human queue. Each queued item surfaces a deep
    # link to the PR's review screen, where a maintainer approves / requests
    # changes / edits under their OWN GitHub account. This is the model for
    # "send an approval request to people's own GitHub" without AutoPR holding
    # write credentials. Off by default; the token-based auto/dry-run paths are
    # unchanged when it is off. See docs/decisions/phase-13.md.
    handoff_mode: bool = False

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

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse cors_origins into a list; a literal '*' means allow-all."""
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return ["*"] if "*" in origins else origins

    @property
    def github_web_url(self) -> str:
        """The GitHub *web* origin, derived from the REST API base.

        The dashboard links to pull requests on the web UI, whose host differs
        from the API host: ``api.github.com`` → ``github.com``. For GitHub
        Enterprise the API lives under a path (``https://ghe.example.com/api/v3``)
        while the web UI is the bare host (``https://ghe.example.com``). Deriving
        it here keeps ``github_api_url`` the single source of truth, so the
        frontend never has to hard-code ``github.com``.
        """
        from urllib.parse import urlsplit

        parts = urlsplit(self.github_api_url)
        host = parts.netloc
        if host == "api.github.com":
            return "https://github.com"
        scheme = parts.scheme or "https"
        return f"{scheme}://{host}"

    @model_validator(mode="after")
    def _enforce_secure_secret(self) -> Settings:
        """Fail fast: refuse to start with the placeholder webhook secret.

        A service that boots with a well-known shared secret is not secure — any
        caller could forge webhook signatures. We turn that latent misconfig into
        an immediate, loud startup failure. allow_insecure is the deliberate,
        documented opt-out for local/dev/test runs.
        """
        if not self.allow_insecure and self.webhook_secret == _PLACEHOLDER_WEBHOOK_SECRET:
            raise ValueError(
                "AUTOPR_WEBHOOK_SECRET is still the placeholder. Generate a real "
                'secret (python -c "import secrets; print(secrets.token_hex(32))") '
                "and set it, or set AUTOPR_ALLOW_INSECURE=1 for local/dev/tests."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
