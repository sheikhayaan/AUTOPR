# Phase 4 — Decisions Log

Risk-based routing and human-in-the-loop. The pipeline now *disposes* of its own
output: after the graph finishes, a terminal `router` node decides what to do
with the result — post it automatically, or queue it for a human to approve —
and an ops API is the surface where a maintainer signs off. This is the phase
where AutoPR stops being "analysis that ends in state" and becomes "analysis
that causes an action," so every decision here is about *who is allowed to cause
what, and how we prove we never crossed that line*. Each entry states what was
chosen, the rejected alternative, and why.

---

## 1. The disposition is a pure function, separate from the act

**Chosen.** `policy.route(state) -> RoutingDecision` is a total, side-effect-free
function: it reads the finished state and returns *what the system wants to do*
(an `Action`), *whether that needs human approval* (`requires_approval`), and the
*exact markdown it would post* (`body`) — with no GitHub call, no DB write, no
LLM. A separate layer (`router_node` / `execute_decision`) performs the I/O.

**Why.** The disposition rules are the part that must be *correct* — a wrong edge
is the difference between "posts a helpful comment" and "pushes a code change
nobody approved." Making them a pure function means the entire decision matrix is
exhaustible in unit tests (`test_routing_policy.py`) with plain dicts and zero
infrastructure. The messy, un-unit-testable part (does httpx reach GitHub) is
quarantined behind a boundary and faked. This mirrors the Phase 3 split between
the Fix Agent (proposes) and the verifier (proves): decide and do are different
trust levels and belong in different, independently testable places.

**Rejected — decide-and-post in one node.** Folding the GitHub call into the
routing logic makes the safety-critical decision reachable only through a mock of
the network layer, and tangles "is this the right call" with "did the call
succeed." Two failure modes in one function that you can no longer test apart.

---

## 2. Any code-changing action is ALWAYS human-gated — no risk score can auto-promote

**Chosen.** A `PROPOSE_FIX` decision (a sandbox-verified fix from the CI track)
sets `requires_approval=True` unconditionally, *regardless of risk score*. Even a
`trivial`-risk verified fix is queued, never posted automatically. The risk-based
auto path applies only to `COMMENT_REVIEW` — an action that changes no code.

**Why.** This is the load-bearing safety invariant of the whole system, and it is
the direct continuation of Phase 3 decision #6 ("the sandbox never touches the
real repo — verdict only"). A green sandbox says "this patch resolves the
diagnosed failure"; it does *not* say "ship it unreviewed." The credibility of an
autonomous PR bot rests entirely on it never making an unapproved code change, so
the gate is not a function of a heuristic risk score that could itself be wrong —
it is absolute. Risk thresholds decide *how loud a comment* is allowed to be
without a human; they never decide *whether code changes* without one.

**Rejected — auto-promote low-risk verified fixes.** Tempting (it would make the
system feel more autonomous) and exactly the wrong default. It makes the
worst-case action — a wrong code change merged with no human — reachable through
two independent estimates both being right (risk *and* verification). One bad
risk label and the invariant is gone. The gate must not be conditional.

---

## 3. Risk-thresholded auto-commenting, defaulting to a conservative ceiling

**Chosen.** For the PR-review track, `route` auto-posts a comment only when
`risk_rank(risk) <= risk_rank(settings.auto_comment_max_risk)`. The ceiling
defaults to `"low"`, so `trivial`/`low` reviews post automatically and
`medium`/`high` are queued for approval. `risk_rank` treats any *unrecognized*
label as `high` — the most cautious bucket — so a garbled risk score fails closed.

**Why.** Not every action deserves the same friction. A comment changes no code
and is trivially reversible (delete it), so gating *every* review behind a human
would make the system a nag that never earns trust on the easy cases. But a
review on a risky change is where a wrong or noisy bot comment does reputational
damage, so those wait for a maintainer. The threshold is a single config knob
(`auto_comment_max_risk`) so the operator picks their own autonomy/safety point
without a code change, and the unknown-ranks-as-high rule means the failure
direction is always toward *more* human oversight, never less.

**Rejected — gate all comments** (safe but useless: no autonomy, every trivial
nit needs a human). **Rejected — auto-post all comments** (noisy and risky on
exactly the changes where being wrong is expensive). The threshold is the
defensible middle, and it is *ordinal* (via `RISK_ORDER`) rather than a set of
hardcoded `if risk == "high"` branches, so adding a level doesn't touch the logic.

