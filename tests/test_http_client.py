"""The pooled HTTP client: reuse, and why reuse is safe across tenants.

Two things are asserted here, and only one of them is about speed:

* **Reuse.** ``httpx.Client`` construction costs ~280 ms on this machine (it
  builds an SSL context and a pool). The old code built one per call — or called
  ``httpx.get``, which does the same — and a job makes four to seven calls.
  ``test_client_is_built_once_per_instance`` and ``test_shared_client_is_a_singleton``
  pin the reuse.
* **Isolation.** A shared pool is safe *only because* the token travels in
  per-request headers and never on the client. If anyone ever "simplifies" this
  by setting ``headers=`` or ``auth=`` on the client, one tenant's credential
  would ride along on another tenant's call. ``test_client_carries_no_credential``
  is the guard for that, and it fails the moment that refactor happens.

The negative control for the *claim* lives in `scripts/negative_control_claim.py`;
this file's counterpart is `test_client_carries_no_credential`, whose failure mode
is silent credential bleed rather than a slower request.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.http import build_client, reset_shared_client, shared_client
from app.routing.github import HttpGitHubClient, HttpGitHubReader


@pytest.fixture(autouse=True)
def _clean_shared():
    reset_shared_client()
    yield
    reset_shared_client()


def test_shared_client_is_a_singleton():
    """Two calls, one client — the whole point of the pool."""
    first = shared_client()
    second = shared_client()
    assert first is second
    assert not first.is_closed


def test_reset_closes_and_rebuilds():
    first = shared_client()
    reset_shared_client()
    assert first.is_closed, "a pool that is dropped must not keep sockets open"
    second = shared_client()
    assert second is not first
    assert not second.is_closed


def test_shared_client_thread_safe_first_use():
    """Two threads racing on first use must not each build a client."""
    import threading

    seen: list[httpx.Client] = []
    lock = threading.Lock()
    start = threading.Barrier(2, timeout=5)

    def grab():
        start.wait(5)
        client = shared_client()
        with lock:
            seen.append(client)

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert len(seen) == 2
    assert seen[0] is seen[1], "the lazy init lock must yield exactly one client"


def test_pool_limits_come_from_config(monkeypatch):
    monkeypatch.setattr(settings, "http_max_connections", 7)
    monkeypatch.setattr(settings, "http_max_keepalive", 3)
    client = build_client()
    try:
        pool = client._transport._pool  # httpcore pool: the real limit carrier
        assert pool._max_connections == 7
        assert pool._max_keepalive_connections == 3
    finally:
        client.close()


def test_client_carries_no_credential():
    """The load-bearing isolation property.

    The pooled client is shared by every tenant, so it must hold *no* default
    ``Authorization`` header and no ``auth=``. Credentials are per-request.
    """
    client = shared_client()
    assert "authorization" not in {k.lower() for k in client.headers}
    assert client._auth is None
    assert client._cookies is not None, "cookies must stay per-client and empty"
    assert not list(client.cookies.items())


def test_write_client_builds_one_http_client_and_reuses_it():
    """A job's several GitHub calls share one client (and so one connection)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = HttpGitHubClient(token="t", transport=httpx.MockTransport(handler))
    pooled = client._http()
    client.post_issue_comment("acme/app", 1, "hello")
    client.post_issue_comment("acme/app", 1, "hello again")
    assert client._http() is pooled, "each call must not build a new pool"
    assert len(seen) == 2
    client.close()
    assert client._client is None


def test_write_client_sends_its_own_token_on_every_request():
    """Two clients, one process pool: each request carries its own bearer token."""
    tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})

    a = HttpGitHubClient(token="token-a", transport=httpx.MockTransport(handler))
    b = HttpGitHubClient(token="token-b", transport=httpx.MockTransport(handler))
    a.post_issue_comment("acme/one", 1, "from a")
    b.post_issue_comment("acme/two", 2, "from b")
    a.post_issue_comment("acme/one", 3, "from a again")

    assert tokens == ["Bearer token-a", "Bearer token-b", "Bearer token-a"]
    a.close()
    b.close()


def test_reader_pool_is_lazy_and_closable():
    """Constructing a reader opens nothing; the tarball/diff calls share one pool."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"filename": "a.py", "patch": "+x"}])

    reader = HttpGitHubReader(token="t", transport=httpx.MockTransport(handler))
    assert reader._client is None, "import/construct must not open a socket"
    reader.list_pull_files("acme/app", 1)
    pooled = reader._http()
    reader.list_pull_files("acme/app", 1)
    assert reader._http() is pooled
    assert len(seen) == 2
    reader.close()
    assert reader._client is None


def test_reader_close_is_idempotent():
    reader = HttpGitHubReader(
        token="t", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    reader.close()  # never opened
    reader.close()
    assert reader._client is None


def test_transport_bound_clients_do_not_share_the_process_pool():
    """A MockTransport carries its test's state, so it must never be process-wide."""
    client = build_client(httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        assert client is not shared_client()
    finally:
        client.close()
