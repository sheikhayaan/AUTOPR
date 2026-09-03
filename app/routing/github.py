"""The outward action boundary: talking to GitHub.

Everything that leaves the system and touches a real PR goes through a
`GitHubClient`. Isolating it behind a Protocol means:
  * the router and the ops API are testable with a fake that records calls and
    touches nothing;
  * "post for real" vs "dry run" is a single factory decision driven by config,
    not scattered `if` statements;
  * a missing/invalid token degrades to a safe no-op (dry run) instead of
    crashing or, worse, half-posting.

Design stance: the *real* client is deliberately thin — three REST calls. The
interesting, load-bearing logic (when to act, what to say, who must approve)
lives in policy.py and router.py where it is exhaustively unit-tested. The HTTP
client is the boring edge, and boring is correct for the edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import structlog

from app.config import settings

log = structlog.get_logger()


@dataclass(frozen=True)
class ActionResult:
    """Outcome of an outward action. `url` is the created comment/PR when real."""

    ok: bool
    kind: str  # "comment" | "pull_request" | "review_request" | "noop"
    url: str = ""
    detail: str = ""


@runtime_checkable
class GitHubClient(Protocol):
    """The three outward actions Phase 4 needs. Kept minimal on purpose."""

    def post_issue_comment(self, repo: str, pr_number: int, body: str) -> ActionResult: ...

    def create_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> ActionResult: ...

    def request_review(self, repo: str, pr_number: int, reviewers: list[str]) -> ActionResult: ...


@dataclass
class FakeGitHubClient:
    """Records calls and performs no I/O. The default in tests and in dry-run.

    Also the honest production default when no token is configured: a run still
    completes end-to-end and the *intended* action is captured, we just don't
    reach out. `calls` lets tests assert exactly what would have been sent.
    """

    calls: list[dict] = field(default_factory=list)
    _n: int = 0

    def _next_url(self, kind: str) -> str:
        self._n += 1
        return f"https://fake.github/{kind}/{self._n}"

    def post_issue_comment(self, repo: str, pr_number: int, body: str) -> ActionResult:
        self.calls.append({"op": "comment", "repo": repo, "pr_number": pr_number, "body": body})
        return ActionResult(ok=True, kind="comment", url=self._next_url("comment"))

    def create_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> ActionResult:
        self.calls.append(
            {
                "op": "pull_request",
                "repo": repo,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            }
        )
        return ActionResult(ok=True, kind="pull_request", url=self._next_url("pull"))

    def request_review(self, repo: str, pr_number: int, reviewers: list[str]) -> ActionResult:
        self.calls.append(
            {"op": "review_request", "repo": repo, "pr_number": pr_number, "reviewers": reviewers}
        )
        return ActionResult(ok=True, kind="review_request", url=self._next_url("review"))


class HttpGitHubClient:
    """Thin real client over the GitHub REST API (v3), using a bearer token.

    Only instantiated when a token is configured. Uses httpx synchronously to
    match the rest of the app's sync I/O model. Network/HTTP errors are turned
    into `ActionResult(ok=False, ...)` rather than exceptions, so one failed
    post never crashes a worker or an approve request — the caller decides what
    to do with a failed action (we mark the decision as failed, not executed).
    """

    def __init__(self, token: str, api_url: str | None = None) -> None:
        self.token = token
        self.api_url = (api_url or settings.github_api_url).rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _post(self, path: str, json_body: dict, kind: str) -> ActionResult:
        import httpx

        url = f"{self.api_url}{path}"
        try:
            resp = httpx.post(url, headers=self._headers(), json=json_body, timeout=15.0)
        except httpx.HTTPError as exc:
            log.error("github.http_error", kind=kind, error=repr(exc))
            return ActionResult(ok=False, kind=kind, detail=repr(exc))
        if resp.status_code >= 300:
            log.error("github.bad_status", kind=kind, status=resp.status_code, body=resp.text[:300])
            return ActionResult(ok=False, kind=kind, detail=f"HTTP {resp.status_code}")
        data = resp.json() if resp.content else {}
        return ActionResult(ok=True, kind=kind, url=data.get("html_url", ""))

    def post_issue_comment(self, repo: str, pr_number: int, body: str) -> ActionResult:
        # Issue-comment endpoint works for PRs too (a PR is an issue in the API).
        return self._post(f"/repos/{repo}/issues/{pr_number}/comments", {"body": body}, "comment")

    def create_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> ActionResult:
        return self._post(
            f"/repos/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
            "pull_request",
        )

    def request_review(self, repo: str, pr_number: int, reviewers: list[str]) -> ActionResult:
        return self._post(
            f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
            {"reviewers": reviewers},
            "review_request",
        )


def get_github_client() -> GitHubClient:
    """Pick the client from config.

    A real `HttpGitHubClient` only when a token is present AND dry-run is off;
    otherwise the `FakeGitHubClient`, which records intended actions without
    reaching out. Defaulting to the fake makes the system safe by default: you
    have to *opt in* to it touching real repositories.

    Hand-off mode overrides everything: AutoPR must never write to GitHub, so we
    return the no-op client even when a token is configured. Humans act on their
    own accounts via the review link the queue surfaces.
    """
    if settings.handoff_mode:
        log.info("github.client", mode="handoff_readonly")
        return FakeGitHubClient()
    if settings.github_token and not settings.github_dry_run:
        log.info("github.client", mode="http", api=settings.github_api_url)
        return HttpGitHubClient(settings.github_token, settings.github_api_url)
    log.info("github.client", mode="dry_run")
    return FakeGitHubClient()


# --- read boundary -----------------------------------------------------------
# The *read* side is a separate boundary from the *write* client above, and it
# is gated differently on purpose. Writes are mutations, so they hide behind the
# `github_dry_run` master switch (you opt in to touching a real repo). A read is
# not a mutation — fetching a PR's diff changes nothing — so gating it behind
# dry-run would make the "live fetch" not actually live in the default config.
# Reads therefore go live as soon as a *token* is present, independent of
# dry-run. Net effect: by default (token set, dry_run on) the worker fetches the
# real diff and *records* the intended review without posting it. Safe, and real.


@runtime_checkable
class GitHubReader(Protocol):
    """The reads the pipeline needs.

    Two shapes, one per track: the PR-review track asks what files a PR changed;
    the CI-fix track asks for a whole-repo snapshot at a commit, so the sandbox
    can apply a candidate patch and re-run the failing check against the real
    tree (not just the PR's changed files — pytest needs the whole module graph).
    """

    def list_pull_files(self, repo: str, pr_number: int) -> list[dict]: ...

    def snapshot_repo(self, repo: str, ref: str) -> list[tuple[str, str]]: ...


@dataclass
class FakeGitHubReader:
    """Returns canned reads and records calls. No I/O.

    The default when no token is configured, and the injection point in tests:
    set `files` to the diff the PR-review track should see, and `snapshot` to the
    repo tree the CI-fix track's sandbox should apply the patch against.
    """

    files: list[dict] = field(default_factory=list)
    snapshot: list[tuple[str, str]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def list_pull_files(self, repo: str, pr_number: int) -> list[dict]:
        self.calls.append({"op": "list_files", "repo": repo, "pr_number": pr_number})
        return list(self.files)

    def snapshot_repo(self, repo: str, ref: str) -> list[tuple[str, str]]:
        self.calls.append({"op": "snapshot", "repo": repo, "ref": ref})
        return list(self.snapshot)


class HttpGitHubReader:
    """Thin real reader over the GitHub REST API (v3).

    Unlike the write client, read errors are *raised*, not swallowed into a
    result object: if we can't fetch the diff there is nothing to review, and
    letting it propagate lets the worker's existing retry/PEL machinery redeliver
    the job rather than silently reviewing an empty changeset.
    """

    def __init__(self, token: str = "", api_url: str | None = None) -> None:
        self.token = token
        self.api_url = (api_url or settings.github_api_url).rstrip("/")

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def list_pull_files(self, repo: str, pr_number: int) -> list[dict]:
        import httpx

        out: list[dict] = []
        page = 1
        while True:
            url = f"{self.api_url}/repos/{repo}/pulls/{pr_number}/files"
            resp = httpx.get(
                url,
                headers=self._headers(),
                params={"per_page": 100, "page": page},
                timeout=15.0,
            )
            resp.raise_for_status()
            batch = resp.json() or []
            for f in batch:
                # GitHub calls it `filename`; our state uses `path`. `patch` is
                # absent for binary/too-large files — normalize to "".
                out.append({"path": f.get("filename", ""), "patch": f.get("patch", "") or ""})
            if len(batch) < 100:
                break
            page += 1
        log.info("github.read.list_files", repo=repo, pr=pr_number, n=len(out))
        return out

    def snapshot_repo(self, repo: str, ref: str) -> list[tuple[str, str]]:
        """Whole-repo text snapshot at `ref`, via the tarball API.

        One request — `GET /repos/{repo}/tarball/{ref}`, which 302-redirects to a
        signed codeload URL — returns a gzipped tar of the entire tree. That is
        deliberately cheaper than the alternative (walk the git-tree API, then GET
        each blob: one call per file, i.e. N+1). We follow the redirect, then hand
        the raw bytes to the pure `_extract_repo_tarball` for decoding + bounding.

        Like `list_pull_files`, failures raise: an empty/partial snapshot would
        make the sandbox verify a patch against the wrong tree, so we let the
        worker's retry/PEL machinery redeliver instead of proceeding on bad data.
        """
        import httpx

        url = f"{self.api_url}/repos/{repo}/tarball/{ref}"
        resp = httpx.get(url, headers=self._headers(), timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        files = _extract_repo_tarball(
            resp.content,
            max_file_bytes=settings.snapshot_max_file_bytes,
            max_files=settings.snapshot_max_files,
        )
        log.info("github.read.snapshot", repo=repo, ref=ref[:8], n=len(files))
        return files


def _extract_repo_tarball(
    raw_gz: bytes, *, max_file_bytes: int, max_files: int
) -> list[tuple[str, str]]:
    """Decode a GitHub repo tarball into `[(relpath, text), ...]`. Pure + testable.

    Kept as a free function (no network, no self) precisely so the fiddly parts —
    the path rewrite and the two caps — are unit-testable from an in-memory
    tar.gz without touching GitHub.

    Three behaviours worth stating:
      * GitHub nests the whole repo under one top dir named `<owner>-<repo>-<sha>/`;
        we strip that first component so paths are repo-relative (what the diff
        and the sandbox patch expect).
      * Only regular files that decode as UTF-8 are kept. Binaries (and any
        non-UTF-8 file) are skipped — every downstream sandbox check (ruff, mypy,
        pytest) is a text tool, so a binary in the tree is noise, not signal.
      * Both caps are defensive against a pathological/huge repo blowing up
        worker memory: a file over `max_file_bytes` is skipped, and we stop once
        `max_files` text files are collected. The caller logs the resulting count;
        silent truncation is a documented corner (see docs/decisions/phase-5.md).
    """
    import io
    import tarfile

    out: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(raw_gz), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > max_file_bytes:
                continue
            # Strip the single leading "<top>/" component GitHub prepends.
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            # Defense-in-depth against a crafted tar: no absolute paths, no
            # parent escapes. (The sandbox's _write_context refuses these too.)
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                text = fh.read().decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary / non-utf8 — not something the checks can use
            out.append((rel, text))
            if len(out) >= max_files:
                break
    return out


def get_github_reader() -> GitHubReader:
    """Pick the reader from config: real HTTP reader when a token is present.

    Note the different gate from `get_github_client`: reads don't consult
    `github_dry_run` (a read is not a mutation), so a token alone opts you into
    live fetching. No token => the fake (records calls, returns []).
    """
    if settings.github_token:
        log.info("github.reader", mode="http", api=settings.github_api_url)
        return HttpGitHubReader(settings.github_token, settings.github_api_url)
    if settings.handoff_mode:
        log.info("github.reader", mode="http_anonymous", api=settings.github_api_url)
        return HttpGitHubReader("", settings.github_api_url)
    log.info("github.reader", mode="fake")
    return FakeGitHubReader()
