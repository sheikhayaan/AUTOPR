# Phase 2 — Decisions Log

Multi-agent orchestration: four AI agents (Code Reviewer, Test Generator, CI
Monitor, Fix Agent) wired as a LangGraph state machine, grounded by a
Qdrant-backed RAG pipeline, driven by Groq LLMs. Each entry states what was
chosen, the rejected alternative, and why.

---

## 1. Orchestration: LangGraph `StateGraph` (not a hand-rolled pipeline, not raw chains)

**Chosen.** A `StateGraph(PRState)` with a shared TypedDict state, explicit
nodes per agent, and **conditional edges** for routing. Entry routing and the
post-CI-Monitor branch are pure functions over state.

**Why.** The pipeline is not linear — it has two disjoint tracks and a
data-dependent branch (fix only if a failure was confidently diagnosed).
LangGraph models exactly this: nodes are agents, edges are control flow, and
routing lives in small pure functions I can unit-test **without an LLM**
(`tests/test_graph_routing.py`). The graph is also the natural place Phase 4's
human-in-the-loop interrupt and Phase 3's persistence/checkpointing will hook
in — choosing it now avoids a rewrite later.

**Rejected — a hand-rolled `if/elif` driver.** Works for four agents today, but
every new agent or branch grows a tangle of imperative glue, and the routing
logic gets entangled with the execution logic (can't test one without the
other). LangGraph keeps routing declarative and separately testable.

**Rejected — a single LangChain sequential chain.** A chain is linear by
construction; it can't express "PR events go left, CI events go right, and the
Fix Agent may or may not run." Forcing branches into a chain means smuggling
control flow into prompts — the opposite of what I want to demonstrate.

---

## 2. Two disjoint tracks, selected at entry by event type

**Chosen.** `_entry_router` inspects the state and routes to one of two tracks:

    PR-open / synchronize:   code_reviewer -> test_generator -> END
    CI-failure:              ci_monitor -> (diagnosed?) fix_agent -> END : END

A PR event never touches CI Monitor/Fix; a CI-failure event never touches
Reviewer/TestGen.

**Why.** The two triggers carry different inputs and demand different work.
Keeping the tracks disjoint means each run only invokes the agents that have
something to do — no wasted LLM calls, and each agent sees only state relevant
to its track (cleaner prompts, fewer ways to confuse the model). It also maps
one-to-one onto the GitHub events Phase 1 already ingests (`pull_request` vs
`workflow_run`/`check_run`).

**Rejected — one linear pipeline that runs all four agents every time.** Simpler
graph, but it burns tokens running a code review on a CI-failure event (and
vice versa), and forces every agent to defensively ignore irrelevant state.

---

## 3. Fix Agent gated on a confident diagnosis (`failure_type != "unknown"`)

**Chosen.** After CI Monitor classifies a failure, `_after_ci_monitor` routes to
the Fix Agent **only** if `failure_type` is one of the known, actionable
categories (`lint`, `type_error`, `test`, `import`, `dependency`). Anything
else — including a missing field — ends the run. The Fix Agent *also*
self-guards: it short-circuits to an empty patch on `unknown` **without calling
the LLM**.

**Why.** A patch generated from a failure the system doesn't understand is worse
than no patch — it wastes a review cycle and erodes trust. Gating on a confident
classification means the auto-fix only fires where it has a real chance of being
right; everything else escalates to a human (Phase 4). The belt-and-suspenders
self-guard in the agent keeps that safety property true even if someone wires
the node in outside the graph.

**Rejected — always attempt a fix and let review catch bad patches.** Shifts the
cost of a bad guess downstream onto a human reviewer and spends an LLM call to
produce noise. Cheaper and safer to not attempt what we can't diagnose.

---

## 4. RAG: Qdrant + fastembed local embeddings (not an embeddings API, not pgvector)

**Chosen.** `RepoRAG` chunks repo files with `RecursiveCharacterTextSplitter`,
embeds them with **fastembed** (`BAAI/bge-small-en-v1.5`, 384-dim, runs
locally), and stores vectors + metadata in **Qdrant** (cosine distance).
Retrieval embeds the query and returns the top-k chunk payloads. Code Reviewer
and Fix Agent are RAG-aware; passing `rag=None` yields ungrounded operation for
tests and pre-ingestion runs.

**Why fastembed (local) over an embeddings API.** Embeddings run on every
ingested chunk and every retrieval — doing that over a paid API is a recurring
cost and a network dependency on the hot path. fastembed is local, free, and
fast, and bge-small is strong enough for code-similarity grounding. It also
keeps the whole RAG path runnable offline in CI.

**Why Qdrant over pgvector.** We already run Postgres (Phase 1), so pgvector was
the tempting "one less service" choice. Qdrant won because it's a purpose-built
vector store with first-class payload filtering and an **in-memory mode**
(`:memory:`) that makes the RAG tests fast and hermetic — no server, no
migration, no cross-contamination with the Phase 1 schema. The trade is one more
container in compose; worth it for a component whose whole job is vector search.

**Rejected — no RAG, prompt the model cold.** The agents would happily invent
repo conventions that don't exist. Grounding in actual retrieved code is the
difference between "looks plausible" and "matches this codebase."

