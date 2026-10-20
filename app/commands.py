"""Approval from inside the PR thread — the only approval path a stranger can use.

Why this exists at all: a stranger who installs the App has no dashboard session
and no operator token, and we will never hand one out. The one channel they
already have, already authenticated, is a comment on their own pull request. So
the approval gate moves there:

    /autopr approve 42

and three properties fall out of that choice, all of which are why it is safe:

1. **Identity is GitHub's.** The commenter's login comes from a payload GitHub
   signed. No accounts, no passwords, no session for us to get wrong.
2. **Authority is the repository's.** Before acting we ask GitHub
   `GET /repos/{repo}/collaborators/{login}/permission` and require
   admin/maintain/write. AutoPR stores no maintainer list, so it can never hold a
   stale ACL that disagrees with the repo. Revoke someone on GitHub and their
   ability to approve here disappears with it.
3. **It cannot be replayed into another tenant.** The credential used to *act* is
   the installation token of the installation that delivered the comment, and the
   decision must belong to that same installation and that same repo. A decision
   id from someone else's queue is not addressable.

Rejected alternative — accepting `/autopr approve` from *anyone* who can comment
(a public repo allows drive-by comments): that would let a random commenter fire
a queued code-changing action on a repo they have no rights in. The permission
check is not optional.

Failures are silent by design: an unauthorised commenter gets no reply, so the
thread cannot be used to probe which decision ids exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.github_app import AppAuth, GitHubAppError
from app.models import DecisionStatus, ReviewDecision
from app.observability import current_correlation_id
from app.routing import store
from app.routing.github import FakeGitHubClient
from app.routing.router import execute_decision

log = structlog.get_logger(__name__)

# Who may fire a queued action. GitHub reports "admin" | "maintain" | "write" |
# "triage" | "read" | "none"; older payloads/enterprise say "push" for write.
# Triage is excluded on purpose: it can manage issues but cannot merge, so it
# must not be able to authorise a comment on someone's behalf either.
APPROVER_PERMISSIONS = frozenset({"admin", "maintain", "write", "push"})

# `/autopr`, `/autopr-bot`, `/AutoPR` — case-insensitive, optional bot suffix.
_PREFIX = re.compile(r"^/(?:auto-?pr)(?:-bot)?\b", re.IGNORECASE)
_VERB = re.compile(r"^(approve|reject|status|help)\b", re.IGNORECASE)
_ID = re.compile(r"#?(\d{1,12})")


@dataclass(frozen=True)
class Command:
    """A parsed instruction from a PR comment."""

    verb: str  # approve | reject | status | help
    decision_id: int | None


def parse_command(body: str) -> Command | None:
    """Parse the first meaningful line of a comment, or None.

    First line only: a reviewer often quotes `/autopr approve 42` while asking
    "does this do what I think?", and acting on a quoted command inside prose is
    how a helper becomes a hazard.
    """
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _PREFIX.match(line)
        if not match:
            return None  # first content line is prose: not addressed to us
        rest = line[match.end() :].strip()
        if not rest:
            return Command(verb="help", decision_id=None)
        verb_match = _VERB.match(rest)
        if not verb_match:
            return None
        verb = verb_match.group(1).lower()
        if verb in ("status", "help"):
            return Command(verb=verb, decision_id=None)
        id_match = _ID.search(rest[verb_match.end() :])
        if not id_match:
            return Command(verb=verb, decision_id=None)
        return Command(verb=verb, decision_id=int(id_match.group(1)))
    return None


def _decision_for(
    db: Session, decision_id: int, repo: str, installation_id: int
) -> ReviewDecision | None:
    """Fetch a decision that this exact tenant+repo is allowed to address."""
    row = db.execute(
        select(ReviewDecision).where(ReviewDecision.id == decision_id)
    ).scalar_one_or_none()
    if row is None or row.repo.lower() != repo.lower():
        return None
    if row.installation_id != installation_id:
        # Operator-era rows (installation_id NULL) are deliberately not
        # approvable from a tenant's thread: the tenant did not create them.
        return None
    return row


def _pending_for_repo(db: Session, repo: str, installation_id: int) -> list[ReviewDecision]:
    return list(
        db.execute(
            select(ReviewDecision)
            .where(
                ReviewDecision.status == DecisionStatus.PENDING,
                ReviewDecision.installation_id == installation_id,
                ReviewDecision.repo == repo,
            )
            .order_by(ReviewDecision.created_at.asc())
            .limit(10)
        ).scalars()
    )


def _reply(client, repo: str, pr_number: int, body: str) -> dict:
    """Post a comment. A reply failure must not corrupt the action's outcome."""
    result = client.post_issue_comment(repo, pr_number, body)
    if not result.ok:
        log.warning("commands.reply_failed", repo=repo, pr=pr_number, detail=result.detail)
    return {"status": "replied", "url": result.url}


