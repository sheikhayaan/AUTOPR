"""Tenancy: one GitHub App installation == one customer.

This module is the only place that reads or writes the `installations` table, so
"who is this delivery for?" has exactly one answer everywhere in the codebase.

Three rules the functions here enforce, each of which is a bug if broken:

* **An inactive installation does no work.** Suspended (reversible: billing,
  abuse review) and deleted (final: uninstalled) both stop processing, but are
  stored separately because an operator staring at a 429 needs to know whether
  the tenant can come back.
* **Onboarding never depends on webhook ordering.** GitHub may deliver a
  `pull_request` before the `installation` event, and it may never deliver
  `installation` at all if our endpoint was down when the App was installed.
  Every delivery carries the same installation object, so a PR delivery enrols
  the tenant on the spot. Missing-onboarding is therefore not a failure mode.
* **Enrolment is not authorisation.** A row appearing here only means "this
  installation exists". Authority is GitHub's — the installation token scopes the
  calls, and `collaborator_permission` decides who may approve. We never maintain
  our own ACL, because a stale local ACL is an outage that looks like a security
  bug.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Installation

log = structlog.get_logger(__name__)

# Installation actions that mean "create or refresh", "pause" and "gone".
_ACTIVE_ACTIONS = frozenset({"created", "new_permissions_accepted", "unsuspend"})


def _as_int(raw: object) -> int | None:
    """GitHub sometimes sends an id as a string; anything unusable is just absent."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def installation_fields(installation: dict) -> dict:
    """Flatten GitHub's installation object into column values.

    Tolerant on purpose: this runs inside a webhook handler, and a payload shape
    change must degrade to "tenant recorded with less metadata", never to a 500
    that makes GitHub disable the webhook for repeated failures.
    """
    account = installation.get("account") or {}
    return {
        "installation_id": _as_int(installation.get("id")),
        "account_login": str(account.get("login", "") or ""),
        "account_type": str(account.get("type", "") or ""),
        "account_id": _as_int(account.get("id")),
        "target_type": str(installation.get("target_type", "") or ""),
        "repository_selection": str(installation.get("repository_selection", "") or ""),
    }


def get(db: Session, installation_id: int) -> Installation | None:
    """The row for one installation, active or not."""
    return db.execute(
        select(Installation).where(Installation.installation_id == installation_id)
    ).scalar_one_or_none()


def get_active(db: Session, installation_id: int) -> Installation | None:
    """The row only if work is permitted. Callers must treat None as "refuse"."""
    row = get(db, installation_id)
    if row is None or not row.is_active:
        return None
    return row


def upsert(db: Session, installation: dict) -> Installation | None:
    """Create or refresh a tenant row. Returns None if the payload has no id.

    Re-running is a no-op apart from refreshing account metadata, so every
    delivery can call it without thinking about idempotency.
    """
    fields = installation_fields(installation)
    if fields["installation_id"] is None:
        return None
    row = get(db, fields["installation_id"])
    if row is None:
        row = Installation(**fields)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent delivery enrolled the same installation first. That is
            # the outcome we wanted, not an error.
            db.rollback()
            return get(db, fields["installation_id"])  # type: ignore[arg-type]
        log.info(
            "tenancy.installation_registered",
            installation_id=fields["installation_id"],
            account=fields["account_login"],
        )
        return row

    # Refresh metadata but never resurrect a deleted installation: an uninstall
    # followed by a stray delivery must not re-open the door.
    if row.deleted_at is not None:
        return row
    changed = False
    for key in (
        "account_login",
        "account_type",
        "account_id",
        "target_type",
        "repository_selection",
    ):
        if getattr(row, key) != fields[key]:
            setattr(row, key, fields[key])
            changed = True
    if changed:
        db.commit()
    return row


def apply_event(db: Session, action: str, payload: dict) -> str:
    """Apply an `installation` webhook. Returns a short status for logging.

    `deleted` is terminal: the row keeps its history (so an operator can still
    explain old decisions) but loses the ability to mint tokens or have work
    processed. Purging rows on uninstall would destroy exactly the audit trail a
    "who did this?" question needs.
    """
    installation = payload.get("installation") or {}
    row = upsert(db, installation)
    if row is None:
        log.warning("tenancy.event_without_installation", action=action)
        return "ignored"

    if action in _ACTIVE_ACTIONS:
        if row.deleted_at is not None:
            return "deleted_installation_ignored"
        if row.suspended_at is not None:
            row.suspended_at = None
            db.commit()
        return "active"
    if action == "suspend":
        row.suspended_at = row.suspended_at or datetime.now(UTC)
        db.commit()
        log.warning(
            "tenancy.installation_suspended",
            installation_id=row.installation_id,
            account=row.account_login,
        )
        return "suspended"
    if action == "deleted":
        row.deleted_at = row.deleted_at or datetime.now(UTC)
        db.commit()
        log.warning(
            "tenancy.installation_deleted",
            installation_id=row.installation_id,
            account=row.account_login,
        )
        return "deleted"
    return "noop"


def apply_repositories_event(db: Session, action: str, payload: dict) -> str:
    """Record repository-selection changes.

    Nothing is enforced here: the installation token already limits which repos
    are reachable, so a stale local list could only produce false refusals. The
    handler exists to keep the log honest about what the tenant changed.
    """
    installation = payload.get("installation") or {}
    row = upsert(db, installation)
    if row is None:
        return "ignored"
    added = [r.get("full_name") for r in (payload.get("repositories_added") or [])]
    removed = [r.get("full_name") for r in (payload.get("repositories_removed") or [])]
    log.info(
        "tenancy.repositories_changed",
        installation_id=row.installation_id,
        action=action,
        added=len(added),
        removed=len(removed),
    )
    return "recorded"
