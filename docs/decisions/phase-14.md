# Phase 14 — Two settings that fail at boot, and one that bounds cost

**Status:** accepted · **Supersedes nothing** · **Amends** phase-7 (fail-fast secret)

## Context

A security review of the shipped posture asked a narrow question: *what does a
stranger have to do to make this bot write to someone's repository, or spend
someone's money?* Reading the code, the answer depended entirely on configuration
rather than on code, which is the dangerous shape — configuration fails quietly.

Three findings, in order of severity:

1. **An empty `AUTOPR_API_TOKEN` only warned.** `POST /reviews/{id}/approve` then
   accepted *any* caller, so the human gate became decorative: the approver was
   whoever could reach the port. The webhook secret already failed fast
   (phase-7) — the token, which guards the *stronger* capability, did not.
2. **`GET /jobs`, `/reviews/pending`, `/stats` are unauthenticated by default**
   (`require_auth_for_reads=false`). No secrets leak, but repo names, PR numbers,
   authors and the exact comment text awaiting approval are world-readable.
   Verified: `/jobs` returned `psf/requests`, `acme/auth-service` anonymously.
3. **No repo allowlist.** Forging a webhook is impossible without the secret, but
   on a repo strangers can open PRs on, a **fork PR is a genuinely signed
   delivery** — four LLM calls on the operator's key, triggered by anyone. The
   per-IP webhook limit does not help: it keys on GitHub's IP, not the attacker's.

## Decision

**Make the token part of the boot contract.** `_enforce_secure_config` now raises
when `api_token` is empty (whitespace counts as empty), alongside the existing
placeholder-secret check. Both share the `allow_insecure` escape hatch, which the
test suite, the bench harness and `scripts/seed_demo.py` already set — so the
suite stays hermetic and CI's migration job (which sets `ALLOW_INSECURE=1`) keeps
working unchanged.

**Add an optional `AUTOPR_ALLOWED_REPOS` allowlist**, enforced in the webhook
route via `ingest.repo_is_allowed()` *before* the INSERT: an unlisted repo gets
`200 {"status":"ignored","reason":"repo_not_allowed"}`, creates no row, publishes
no stream message, and spends nothing. Case-insensitive, exact match (no prefix
matching — `acme/api-evil` must not satisfy `acme/api`), empty list means
"allow all" so the default posture for a personal private-repo install is
unchanged.

**Leave `require_auth_for_reads` at `false`** and document the two-shape
deployment in `docs/operations.md` (whole app behind a proxy with the flag on, or
loopback + a tunnel whose ingress routes only `/webhook`). It is not made
mandatory because the read API is what makes the dashboard usable on a laptop,
and a flag that must be off locally and on in production is better expressed as
deployment shape than as a fail-fast check.

## Consequences

* A deployment that would answer `/approve` anonymously now cannot start. The
  failure message names the variable and the generator command.
* Compose had to pass `AUTOPR_API_TOKEN` to the **worker** as well — it imports
  `app.config`, so it must satisfy the same validator. That is deliberate: the
  two processes should agree on what a valid production posture is.
* Public installs get a cost bound that is independent of prompt-injection
  resistance; private installs pay nothing for it (empty list, one branch).
* Reads remain open by default. Accepted, documented, and cheap to close; the
  alternative (failing boot without `require_auth_for_reads`) would break every
  local workflow including the 60-second smoke test.

## Verification

`tests/test_secure_boot.py` (10 tests) pins both changes, including the two
negative controls that matter: temporarily disabling the token check fails
`test_empty_api_token_refuses_to_boot`, and disabling the allowlist branch fails
`test_webhook_from_a_disallowed_repo_creates_no_job`. `tests/test_auth.py` gained
`test_real_secret_alone_is_no_longer_enough` to record that the old posture is
now rejected.

## Alternatives considered

* **Warn instead of fail** (the status quo): rejected — the failure mode is
  silent, remote, and writes to the user's repository.
* **Make the token required at the type level** (`api_token: str` with no
  default): rejected — it breaks the `allow_insecure` path and produces a
  confusing pydantic "field required" instead of an actionable message.
* **Enforce the allowlist inside `ingest()`**: rejected — the route already owns
  "should this delivery create work at all", and keeping the check out of the
  ingest funnel leaves the unit-testable ingest path free of policy.
* **Repo allowlist on by default with a required value**: rejected — it would
  force every personal install to enumerate its own repos for no gain.
