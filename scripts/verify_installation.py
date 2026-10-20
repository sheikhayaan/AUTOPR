#!/usr/bin/env python
"""Prove this deployment can act as its GitHub App — in one command.

The gap this closes: the suite proves the App *protocol* against a mock, and
`tests/test_github_live.py` proves the plain-REST reader against real GitHub, but
neither can tell you whether **your** App, **your** private key and **your**
installation actually work together. That answer needs credentials, so it cannot
be a unit test — it is this script, which a human with the App configured runs
once and reads top to bottom.

Every check is a real call to the GitHub API with the real credential. Nothing is
simulated, and a mock is never substituted (the `--transport` hook exists only so
the script's own logic can be tested).

    # the read-only path — safe against any repo
    python scripts/verify_installation.py --installation 12345 \\
        --repo octocat/hello-world

    # also post a comment, proving the write path (needs write permission)
    python scripts/verify_installation.py ... --comment 42 --yes

Exit codes: 0 = every check passed, 1 = at least one FAIL, 2 = config missing.
SKIP means "you did not ask for it"; MANUAL means "only GitHub can trigger this".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import jwt as pyjwt  # noqa: E402

from app.config import settings  # noqa: E402
from app.github_app import AppAuth, GitHubAppError, normalise_private_key  # noqa: E402
from app.http import build_client  # noqa: E402

PASS, FAIL, SKIP, MANUAL = "PASS", "FAIL", "SKIP", "MANUAL"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    # `failed` rather than a bare string compare, so callers never accidentally
    # treat MANUAL (a thing a human still has to do) as a pass.
    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> Check:
        check = Check(name, status, detail)
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=10)
        lines = [f"{'CHECK'.ljust(width)}  STATUS  DETAIL"]
        for c in self.checks:
            lines.append(f"{c.name.ljust(width)}  {c.status:<6}  {c.detail}")
        return "\n".join(lines)


# --- individual checks -------------------------------------------------------


def check_config(report: Report) -> None:
    """The private key must actually parse — a truncated PEM fails here, not at 3am."""
    app_id = settings.github_app_id
    key = (settings.github_app_private_key or "").strip()
    if not app_id or not key:
        report.add(
            "config",
            FAIL,
            "set AUTOPR_GITHUB_APP_ID and AUTOPR_GITHUB_APP_PRIVATE_KEY (.env or environment)",
        )
        return
    try:
        from cryptography.hazmat.primitives import serialization

        serialization.load_pem_private_key(normalise_private_key(key).encode(), password=None)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the finding
        report.add("config", FAIL, f"private key does not parse: {exc}")
        return
    mode = "multi-tenant (App)" if settings.multi_tenant_enabled else "NOT multi-tenant"
    report.add("config", PASS, f"app_id={app_id} | key parses | mode={mode}")


def check_jwt(report: Report, auth: AppAuth, private_key: str) -> None:
    """Verify our own JWT the way GitHub will: signature, `iss`, and the 10-minute cap."""
    token = auth.jwt()
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        normalise_private_key(private_key).encode(), password=None
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    try:
        claims = pyjwt.decode(
            token, public_pem, algorithms=["RS256"], options={"verify_exp": False}
        )
    except Exception as exc:  # noqa: BLE001
        report.add("jwt", FAIL, f"signature does not verify with the configured public key: {exc}")
        return

    now = int(time.time())
    problems = []
    if int(claims.get("iss", -1)) != int(settings.github_app_id or -1):
        problems.append(f"iss={claims.get('iss')} != app_id={settings.github_app_id}")
    lifetime = int(claims.get("exp", 0)) - int(claims.get("iat", now))
    if lifetime > 600:
        problems.append(f"lifetime {lifetime}s exceeds GitHub's 600s limit")
    if int(claims.get("iat", now)) > now + 60:
        problems.append("iat is in the future (clock skew > 60s)")
    if problems:
        report.add("jwt", FAIL, "; ".join(problems))
        return
    report.add("jwt", PASS, f"RS256 ok | iss={claims['iss']} | lifetime={lifetime}s")


def _get_json(client: httpx.Client, url: str, token: str) -> tuple[int, Any]:
    resp = client.get(url, headers={**API_HEADERS, "Authorization": f"Bearer {token}"})
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:200]


def check_app_identity(report: Report, client: httpx.Client, jwt_token: str) -> None:
    """`GET /app` with the JWT — the cheapest proof GitHub accepts us as the App."""
    status, body = _get_json(client, f"{settings.github_api_url}/app", jwt_token)
    if status == 401:
        report.add("app_identity", FAIL, "GitHub rejected the JWT (401): wrong key or app id")
        return
    if status >= 300:
        report.add("app_identity", FAIL, f"GET /app -> HTTP {status}: {body}")
        return
    got_id = body.get("id") if isinstance(body, dict) else None
    slug = body.get("slug") if isinstance(body, dict) else None
    if got_id is not None and int(got_id) != int(settings.github_app_id or -1):
        report.add(
            "app_identity", FAIL, f"GitHub says app {got_id}, config says {settings.github_app_id}"
        )
        return
    report.add("app_identity", PASS, f"slug={slug} id={got_id}")


def check_installations(
    report: Report, client: httpx.Client, jwt_token: str, wanted: int | None
) -> None:
    """The App must be installed somewhere; the id we will use must be among them."""
    status, body = _get_json(client, f"{settings.github_api_url}/app/installations", jwt_token)
    if status >= 300:
        report.add("installations", FAIL, f"GET /app/installations -> HTTP {status}: {body}")
        return
    items = body if isinstance(body, list) else []
    ids = [int(i["id"]) for i in items if isinstance(i, dict) and "id" in i]
    if not ids:
        report.add(
            "installations",
            FAIL,
            "the App has no installations — install it: "
            f"{settings.install_url or 'the App settings page'}",
        )
        return
    if wanted is not None and wanted not in ids:
        report.add(
            "installations",
            FAIL,
            f"installation {wanted} not in {ids} (installed: {ids})",
        )
        return
    target = wanted if wanted is not None else ids[0]
    report.add("installations", PASS, f"using installation {target} of {ids}")


def check_installation_token(report: Report, auth: AppAuth, installation_id: int) -> str | None:
    """Mint an installation token — the step that turns an App into a tenant."""
    try:
        token = auth.fetch_installation_token(installation_id)
    except GitHubAppError as exc:
        report.add("installation_token", FAIL, str(exc))
        return None
    raw = getattr(token, "token", "")
    expires_at = getattr(token, "expires_at", 0.0)
    if not raw.startswith("ghs_"):
        report.add(
            "installation_token",
            FAIL,
            f"unexpected token prefix {raw[:4]!r} (an installation token always starts ghs_)",
        )
        return None
    # InstallationToken.expires_at is a *monotonic* deadline, not a wall clock
    # reading; subtracting time.time() would report every fresh token as expired.
    remaining = int(expires_at - time.monotonic())
    if remaining <= 0:
        report.add("installation_token", FAIL, f"token already expired ({remaining}s)")
        return None
    report.add("installation_token", PASS, f"ghs_… len={len(raw)} | expires in {remaining}s")
    return raw


def check_repo_access(
    report: Report, client: httpx.Client, token: str, repo: str | None, installation_id: int
) -> bool:
    """The token must be able to see the repo we intend to review."""
    status, body = _get_json(client, f"{settings.github_api_url}/installation/repositories", token)
    if status >= 300:
        report.add("repo_access", FAIL, f"GET /installation/repositories -> HTTP {status}: {body}")
        return False
    repos = [r.get("full_name", "") for r in (body or {}).get("repositories", [])]
    if not repos:
        report.add("repo_access", FAIL, f"installation {installation_id} sees no repositories")
        return False
    if repo is not None and repo.lower() not in {r.lower() for r in repos}:
        report.add(
            "repo_access",
            FAIL,
            f"{repo} is not visible to installation {installation_id}; "
            "grant it in the App settings",
        )
        return False
    shown = ", ".join(repos[:3]) + ("…" if len(repos) > 3 else "")
    report.add("repo_access", PASS, f"{len(repos)} repo(s) visible: {shown}")
    return True


def check_repo_lookup(
    report: Report, auth: AppAuth, repo: str | None, installation_id: int
) -> None:
    """The production authorization path: repo -> installation, answered by GitHub."""
    if repo is None:
        report.add("repo_lookup", SKIP, "pass --repo owner/name to check it")
        return
    try:
        found = auth.repo_installation(repo)
    except GitHubAppError as exc:
        report.add("repo_lookup", FAIL, str(exc))
        return
    if found is None:
        report.add("repo_lookup", FAIL, f"{repo}: the App is not installed on this repo")
        return
    if int(found) != int(installation_id):
        report.add(
            "repo_lookup", FAIL, f"{repo} maps to installation {found}, not {installation_id}"
        )
        return
    report.add("repo_lookup", PASS, f"{repo} -> installation {found}")


def check_permission(
    report: Report, auth: AppAuth, repo: str | None, login: str | None, installation_id: int
) -> None:
    """The approval gate depends on this answer, so read it against the real repo."""
    if repo is None or login is None:
        report.add("permission", SKIP, "pass --repo and --login to check the approval gate")
        return
    try:
        level = auth.collaborator_permission(installation_id, repo, login)
    except GitHubAppError as exc:
        report.add("permission", FAIL, str(exc))
        return
    report.add("permission", PASS, f"{login} on {repo}: {level or 'none'}")


def check_comment(
    report: Report,
    client: httpx.Client,
    token: str,
    repo: str | None,
    pr_number: int | None,
    confirmed: bool,
) -> None:
    """The write path, if asked: post one comment and report the URL."""
    if repo is None or pr_number is None:
        report.add("comment", SKIP, "pass --comment N --repo owner/name to post a real comment")
        return
    body = "AUTOPR installation check: this comment was posted by scripts/verify_installation.py."
    if not confirmed:
        report.add(
            "comment",
            SKIP,
            f"would POST {settings.github_api_url}/repos/{repo}/issues/{pr_number}/comments "
            "(re-run with --yes to actually post)",
        )
        return
    resp = client.post(
        f"{settings.github_api_url}/repos/{repo}/issues/{pr_number}/comments",
        headers={**API_HEADERS, "Authorization": f"Bearer {token}"},
        json={"body": body},
    )
    if resp.status_code >= 300:
        report.add("comment", FAIL, f"HTTP {resp.status_code}: {resp.text[:200]}")
        return
    url = resp.json().get("html_url", "")
    report.add("comment", PASS, f"posted: {url}")


def check_webhook(report: Report, base_url: str | None) -> None:
    """Deliveries come from GitHub, so this cannot be self-triggered — say so plainly."""
    if base_url:
        try:
            resp = httpx.get(f"{base_url.rstrip('/')}/healthz", timeout=10)
            reachable = f"GET {base_url}/healthz -> HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            reachable = f"GET {base_url}/healthz failed: {exc!r}"
        report.add("webhook_endpoint", PASS if "200" in reachable else FAIL, reachable)
    report.add(
        "webhook_delivery",
        MANUAL,
        "GitHub App -> Advanced -> Recent Deliveries -> Redeliver the Ping, and confirm "
        "/webhook returned 2xx (only GitHub can trigger this)",
    )


# --- driver ------------------------------------------------------------------


def verify(
    installation_id: int | None = None,
    repo: str | None = None,
    login: str | None = None,
    pr_number: int | None = None,
    confirmed: bool = False,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Report:
    """Run every check and return the report (no printing, no exiting)."""
    report = Report()
    check_config(report)
    if report.failures:
        return report

    private_key = settings.github_app_private_key
    auth = AppAuth(
        app_id=int(settings.github_app_id or 0),
        private_key=private_key,
        transport=transport,
    )
    client = build_client(transport)
    try:
        check_jwt(report, auth, private_key)
        check_app_identity(report, client, auth.jwt())
        check_installations(report, client, auth.jwt(), installation_id)
        target = installation_id
        if target is None:
            status, body = _get_json(
                client, f"{settings.github_api_url}/app/installations", auth.jwt()
            )
            if isinstance(body, list) and body:
                target = int(body[0]["id"])
        if target is None:
            report.add("installation_token", SKIP, "no installation to mint a token for")
        else:
            token = check_installation_token(report, auth, target)
            if token:
                check_repo_access(report, client, token, repo, target)
                check_repo_lookup(report, auth, repo, target)
                check_permission(report, auth, repo, login, target)
                check_comment(report, client, token, repo, pr_number, confirmed)
        check_webhook(report, base_url)
    finally:
        auth.close()
        client.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--installation", type=int, help="installation id to verify")
    parser.add_argument("--repo", help="owner/name the App should be able to review")
    parser.add_argument("--login", help="a GitHub login whose permission level should be read")
    parser.add_argument("--comment", type=int, metavar="PR", help="post one comment to this PR")
    parser.add_argument("--yes", action="store_true", help="actually post the comment")
    parser.add_argument("--base-url", help="this deployment's public URL, to probe /healthz")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = verify(
        installation_id=args.installation,
        repo=args.repo,
        login=args.login,
        pr_number=args.comment,
        confirmed=args.yes,
        base_url=args.base_url,
    )
    if args.json:
        print(json.dumps([c.__dict__ for c in report.checks], indent=2))
    else:
        print(report.render())
        pending = [c.name for c in report.checks if c.status == MANUAL]
        if pending:
            print(f"\nStill needs a human: {', '.join(pending)}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
