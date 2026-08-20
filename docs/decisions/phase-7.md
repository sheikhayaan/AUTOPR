# Phase 7 — Decisions Log

Putting a **perimeter** around a system that already had a strong core. Phases
1–6 built the distributed pipeline (exactly-once ledger, PEL/reclaim, dry-run
write boundary) and made it reproducible and CI-gated. But the front door was
open: the only path to a real GitHub write — `POST /reviews/{id}/approve` — took
no credential, the webhook secret shipped as `changeme-…` and would happily boot
that way, and CORS was `*`. This phase closes those without adding operational
weight the stated scale (a showcase serving ~1–50 requests, single operator)
does not need. Each entry states what was chosen, the rejected alternative, and
why.

The through-line: **proportionate** security. Every control here is the smallest
mechanism that actually holds the property — a constant-time token compare, an
in-process counter, an allowlist, a structured audit event — not a framework
that would be harder to justify under questioning than the threat it addresses.

---

## 1. The webhook secret **fails fast**: refuse to boot on the placeholder

**Chosen.** A pydantic `model_validator(mode="after")` on `Settings`
(`_enforce_secure_secret`) raises at construction if `webhook_secret` is still
the shipped `changeme-generate-a-real-secret` — unless `AUTOPR_ALLOW_INSECURE=1`
is set. Because `settings = get_settings()` runs at import, a misconfigured
deployment dies immediately and loudly, before serving a single request. The
`.env` secret was rotated to a real 32-byte value as part of this phase.

**Why.** A service that boots with a well-known shared secret is not "insecure
later" — it is forgeable *now*: anyone who has read the public repo can compute a
valid `X-Hub-Signature-256` and inject webhook jobs. That is a latent
misconfiguration that looks fine (the app is up, health is green) right up until
it is exploited. Turning it into a startup crash converts a silent security hole
into an obvious operational error the operator fixes in thirty seconds. The
`allow_insecure` escape hatch keeps local dev and the test suite frictionless
(CI has no `.env`, so the placeholder would otherwise block every run) while
making the insecure path a *deliberate, named opt-in* rather than the default.

