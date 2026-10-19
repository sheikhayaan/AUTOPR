# Phase 15 — A public demo that strangers can use, and nobody can abuse

**Status:** accepted · **Extends** phase-14 (fail-fast token, allowlist)

## Context

Phase 14 closed the anonymous *write* path and the unbounded *spend* path for a
single-operator install. The remaining ask was different: **let strangers use it.**
A portfolio piece nobody can open is not a portfolio piece.

That flips the threat model. On a private install the operator is the only actor,
so "reads are open" is harmless. On a public URL the actors are unknown, so every
default that was merely conservative becomes a liability:

- open reads leak repo names, PR numbers, authors and the exact comment text
  awaiting approval;
- an empty allowlist means any repo with a signed webhook can spend your key —
  and on a public repo a **fork PR is a genuinely signed delivery**;
- an unbounded job count means one loop can run up a bill;
- the per-IP limiter keys on the socket peer, so behind a proxy every visitor
  shares one bucket (the limiter silently stops limiting), and the naive fix of
  trusting `X-Forwarded-For` is a full bypass — a client could mint a fresh bucket
  per request.

## Decision

**One flag, four enforced invariants, no new endpoints.**

`AUTOPR_PUBLIC_MODE=true` means "look, don't touch". Three of the four are boot
failures in `_enforce_public_mode`, so an unsafe demo cannot start:

| Invariant | Why boot-fatal |
|---|---|
| `AUTOPR_ALLOWED_REPOS` non-empty | otherwise strangers add work, and work is money |
| `AUTOPR_GITHUB_DRY_RUN=true` | otherwise the demo can modify repositories |
| both quotas > 0 | otherwise one loop is an unbounded bill |
| approve/reject answer 403 | enforced per request, since it is behaviour not config |

The 403 applies to **everyone, operator token included**. That is deliberate: a
mode whose name is "read-only" should not have an exception the operator can
forget they enabled. An instance that needs to act is not a public demo — unset
the flag.

Reads stay open. That is the entire point of the feature; the mitigation for the
leak is the allowlist and the instruction to seed synthetic data, not a login.

**Quotas are soft.** Two racing deliveries can both read `limit - 1` and both
proceed, so the true maximum is `limit + concurrency`. Making it exact needs a
lock or a counter row with a conditional UPDATE, which buys nothing: the goal is
"a stranger cannot run up an unbounded bill", not exactly-N accounting. A
repeat delivery of work already held is exempted (`dedup_exists`) — a GitHub
retry spends nothing new, and turning it into a 429 would break the retry
semantics the rest of the API depends on.

**`X-Forwarded-For` is honoured only behind a declared proxy, from the right.**
`resolve_client_ip(peer, xff, trusted)` returns `peer` unless the peer itself is a
trusted proxy (exact IP or CIDR); then it walks the chain right-to-left and stops
at the first untrusted hop, because that is the only entry our own proxy wrote —
everything to its left is text the attacker chose. An empty `AUTOPR_TRUSTED_PROXIES`
(default) makes this exactly the old behaviour, so a direct deployment is unchanged.

## Consequences

* The demo is a genuine product surface: a visitor sees real queue state, real
  review text, and can copy it — with no write path in reach.
* The dashboard hides approve/reject in public mode instead of offering a button
  whose only outcome is an error toast. The server refuses the `curl` too.
* `AUTOPR_PUBLIC_MODE` invariants are checked *before* the `ALLOW_INSECURE` early
  return, unlike the secret/token checks: exposure is a claim about deployment,
  not a local convenience, so previewing the demo locally exercises the same
  posture production will run.
* Private installs are untouched: quotas default to 0 (off), `public_mode` is
  false, and an empty `trusted_proxies` means the resolver is the identity
  function it always was.
* We did **not** build per-visitor identity. Public mode has exactly one identity
  for everyone: read. Anything finer is multi-tenancy, which stays out of scope
  and is stated as such in `## Limitations`.

## Verification

`tests/test_public_mode.py` (18) covers the three boot refusals, the 403 on both
mutating endpoints, that a refused approve leaves the row PENDING, that reads stay
open, the per-repo and instance quotas, the 429 contract, the no-row-on-refusal
guarantee, the duplicate exemption, and window expiry.
`tests/test_client_ip.py` (14) covers the spoofing direction as carefully as the
proxy direction. Four negative controls were run by disabling each guard and
confirming the suite fails: the 403 branch (3 failures), the quota check (4), the
boot invariants (4), and the trusted-proxy condition — i.e. replacing it with the
naive "always trust the header" implementation (2). Frontend: one new vitest
asserting no mutating control renders in demo mode.

## Alternatives considered

* **Require login for the demo.** Rejected: no accounts exist, and an SSO proxy in
  front is already the documented answer for attribution.
* **Make public mode force `require_auth_for_reads=true`.** Rejected: it would
  make the demo unreadable to the strangers it exists for.
* **Per-IP quotas instead of per-repo.** Rejected: the caller is GitHub, whose IPs
  are shared by every tenant — it would either be trivially bypassed (fork PRs come
  from many IPs) or block the platform.
* **Hard quotas via a counter row.** Rejected as above; the overshoot is bounded
  by request concurrency and is not a correctness property.
* **Trust `X-Forwarded-For` when `--proxy-headers` is set.** Rejected: that
  couples a security control to a CLI flag an operator can flip without noticing,
  and it still needs a trust list to know which hop to believe.
