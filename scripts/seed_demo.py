"""Seed the local database with realistic demo data for the dashboard.

Run this against the SAME working directory the API server uses, so both point
at the same SQLite file (``./autopr.db`` by default — see
``AUTOPR_DATABASE_URL``):

    ./.venv/Scripts/python.exe scripts/seed_demo.py

It is safe to re-run: it first deletes only the demo rows it owns (every demo
repo is under the ``acme/`` org) and then re-inserts a fresh, consistent set.
Real rows from actual webhooks (any other repo) are never touched.

The data is shaped to exercise every state the UI renders:
  * jobs in each lifecycle state (done / processing / queued / pending / dead),
  * both tracks: PR-review jobs (event=pull_request) and CI-fix jobs
    (event=check_run / workflow_run),
  * a human-review queue with medium- and high-risk PR reviews, a sandbox-verified
    `propose_fix`, and a CI `escalate` all pending,
  * decision history covering executed / rejected / failed outcomes,
so the Overview distributions, the Review Queue, and the Jobs table all light up.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as `python scripts/seed_demo.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    DecisionStatus,
    JobResult,
    JobStatus,
    PRJob,
    ReviewDecision,
)

DEMO_ORG = "acme/"
NOW = datetime.now(timezone.utc)


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


# --- Comment bodies (what CommentPreview renders) ---------------------------
# Written the way policy.py emits: markdown with headings, bullets, and diff
# fences. These are the payloads a maintainer would actually see.

BODY_SQLI = """\
## AutoPR review — `auth-service`

I reviewed the 2 changed files on this PR and found **one issue worth a human's
attention** before merge.

### Possible SQL injection in `lookup_token`

The token lookup interpolates the raw `token` value straight into the query:

```diff
-    cur.execute(f"SELECT user_id FROM sessions WHERE token = '{token}'")
+    cur.execute("SELECT user_id FROM sessions WHERE token = ?", (token,))
```

An attacker who controls `token` could read or drop the `sessions` table.
Parameterizing the query closes this.

### Notes
- The remaining diff (logging in `middleware.py`) looks fine.
- No test currently covers a malformed token — worth adding one.

_Risk assessed as **medium** → routed for human approval before posting._
"""

BODY_ESCALATE = """\
## AutoPR review — `mobile-app`

This PR touches **7 files across the auth flow** (login, refresh, logout). The
change is plausibly correct, but its blast radius is large enough that I'm
escalating rather than auto-commenting.

<details>
<summary>Why this was escalated</summary>

- Modifies shared session handling used by three entry points.
- Removes a retry guard around token refresh:

```diff
-        if attempts < MAX_RETRIES:
-            return self._refresh(token)
```

- No integration test exercises the logout → re-login sequence.

</details>

### Checklist for the reviewer
- Confirm the refresh-retry removal is intentional.
- Verify logout clears the keychain entry on iOS.

_Risk assessed as **high** → human approval required._
"""

BODY_LOW_EXECUTED = """\
## AutoPR review — `web-dashboard`

Small, self-contained change — looks good to merge.

- Renames `fetchUser` → `loadUser` consistently across the module.
- No behavior change; the types line up.

_Low risk → auto-posted._
"""

BODY_TRIVIAL_EXECUTED = """\
## AutoPR review — `payments-api`

Docs-only change. Nothing to flag.

- Fixes a typo in the `/charges` endpoint docstring.

_Trivial risk → auto-posted._
"""

BODY_REJECTED = """\
## AutoPR review — `data-pipeline`

I flagged the removal of the null check in `transform()`:

```diff
-    if record is None:
-        continue
```

_Risk assessed as **medium** → routed for approval._
"""

BODY_FAILED = """\
## AutoPR review — `legacy-monolith`

Suggested tightening an overly-broad `except Exception:` in the billing job.

_Risk assessed as **medium** → routed for approval._
"""


# --- CI-fix track bodies (Phase 5) ------------------------------------------
# These mirror what policy._render_fix_body / _render_escalation_body emit for
# the CI-failure track: a sandbox-verified proposed fix, and an honest "couldn't
# auto-fix" escalation with the sandbox evidence.

BODY_PROPOSE_FIX = """\
## 🤖 AutoPR proposed fix (sandbox-verified)

**Failure type:** `test`

**Diagnosis:** `test_apply_discount` expects a `Decimal` but `apply_discount`
returns a float, so the equality assertion fails on `19.99 != Decimal('19.99')`.