def handle_issue_comment(
    *,
    db: Session,
    payload: dict,
    auth: AppAuth | None,
) -> dict:
    """Handle an `issue_comment` delivery. Never raises: a webhook must 200-ack.

    Returns a dict describing what happened, for logging and for tests.
    """
    if not settings.github_comment_commands:
        return {"status": "ignored", "reason": "comment_commands_disabled"}
    if payload.get("action") != "created":
        return {"status": "ignored", "reason": "not_created"}

    issue = payload.get("issue") or {}
    if not issue.get("pull_request"):
        return {"status": "ignored", "reason": "not_a_pull_request"}
    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    command = parse_command(body)
    if command is None:
        return {"status": "ignored", "reason": "not_a_command"}

    actor_obj = comment.get("user") or {}
    actor = str(actor_obj.get("login") or "")
    if not actor:
        return {"status": "ignored", "reason": "anonymous"}
    # Bots (including ourselves) must never be able to drive the loop, or one
    # comment becomes an infinite conversation between two bots.
    if str(actor_obj.get("type") or "").lower() == "bot":
        return {"status": "ignored", "reason": "bot_comment"}

    repo = str((payload.get("repository") or {}).get("full_name") or "")
    pr_number = issue.get("number")
    installation_id = (payload.get("installation") or {}).get("id")
    if not repo or pr_number is None or installation_id is None:
        return {"status": "ignored", "reason": "incomplete_delivery"}

    if auth is None:
        return {"status": "ignored", "reason": "no_app_credentials"}

    from app.tenancy import get_active

    if get_active(db, int(installation_id)) is None:
        log.warning(
            "commands.inactive_installation",
            installation_id=installation_id,
            repo=repo,
            correlation_id=current_correlation_id(),
        )
        return {"status": "ignored", "reason": "installation_inactive"}

    # --- authority check (GitHub's answer, not ours) ---
    # Ordered before building a client on purpose: `auth.client()` mints (or
    # fetches) an installation token, and a refused commenter must not cost us an
    # API call, let alone a credential in memory.
    try:
        permission = auth.collaborator_permission(int(installation_id), repo, actor)
    except GitHubAppError as exc:
        log.error("commands.permission_check_failed", repo=repo, actor=actor, error=str(exc))
        return {"status": "error", "reason": "permission_check_failed"}

    if permission not in APPROVER_PERMISSIONS:
        # Silent: replying would let anyone probe for decision ids.
        log.warning(
            "commands.unauthorised_actor",
            repo=repo,
            pr=int(pr_number),
            actor=actor,
            permission=permission,
            verb=command.verb,
            correlation_id=current_correlation_id(),
        )
        return {"status": "ignored", "reason": "insufficient_permission"}

    client = auth.client(int(installation_id))

    if command.verb == "help":
        return _reply(
            client,
            repo,
            int(pr_number),
            _help_text(),
        )
    if command.verb == "status":
        return _reply(client, repo, int(pr_number), _status_text(db, repo, int(installation_id)))

    if command.decision_id is None:
        return _reply(
            client,
            repo,
            int(pr_number),
            f"Usage: `/autopr {command.verb} <id>` — see `/autopr status` for ids.",
        )

    row = _decision_for(db, command.decision_id, repo, int(installation_id))
    if row is None:
        # Covers "no such decision" and "belongs to another tenant" alike; the
        # reply must not distinguish them.
        return _reply(
            client,
            repo,
            int(pr_number),
            f"No decision `#{command.decision_id}` is awaiting review on this repository.",
        )

    if command.verb == "reject":
        return _reject(db, client, row, repo, int(pr_number), actor)

    return _approve(db, client, row, repo, int(pr_number), actor, int(installation_id))


