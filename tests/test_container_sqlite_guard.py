"""SQLite inside a container is refused, loudly.

Found by building the image and running it, not by reading it: `docker run
autopr:local` died with `sqlalchemy.exc.OperationalError: unable to open database
file`, because the app's default is a *relative* SQLite path and the runtime stage
runs as `appuser` in a root-owned `/app`. The worse half of the same setting is
silent rather than loud: once the path *is* writable, the job queue lives in the
container filesystem, so every restart discards pending work with no error at all.

So the rule is: SQLite is fine on a host, and refused in a container unless the
operator says the jobs do not matter (`AUTOPR_ALLOW_INSECURE=1`). These tests pin
both halves, plus the probe's override, which is what makes the rule testable.
"""

from __future__ import annotations

import pytest

from app.config import Settings, running_in_container

_POSTGRES = "postgresql+psycopg://autopr:autopr@postgres:5432/autopr"
_SQLITE = "sqlite+pysqlite:////var/lib/autopr/autopr.db"


def _settings(**overrides: object) -> Settings:
    """Build Settings from this test's values only — never from the ambient env.

    ``allow_insecure`` is passed explicitly and defaults to the *production*
    answer here, because ``tests/conftest.py`` sets ``AUTOPR_ALLOW_INSECURE=1``
    for the rest of the suite; inheriting that would make every assertion below
    vacuous (the escape hatch returns before the guard is reached).
    """
    base: dict[str, object] = {
        "webhook_secret": "a-real-secret-value",
        "api_token": "a-real-token",
        "database_url": _POSTGRES,
        "allow_insecure": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type, call-arg]


def test_sqlite_in_a_container_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure must name the fix, not surface as an OperationalError later."""
    monkeypatch.setattr("app.config.running_in_container", lambda: True)
    with pytest.raises(ValueError, match="SQLite inside a container"):
        _settings(database_url=_SQLITE)


def test_the_error_names_both_ways_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard that does not say how to proceed just moves the dead end."""
    monkeypatch.setattr("app.config.running_in_container", lambda: True)
    with pytest.raises(ValueError) as excinfo:
        _settings(database_url=_SQLITE)
    message = str(excinfo.value)
    assert "Postgres" in message
    assert "AUTOPR_ALLOW_INSECURE=1" in message


def test_the_escape_hatch_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throwaway instance is a legitimate use: demo, smoke test, CI."""
    monkeypatch.setattr("app.config.running_in_container", lambda: True)
    assert _settings(database_url=_SQLITE, allow_insecure=True).is_sqlite is True


def test_postgres_in_a_container_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule is about ephemeral storage, not about containers."""
    monkeypatch.setattr("app.config.running_in_container", lambda: True)
    assert _settings(database_url=_POSTGRES).is_sqlite is False


def test_sqlite_on_a_host_is_still_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local developer story (and the whole test suite) runs on SQLite."""
    monkeypatch.setattr("app.config.running_in_container", lambda: False)
    assert _settings(database_url=_SQLITE).is_sqlite is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_override_forces_the_probe_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTOPR_IN_CONTAINER", value)
    assert running_in_container() is True


@pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
def test_override_forces_the_probe_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTOPR_IN_CONTAINER", value)
    assert running_in_container() is False


def test_probe_falls_back_to_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """No override, no Kubernetes env: the answer comes from the container marker.

    The marker is patched rather than assumed, so this passes on a laptop outside
    a container and inside one — the assertion is about which path is consulted.
    """
    monkeypatch.delenv("AUTOPR_IN_CONTAINER", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    class _Exists:
        def __init__(self, result: bool) -> None:
            self.result = result

        def exists(self) -> bool:
            return self.result

    monkeypatch.setattr("app.config.Path", lambda _p: _Exists(True))
    assert running_in_container() is True
    monkeypatch.setattr("app.config.Path", lambda _p: _Exists(False))
    assert running_in_container() is False


def test_kubernetes_env_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `/.dockerenv` in a pod, and a pod is exactly where this must fire."""
    monkeypatch.delenv("AUTOPR_IN_CONTAINER", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert running_in_container() is True