This patch was applied to a snapshot of the repo and the matching check passed
inside an isolated sandbox. A maintainer must approve before it is applied to the
real branch.

```diff
--- a/checkout/pricing.py
+++ b/checkout/pricing.py
@@ -14,7 +14,7 @@ def apply_discount(price, pct):
-    return float(price) * (1 - pct / 100)
+    return (Decimal(str(price)) * (1 - Decimal(pct) / 100)).quantize(Decimal("0.01"))
```

_The failing `pytest` run was re-run against the patched snapshot and passed
(exit 0). Verified fixes change code, so this is **always** human-gated._
"""

BODY_CI_ESCALATE = """\
## 🤖 AutoPR could not auto-fix this — human needed

**Failure type:** `dependency`

**Diagnosis:** The `notifications` workflow failed resolving `aiosmtplib>=3.0`
against a pinned `anyio==3.7` — a version conflict, not a code bug.

**Why not auto-fixed:** `not_verifiable`

<details><summary>Sandbox output</summary>

```
ERROR: Cannot install aiosmtplib==3.0.1 because these package versions have
conflicting dependencies. anyio 3.7.0 is incompatible with anyio>=4.0 (aiosmtplib).
```
</details>

_Dependency changes have wide blast radius and can't be verified offline
(`--network none`), so this is escalated rather than auto-fixed._
"""


# --- Row builders -----------------------------------------------------------

def job(repo, pr, sha, author, status, *, attempts=1, event="pull_request",
        summary=None, last_error=None, created):
    return {
        "row": PRJob(
            dedup_key=f"{repo}|{pr}|{sha}|{event}",
            repo=repo,
            pr_number=pr,
            commit_sha=sha,
            author=author,
            event=event,
            status=status,
            attempts=attempts,
            last_error=last_error,
            created_at=created,
            updated_at=created,
        ),
        "summary": summary,
    }


def decision(repo, pr, sha, action, risk, reason, title, body, status, *,
             result_url=None, last_error=None, created):
    return ReviewDecision(
        dedup_key=f"{repo}|{pr}|{sha}|{action}",
        repo=repo,
        pr_number=pr,
        commit_sha=sha,
        action=action,
        risk=risk,
        reason=reason,
        title=title,
        body=body,
        status=status,
        result_url=result_url,
        last_error=last_error,
        created_at=created,
        updated_at=created,
    )


JOBS = [
    job("acme/auth-service", 77, "9f3c1a2b4d5e6f70", "priya-dev",
        JobStatus.PROCESSING, created=ago(minutes=1),
        summary=None),
    job("acme/data-pipeline", 231, "1b2c3d4e5f60718a", "sam-ml",
        JobStatus.QUEUED, created=ago(minutes=2)),
    job("acme/mobile-app", 305, "abcd1234ef567890", "lena-mobile",
        JobStatus.DONE, created=ago(minutes=6),
        summary="review: risk=high action=escalate (queued for approval)"),
    job("acme/web-dashboard", 1290, "77aa88bb99cc00dd", "kenji-fe",
        JobStatus.DONE, created=ago(minutes=14),
        summary="review: risk=low action=comment_review (auto-posted)"),
    job("acme/payments-api", 482, "deadbeef0badc0de", "marco-pay",
        JobStatus.DONE, created=ago(minutes=27),
        summary="review: risk=trivial action=comment_review (auto-posted)"),
    job("acme/infra-terraform", 58, "5e5e5e5e6f6f6f6f", "ops-bot",
        JobStatus.PENDING, created=ago(minutes=1, seconds=20)),
    job("acme/legacy-monolith", 9001, "0f0f0f0f1a1a1a1a", "contractor-x",
        JobStatus.DEAD, attempts=5, created=ago(hours=1, minutes=5),
        last_error="GitHub read failed: 404 Not Found (PR may be closed or private)"),
    # CI-fix track (Phase 5): a check_run failure whose fix was sandbox-verified,
    # and a workflow_run failure that couldn't be auto-fixed (dependency conflict).
    job("acme/checkout-service", 512, "c1c2c3c4d5d6e7f8", "github-actions",
        JobStatus.DONE, event="check_run", created=ago(minutes=4),
        summary="ci-fix pr acme/checkout-service#512: failure=test verified=True action=propose_fix"),
    job("acme/notifications", 143, "a9b8c7d6e5f40312", "github-actions",
        JobStatus.DONE, event="workflow_run", created=ago(minutes=9),
        summary="ci-fix pr acme/notifications#143: failure=dependency verified=False action=escalate"),
]

DECISIONS = [
    # Pending — these populate the Review Queue (oldest first there).
    # A sandbox-verified fix is ALWAYS human-gated (never auto-promoted).
    decision("acme/checkout-service", 512, "c1c2c3c4d5d6e7f8", "propose_fix",
             "medium", "verified_fix_needs_promotion",
             "Verified fix ready for maintainer approval",
             BODY_PROPOSE_FIX, DecisionStatus.PENDING, created=ago(minutes=4)),
    decision("acme/notifications", 143, "a9b8c7d6e5f40312", "escalate",
             "medium", "not_verifiable",
             "CI failure needs human attention",
             BODY_CI_ESCALATE, DecisionStatus.PENDING, created=ago(minutes=9)),
    decision("acme/mobile-app", 305, "abcd1234ef567890", "escalate", "high",
             "elevated_risk_needs_approval",
             "Broad refactor touches the auth flow — needs human review",
             BODY_ESCALATE, DecisionStatus.PENDING, created=ago(minutes=6)),
    decision("acme/auth-service", 77, "9f3c1a2b4d5e6f70", "comment_review",
             "medium", "elevated_risk_needs_approval",
             "Possible SQL injection in token lookup",
             BODY_SQLI, DecisionStatus.PENDING, created=ago(minutes=1)),
    # History — executed / rejected / failed.
    decision("acme/web-dashboard", 1290, "77aa88bb99cc00dd", "comment_review",
             "low", "low_risk_auto", "Consistent rename, no behavior change",
             BODY_LOW_EXECUTED, DecisionStatus.EXECUTED,
             result_url="https://github.com/acme/web-dashboard/pull/1290#issuecomment-2001",
             created=ago(minutes=14)),
    decision("acme/payments-api", 482, "deadbeef0badc0de", "comment_review",
             "trivial", "low_risk_auto", "Docstring typo fix",
             BODY_TRIVIAL_EXECUTED, DecisionStatus.EXECUTED,
             result_url="https://github.com/acme/payments-api/pull/482#issuecomment-1990",
             created=ago(minutes=27)),
    decision("acme/data-pipeline", 231, "1b2c3d4e5f60718a", "comment_review",
             "medium", "elevated_risk_needs_approval",
             "Removed null guard in transform()",
             BODY_REJECTED, DecisionStatus.REJECTED, created=ago(minutes=40)),
    decision("acme/legacy-monolith", 9001, "0f0f0f0f1a1a1a1a", "comment_review",
             "medium", "elevated_risk_needs_approval",
             "Overly-broad exception handler in billing job",
             BODY_FAILED, DecisionStatus.FAILED,
             last_error="GitHub API 403: resource not accessible by integration",
             created=ago(hours=1)),
]


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Idempotent: remove only demo rows (acme/*) before reseeding. Real
        # rows from actual webhooks are left untouched.
        demo_job_ids = [
            r[0]
            for r in db.execute(
                PRJob.__table__.select().with_only_columns(PRJob.id).where(
                    PRJob.repo.like(f"{DEMO_ORG}%")
                )
            ).all()
        ]
        if demo_job_ids:
            db.execute(delete(JobResult).where(JobResult.job_id.in_(demo_job_ids)))
        db.execute(delete(PRJob).where(PRJob.repo.like(f"{DEMO_ORG}%")))
        db.execute(delete(ReviewDecision).where(ReviewDecision.repo.like(f"{DEMO_ORG}%")))
        db.commit()

        for spec in JOBS:
            row = spec["row"]
            db.add(row)
            db.flush()  # assign row.id
            if spec["summary"] is not None:
                db.add(JobResult(job_id=row.id, summary=spec["summary"], created_at=row.created_at))
        for d in DECISIONS:
            db.add(d)
        db.commit()

        n_jobs = db.query(PRJob).filter(PRJob.repo.like(f"{DEMO_ORG}%")).count()
        n_dec = db.query(ReviewDecision).filter(ReviewDecision.repo.like(f"{DEMO_ORG}%")).count()
        n_pending = (
            db.query(ReviewDecision)
            .filter(ReviewDecision.status == DecisionStatus.PENDING)
            .count()
        )
        print(f"Seeded {n_jobs} jobs and {n_dec} review decisions ({n_pending} pending).")
        print("Start the API (uvicorn app.main:app --reload) and the dashboard to see it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
