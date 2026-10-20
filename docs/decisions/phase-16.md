# Phase 16 — Multi-tenant GitHub App: strangers can install this

**Status:** accepted · **Date:** 2026-10-19 · **Supersedes:** nothing · **Depends on:** phase 13
(hand-off mode), phase 15 (public mode)

## Context

Phases 13 and 15 made AutoPR safe to *show*: hand-off mode removed automatic writes, and
public mode turned the instance into a read-only demo. Neither makes it usable by a stranger.
Every credential in the system was singular — one `AUTOPR_GITHUB_TOKEN`, one webhook secret,
one approval token, one repository allowlist — which is exactly right for a bot on your own
repos and structurally wrong for a bot someone installs on theirs.

The gap was not "authentication". It was **tenancy**: there was no object in the schema that
could own a piece of work, so there was no way to say "this review belongs to that customer",
no way to bill a suspension, and no way to stop one tenant's queue from being another's.

Two half-measures were considered and rejected outright:

- *Add an `owner` column and trust the payload.* A string column with no credential behind it
  documents intent without enforcing anything; every write would still have used the single
  operator token.
- *Issue per-tenant API tokens for a hosted dashboard.* That means building account
  management, password reset, session security, and an approval UI — weeks of security surface
  whose only job is to re-derive an identity GitHub already knows.

## Decision

Become a **GitHub App**, and let both the credential and the approval live where GitHub already
owns them.

### 1. Credentials are per installation, minted on demand

`app/github_app.py` implements the two-step app auth: an RS256 App JWT (`iss` = app id, 9-minute
TTL, backdated 60s for clock skew) mints installation tokens via
`POST /app/installations/{id}/access_tokens`. Tokens are cached per installation until 60s
before expiry.

The consequence that matters: **there is no instance-wide repository credential in multi-tenant
mode.** `get_github_client()` returns a `TenantGitHubClient` that resolves the owning
installation from the repo name (`GET /repos/{owner}/{repo}/installation`, cached 5 minutes) and
posts with *that* installation's token. A bug in token selection can therefore be a bug in
*which* tenant, never a token that reaches every tenant.

When the App is not installed on a repo, the write is refused. Falling back to a PAT there would
silently act on behalf of someone who never installed us.

### 2. Approval moves into the PR thread

A tenant has no dashboard session and never will, so approval happens where they already are:

```
/autopr approve 42      /autopr reject 42      /autopr status      /autopr
```

Three properties follow, each of which is a security decision:

| Property | Mechanism |
|---|---|
| Identity is GitHub's | the commenter's login arrives in an HMAC-signed payload |
| Authority is the repo's | `GET /repos/{repo}/collaborators/{login}/permission` must return admin/maintain/write |
| Cross-tenant replay is impossible | the acting token is the delivering installation's, and the decision must match that installation *and* that repo |

AutoPR stores no maintainer list, so it can never hold a stale ACL that disagrees with the
repository. Revoke someone on GitHub and their ability to approve here disappears with it.

Unauthorised commenters get **no reply**. A refusal message would let anyone enumerate decision
ids by watching which ones answer.

`status` and `help` are gated at the same bar as `approve`, because `status` lists unposted draft
reviews — the maintainer is meant to see those first.

### 3. Tenancy is a schema boundary, not a header

`installations` holds one row per installation; `pr_jobs` and `review_decisions` carry a
nullable `installation_id`. Nullable is deliberate and is the entire upgrade story: every row
written before this phase belongs to a single-operator instance that has no installation, and
NULL says exactly that. A backfill would have to invent an owner.

Enrolment happens from **any** delivery that carries the installation object, not only from the
`installation` webhook. GitHub can deliver `pull_request` before `installation`, and it never
retries a lifecycle event we were down for; making onboarding depend on ordering would turn a
webhook outage into a permanently broken customer.

`suspended_at` and `deleted_at` are separate columns because they mean different things:
suspension is reversible (billing, abuse review) and an operator needs to know the tenant can
come back. `deleted` is terminal — a later `created` event cannot revive the row, so an uninstall
followed by a replay stays closed.

### 4. A queued action is announced where the tenant can see it

A tenant has no dashboard, so a decision that waits silently is a decision nobody can approve.
When the gate queues work *and* this delivery carries an installation, the router posts one short
notice on the PR: the decision id, the action, the risk, and the exact command. It fires only on
first creation, so a redelivered job cannot spam the thread.

Single-operator installs are untouched — no `installation_id` in state means no notice, because
that operator already has the queue view and PR chatter would be a regression for them.

### 5. Budget is per tenant

