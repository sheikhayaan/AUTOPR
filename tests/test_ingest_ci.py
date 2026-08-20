"""Tests for the CI-fix track's webhook parsing (Phase 5).

`parse_ci_event` is the gate that decides whether a check_run/workflow_run
delivery becomes a CI-fix job. It must be conservative: act ONLY on a completed,
failed run that is tied to a PR (the outward action is a PR comment — a failure
with no PR has nowhere to land). Everything else is a 200-ack no-op (None).

These are pure-function tests: payload dict in, PRPayload-or-None out. No DB, no
network. The dedup_key assertions pin the idempotency contract — logs are
context, not identity, so they must not change the key.
"""

from __future__ import annotations

from app.ingest import parse_ci_event, parse_event, parse_pull_request_event


def _check_run_payload(
    *,
    action: str = "completed",
    conclusion: str = "failure",
    sha: str | None = "abc123",
    prs: list | None = None,
    app_slug: str | None = "github-actions",
) -> dict:
    cr: dict = {
        "name": "pytest",
        "conclusion": conclusion,
        "head_sha": sha,
        "pull_requests": [{"number": 7}] if prs is None else prs,
        "output": {
            "title": "1 failed",
            "summary": "test_foo failed",
            "text": "E assert 1 == 2",
        },
    }
    if app_slug is not None:
        cr["app"] = {"slug": app_slug}
    return {
        "action": action,
        "repository": {"full_name": "octocat/hello-world"},
        "check_run": cr,
    }


def _workflow_run_payload(
    *,
    action: str = "completed",
    conclusion: str = "failure",
    sha: str | None = "def456",
    prs: list | None = None,
) -> dict:
    return {
        "action": action,
        "repository": {"full_name": "octocat/hello-world"},
        "workflow_run": {
            "name": "CI",
            "conclusion": conclusion,
            "event": "pull_request",
            "head_sha": sha,
            "display_title": "Fix stuff",
            "pull_requests": [{"number": 8}] if prs is None else prs,
            "actor": {"login": "octocat"},
            "head_commit": {"message": "broke the build"},
        },
    }


# --- check_run ---------------------------------------------------------------


def test_check_run_failure_becomes_a_ci_payload():
    p = parse_ci_event("check_run", _check_run_payload())
    assert p is not None
    assert p.repo == "octocat/hello-world"
    assert p.pr_number == 7
    assert p.commit_sha == "abc123"
    assert p.event == "check_run"
    assert p.author == "github-actions"
    # The inline output block is assembled into diagnosable logs.
    assert "pytest" in p.ci_logs
    assert "failure" in p.ci_logs
    assert "test_foo failed" in p.ci_logs
    assert "assert 1 == 2" in p.ci_logs


def test_check_run_timed_out_and_action_required_are_failures():
    for conclusion in ("timed_out", "action_required"):
        p = parse_ci_event("check_run", _check_run_payload(conclusion=conclusion))
        assert p is not None, conclusion


def test_check_run_success_and_neutral_are_ignored():
    for conclusion in ("success", "neutral", "cancelled", "skipped"):
        assert parse_ci_event("check_run", _check_run_payload(conclusion=conclusion)) is None


def test_check_run_not_completed_is_ignored():
    assert parse_ci_event("check_run", _check_run_payload(action="created")) is None


def test_check_run_without_pr_is_ignored():
    # A failing check not attached to any PR has nowhere to post a fix.
    assert parse_ci_event("check_run", _check_run_payload(prs=[])) is None


def test_check_run_without_sha_is_ignored():
    assert parse_ci_event("check_run", _check_run_payload(sha=None)) is None


def test_check_run_missing_app_defaults_author_to_ci():
    p = parse_ci_event("check_run", _check_run_payload(app_slug=None))
    assert p is not None and p.author == "ci"


# --- workflow_run ------------------------------------------------------------


def test_workflow_run_failure_becomes_a_ci_payload():
    p = parse_ci_event("workflow_run", _workflow_run_payload())
    assert p is not None
    assert p.pr_number == 8
    assert p.commit_sha == "def456"
    assert p.event == "workflow_run"
    assert p.author == "octocat"
    assert "CI" in p.ci_logs
    assert "broke the build" in p.ci_logs


def test_workflow_run_success_is_ignored():
    assert parse_ci_event("workflow_run", _workflow_run_payload(conclusion="success")) is None


def test_workflow_run_without_pr_is_ignored():
    assert parse_ci_event("workflow_run", _workflow_run_payload(prs=[])) is None


def test_unknown_ci_event_type_is_ignored():
    # parse_ci_event only understands the two run events.
    assert parse_ci_event("push", _check_run_payload()) is None


# --- parse_event dispatch ----------------------------------------------------


def test_parse_event_routes_ci_events_to_ci_parser():
    p = parse_event("check_run", _check_run_payload())
    assert p is not None and p.event == "check_run"
    p2 = parse_event("workflow_run", _workflow_run_payload())
    assert p2 is not None and p2.event == "workflow_run"


def test_parse_event_routes_pull_request_to_pr_parser():
    pr_payload = {
        "repository": {"full_name": "octocat/hello-world"},
        "pull_request": {"number": 3, "head": {"sha": "cafe"}, "user": {"login": "octocat"}},
    }
    p = parse_event("pull_request", pr_payload)
    assert p is not None and p.event == "pull_request" and p.pr_number == 3


def test_parse_event_unknown_event_falls_through_and_is_ignored():
    # An event we don't handle with no PR body -> the PR parser returns None.
    assert parse_event("issues", {}) is None
    assert parse_event("ping", {"zen": "..."}) is None


# --- dedup_key contract ------------------------------------------------------


def test_ci_dedup_key_is_independent_of_log_text():
    # Two deliveries of the same failing check with different inline logs are the
    # SAME job — logs are context, not identity.
    a = parse_ci_event("check_run", _check_run_payload())
    noisy = _check_run_payload()
    noisy["check_run"]["output"]["text"] = "totally different tail\n" * 50
    b = parse_ci_event("check_run", noisy)
    assert a is not None and b is not None
    assert a.ci_logs != b.ci_logs
    assert a.dedup_key == b.dedup_key


def test_ci_and_pr_dedup_keys_differ_for_same_commit():
    # The event is part of the key, so a check_run job and a pull_request job for
    # the same repo/PR/SHA don't collide.
    ci = parse_ci_event("check_run", _check_run_payload())
    pr = parse_pull_request_event(
        "pull_request",
        {
            "repository": {"full_name": "octocat/hello-world"},
            "pull_request": {"number": 7, "head": {"sha": "abc123"}, "user": {"login": "x"}},
        },
    )
    assert ci is not None and pr is not None
    assert ci.dedup_key != pr.dedup_key


def test_ci_logs_are_clipped():
    huge = _check_run_payload()
    huge["check_run"]["output"]["text"] = "x" * 50_000
    p = parse_ci_event("check_run", huge)
    assert p is not None
    assert len(p.ci_logs) < 20_000
    assert "truncated" in p.ci_logs
