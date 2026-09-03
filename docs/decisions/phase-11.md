# Phase 11 — Decisions Log

Making the project **legible**. The system was secure (7), durable (8),
observable (9), and typed end-to-end (10); what remained was that none of it was
written down where a reader — an interviewer, a new contributor, or me in six
months — would find it. The root README still described only Phase 1's webhook,
and the architecture lived entirely in the code and in ten phase ADRs that
explain *decisions* without ever laying out the *system*. This phase writes the
documentation layer: a README that matches the code's actual maturity, an
architecture document, an operations runbook, and an API reference.

The through-line: **documentation is a deliverable, not a courtesy.** For a
portfolio artifact the README is the first thing read and often the only thing
read before the code; it has to earn the reader's belief that the depth is real
in the first screen, then let them verify it. Every claim in these docs points at
the file or command that backs it, so nothing is asserted that the reader can't
check.

---

## 1. **Rewrite the README around the safety story**, not a feature list

**Chosen.** The README leads with the tension the whole system resolves — LLMs
are good at PR review and unsafe to let loose on a repo — and makes the
**safety invariants** (dry-run default, unconditional human-gating of code
changes, authenticated webhook and write API) a top-level, numbered section.
Architecture, engineering highlights, quick-start, tests, and config follow. It
states the 1–50-request scale explicitly and links the decision logs for every
trade-off.

**Why.** A reviewer skimming a "PR automation" project's README will have exactly
one skeptical question — *what stops it from committing garbage to my repo?* — and
the README must answer it above the fold, because that question is the entire
reason the project is interesting. Organizing around the invariants (rather than
a bullet list of agents and endpoints) signals that the author understands the
actual hard part is the control boundary, not the prompts. Pointing every
non-trivial claim at a file or a runnable command makes the document
*checkable* — the opposite of a README that oversells a thin prototype.

**Rejected — incrementally patch the Phase-1 README.** It described a different,
smaller system; patching would leave a document whose shape still centred on the
webhook. A control plane with a human gate needs a README built around that gate.
**Rejected — a generated feature/endpoint dump.** Accurate and lifeless; it would
bury the one thing that matters (safety) in a list where every item looks equally
weighted.

---

## 2. **Architecture as rendered diagrams** (Mermaid) committed alongside prose

**Chosen.** `docs/architecture.md` carries a component map, an ingress sequence
diagram, a job-lifecycle state machine, and both agent-track flowcharts as
**Mermaid** fenced blocks, interleaved with prose that explains *why* each seam
exists (two processes for fast ingress, effectively-once via an idempotent
ledger, the human gate as the single write path).

**Why.** The system's essence is a flow across process boundaries — webhook →
queue → worker → graph → router → human → GitHub — and that is far more honestly
conveyed by a diagram than by paragraphs a reader has to reassemble into a mental
picture. Mermaid renders natively on GitHub, so the diagrams are versioned text
in the repo (diff-able, editable in a PR) rather than opaque PNGs that rot the
moment the code changes and that no one can update without the original drawing
file. The state machine and sequence diagram in particular pin down the two
things people get wrong about queue systems — the retry/DEAD lifecycle and the
commit-before-publish ordering.

**Rejected — exported PNG/SVG diagrams.** Prettier control over layout, but
binary blobs drift from the code and can't be reviewed in a diff; the next schema
change silently invalidates them. **Rejected — prose only.** The flow is
genuinely 2-dimensional; forcing it into linear text is where architecture docs
become unreadable.

---

## 3. Operations runbook is a **failure playbook**, not a deploy transcript

**Chosen.** `docs/operations.md` covers bring-up and migrations, but its centre
of gravity is a **symptom → meaning → action** table for the real failure modes
(Redis down, DB down, placeholder-secret crash, jobs going DEAD, approve→502,
CORS), plus secret rotation, worker scaling, and the deliberate steps to leave
dry-run.

**Why.** A runbook earns its keep at 3am, not on the happy path — the happy path
is already in the README's quick-start. The valuable content is what an operator
needs when something is *wrong*: what a given red signal means and the specific
action that resolves it. Framing degradation explicitly (Redis down still serves
reads; the webhook fails closed with 503) documents that the failure behaviour is
*designed*, which is a stronger claim than documenting only that it works when
everything is up. Rotation and go-live get step lists because they're the
error-prone, infrequent procedures where a written sequence prevents a mistake.

**Rejected — fold ops notes into the README.** The README is for evaluation and
first-run; operational depth there would bury the pitch and still be incomplete.
Separation lets each document be complete for its audience.

---

## 4. API reference is a **hand-written narrative** that defers exhaustive detail to `/docs`

**Chosen.** `docs/api.md` documents auth (both mechanisms), the failure-status
table, and every endpoint with response-shape examples and `curl` — including the
non-obvious HMAC-signing one-liner for `/webhook` — while explicitly pointing at
FastAPI's generated `/docs` and `/redoc` for the exhaustive, always-current
schema.

**Why.** The generated OpenAPI docs are authoritative for field-level detail and
never drift, so re-documenting every field by hand would be duplication that goes
stale. But generated docs are bad at the things a human integrator most needs:
*how do I authenticate*, *what does a 503 here actually mean*, and *give me a
working `curl`*. The hand-written page owns exactly that — the semantics and the
copy-pasteable examples — and delegates the schema to the tool that generates it
from the code. The HMAC example is worth its space because computing the
signature is the single thing that trips up everyone integrating a signed
webhook.

**Rejected — commit a static OpenAPI export.** Drifts from the code the moment an
endpoint changes; the live `/docs` is strictly better and free. **Rejected —
rely on `/docs` alone.** It can't teach auth or explain what a status *means* in
this system's terms, and it isn't visible to someone reading the repo on GitHub.

---

## Corners cut (flagged, deferred)

1. **No screenshots committed yet.** The README describes the dashboard and points
   at the local URL rather than embedding images, because a broken image link is
   worse than none and the dashboard is trivial to run locally
   (`npm run dev`). Adding `docs/img/*.png` once captured is the obvious follow-up;
   the prose is written so it reads correctly with or without them.

2. **No docs linting / link-checking in CI.** Markdown lint and a link-checker
   (e.g. `lychee`) would catch a dead relative link or a broken anchor
   automatically; today that's verified by hand. For a docs set this size the
   manual bar is proportionate, and the alternative adds a CI job and a class of
   flaky failures (external links going down). Flagged as the scale-out path.

3. **The decision logs aren't consolidated.** There are now eleven
   `phase-*.md` ADRs plus these documents; there is no single "architecture
   decision index" tying them together. The README links the folder and the
   architecture doc references specific phases inline, which is enough at this
   count — a formal ADR index (or migrating to an `adr-tools` layout) is deferred
   until the number justifies the ceremony.

4. **API reference examples are illustrative, not contract-tested.** The `curl`
   snippets and JSON shapes are hand-verified against the serializers, but nothing
   in CI executes them to guarantee they stay correct as the API evolves (the
   endpoint *tests* do that against the code, not against this doc). A docs-example
   test harness is possible but disproportionate here; the generated `/docs`
   remains the always-correct authority the page defers to.
