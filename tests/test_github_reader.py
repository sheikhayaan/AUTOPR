"""Tests for the GitHub read boundary.

The read side feeds both tracks their input: the PR-review track its changed
files, the CI-fix track a whole-repo snapshot to verify a patch against. We test
the fakes (record calls, return canned data) and the factory's gating, which is
deliberately DIFFERENT from the write client: a read goes live as soon as a token
is present, independent of `github_dry_run` (a read is not a mutation). The HTTP
reader's wire calls aren't exercised here — that needs a live token/server; the
factory test proves when we do vs. don't construct it. The tarball *decoding* is
factored into a pure helper (`_extract_repo_tarball`) so it IS testable offline.
"""

from __future__ import annotations

import io
import tarfile

from app.routing.github import (
    FakeGitHubReader,
    GitHubReader,
    HttpGitHubReader,
    _extract_repo_tarball,
    get_github_reader,
)


def test_fake_returns_configured_files_and_records():
    files = [{"path": "a.py", "patch": "+x"}, {"path": "b.py", "patch": "-y"}]
    reader = FakeGitHubReader(files=files)
    out = reader.list_pull_files("o/r", 5)
    assert out == files
    assert reader.calls == [{"op": "list_files", "repo": "o/r", "pr_number": 5}]


def test_fake_defaults_to_empty():
    assert FakeGitHubReader().list_pull_files("o/r", 1) == []


def test_fake_returns_a_copy_not_the_backing_list():
    # Mutating the returned list must not corrupt the reader's canned data.
    reader = FakeGitHubReader(files=[{"path": "a.py", "patch": "+x"}])
    out = reader.list_pull_files("o/r", 1)
    out.append({"path": "evil", "patch": ""})
    assert len(reader.list_pull_files("o/r", 1)) == 1


def test_fake_satisfies_protocol():
    assert isinstance(FakeGitHubReader(), GitHubReader)


def test_factory_returns_fake_without_token(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "github_token", "", raising=False)
    assert isinstance(get_github_reader(), FakeGitHubReader)


def test_factory_returns_http_when_token_present_regardless_of_dry_run(monkeypatch):
    from app.config import settings

    # The key difference from the write client: reads go live on token alone,
    # even with dry_run ON (the safe default). Fetching a diff mutates nothing.
    monkeypatch.setattr(settings, "github_token", "ghp_xxx", raising=False)
    monkeypatch.setattr(settings, "github_dry_run", True, raising=False)
    reader = get_github_reader()
    assert isinstance(reader, HttpGitHubReader)
    assert reader.token == "ghp_xxx"


def test_http_reader_satisfies_protocol():
    assert isinstance(HttpGitHubReader(token="x"), GitHubReader)


# --- snapshot (CI-fix track) -------------------------------------------------


def test_fake_snapshot_returns_configured_and_records():
    snap = [("app/foo.py", "print(1)\n"), ("README.md", "# hi\n")]
    reader = FakeGitHubReader(snapshot=snap)
    out = reader.snapshot_repo("o/r", "abc123")
    assert out == snap
    assert reader.calls == [{"op": "snapshot", "repo": "o/r", "ref": "abc123"}]


def test_fake_snapshot_defaults_to_empty():
    assert FakeGitHubReader().snapshot_repo("o/r", "sha") == []


def test_fake_snapshot_returns_a_copy():
    reader = FakeGitHubReader(snapshot=[("a.py", "x")])
    out = reader.snapshot_repo("o/r", "sha")
    out.append(("evil", ""))
    assert len(reader.snapshot_repo("o/r", "sha")) == 1


# --- _extract_repo_tarball (the pure, offline-testable core) -----------------

_TOP = "octocat-hello-world-abc123"


def _make_tarball(files: dict[str, bytes], *, top: str = _TOP, add_dir: bool = True) -> bytes:
    """Build an in-memory tar.gz shaped like GitHub's tarball API response."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if add_dir:
            di = tarfile.TarInfo(name=top)
            di.type = tarfile.DIRTYPE
            di.mode = 0o755
            tar.addfile(di)
        for rel, content in files.items():
            info = tarfile.TarInfo(name=f"{top}/{rel}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_extract_strips_top_dir_and_decodes_text():
    raw = _make_tarball({"app/foo.py": b"def foo():\n    return 1\n", "README.md": b"# hi\n"})
    out = dict(_extract_repo_tarball(raw, max_file_bytes=1_000_000, max_files=2_000))
    assert out == {"app/foo.py": "def foo():\n    return 1\n", "README.md": "# hi\n"}
    # The "<owner>-<repo>-<sha>/" prefix is gone -> paths are repo-relative.
    assert all(not k.startswith(_TOP) for k in out)


def test_extract_skips_binary_files():
    raw = _make_tarball({"app/foo.py": b"ok\n", "logo.png": b"\xff\xd8\xff\x00\x01\x02"})
    out = dict(_extract_repo_tarball(raw, max_file_bytes=1_000_000, max_files=2_000))
    assert "app/foo.py" in out
    assert "logo.png" not in out  # non-UTF-8 -> skipped


def test_extract_skips_oversize_files():
    raw = _make_tarball({"small.py": b"x\n", "big.py": b"y" * 5000})
    out = dict(_extract_repo_tarball(raw, max_file_bytes=100, max_files=2_000))
    assert "small.py" in out
    assert "big.py" not in out


def test_extract_enforces_file_count_cap():
    files = {f"f{i}.py": b"x\n" for i in range(10)}
    out = _extract_repo_tarball(_make_tarball(files), max_file_bytes=1_000_000, max_files=3)
    assert len(out) == 3


def test_extract_ignores_directory_entries():
    # Only regular files come back; the DIRTYPE top entry is not a (path, text).
    raw = _make_tarball({"a.py": b"1\n"})
    out = _extract_repo_tarball(raw, max_file_bytes=1_000_000, max_files=2_000)
    assert out == [("a.py", "1\n")]


def test_extract_refuses_path_traversal():
    # A crafted member that escapes the top dir must be dropped, not returned.
    raw = _make_tarball({"../../etc/passwd": b"root\n", "ok.py": b"1\n"})
    out = dict(_extract_repo_tarball(raw, max_file_bytes=1_000_000, max_files=2_000))
    assert "ok.py" in out
    assert not any(".." in p for p in out)
