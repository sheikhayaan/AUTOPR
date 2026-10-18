"""OPT-IN integration test against the real GitHub API.

The hermetic suite fakes GitHub because network tests are slow and flaky, but
that leaves one thing unproven that only reality can prove: that our request
shape is *accepted* by GitHub — auth header, API version, `pulls/{n}/files`
pagination, and the tarball redirect for `snapshot_repo`.

Run it deliberately, against a repo you control:

    export AUTOPR_LIVE_GITHUB_TOKEN=ghp_...        # repo scope (read is enough)
    export AUTOPR_LIVE_GITHUB_REPO=octocat/hello-world
    export AUTOPR_LIVE_GITHUB_PR=1347              # any open/closed PR number
    export AUTOPR_LIVE_GITHUB_REF=<sha or branch>  # a commit in that repo
    pytest tests/test_github_live.py -v

Everything is skipped unless the token is set, so `pytest` stays green offline
and in CI. The test is strictly READ-ONLY: it never writes to the repository, so
running it against a real project cannot change anything.
"""

from __future__ import annotations

import os

import pytest

from app.routing.github import HttpGitHubReader

pytestmark = pytest.mark.skipif(
    not os.environ.get("AUTOPR_LIVE_GITHUB_TOKEN"),
    reason="set AUTOPR_LIVE_GITHUB_TOKEN (+ repo/pr) to run the live GitHub test",
)

TOKEN = os.environ.get("AUTOPR_LIVE_GITHUB_TOKEN", "")
REPO = os.environ.get("AUTOPR_LIVE_GITHUB_REPO", "")
PR = os.environ.get("AUTOPR_LIVE_GITHUB_PR", "")
REF = os.environ.get("AUTOPR_LIVE_GITHUB_REF", "")

needs_repo = pytest.mark.skipif(
    not (REPO and PR), reason="set AUTOPR_LIVE_GITHUB_REPO and AUTOPR_LIVE_GITHUB_PR"
)
needs_ref = pytest.mark.skipif(not REF, reason="set AUTOPR_LIVE_GITHUB_REF")


@pytest.fixture()
def reader() -> HttpGitHubReader:
    return HttpGitHubReader(TOKEN)


@needs_repo
def test_list_pull_files_against_real_github(reader):
    files = reader.list_pull_files(REPO, int(PR))
    assert files, "GitHub returned no files for that PR — pick a non-empty one"
    for f in files:
        assert set(f) == {"path", "patch"}, "the reader must normalize to path/patch"
        assert f["path"], "every file entry needs a path"
        assert isinstance(f["patch"], str)  # "" for binary files, never None
    # At least one file should carry real diff text; otherwise the shape is right
    # but the content contract is broken (e.g. we asked for JSON when we needed
    # the diff, or vice versa).
    assert any(f["patch"] for f in files)


@needs_ref
def test_snapshot_repo_against_real_github(reader):
    tree = reader.snapshot_repo(REPO, REF)
    assert tree, "empty snapshot — the tarball redirect or extraction broke"
    paths = dict(tree)
    assert any(p.endswith((".py", ".js", ".ts", ".md", ".txt", ".yaml", ".yml")) for p in paths), (
        "no text files in the snapshot; the extractor may be dropping everything"
    )
    # Paths are repo-relative, never absolute or escaping the root — the sandbox
    # relies on this to contain everything it writes.
    for p in paths:
        assert not p.startswith("/") and ".." not in p.split("/")


def test_a_bad_token_is_rejected_not_silently_ignored():
    """Reads must raise on a 401 so the job is redelivered, not reviewed blind."""
    import httpx

    bad = HttpGitHubReader("ghp_definitely_not_a_real_token")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        bad.list_pull_files(REPO or "octocat/hello-world", int(PR or 1))
    assert exc.value.response.status_code == 401