def _approve(
    db: Session,
    client,
    row: ReviewDecision,
    repo: str,
    pr_number: int,
    actor: str,
    installation_id: int,
) -> dict:
    """Execute a queued action after a permission-checked comment.

    Reuses the same compare-and-set the ops API uses, so a comment and a
    dashboard click racing each other still fire exactly one action.
    """
    claimed, outcome = store.claim_approval(db, row.id)
    if claimed is None:
        return {"status": "ignored", "reason": "decision_missing"}
    if outcome == "already_executed":
        return _reply(client, repo, pr_number, f"`#{row.id}` was already executed.")
    if outcome == "rejected":
        return _reply(client, repo, pr_number, f"`#{row.id}` was rejected; nothing to do.")
    if outcome == "in_progress":
        return _reply(client, repo, pr_number, f"`#{row.id}` is being executed right now.")

    state = {"repo": claimed.repo, "pr_number": claimed.pr_number, "commit_sha": claimed.commit_sha}
    decision = {"action": claimed.action, "body": claimed.body, "title": claimed.title}
    result = execute_decision(client, decision, state)
    if result.ok:
        store.mark_executed(db, claimed, result.url)
        log.info(
            "audit.review_approved",
            decision_id=claimed.id,
            action=claimed.action,
            risk=claimed.risk,
            repo=claimed.repo,
            pr_number=claimed.pr_number,
            actor=actor,
            installation_id=installation_id,
            source="pr_comment",
            outcome="executed",
            result_url=result.url,
        )
        return {
            "status": "executed",
            "decision_id": claimed.id,
            "url": result.url,
            "actor": actor,
        }

    store.mark_failed(db, claimed, result.detail)
    log.warning(
        "audit.review_approve_failed",
        decision_id=claimed.id,
        repo=claimed.repo,
        pr_number=claimed.pr_number,
        actor=actor,
        source="pr_comment",
        detail=result.detail,
    )
    # Say so in the thread: a human who believes it worked must not be left
    # assuming the comment was posted.
    _reply(client, repo, pr_number, f"Could not execute `#{claimed.id}`: {result.detail}")
    return {"status": "action_failed", "decision_id": claimed.id, "detail": result.detail}


def _reject(
    db: Session, client, row: ReviewDecision, repo: str, pr_number: int, actor: str
) -> dict:
    claimed, outcome = store.reject_pending(db, row.id)
    if claimed is None:
        return {"status": "ignored", "reason": "decision_missing"}
    if outcome == "already_executed":
        return _reply(client, repo, pr_number, f"`#{row.id}` already executed; cannot reject.")
    if outcome == "in_progress":
        return _reply(client, repo, pr_number, f"`#{row.id}` is executing; cannot reject.")
    log.info(
        "audit.review_rejected",
        decision_id=claimed.id,
        repo=claimed.repo,
        pr_number=claimed.pr_number,
        actor=actor,
        source="pr_comment",
        outcome="rejected",
    )
    return {"status": "rejected", "decision_id": claimed.id, "actor": actor}


def _status_text(db: Session, repo: str, installation_id: int) -> str:
    pending = _pending_for_repo(db, repo, installation_id)
    if not pending:
        return "Nothing is waiting for review on this repository."
    lines = [f"**{len(pending)} item(s) waiting for review**", ""]
    for row in pending:
        lines.append(
            f"- `#{row.id}` **{row.action}** · risk `{row.risk}` · {row.title or 'no title'}"
        )
    lines += ["", "Approve with `/autopr approve <id>`, decline with `/autopr reject <id>`."]
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "**AutoPR commands**\n\n"
        "- `/autopr status` — what is waiting for review on this repository\n"
        "- `/autopr approve <id>` — carry out a queued action\n"
        "- `/autopr reject <id>` — decline it (no GitHub action is taken)\n\n"
        "Only people with write access or above can approve. Code changes are never "
        "applied automatically: they wait here for a human."
    )


def noop_client():
    """The dry-run client, exposed for callers that must not touch GitHub."""
    return FakeGitHubClient()