---

## 4. The GitHub client is dry-run by default; real posting is an explicit opt-in

**Chosen.** `get_github_client()` returns a `FakeGitHubClient` (records the
intended call, touches nothing) unless BOTH `settings.github_token` is set AND
`settings.github_dry_run is False`. The default (`github_dry_run=True`) means the
system runs end-to-end — routing, queuing, "posting" — while never contacting
GitHub. The client is a `Protocol` (`GitHubClient`) with a fake and an httpx
implementation behind the same interface.

**Why.** Safe-by-default is the right posture for the one component that can act
on the outside world. During development, demos, and tests you want the full
pipeline to *run* — you just don't want it to actually comment on a real PR from
a half-finished routing rule. Requiring two independent opt-ins (a token present
*and* dry-run explicitly disabled) means no single forgotten env var causes a
surprise real-world side effect. The `Protocol` + fake also makes every layer
above testable without a network: the router, store, and API tests all inject the
fake and assert on `.calls`.

**Rejected — real client whenever a token exists.** A token is often present for
*read* reasons; letting its mere presence enable *writes* means one leaked or
reused credential turns a test run into real GitHub activity. Dry-run must be its
own explicit switch.

---

## 5. A durable approval store keyed on an idempotent dedup key

**Chosen.** A human-gated decision is persisted as a `ReviewDecision` row
(`review_decisions` table) with a `UNIQUE` constraint on
`dedup_key = repo|pr|commit|action`. `store.enqueue` is get-or-create on that
key: it returns the existing row if present, and on a concurrent-insert race
catches `IntegrityError`, rolls back, and re-selects the winner. Status moves
`PENDING → APPROVED → EXECUTED` (or `→ REJECTED`, or `→ FAILED`).

