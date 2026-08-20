"""Tests for the GitHub action boundary.

We test the fake (records intended calls, no I/O) and the factory's safe-by-
default selection. The HTTP client's wire calls are not exercised here — that
needs a live token/server; the factory test proves we never construct it
without an explicit opt-in.
"""

from __future__ import annotations

from app.routing.github import (
    ActionResult,
    FakeGitHubClient,
    GitHubClient,
    HttpGitHubClient,
    get_github_client,
)


def test_fake_records_comment_and_returns_url():
    gh = FakeGitHubClient()
    res = gh.post_issue_comment("o/r", 5, "hello")
    assert res.ok and res.kind == "comment" and res.url
    assert gh.calls == [{"op": "comment", "repo": "o/r", "pr_number": 5, "body": "hello"}]


def test_fake_records_pr_and_review_request():
    gh = FakeGitHubClient()
    gh.create_pull_request("o/r", "fix-branch", "main", "t", "b")
    gh.request_review("o/r", 5, ["alice"])
    ops = [c["op"] for c in gh.calls]
    assert ops == ["pull_request", "review_request"]


def test_fake_satisfies_protocol():
    assert isinstance(FakeGitHubClient(), GitHubClient)


def test_factory_returns_fake_when_no_token(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "github_token", "", raising=False)
    monkeypatch.setattr(settings, "github_dry_run", True, raising=False)
    assert isinstance(get_github_client(), FakeGitHubClient)


def test_factory_returns_fake_in_dry_run_even_with_token(monkeypatch):
    from app.config import settings

    # Safe by default: a token present but dry_run on must NOT touch GitHub.
    monkeypatch.setattr(settings, "github_token", "ghp_xxx", raising=False)
    monkeypatch.setattr(settings, "github_dry_run", True, raising=False)
    assert isinstance(get_github_client(), FakeGitHubClient)


def test_factory_returns_http_only_when_token_and_not_dry_run(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "github_token", "ghp_xxx", raising=False)
    monkeypatch.setattr(settings, "github_dry_run", False, raising=False)
    client = get_github_client()
    assert isinstance(client, HttpGitHubClient)
    assert client.token == "ghp_xxx"


def test_action_result_defaults():
    r = ActionResult(ok=True, kind="noop")
    assert r.url == "" and r.detail == ""