**Rejected — warn-and-continue.** A log line at startup is missed exactly when
it matters (nobody reads a healthy service's logs), and "runs fine in the demo"
guarantees the placeholder ships. A crash cannot be ignored. **Rejected — no
placeholder at all, require the env var unconditionally.** Then the test suite
and a first-time `git clone && pytest` need environment plumbing before anything
runs; the placeholder + `allow_insecure` pair keeps the zero-config path working
while still failing closed in production.

---

## 2. API auth is a **bearer token**, constant-time compared — not OAuth

**Chosen.** A FastAPI dependency (`require_api_token`) enforces
`Authorization: Bearer <token>` on the mutating endpoints (`/reviews/{id}/approve`
and `/reject`), comparing against `AUTOPR_API_TOKEN` with
`hmac.compare_digest` (reusing the constant-time primitive in `app/security.py`,
new `verify_bearer_token`). An **empty token is a documented no-op** — auth is
skipped and startup logs a loud `startup.no_api_token` warning — so local dev and
the existing test suite work unauthenticated, while any real deployment that sets
a token has its write path protected. Reads (`/stats`, `/jobs`, `/reviews*`) are
gated by the same token only when `AUTOPR_REQUIRE_AUTH_FOR_READS=true`; the
default leaves the read-only dashboard open for demos.

**Why.** The threat model is precise: prevent an unauthenticated caller from
driving a code-changing action to GitHub. The actor is a *single operator* (or a
CI job), not a population of end users with accounts. Bearer-token auth is
exactly sized to that: one shared secret, a constant-time compare, no session
store, no callback, no user table. It is also easy to defend line-by-line under
questioning — which is the point of this repo. The constant-time compare matters
even for a bearer token: a byte-by-byte `==` leaks the token's length and prefix
through timing, and the same discipline already guards the webhook HMAC, so the
codebase stays consistent.

**Rejected — OAuth2 / OIDC.** Sessions, an authorization-code callback, token
storage and refresh, and an identity provider are a large surface that buys
nothing for a single-operator control plane — it would be *more* code to audit,
not less, and harder to justify as proportionate. **Rejected — HTTP Basic.**
Sends a reusable username\:password on every request with no advantage over a
bearer token, and invites password-reuse. **Rejected — mTLS.** Correct for
service-to-service meshes; wildly out of proportion for a dashboard one person
clicks. **Rejected — signing every ops request like the webhook.** The webhook
is signed because GitHub is the client and already does HMAC; the human operator
holding a token is a simpler, standard fit.

---

## 3. Rate limiting is an **in-process fixed-window counter**, hand-rolled — not slowapi/Redis

**Chosen.** A dependency-free `FixedWindowRateLimiter` (`app/ratelimit.py`):
per-client-IP buckets, a fixed 60-second window, thread-safe (sync FastAPI
handlers run in a threadpool), `time.monotonic()` so it is immune to wall-clock
jumps, and an injectable clock for deterministic tests. Applied as dependencies
to `/webhook` (120/min default) and the mutating endpoints (30/min default),
returning `429` with a `Retry-After` header when tripped. Toggle via
`AUTOPR_RATE_LIMIT_ENABLED`.

**Why.** At 1–50 requests the job of a rate limiter is coarse abuse protection —
stop a hot loop or a trivial flood from hammering the LLM/GitHub paths — not
fair-share scheduling across a fleet. A per-process counter does that with zero
new dependencies and no Redis round-trip on the hot path. The tradeoffs are
written into the module docstring rather than hidden: with N API replicas the
effective global limit is `limit × N` (each holds its own counters), and a fixed
window admits up to `2 × limit` across a boundary. Both are acceptable at this
scale and both have a known fix (move counters to Redis, keyed identically; use a
sliding log) documented for when scaling actually arrives. Writing ~40 lines I
fully understand beats importing a library whose middleware ordering and storage
backends I would then have to explain.

**Rejected — `slowapi`.** The plan named it, but it pulls a dependency and its
own middleware/limits-string DSL to wrap a counter this simple; more surface to
justify than the counter it replaces. **Rejected — a Redis token bucket now.**
The "correct at scale" answer, but it adds a network hop and a Redis dependency
to the request path to solve a multi-replica problem this single-process showcase
does not have. Deferred, not dismissed — the docstring says exactly how. **Rejected
— no rate limiting.** Leaves the LLM/GitHub-backed endpoints one `while true`
away from a cost/abuse incident.

---

## 4. CORS is an **allowlist**, not `*`

**Chosen.** `AUTOPR_CORS_ORIGINS` (comma-separated) parsed by a
`cors_origin_list` property into the `CORSMiddleware` allowlist; the default is
the local Vite dev/preview origins. A literal `*` is still honored for a
deliberately wide-open demo, but it is no longer the default.

**Why.** The API sends no cookies (`allow_credentials=False`), so CORS here is
not guarding a credential — it bounds *which sites' JavaScript may call the API
from a browser*. Defaulting that to the known dashboard origins is free defense
in depth against a random page driving a logged-in operator's browser at the
API, while the `*` opt-out preserves the effortless demo when someone wants it.

**Rejected — keep `*`.** It was fine when the API was read-only and toothless;
with a mutating, token-guarded write path it is sloppy by default. **Rejected —
hardcode the origins.** Deployments serve the dashboard from different hosts;
an env-driven allowlist is the difference between configurable and patched.

---

## 5. Approve/reject emit **structured audit events**

**Chosen.** `approve`/`reject` log `audit.review_approved`,
`audit.review_approve_failed`, and `audit.review_rejected` via structlog with the
actor IP (`request.client.host`), decision id, action, risk, repo, PR number, and
outcome — on the executed, failed, and rejected branches.

**Why.** Approving a decision is the one action in the system that changes a real
repository. That event needs a trail answering *who* triggered *what* and *what
happened* — the minimum an operator or reviewer needs to reconstruct a
code-changing event after the fact. Emitting it as structured key/values (not a
formatted string) means it is greppable and, once Phase 9 makes the API log JSON,
directly queryable. It costs three log lines and is exactly the kind of control a
FAANG reviewer expects to see on a privileged mutation.

**Rejected — a dedicated audit table / tamper-evident store.** Real for a
compliance system; overkill here, and it would imply guarantees (immutability,
retention) this scope does not make. The structured log is the honest,
proportionate version. **Rejected — no audit.** Then a GitHub write has no
recorded origin, which is indefensible for the system's single most sensitive
operation.

---

## 6. Tests neutralize the perimeter by **mutating the settings singleton** in conftest

**Chosen.** `conftest.py` sets `AUTOPR_ALLOW_INSECURE=1` in `os.environ` *before*
any `app.*` import (so construction survives CI's missing `.env`), then — after
import — mutates the process-wide `settings` object: `api_token=""`,
`require_auth_for_reads=False`, `rate_limit_enabled=False`. Tests that exercise
auth or limits opt back in locally via `monkeypatch`.

**Why.** This was a real bug caught in this phase, not a hypothetical: rotating
the developer's `.env` to a *real* `AUTOPR_API_TOKEN` (Decision 1's sibling step)
made pydantic load that token into the test settings, and the pre-existing
unauthenticated `approve/reject` tests started returning 401. The fix has to make
the suite independent of whatever a local `.env` happens to contain. Mutating the
singleton that `app.main` already imported is bulletproof for that: it is the
same object the dependencies read, so the override is guaranteed visible and does
not depend on env-var-vs-dotenv precedence subtleties. `ALLOW_INSECURE` stays an
env var because it must be true *before* `Settings()` is constructed at import;
everything else is set *after* construction, where direct assignment is clearest.

**Rejected — set every field via `os.environ` and trust precedence.** Relies on
"an empty env var overrides a non-empty `.env` value," which is true in
pydantic-settings but subtle enough that a reader (or a future pydantic change)
could get it wrong; the explicit mutation states intent. **Rejected — point tests
at a fixture `.env`.** More machinery, and it still would not stop a stray real
env var from leaking in. **Rejected — freeze `Settings`.** Would forbid exactly
the monkeypatch overrides the auth/limit tests rely on.

---

## 7. Compose **requires** the secrets (`:?`), and injects them from `.env`

**Chosen.** `docker-compose.yml` uses `${AUTOPR_WEBHOOK_SECRET:?…}` and
`${AUTOPR_API_TOKEN:?…}` — compose refuses to start and prints the guidance
message if either is unset — and passes the Groq key through as
`${AUTOPR_GROQ_API_KEY:-}`. Secrets are interpolated from the operator's `.env`,
never written into the compose file or baked into an image.

**Why.** It mirrors Decision 1 at the orchestration layer: the *stack* fails fast
on a missing credential rather than booting an unauthenticated writer or a
forgeable webhook. The worker gets the webhook secret too (it imports
`app.config`, so it must pass the same validator) and the Groq key (it runs the
LLM agents — a gap from the Phase 1 compose that this phase closes in passing).

**Rejected — default the secrets in compose (`:-changeme`).** That is the exact
silent-insecure-boot Decision 1 exists to prevent, just relocated. **Rejected —
`env_file: .env`.** Convenient, but it cannot express "required" — a missing key
becomes an empty value and the stack boots insecure. `:?` buys the fail-fast.

---

## Corners cut (flagged, deferred)

1. **The rate limiter is per-process.** With multiple API replicas the effective
   global limit is `limit × replicas`, and the fixed window allows a burst of up
   to `2 × limit` across a boundary. Both are documented in `app/ratelimit.py`
   and are acceptable at single-process, 1–50-req scale. The scale-out fix (Redis
   counters / sliding window) is noted there; it belongs to a horizontal-scaling
   phase, not this one.

2. **Audit trail is structured logs, not a durable audit store.** The
   `audit.*` events are greppable and (post-Phase-9) queryable, but they are not
   tamper-evident and have no enforced retention. For a compliance-grade system
   that would be a dedicated append-only sink; for this showcase the log is the
   proportionate choice, called out so the boundary is explicit.

3. **A single shared operator token — no rotation, scopes, or per-actor
   identity.** `AUTOPR_API_TOKEN` is one bearer token for the whole write path.
   Rotation is "set a new value and restart"; there are no read-only vs write
   scopes and the audit log records the caller's IP, not a named principal. Right
   for one operator; a multi-user control plane would need real identity (which is
   the OAuth/OIDC path Decision 2 deliberately declined at this scale).

4. **The frontend holds the token in `localStorage` / a build-time env var.**
   `localStorage` is readable by any script that achieves XSS on the dashboard.
   For a single-operator internal tool this is the standard, pragmatic choice; a
   public multi-tenant app would want an httpOnly cookie + CSRF handling (which in
   turn wants the session machinery Decision 2 rejected). Noted as the known
   exposure it is.

5. **Bearer auth assumes the transport is TLS — and this phase does not add
   TLS.** A token sent over plaintext HTTP is only as private as the network. TLS
   termination belongs to the deploy layer (**Phase 12**, Fly.io terminates
   HTTPS at the edge); until then, running this over anything but localhost or a
   trusted network would expose the token in transit. Stated plainly so it is not
   mistaken for handled.

6. **Reads are open by default.** `require_auth_for_reads=False` ships so the
   read-only dashboard demos without a token. The projections expose repo names,
   PR numbers, and decision bodies — fine for a portfolio demo, a deliberate
   choice to revisit (flip the flag) for any deployment where that metadata is
   sensitive.
