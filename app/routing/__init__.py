"""Phase 4 — risk-based routing and human-in-the-loop disposition.

The graph (Phases 2–3) *produces analysis*: a review with a risk score, or a
sandbox-proven fix. This package decides *what to do about it* — and, crucially,
what to do **without** a human versus what must **wait for** one.

The split mirrors the whole project's thesis: the system is trustworthy because
it does not take code-changing actions autonomously. A low-risk explanatory
comment is safe to post on its own; opening a PR, requesting a review, or
promoting a fix is always gated on human approval.

Layering (each layer is independently testable):
  policy.py  — pure decision: (state) -> RoutingDecision. No I/O.
  github.py  — the outward action boundary (Protocol + fake + http + factory).
  store.py   — durable queue of pending human-gated decisions.
  router.py  — the LangGraph terminal node that ties them together.
"""
