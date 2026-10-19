"""Cost ceilings for a shared instance.

The webhook is authenticated (HMAC) but not *rate-limited by identity*: on a
public repo a stranger opening a fork PR produces a genuinely signed delivery,
which costs four LLM calls. Per-IP limits do not help — the caller is GitHub, and
GitHub's IP is shared by every tenant. So the bound has to be on the work itself:
N jobs per repo per hour, M jobs per day in total.

These are soft ceilings by design. Two racing deliveries can both read a count of
``limit - 1`` and both proceed, so the true maximum is ``limit + concurrency``.
Making it exact would mean a lock or a conditional UPDATE on a counter row, which
buys nothing: the goal is "a stranger cannot run up an unbounded bill", not
exactly-N accounting. It is the same tradeoff the in-process rate limiter makes,
and it is documented here rather than discovered later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PRJob


def _count_since(db: Session, cutoff: datetime, repo: str | None = None) -> int:
    """Count jobs created since `cutoff`, optionally for one repo."""
    stmt = select(func.count()).select_from(PRJob).where(PRJob.created_at >= cutoff)
    if repo is not None:
        stmt = stmt.where(PRJob.repo == repo)
    return db.execute(stmt).scalar_one()


def quota_reason(db: Session, repo: str) -> str | None:
    """Return why this delivery should be refused, or None if it may proceed.

    Checked *before* the INSERT so a refusal costs no row, no stream message and
    no model call. A quota of 0 disables that dimension, which is what keeps the
    default private install byte-for-byte unchanged.
    """
    now = datetime.now(UTC)
    per_repo = settings.quota_jobs_per_repo_per_hour
    if per_repo > 0 and _count_since(db, now - timedelta(hours=1), repo=repo) >= per_repo:
        return "quota_repo_hourly"
    total = settings.quota_jobs_total_per_day
    if total > 0 and _count_since(db, now - timedelta(days=1)) >= total:
        return "quota_instance_daily"
    return None


def dedup_exists(db: Session, dedup_key: str) -> bool:
    """Has this exact delivery already been admitted?

    A redelivery of something already queued spends nothing new, so it must not
    be counted against (or refused by) a quota — that would turn a harmless
    GitHub retry into a 429 and lose the retry semantics the rest of the API
    relies on.
    """
    return (
        db.execute(select(PRJob.id).where(PRJob.dedup_key == dedup_key).limit(1)).first()
        is not None
    )
