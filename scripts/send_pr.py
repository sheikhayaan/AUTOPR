"""Send a correctly-signed `pull_request` webhook to a local AutoPR API.

Test the full pipeline against a REAL public PR without setting up a GitHub
webhook + tunnel. Signs the body with the same HMAC scheme GitHub uses
(X-Hub-Signature-256), so it goes through the real /webhook code path.

Usage:
    python scripts/send_pr.py <owner/repo> <pr_number> [head_sha]

If head_sha is omitted it's fetched anonymously from the public GitHub API
(works for public repos — the same tokenless read hand-off mode uses).

The webhook secret is read from the process settings (i.e. your .env), never
printed. Nothing is written to GitHub — hand-off mode routes the review back to
you as a link in the dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys

import httpx

from app.config import settings

API = "http://localhost:8000/webhook"


def _resolve_sha(repo: str, pr: int) -> tuple[str, str]:
    """Fetch (head_sha, author) for a public PR, anonymously."""
    url = f"{settings.github_api_url}/repos/{repo}/pulls/{pr}"
    r = httpx.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data["head"]["sha"], (data.get("user") or {}).get("login", "unknown")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    repo = sys.argv[1]
    pr = int(sys.argv[2])
    sha = sys.argv[3] if len(sys.argv) > 3 else None
    author = "unknown"

    if sha is None:
        print(f"Resolving head SHA for {repo}#{pr} (anonymous)…")
        sha, author = _resolve_sha(repo, pr)
        print(f"  head_sha={sha[:12]}  author={author}")

    payload = {
        "action": "opened",
        "repository": {"full_name": repo},
        "pull_request": {
            "number": pr,
            "head": {"sha": sha},
            "user": {"login": author},
        },
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    resp = httpx.post(
        API,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sig,
        },
        timeout=15,
    )
    print(f"POST /webhook -> {resp.status_code} {resp.text}")
    print("\nWatch the dashboard (Review Queue) — the worker fetches the PR,")
    print("runs the review, and queues a hand-off link. Nothing is written to GitHub.")
    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