**Why.** The queue outlives the process — a maintainer might approve hours later,
after a worker restart — so it must be durable, not in-memory. Idempotency is the
non-negotiable property: a redelivered webhook or a worker retry re-runs the
graph, which re-reaches the router, which must NOT queue the same comment twice.
Enforcing that with a database unique constraint (rather than a read-then-write
check) makes it correct *under concurrency* — two racing workers both INSERT, the
DB picks a winner, the loser falls back to SELECT and returns the same row. The
explicit status lifecycle is what lets `/approve` be safely repeatable (see #6):
the state machine, not a flag, decides what a second approval does.

**Rejected — an in-memory or Redis-list queue.** Loses pending approvals on
restart — unacceptable for a human-in-the-loop step that may take hours.
**Rejected — read-then-insert idempotency.** Has a check-then-act race: two
workers both read "absent" and both insert. The unique constraint closes it.

---

## 6. The ops API is the only path from "queued" to "posted", and it's idempotent-safe

**Chosen.** Three endpoints: `GET /reviews/pending`, `POST /reviews/{id}/approve`,
`POST /reviews/{id}/reject`. Approval is the *only* code path that calls
`execute_decision` for a gated action — the pipeline itself never does. The
endpoints are defensive about repeat/out-of-order calls: approving an already
`EXECUTED` decision is a no-op returning the existing result (no double-post);
approving a `REJECTED` one is `409`; a GitHub failure marks the row `FAILED` and
returns `502` (so it can be retried) rather than falsely reporting success;
rejecting an already-executed decision is `409`.

**Why.** Concentrating the "cause a real action" authority in one explicit,
human-driven surface is what makes decision #2's invariant *enforceable* rather
than aspirational: there is exactly one function that posts a gated action, and it
is only reachable by an operator hitting approve. The idempotency at the HTTP
layer matters because humans double-click and retries happen — the safety story
can't depend on every approval arriving exactly once. Mapping a GitHub failure to
`FAILED`+`502` (not `EXECUTED`) keeps the store honest: a decision is only
`EXECUTED` if the action actually went through, so the pending/failed queues
always reflect reality.

**Rejected — auto-execute on a timer / "approve" as a fire-and-forget flag.**
Reintroduces the possibility of a code change reaching GitHub without a
deliberate human act, and a non-idempotent approve would double-post on a retry.
The endpoint owns the transition *and* its safety.

---

## Integration seam closed (Phase 4.5)

Phases 1–4 each proved themselves in isolation, but the worker still ran Phase
1's `default_handler` — a real webhook never actually drove the graph (corner #1
below, and phase-3 corner #1). Phase 4.5 wires it: a PR `opened`/`synchronize`
webhook now flows worker → fetch the PR's changed files from GitHub → run
`code_reviewer → test_generator → router` → auto-post a low-risk review (dry-run
by default) *or* queue an elevated-risk one for approval → record the result
exactly once. Three decisions made that wiring defensible.

---

## 7. Reads are a distinct boundary from writes, gated on token presence — not on dry-run

**Chosen.** A new `GitHubReader` Protocol (`list_pull_files(repo, pr) -> [{path,
patch}]`) with an httpx `HttpGitHubReader` and a `FakeGitHubReader` (records
calls, returns a configured file list). `get_github_reader()` returns the real
reader whenever `settings.github_token` is set — `github_dry_run` is irrelevant
to reads. The write side (`get_github_client`, decision #4) is untouched: posting
still needs a token *and* `github_dry_run=False`.

**Why.** A read is not a mutation. Gating reads behind `github_dry_run` would mean
the "live fetch" isn't live in the default config — directly contradicting the
chosen behavior for this seam (fetch *real* diffs so the review is about real
code, while posting stays dry-run until explicitly enabled). Splitting reads into
their own boundary lets those two knobs move independently: the worker reads real
PRs by default, and still can't write to one without the deliberate two-switch
opt-in. Same `Protocol`+fake pattern as the write client, so every layer above
stays network-free in tests.

**Rejected — reuse the write client's gating for reads.** Makes the default
config unable to read a real PR, defeating the purpose of a live fetch. **Rejected
— gate nothing behind a token.** Loses the safe-by-default write posture. Reads
and writes are different trust levels and get different gates.

---

## 8. The router is bound a `session_factory`, not a live `Session`

**Chosen.** `router_node(state, github=None, session_factory=None)`;
`build_graph(..., session_factory=)` binds the process-global `SessionLocal`. The
router opens a short-lived session *per enqueue / auto-act* from the factory.
(Renamed from the placeholder `store_session=` in the original phase-4 build.)

**Why.** The graph is compiled once at worker startup, so there's no live per-job
`Session` to bind at compile time — but a process-global *factory* binds fine and
lets the router open exactly the session it needs, when it needs it. This also
decouples the HITL/ledger write from the worker's job-ledger transaction: they
commit independently, so the router's write neither rides on nor blocks the outer
job transaction. Lock-safety falls out of the existing worker shape —
`process_job` commits the `PROCESSING` status *before* invoking the handler
(`app/worker.py`), so the outer session holds no write lock while the router's
separate session commits; no SQLite-file contention. This is what closes corner
#1: in-graph runs now persist.

**Rejected — bind a live `Session`.** Impossible to inject a fresh per-job session
into an already-compiled graph via `partial`, and it would couple two
transactions into one. **Rejected — open a session unconditionally inside the
router.** Breaks pure unit runs that pass no factory; those deliberately keep the
direct-act-only behavior and assert on the fake client.

---

## 9. The auto-post path is made idempotent through the same ReviewDecision ledger

**Chosen.** When a `session_factory` is present, the auto (ungated, low-risk) path
does get-or-create on the ledger *before* posting; if a row for
`repo|pr|commit|action` is already `EXECUTED`, it skips
(`action_taken="already_executed"`), otherwise it posts and then
`mark_executed`/`mark_failed`. With no factory (unit runs), it keeps the direct
post.

**Why.** The worker is at-least-once — a crash after doing the work but before the
Redis ACK causes redelivery. The *gated* path was already idempotent via
`dedup_key` (decision #5), but the *auto* path posted directly with no dedup, so a
redelivered low-risk job would post a **duplicate comment**. Routing the auto path
through the same ledger spine reuses Phase 4's idempotency guarantee for both
paths — one mechanism to keep correct, not two. The honest caveat, stated rather
than hidden: a crash in the narrow window *between* GitHub's POST returning and
our `mark_executed` commit can still double-post on redelivery. That window is one
commit wide, and it is the fundamental two-generals limit of any non-idempotent
external POST — GitHub offers no idempotency key for issue comments, so no client
can fully close it. What we can do, we did: collapse the common case to exactly
once and document the residual.

**Rejected — a second, separate dedup mechanism for auto-posts.** Two idempotency
systems to keep in sync is how one of them rots. **Rejected — leave the auto path
un-deduped.** That's the duplicate-comment bug this closes; "it's only low-risk
comments" is not a reason to knowingly ship a double-post on every redelivery.

---

## Corners cut / simplifying assumptions — be ready to explain these

Deliberate and known. None affect the correctness the tests prove; they are scope
boundaries for Phase 4.

1. **~~The router runs in the graph without a DB session~~ — CLOSED in Phase 4.5.**
   The worker now drives the graph end-to-end: `make_graph_handler` assembles a
   `PRState` for the PR-review track (`repo`, `pr_number`, `commit_sha` from the
   `PRJob` row; `changed_files` from `GitHubReader.list_pull_files`), invokes the
   graph, and returns a one-line summary for the exactly-once `JobResult` ledger.
   `main()` wires it once (`reader=get_github_reader()`, `github=get_github_client()`,
   `graph=build_graph(rag=None, github=..., session_factory=SessionLocal)`). The
   router persists in-graph via the factory (decision #8) and the auto path is
   idempotent (#9). The vertical slice is proven offline in `test_worker_graph.py`
   (webhook-shaped input → graph → auto-post/queue → exactly-once result, on
   SQLite + fakes + mocked LLM). `default_handler` is kept for back-compat. What
   remains rough on this seam:
     - **The CI-fix track is still deferred.** Only the PR-review track is wired.
       Driving a fix from a red check additionally needs `check_run`/`workflow_run`
       webhook parsing and a whole-repo tree snapshot (not just the PR's changed
       files) — additive, and it doesn't change the routing/gating logic already
       built.
     - **The worker runs RAG-ungrounded (`rag=None`).** Reviews run without
       retrieval context by default; RAG stays best-effort and Qdrant isn't
       guaranteed to be running. Grounding the worker is a config flip once an
       index exists, not a code change to the seam.
     - **The live GitHub *read* path is un-smoke-tested against real
       api.github.com** — same status as the write path (corner #3). `HttpGitHubReader`'s
       wire calls are written and unit-covered; no test hits real GitHub. The
       pre-Phase-5 live smoke covers both directions.

2. **A fix is "proposed" as a PR comment, not an actual pull request.**
   `execute_decision` surfaces both `PROPOSE_FIX` and `ESCALATE` as issue comments
   containing the verified diff, for a human to apply. Opening a real fix PR (create
   a branch, commit the patch, `POST /pulls`) is a follow-on. Chosen because the
   comment path exercises the entire approval spine end-to-end while keeping the
   outward action to the single, easily-reversible primitive (a comment) — the
   branch/PR machinery is additive and doesn't change the routing or gating logic.

3. **The httpx `HttpGitHubClient` is not exercised against a live server.** Its
   wire calls (URLs, auth header, error mapping) are written and unit-covered for
   construction, but the factory test proves we never even *build* it without an
   explicit opt-in, and no test posts to real GitHub. A live smoke test against a
   throwaway repo is the pre-Phase-5 acceptance step, mirroring how the live Docker
   test gates Phase 3. Until then the real-network path is "written, not proven."

4. **Approval has no authentication or audit trail.** Anyone who can reach the ops
   API can approve. Real deployment needs authn/authz on `/reviews/*` (who is a
   maintainer) and an append-only audit log (who approved what, when). Out of scope
   for a portfolio phase focused on the routing/gating *logic*; called out because
   an interviewer will rightly ask "who's allowed to hit approve?" — the answer is
   "that's the next layer, and here's exactly where it slots in."

5. **`risk_score` is taken from the reviewer as-is; there's no independent risk
   model.** The gate trusts the risk label the code-reviewer agent produced. A
   compromised or hallucinating reviewer could under-rate a risky change and slip a
   comment through the auto path. Mitigated by the unknown-ranks-as-high rule and by
   the fact that *code changes are gated regardless of risk* (#2), so the worst a
   bad risk score does is auto-post a comment that should have waited — never an
   unapproved code change. A second, independent risk signal is a possible hardening.

6. **`execute_decision` reconstructs a minimal state from the stored row on
   approval.** The approve endpoint rebuilds `{repo, pr_number, commit_sha}` and
   `{action, body, title}` from the persisted `ReviewDecision` rather than re-running
   the pipeline. This is deliberate — the whole point of storing the rendered `body`
   at enqueue time is that approval must act on *exactly what the human reviewed*,
   not a freshly recomputed (and possibly changed) body. The corner is that any
   field not stored on the row is unavailable at execution; today the outward action
   (a comment) needs only what's stored, so this is sufficient, but a richer action
   (opening a PR needs head/base branches) would require persisting those fields too.
