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
    # allow_insecure is set (see _enforce_secure_config).
    webhook_secret: str = _PLACEHOLDER_WEBHOOK_SECRET
    # Bearer token for the mutating/ops API (approve/reject). Also fails fast when
    # empty unless allow_insecure is set: an unauthenticated /approve is not a
    # convenience, it is "anyone who can reach the API may write to your repo".
    # Compared in constant time (see app.security.verify_bearer_token).
    api_token: str = ""
    # When true, the read endpoints (/stats, /jobs, /reviews*) also require the
    # bearer token. Default false so the read-only dashboard demos without auth,
    # while the write path (approve/reject) is always protected once a token is set.
    # Set it to true on any instance reachable from the internet.
    require_auth_for_reads: bool = False
    # Comma-separated repo allowlist ("owner/name"). Empty = every repo that can
    # deliver a signed webhook may create work. Set it when the bot is installed
    # on a repo strangers can open PRs on: a fork PR there is a genuine signed
    # delivery, so without this the allowlist-less bot spends LLM budget on
    # anyone's diff.
    allowed_repos: str = ""
    # --- Public-instance posture -------------------------------------------
    # "Anyone on the internet may use this instance, but only to look." One flag,
    # four enforced invariants (see _enforce_secure_config): a non-empty
    # allowlist, dry-run on, both quotas above zero, and — at request time —
    # approve/reject answering 403 to everyone including the operator. Reads stay
    # open on purpose: a demo nobody can read is not a demo.
    public_mode: bool = False
    # Soft cost ceilings, counted from pr_jobs.created_at. 0 = unlimited (the
    # private-install default). A cap is a spend guard, not a correctness
    # boundary: two racing deliveries may overshoot by one.
    quota_jobs_per_repo_per_hour: int = 0
    quota_jobs_total_per_day: int = 0
    # Comma-separated IPs / CIDRs of reverse proxies whose X-Forwarded-For we
    # trust. Empty (the default) means the header is ignored entirely, because
    # an untrusted client can set it to any value and forge a fresh rate-limit
    # bucket per request.
    trusted_proxies: str = ""
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

    # --- Ingress limits ---
    # Largest webhook body we will buffer. GitHub caps deliveries at 25 MB; we
    # reject above 10 MB before the HMAC check so an unauthenticated caller
    # cannot make us allocate arbitrary memory (the rate limiter bounds request
    # *count*, not size).
    max_webhook_bytes: int = 10_000_000

    # --- Observability (Phase 9) ---
    # JSON logs (one event per line) are the production default: greppable and
    # ready for a log pipeline. Flip to false locally for the colourised console
    # renderer. Both the API and the worker read this through configure_logging.
    log_json: bool = True
    # Expose GET /metrics (Prometheus text format). On by default; toggle off if
    # a deployment scrapes elsewhere or wants the endpoint closed.
    metrics_enabled: bool = True

    # --- Worker behaviour ---
    # Hard cap on *executions* of one job. Attempts 1..N run the handler; delivery
    # N+1 flips the job to DEAD (the human queue) without running it.
    max_attempts: int = 5
    # Entries idle longer than this (ms) in the pending list are considered
    # abandoned by a dead consumer and are reclaimed via XAUTOCLAIM.
    #
    # This MUST exceed the worst-case runtime of one job, or a second worker will
    # reclaim (and concurrently re-run) a healthy-but-slow job. Worst case here is
    # roughly: 4 graph nodes x (1 + retries) LLM calls x llm_timeout_s, plus one
    # sandbox run at sandbox_timeout_s, plus network slack — ~8 minutes with the
    # defaults. 15 minutes leaves margin. Heartbeats/leases would be the rigorous
    # fix; a conservative idle window is the deliberate trade (see
    # docs/decisions/phase-1.md corner #4).
    reclaim_idle_ms: int = 900_000
    # How often the worker re-runs `reconcile_pending` for jobs stranded between
    # the DB commit and the XADD (a publish failure with a GitHub retry landing on
    # the dedup key otherwise never enqueues them).
    reconcile_interval_s: float = 60.0

    # --- Later phases (declared now so config is complete) ---
    qdrant_url: str = "http://localhost:6333"
    # Ground the agents in retrieved repo context. Off by default: it needs the
    # optional 'rag' extra (fastembed models) plus a reachable Qdrant, so an
    # unconfigured deployment must not pay for it. When on, the worker indexes a
    # repo snapshot once per (repo, sha) on the CI track and both the reviewer
    # and the fix agent retrieve against it. See app/agents/rag.build_rag.
    rag_enabled: bool = False
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
    # Total decoded-text ceiling for one snapshot. The two caps above bound a
    # single file and the file count, so their product (2 GB by default) was the
    # real memory exposure: a repo of 2,000 x 1 MB files would be held in worker
    # memory and carried inside the LangGraph state. This cap is the budget that
    # actually matters — keep it well under the worker's memory limit.
    snapshot_max_total_bytes: int = 25_000_000
    # Ceiling on the *compressed* tarball download, enforced while streaming so a
    # huge repo is rejected before it is buffered whole.
    snapshot_max_tarball_bytes: int = 100_000_000

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

    @property
    def allowed_repo_set(self) -> frozenset[str]:
        """Parse allowed_repos into a lower-cased set; empty means "allow all"."""
        return frozenset(r.strip().lower() for r in self.allowed_repos.split(",") if r.strip())

    @property
    def trusted_proxy_list(self) -> tuple[str, ...]:
        """Parse trusted_proxies into bare IP/CIDR strings for the IP resolver."""
        return tuple(p.strip() for p in self.trusted_proxies.split(",") if p.strip())

    @model_validator(mode="after")
    def _enforce_secure_config(self) -> Settings:
        """Fail fast on the two settings that turn a misconfig into someone else's write.

        A service that boots with a well-known shared secret is not secure — any
        caller could forge webhook signatures. And a service that boots with no
        API token answers `/reviews/{id}/approve` to anyone who can reach it, so
        the human gate becomes decorative. Both are latent misconfigs that fail
        silently at request time; we turn them into immediate, loud startup
        failures instead.

        `allow_insecure` is the deliberate, documented opt-out for local/dev/test
        runs (and it is what the test suite and the bench harness set).
        """
        # public_mode is a claim about *exposure*, not a local convenience, so its
        # invariants are checked even under the dev escape hatch: previewing the
        # demo locally then exercises the same posture production will run.
        if self.public_mode:
            self._enforce_public_mode()
        if self.allow_insecure:
            return self
        if self.webhook_secret == _PLACEHOLDER_WEBHOOK_SECRET:
            raise ValueError(
                "AUTOPR_WEBHOOK_SECRET is still the placeholder. Generate a real "
                'secret (python -c "import secrets; print(secrets.token_hex(32))") '
                "and set it, or set AUTOPR_ALLOW_INSECURE=1 for local/dev/tests."
            )
        if not self.api_token.strip():
            raise ValueError(
                "AUTOPR_API_TOKEN is empty, so /reviews/{id}/approve would accept "
                "anonymous approvals and let any caller make this bot write to your "
                'repository. Generate one with: python -c "import secrets; '
                'print(secrets.token_hex(32))" and set it, or set '
                "AUTOPR_ALLOW_INSECURE=1 for local/dev/tests."
            )
        return self

    def _enforce_public_mode(self) -> None:
        """A public instance must be safe by construction, not by operator care.

        Every condition below is one the operator could otherwise forget, and
        each one turns a demo into either a free LLM proxy for strangers or a
        remote code-change service for anyone who guesses the URL. They raise
        instead of warning for the same reason the token does: the failure is
        silent at request time and only visible on a bill or a diff.
        """
        if not self.allowed_repo_set:
            raise ValueError(
                "AUTOPR_PUBLIC_MODE=1 needs AUTOPR_ALLOWED_REPOS. Without an "
                "allowlist any repo that can deliver a signed webhook may spend "
                "your LLM budget, and on a public instance that includes every "
                "stranger who opens a fork PR."
            )
        if not self.github_dry_run:
            raise ValueError(
                "AUTOPR_PUBLIC_MODE=1 requires AUTOPR_GITHUB_DRY_RUN=true. A public "
                "instance that can write to GitHub is a remote code-change service "
                "for whoever reaches /approve; leave writes to a private deploy."
            )
        if self.quota_jobs_per_repo_per_hour <= 0 or self.quota_jobs_total_per_day <= 0:
            raise ValueError(
                "AUTOPR_PUBLIC_MODE=1 requires both quotas to be positive "
                "(AUTOPR_QUOTA_JOBS_PER_REPO_PER_HOUR, AUTOPR_QUOTA_JOBS_TOTAL_PER_DAY). "
                "They are the only thing standing between a stranger and your "
                "per-token bill."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