`quota_jobs_per_installation_per_day` is checked before the INSERT, next to the existing
per-repo/hour and instance/day ceilings. Without it, the instance-wide daily cap is a
denial-of-service weapon: the busiest tenant consumes the budget everyone shares. With it, one
tenant exhausting its allowance returns `429 quota_installation_daily` while every other tenant
is unaffected.

### 6. Public mode and App mode are not mutually exclusive, and the split is deliberate

`AUTOPR_PUBLIC_MODE` makes `POST /reviews/{id}/approve` a 403 for *everyone*, because that
endpoint authenticates with one bearer token every visitor would share. The comment path is not
gated by it, because it authenticates the commenter individually via GitHub. That asymmetry is
the point: a public instance can host live demo installs and those tenants can still clear their
own queue.

What public mode *does* demand in App mode is a positive per-tenant quota, so the demo cannot be
turned into one tenant draining the shared budget.

## The defect this phase shipped and then found

Worth recording, because it is the failure mode this phase is built to prevent.

`enqueue_tracked` never persisted `installation_id`, and the worker's graph state never carried
it. Every queued decision therefore landed with `installation_id = NULL`. `_decision_for`
*correctly* refuses NULL rows for tenants ("the tenant did not create them"), so a customer's
`/autopr approve 42` answered "No decision #42 is awaiting review" — forever. The operator
dashboard showed `0 pending` for every tenant while the queue filled.

The suite was green throughout, because every tenancy test built its own `ReviewDecision` with an
owner already set, and no test walked from worker state to approval. Two guards were correct and
individually tested; the seam between them was not.

Fixes: the worker seeds `job_state()` with `installation_id` (extracted into a pure function so
the seam is testable), `enqueue_tracked` persists it with junk-tolerant coercion (`True` is not
tenant 1), and `tests/test_tenant_approval_path.py` walks the whole path. A negative control now
removes each piece and confirms a named test fails.

## Alternatives rejected

**Per-installation approval tokens shown on a setup page.** Workable, but it asks a maintainer to
copy a secret into CI or a browser before they can approve anything, and a leaked token is a
standing authorisation to fire queued actions. The comment path needs no distribution at all.

**Trusting `repository.permissions` from the webhook payload.** Only present on some events,
absent from `issue_comment` deliveries for non-collaborators, and it is a snapshot of unclear
freshness. Asking GitHub at decision time costs one cached-free API call and cannot be stale.

**Keeping the PAT path as a fallback in App mode.** Tempting for "just in case", but it makes
the tenancy guarantee conditional: any code path that fell back would write with a credential
that reaches every repository. In App mode there is no fallback; refusal is the failure mode.

**Purging installations on uninstall.** Deleting the row would cascade away the audit trail
("who approved that action?") at exactly the moment someone is asking. Delete is a date field.

**Accepting `/autopr approve` from any commenter.** On a public repo, drive-by comments are
free. That would let a stranger fire a queued code-changing action on a repository they have no
rights in — the exact hole the human gate exists to close.

## Consequences

**Good.** A stranger can install the App, open a PR, watch a review appear, and approve the
queued action from the PR thread — with no account, no token, and no operator in the loop. The
write surface is still one call (`post_issue_comment`); tenancy changes *whose* credential makes
it, not what it can do.

**Costs.** Two secrets now matter more than before: the App private key (rotation = mint
everything again) and the webhook secret (shared by every tenant, so a leak is instance-wide).
Token minting adds one API call per installation per hour, which is why the cache exists.

**Still not provided.** Per-user identity beyond "GitHub says this login has write access",
per-tenant model spend accounting beyond job counts, BYO model keys, and a worker lease. An
operator who wants per-seat billing must derive it from `installation_id` counts, and the
roadmap says so.

## Verification

80 new tests: `tests/test_tenancy.py` (23), `tests/test_commands.py` (34),
`tests/test_tenant_webhook.py` (12) and `tests/test_tenant_approval_path.py` (11).

Every guard was verified by disabling it and confirming a *named* test fails:

| Guard disabled | Failing tests |
|---|---|
| collaborator permission check | 3 |
| decision ↔ installation match | 1 |
| decision ↔ repo match | 1 |
| webhook installation-active gate | 2 |
| no-PAT-fallback in the tenant client | 1 |
| per-tenant quota | 4 |
| tenant persisted on the decision row | 1 |
| worker seed carries the job's tenant | 2 |
| queue notice posted | 1 |

Full gate at this phase: **401 passed / 5 skipped** (403 with Docker) · 86% coverage · ruff +
mypy clean · figures `PASS 0/6` · tsc + eslint + prettier clean · 63 vitest.