---

## 5. Shared LLM plumbing: retry/backoff + defensive JSON parsing

**Chosen.** All model calls go through `invoke_llm` (tenacity exponential
backoff, capped at 3 attempts, `reraise=True`) and all structured responses
through `parse_json`, which handles raw JSON, ```json fences, and JSON embedded
in prose.

**Why.** The spec requires retry-with-backoff on any LLM call. Capping attempts
matters: a transient rate-limit should be retried, but a deterministic failure
(bad key, malformed request) must **not** loop forever burning quota — so it
fails after 3 tries and surfaces. `parse_json` exists because LLMs
inconsistently wrap JSON in fences or add prose; a single tolerant parser keeps
that mess out of every agent. Each agent then validates the parsed result
against its own allow-list (e.g. risk score, failure type) and falls back to a
safe default rather than trusting the model's label blindly.

**Rejected — call `llm.invoke` directly in each agent.** Duplicates retry logic
five times, and any agent that forgot it would be a silent reliability hole.
Centralising it makes the guarantee uniform.

---

## 6. Test strategy: mocked LLM for logic, in-memory Qdrant for RAG, live smoke test for the seam

**Chosen.** Three layers:
- **Agent logic** (`tests/test_agents.py`) patches `get_llm` + `invoke_llm` in
  each agent's namespace and feeds canned responses. This asserts on
  parsing/validation/routing deterministically — no network, no key.
- **RAG plumbing** (`tests/test_rag.py`) stubs the embedder with a deterministic
  hash-based vector and uses Qdrant `:memory:`. Tests our chunking, id
  determinism, upsert, and retrieve wiring — not the model's semantic quality.
- **Routing** (`tests/test_graph_routing.py`) asserts the pure edge functions
  directly.

Then a **live end-to-end Groq smoke test** of `code_reviewer_node` against a
divide-by-zero snippet, run manually, confirmed the real pipeline (real ChatGroq
+ real parsing) returns `risk_score: high` with one finding.

**Why.** Mocks prove *our* logic is correct and keep the suite fast and offline
(**47 tests passing, no Phase 1 regressions**). The live smoke test proves the
seam the mocks deliberately don't cover: that the real model, real prompts, and
real JSON parsing actually agree. Testing the mock alone would be testing the
test; testing only live would be slow, flaky, and key-dependent. Both, at their
own layer, is the honest coverage.

**Rejected — assert on live LLM output in the unit suite.** Non-deterministic
(the model can phrase a finding five ways), slow, and requires a key in CI. The
mock/live split gets determinism *and* real-seam confidence.

---

## Corners cut / simplifying assumptions — be ready to explain these

Deliberate and known. None affect the correctness the tests prove; they are
scope boundaries for Phase 2.

1. **Three API-drift bugs, fixed — the ecosystem moves fast.** Running the suite
   surfaced three real (not test-artifact) breakages from library version drift,
   each fixed and worth being able to explain:
   - `RecursiveCharacterTextSplitter` moved out of `langchain` into the separate
     `langchain-text-splitters` package. Added the dep, updated the import.
   - Qdrant rejects a raw SHA-256 hex digest as a point ID — it requires an
     unsigned int or a UUID. Switched `chunk_id` to a deterministic **UUIDv5**
     derived from `file:line-span`, which also preserves idempotent upsert.
   - qdrant-client (>=1.14, confirmed on 1.19) removed `.search()` in favour of
     `.query_points()`, whose response wraps hits in `.points`. Updated
     `retrieve()`.

2. **Chunk line numbers are estimated, not parsed.** `ingest_repo` assigns rough
   `start_line`/`end_line` by chunk index (`i*10+1`), not by tracking real
   offsets or parsing an AST. Good enough to cite a rough location in a prompt;
   a production impl would track true byte/line offsets (or chunk on AST
   boundaries) so findings can point at exact lines.

3. **RAG ingestion is manual / not wired into the event loop yet.** `RepoRAG`
   ingests a list of `(path, content)` tuples on demand; nothing yet fetches the
   repo tree from GitHub and re-ingests on push. The retrieval half is fully
   wired into the agents — the ingestion trigger is the Phase 3 integration
   point.

4. **Fixed-size character chunking, not semantic/AST chunking.** 512-char windows
   with 50-char overlap. Simple and language-agnostic, but can split a function
   mid-body. Semantic chunking (by function/class) would retrieve cleaner units;
   deferred as a RAG-quality refinement.

5. **The graph runs in-memory with no checkpointer.** No persistence of graph
   state between steps, so a crash mid-pipeline restarts the run rather than
   resuming. LangGraph's checkpointer (Postgres-backed) is the Phase 3 add — the
   `StateGraph` choice is what makes dropping it in cheap.

6. **Live verification covered one agent (Code Reviewer), not all four.** The
   smoke test proves the Groq+LangChain+parsing seam works end-to-end; it does
   not individually exercise Test Generator / CI Monitor / Fix Agent against the
   live model. Their *logic* is covered by mocks; a full live run of every track
   is the pre-Phase-4 acceptance step.
