"""GitHub App authentication: the thing that makes this a service strangers can install.

A GitHub App authenticates in two steps, and getting either half subtly wrong is
how multi-tenant bots leak between tenants:

1. **App JWT** — RS256, signed with the App's private key, valid ≤10 minutes,
   `iss` = the App id. It can only read App metadata and mint installation
   tokens; it cannot touch a repository.
2. **Installation token** — `POST /app/installations/{id}/access_tokens` with
   that JWT. It is scoped to exactly one installation (one account's repos), lasts
   an hour, and is the *only* credential that ever touches a repository.

The design consequence worth stating plainly: there is no instance-wide repository
credential any more. Every write is bound to `installation_id`, so a bug that
serialises the wrong token can only be a bug in *which* installation, never a
token that reaches everything. That is the property that makes tenancy safe.

Tokens are cached per installation until 60s before expiry: minting on every
request would add ~200ms and burn GitHub's rate limit for nothing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt as pyjwt
import structlog

from app.config import settings
from app.http import build_client
from app.routing.github import ActionResult, FakeGitHubClient, HttpGitHubClient

log = structlog.get_logger(__name__)

# GitHub rejects anything over 10 minutes; 9 leaves room for clock skew.
_JWT_TTL_S = 540
_JWT_BACKDATE_S = 60
# Refresh this long before the token actually expires, so a token can never
# expire mid-request between the cache check and the API call.
_TOKEN_REFRESH_MARGIN_S = 60
# Repository -> installation mappings change only on install/uninstall, so a
# short cache is pure profit: the worker would otherwise ask GitHub on every post.
_REPO_CACHE_S = 300.0


class GitHubAppError(RuntimeError):
    """App-level auth failed (bad key, wrong app id, GitHub refused). Never a tenant's fault."""


@dataclass
class InstallationToken:
    """One cached installation credential."""

    token: str
    expires_at: float  # monotonic deadline, already margin-adjusted

    def is_live(self, now: float) -> bool:
        return now < self.expires_at


def normalise_private_key(raw: str) -> str:
    """Accept a PEM pasted into a single-line env var.

    Every hosting platform stores env vars on one line, so the private key
    arrives with literal ``\\n`` where the PEM needs newlines. Handling that here
    means an operator never has to discover the failure mode "InvalidKeyError"
    from a value that looks correct in their dashboard.
    """
    key = raw.strip().strip('"')
    if "\\n" in key:
        key = key.replace("\\n", "\n")
    if "BEGIN" not in key:
        raise GitHubAppError(
            "AUTOPR_GITHUB_APP_PRIVATE_KEY is not a PEM key. Paste the .pem file "
            "content (it starts with -----BEGIN RSA PRIVATE KEY-----)."
        )
    return key if key.endswith("\n") else key + "\n"


