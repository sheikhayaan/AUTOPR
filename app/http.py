"""One HTTP client per process, with connection reuse.

Every call to the GitHub API used to build its own ``httpx.Client`` (or call the
module-level ``httpx.get``), which means a fresh TCP connection *and* a fresh TLS
handshake per request — pure latency that no amount of optimisation elsewhere can
recover. A single review job makes four to seven of them: the diff, the tarball,
the comment, the collaborator-permission check, plus any pagination.

``httpx.Client`` is thread-safe and pools keep-alive connections, so one instance
serves the API process and every worker thread. The credential is *not* on the
client: tokens are per-tenant and travel in per-request headers, so sharing the
pool cannot leak one tenant's token to another — which is the only reason this is
safe in a multi-tenant deployment.

Tests inject ``httpx.MockTransport``; those get their own client keyed by nothing
else, because a transport carries its test's mock state. Real traffic shares one.
"""

from __future__ import annotations

import threading

import httpx

from app.config import settings

_lock = threading.Lock()
_shared: httpx.Client | None = None


def build_client(
    transport: httpx.BaseTransport | None = None, timeout: float = 15.0
) -> httpx.Client:
    """A client with pooling sized from config, optionally transport-bound."""
    return httpx.Client(
        transport=transport,
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
            keepalive_expiry=settings.http_keepalive_expiry_s,
        ),
        follow_redirects=False,
    )


def shared_client() -> httpx.Client:
    """The process-wide client for real network traffic.

    Lazily built so importing this module never opens a socket, and locked so two
    threads racing on first use cannot each create one.
    """
    global _shared
    with _lock:
        if _shared is None or _shared.is_closed:
            _shared = build_client()
        return _shared


def reset_shared_client() -> None:
    """Close and drop the shared client (tests, and config reloads)."""
    global _shared
    with _lock:
        if _shared is not None:
            _shared.close()
        _shared = None
