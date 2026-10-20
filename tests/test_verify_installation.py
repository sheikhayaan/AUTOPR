"""Tests for `scripts/verify_installation.py`.

The script exists to answer a question only real credentials can answer, so its
value is in the real run. These tests cover the thing a real run cannot: that each
check *fails* when it should. A verification script that reports PASS on a broken
credential is worse than no script at all, so every failure mode below is pinned:
a rejected JWT, an installation that is not installed, a token with the wrong
prefix, an expired token, a repo the installation cannot see, and a repo mapped
to a different installation.

The fake GitHub here answers with the shapes the real API returns (verified
against api.github.com for the read endpoints — see tests/test_github_live.py).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def verifier():  # noqa: ANN201 - dynamically loaded module
    path = ROOT / "scripts" / "verify_installation.py"
    spec = importlib.util.spec_from_file_location("verify_installation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_installation"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch, private_key_pem: str) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "github_app_id", 12345, raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", private_key_pem, raising=False)
    monkeypatch.setattr(settings, "github_api_url", "https://api.github.test", raising=False)


def _json(payload: object) -> httpx.Response:
    return httpx.Response(
        200, content=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )


class FakeGitHub:
    """A handler that answers like GitHub, with knobs for each failure mode."""

    def __init__(
        self,
        *,
        token: str | None = None,
        expires_in: int = 3600,
        repositories: list[str] | None = None,
        repo_installation: int | None = 999,
        permission: str = "write",
        reject_jwt: bool = False,
        installations: list[int] | None = None,
        comment_status: int = 201,
    ) -> None:
        self.token = token or "ghs_" + "x" * 36
        self.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        self.repositories = repositories if repositories is not None else ["octocat/hello-world"]
        self.repo_installation = repo_installation
        self.permission = permission
        self.reject_jwt = reject_jwt
        self.installations = installations if installations is not None else [999]
        self.comment_status = comment_status
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if self.reject_jwt and path in {"/app", "/app/installations"}:
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        if path == "/app":
            return _json({"id": 12345, "slug": "autopr-test"})
        if path == "/app/installations":
            return _json([{"id": i} for i in self.installations])
        if path.endswith("/access_tokens"):
            return _json({"token": self.token, "expires_at": self.expires_at.isoformat()})
        if path == "/installation/repositories":
            return _json({"repositories": [{"full_name": r} for r in self.repositories]})
        if path.endswith("/installation"):
            if self.repo_installation is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return _json({"id": self.repo_installation})
        if path.endswith("/permission"):
            return _json({"permission": self.permission})
        if path.endswith("/comments"):
            return httpx.Response(
                self.comment_status, json={"html_url": "https://github.com/x/y/pull/42#c1"}
            )
        return httpx.Response(404, json={"message": f"unhandled {path}"})


def _run(verifier, fake: FakeGitHub, **kw) -> object:  # noqa: ANN001, ANN003
    transport = httpx.MockTransport(fake)
    args = {"repo": "octocat/hello-world", "login": "alice", "transport": transport}
    args.update(kw)
    return verifier.verify(**args)


def _statuses(report) -> dict[str, str]:  # noqa: ANN001
    return {c.name: c.status for c in report.checks}


def test_a_healthy_app_passes_every_check(verifier, configured) -> None:
    fake = FakeGitHub()
    report = _run(verifier, fake)

    statuses = _statuses(report)
    for name in (
        "config",
        "jwt",
        "app_identity",
        "installations",
        "installation_token",
        "repo_access",
        "repo_lookup",
        "permission",
    ):
        assert statuses[name] == verifier.PASS, f"{name} -> {statuses[name]}"
    # A comment that was not confirmed must be a SKIP, never a PASS.
    assert statuses["comment"] == verifier.SKIP
    # Only GitHub can deliver a webhook, so this is never claimed as proven.
    assert statuses["webhook_delivery"] == verifier.MANUAL
    assert report.failures == []
    # The token really was minted and used against the installation endpoints.
    assert "POST /app/installations/999/access_tokens" in fake.calls


def test_a_rejected_jwt_fails_and_stops_the_run(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(reject_jwt=True))
    statuses = _statuses(report)
    assert statuses["app_identity"] == verifier.FAIL
    assert "401" in next(c.detail for c in report.checks if c.name == "app_identity")
    assert report.failures


def test_no_installations_is_a_failure_with_the_install_url(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(installations=[]))
    statuses = _statuses(report)
    assert statuses["installations"] == verifier.FAIL
    detail = next(c.detail for c in report.checks if c.name == "installations")
    assert "install" in detail.lower()


def test_asking_for_an_installation_that_does_not_exist_fails(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(installations=[999]), installation_id=111)
    statuses = _statuses(report)
    assert statuses["installations"] == verifier.FAIL
    assert "111" in next(c.detail for c in report.checks if c.name == "installations")


def test_a_token_without_the_ghs_prefix_fails(verifier, configured) -> None:
    """A `ghp_`/`github_pat_` token here means the App path silently fell back to
    an instance-wide PAT — exactly the multi-tenant leak the App exists to stop."""
    report = _run(verifier, FakeGitHub(token="ghp_" + "y" * 36))
    statuses = _statuses(report)
    assert statuses["installation_token"] == verifier.FAIL
    assert "ghs_" in next(c.detail for c in report.checks if c.name == "installation_token")
    # ...and nothing downstream pretends to have been verified.
    assert "repo_access" not in statuses


def test_an_expired_token_fails(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(expires_in=0))
    assert _statuses(report)["installation_token"] == verifier.FAIL


def test_a_repo_outside_the_installation_fails(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(repositories=["someone/else"]))
    statuses = _statuses(report)
    assert statuses["repo_access"] == verifier.FAIL
    assert "octocat/hello-world" in next(c.detail for c in report.checks if c.name == "repo_access")


def test_a_repo_mapped_to_another_installation_fails(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(repo_installation=777))
    statuses = _statuses(report)
    assert statuses["repo_lookup"] == verifier.FAIL
    assert "777" in next(c.detail for c in report.checks if c.name == "repo_lookup")


def test_an_uninstalled_repo_fails(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(repo_installation=None))
    assert _statuses(report)["repo_lookup"] == verifier.FAIL


def test_missing_configuration_fails_before_any_network_call(
    verifier, monkeypatch, configured
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "github_app_id", None, raising=False)
    monkeypatch.setattr(settings, "github_app_private_key", "", raising=False)
    fake = FakeGitHub()
    report = _run(verifier, fake)
    statuses = _statuses(report)
    assert statuses["config"] == verifier.FAIL
    assert set(statuses) == {"config"}, "no point calling GitHub without credentials"
    assert fake.calls == []


def test_a_bad_private_key_fails_at_config(verifier, monkeypatch, configured) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "github_app_private_key", "not a pem", raising=False)
    report = _run(verifier, FakeGitHub())
    assert _statuses(report)["config"] == verifier.FAIL


def test_the_comment_write_path_is_exercised_when_confirmed(verifier, configured) -> None:
    fake = FakeGitHub()
    report = _run(verifier, fake, pr_number=42, confirmed=True)
    statuses = _statuses(report)
    assert statuses["comment"] == verifier.PASS
    assert "POST /repos/octocat/hello-world/issues/42/comments" in fake.calls


def test_a_refused_comment_fails(verifier, configured) -> None:
    report = _run(verifier, FakeGitHub(comment_status=403), pr_number=42, confirmed=True)
    statuses = _statuses(report)
    assert statuses["comment"] == verifier.FAIL
    assert "403" in next(c.detail for c in report.checks if c.name == "comment")


def test_no_comment_requested_never_writes(verifier, configured) -> None:
    fake = FakeGitHub()
    _run(verifier, fake, pr_number=None)
    assert not any(call.startswith("POST /repos") for call in fake.calls)


def test_json_output_is_machine_readable(verifier, capsys, monkeypatch) -> None:
    """`--json` is what a CI step parses, so its shape is a contract."""
    monkeypatch.setattr(
        verifier, "verify", lambda **kw: verifier.Report([verifier.Check("config", "PASS", "")])
    )
    code = verifier.main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"name": "config", "status": "PASS", "detail": ""}]
    assert code == 0


def test_exit_code_is_non_zero_when_a_check_fails(verifier, configured, monkeypatch) -> None:
    monkeypatch.setattr(
        verifier, "verify", lambda **kw: verifier.Report([verifier.Check("jwt", "FAIL", "bad")])
    )
    assert verifier.main([]) == 1