def app_jwt(app_id: int, private_key: str, now: float | None = None) -> str:
    """Build the short-lived App JWT that authorises token minting.

    `iat` is backdated 60s because GitHub rejects tokens whose issue time is in
    the future by even a second, and container clocks drift.
    """
    issued = int(now if now is not None else time.time())
    # `iss` is a string: PyJWT (rightly) refuses a non-string issuer, and GitHub
    # accepts the stringified app id. Numeric iss is the classic silent failure
    # here — the JWT looks valid, GitHub answers 401, and the message blames the key.
    payload = {
        "iat": issued - _JWT_BACKDATE_S,
        "exp": issued + _JWT_TTL_S,
        "iss": str(app_id),
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


class AppAuth:
    """Mints and caches installation tokens for one GitHub App.

    Thread-safe because sync FastAPI endpoints run in a threadpool and the worker
    is a separate process sharing nothing — each process keeps its own cache,
    which is correct: installation tokens are per-installation, not per-process.
    """

    def __init__(
        self,
        app_id: int,
        private_key: str,
        api_url: str | None = None,
        *,
        transport: Any = None,
    ) -> None:
        self.app_id = app_id
        self.private_key = normalise_private_key(private_key)
        self.api_url = (api_url or settings.github_api_url).rstrip("/")
        self._transport = transport  # injectable for tests; None = real network
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()
        self._tokens: dict[int, InstallationToken] = {}
        self._repo_cache: dict[str, tuple[int | None, float]] = {}
        self._jwt: tuple[str, float] | None = None

    # --- JWT -------------------------------------------------------------

    def jwt(self) -> str:
        """The current App JWT, reused until shortly before it expires."""
        now = time.time()
        with self._lock:
            if self._jwt is not None and now < self._jwt[1]:
                return self._jwt[0]
        token = app_jwt(self.app_id, self.private_key, now)
        # Per-request fresh signing is cheap; caching only avoids redundant RSA
        # ops within a burst. Reuse for half the TTL, then sign again.
        with self._lock:
            self._jwt = (token, now + (_JWT_TTL_S / 2))
        return token

    # --- installation tokens ---------------------------------------------

    def _http(self) -> httpx.Client:
        """The pooled client for App-level calls (token mints, install lookups).

        Cached per instance. A token mint happens per installation per hour, and
        a new client each time paid a TLS handshake for a request that is already
        the most latency-visible call in the tenancy path.
        """
        if self._client is None:
            self._client = build_client(self._transport)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def fetch_installation_token(self, installation_id: int) -> InstallationToken:
        """Mint a fresh token. Raises `GitHubAppError` if GitHub refuses."""
        import httpx

        url = f"{self.api_url}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self.jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            client = self._http()
            resp = client.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAppError(f"token mint transport error: {exc!r}") from exc
        if resp.status_code >= 300:
            # 401 = bad key/app id, 404 = installation gone (uninstalled).
            raise GitHubAppError(
                f"token mint refused for installation {installation_id}: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        token = data.get("token", "")
        if not token:
            raise GitHubAppError("token mint returned no token")
        expires_at = _parse_expiry(data.get("expires_at"))
        deadline = time.monotonic() + max(0.0, expires_at - _TOKEN_REFRESH_MARGIN_S)
        return InstallationToken(token=token, expires_at=deadline)

    def installation_token(self, installation_id: int) -> str:
        """Cached installation token, minting only when needed."""
        now = time.monotonic()
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and cached.is_live(now):
                return cached.token
        fresh = self.fetch_installation_token(installation_id)
        with self._lock:
            self._tokens[installation_id] = fresh
        log.info("github_app.token_minted", installation_id=installation_id)
        return fresh.token

    def invalidate(self, installation_id: int) -> None:
        """Drop a cached token — used when GitHub says it is no longer valid."""
        with self._lock:
            self._tokens.pop(installation_id, None)

    # --- per-tenant clients ----------------------------------------------

    def client(self, installation_id: int) -> HttpGitHubClient | FakeGitHubClient:
        """A GitHub client bound to exactly one installation.

        Dry-run still returns the *real* shape but a no-op body, so the code path
        is identical between a demo and production — only the effect differs.
        """
        if settings.handoff_mode or settings.github_dry_run:
            return FakeGitHubClient()
        return HttpGitHubClient(
            self.installation_token(installation_id), self.api_url, transport=self._transport
        )

    # --- permission checks -----------------------------------------------

    def collaborator_permission(self, installation_id: int, repo: str, login: str) -> str:
        """Ask GitHub what `login` may do in `repo` ("admin"|"maintain"|"write"|"read"|"none").

        This is the whole trick behind comment-driven approval: the identity and
        the authority both come from GitHub, so AutoPR never stores a maintainer
        list and can never disagree with the repository's own permissions.
        """
        import httpx

        url = f"{self.api_url}/repos/{repo}/collaborators/{login}/permission"
        headers = {
            "Authorization": f"Bearer {self.installation_token(installation_id)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            client = self._http()
            resp = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAppError(f"permission lookup failed: {exc!r}") from exc
        if resp.status_code == 404:
            return "none"  # not a collaborator at all
        if resp.status_code >= 300:
            raise GitHubAppError(
                f"permission lookup refused: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return str(resp.json().get("permission", "none")).lower()

    def repo_installation(self, repo: str) -> int | None:
        """Which installation owns `repo`? Cached, because the worker asks per write.

        `GET /repos/{owner}/{repo}/installation` is GitHub's own answer, which is
        the point: we never keep a repo->tenant table that could drift from the
        truth. None means the App is not installed there, so a write must not
        happen at all.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._repo_cache.get(repo.lower())
            if cached is not None and now < cached[1]:
                return cached[0]
        info = self._repo_installation_uncached(repo)
        with self._lock:
            self._repo_cache[repo.lower()] = (info, now + _REPO_CACHE_S)
        return info

    def _repo_installation_uncached(self, repo: str) -> int | None:
        import httpx

        url = f"{self.api_url}/repos/{repo}/installation"
        headers = {
            "Authorization": f"Bearer {self.jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            client = self._http()
            resp = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAppError(f"repo installation lookup failed: {exc!r}") from exc
        if resp.status_code == 404:
            return None  # not installed on this repo
        if resp.status_code >= 300:
            raise GitHubAppError(
                f"repo installation lookup refused: HTTP {resp.status_code} {resp.text[:200]}"
            )
        raw = resp.json().get("id")
        return int(raw) if raw is not None else None

    def installation_info(self, installation_id: int) -> dict:
        """Read one installation's metadata (used to heal a missed webhook)."""
        import httpx

        url = f"{self.api_url}/app/installations/{installation_id}"
        headers = {
            "Authorization": f"Bearer {self.jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            client = self._http()
            resp = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAppError(f"installation lookup failed: {exc!r}") from exc
        if resp.status_code >= 300:
            raise GitHubAppError(
                f"installation lookup refused: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()


class TenantGitHubClient:
    """A GitHub writer that resolves the owning installation per repository.

    The worker builds its graph once and posts to whatever repo a job names, so
    it has no tenant argument to pass around. Resolving from the repo keeps the
    "one credential per installation" invariant without threading tenancy through
    every agent node — and the cache makes it free after the first write.

    When the App is not installed on the repo, the write is refused. That is the
    important half: falling back to an instance-wide token would silently write
    on behalf of a tenant that never asked for it.
    """

    def __init__(self, auth: AppAuth) -> None:
        self._auth = auth

    def post_issue_comment(self, repo: str, pr_number: int, body: str) -> ActionResult:
        # Dry run answers before any lookup: a demo must not even read from GitHub,
        # and an offline test run should never discover a socket.
        if settings.handoff_mode or settings.github_dry_run:
            return FakeGitHubClient().post_issue_comment(repo, pr_number, body)
        try:
            installation_id = self._auth.repo_installation(repo)
        except GitHubAppError as exc:
            log.error("github_app.repo_lookup_failed", repo=repo, error=str(exc))
            return ActionResult(ok=False, kind="comment", detail=str(exc))
        if installation_id is None:
            log.warning("github_app.not_installed", repo=repo)
            return ActionResult(
                ok=False, kind="comment", detail="app not installed on this repository"
            )
        return self._auth.client(installation_id).post_issue_comment(repo, pr_number, body)


def _parse_expiry(raw: str | None) -> float:
    """Seconds until GitHub's `expires_at`, defaulting to 50 minutes."""
    if not raw:
        return 3000.0
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 3000.0
    return max(60.0, expires.timestamp() - time.time())


# --- process-wide singleton ---------------------------------------------------

_app_auth: AppAuth | None = None
_app_lock = threading.Lock()


def get_app_auth() -> AppAuth | None:
    """The App authenticator, or None when this install has no App configured.

    None is a supported mode: a single-operator deployment with a PAT keeps
    working exactly as before. Tenancy is opt-in by supplying App credentials.
    """
    global _app_auth
    if not settings.multi_tenant_enabled:
        return None
    with _app_lock:
        if _app_auth is None:
            assert settings.github_app_id is not None
            _app_auth = AppAuth(
                settings.github_app_id,
                settings.github_app_private_key,
                settings.github_api_url,
            )
            log.info("github_app.ready", app_id=settings.github_app_id)
        return _app_auth


def reset_app_auth() -> None:
    """Drop the singleton (tests, and after a credential rotation)."""
    global _app_auth
    with _app_lock:
        _app_auth = None


def ok_action(kind: str, url: str = "") -> ActionResult:
    """Small helper so callers need not import ActionResult from two modules."""
    return ActionResult(ok=True, kind=kind, url=url)
