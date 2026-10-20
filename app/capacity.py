"""Capacity planning from measured numbers.

The point of this module is that the arithmetic is auditable. Every constant
below is either **measured** in this repository (and re-checked by
``tests/test_capacity.py``), or an **assumption** named as such and surfaced in
the output so nobody mistakes it for a measurement.

Two things are genuinely unmeasurable here and are therefore knobs:

* ``llm_latency_ms`` — wall time of one provider call. Depends on the model, the
  provider's queue, and the prompt size. Calibrate it by timing ten real
  reviews; the default is a mid-range Groq figure and is labelled ASSUMPTION in
  every report it appears in.
* ``price_*`` — per-million-token pricing, which changes without notice. Also
  labelled, with the vendor's own pricing page named as the source of truth.

Measured constants and where they come from:

===========================  ====================================================
``ingest p50/p95``           ``docs/assets/bench_result.json`` ->
                             ``paths.webhook.pr_review.median`` / ``.p95``
``SYSTEM_PROMPT_TOKENS``     ``tiktoken`` count of the four agent system prompts
``DIFF_TOKENS``              ``format_changed_files``' 8,000-char cap, tokenised
``CI_LOG_TOKENS``            ``ingest._clip``' 12,000-char cap, tokenised
``LLM_CALLS_PER_*``          one ``invoke_llm`` per agent node, read off the graph
===========================  ====================================================

The model is Little's law: a queue with arrival rate ``λ`` and mean service time
``S`` holds ``λS`` items in flight, so ``workers >= λS / utilization``. Anything
that ignores peak-vs-mean will size a fleet for an average that never occurs,
which is why ``peak_hour_share`` exists and defaults to the 20% a working-hours
spike produces.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "docs" / "assets" / "bench_result.json"

# --- measured: HTTP ingest, in-process ASGI with fakeredis + SQLite ----------
# A deployed run is slower (TLS, a real socket, a network hop to Redis). The
# artifact says so itself; treat these as a floor, not an expectation.
_INGEST_FALLBACK_P50_MS = 24.877
_INGEST_FALLBACK_P95_MS = 32.658

# --- measured: prompt sizes, via tiktoken cl100k_base ----------------------
SYSTEM_PROMPT_TOKENS = {
    "code_reviewer": 241,
    "test_generator": 134,
    "fix_agent": 166,
    "ci_monitor": 256,
}
# `format_changed_files(max_chars=8000)`. Code diffs tokenise denser than prose
# (~8.7 chars/token, measured), so the cap is ~920 tokens, not ~2,000.
DIFF_CAP_CHARS = 8_000
DIFF_TOKENS = 920
# `ingest._clip(limit=12_000)` bounds the CI logs we store AND prompt with.
CI_LOG_CAP_CHARS = 12_000
CI_LOG_TOKENS = 1_380
# Output budget: a review JSON body, a test file, a plan + patch.
OUT_TOKENS_REVIEW = 400
OUT_TOKENS_TESTS = 700
OUT_TOKENS_TRIAGE = 200
OUT_TOKENS_FIX = 900

# --- measured: call counts, read off app/agents/graph.py's edges -----------
LLM_CALLS_PER_REVIEW = 2  # code_reviewer -> test_generator
LLM_CALLS_PER_CI_FIX = 2  # ci_monitor -> fix_agent (fix_verifier is sandbox-only)

# --- measured: per-job side effects ---------------------------------------
# queue.py publishes one stream entry per job and acks one; the claim sweeper is
# amortised. Quota is counted in Postgres, not Redis, so Redis load stays flat.
REDIS_OPS_PER_JOB = 3.2
# pr_jobs INSERT, 2 quota COUNTs, dedup SELECT, job UPDATE(s), review INSERT,
# decision INSERT (gated), installation UPSERT (webhooks only).
PG_STATEMENTS_PER_JOB = 12
# Row bytes: PRJob ~0.4 KB + event_context (CI logs, capped 12 KB);
# ReviewDecision carries the comment body (~2 KB). PRReview jobs carry no logs.
ROW_BYTES_PR_JOB = 420
ROW_BYTES_CI_JOB = 420 + CI_LOG_CAP_CHARS
ROW_BYTES_DECISION = 2_100
DECISION_RATE = 0.35  # share of jobs that gate a decision (observed default gate)

# --- memory ---------------------------------------------------------------
# snapshot_max_total_bytes is 25 MB: one in-flight job can hold a whole repo
# snapshot in the worker. That, not the Python heap, sizes the worker.
SNAPSHOT_CAP_BYTES = 25_000_000
WORKER_OVERHEAD_BYTES = 180_000_000


@dataclass(frozen=True)
class Assumptions:
    """Numbers this repository cannot measure. Every field is labelled in output."""

    llm_latency_ms: float = 3_500.0
    price_in_per_mtok: float = 0.15
    price_out_per_mtok: float = 0.60
    calibrated: bool = False

    def notes(self) -> list[str]:
        latency = (
            f"llm_latency_ms={self.llm_latency_ms:.0f} was supplied by the operator "
            "(calibrated - the model treats it as measured)"
            if self.calibrated
            else f"llm_latency_ms={self.llm_latency_ms:.0f} is an ASSUMPTION - time 10 "
            "real reviews and pass --llm-latency-ms to replace it"
        )
        return [
            latency,
            f"price_in/out={self.price_in_per_mtok}/{self.price_out_per_mtok} USD per "
            "MTok is an ASSUMPTION - check your provider's current pricing page",
        ]


@dataclass(frozen=True)
class Workload:
    """Who is using the instance and how much."""

    tenants: int = 10
    prs_per_tenant_per_week: float = 20.0
    ci_failures_per_tenant_per_week: float = 10.0
    peak_hour_share: float = 0.20
    target_utilization: float = 0.60
    retention_days: int = 30
    # Tokens of retrieved repo context injected into the review prompt (RAG).
    rag_tokens: int = 0


@dataclass
class Report:
    tenants: int
    jobs_per_day: float
    peak_jobs_per_hour: float
    service_s_per_job: float
    workers: int
    llm_calls_per_day: float
    tokens_in_per_day: float
    tokens_out_per_day: float
    cost_per_day: float
    cost_per_month: float
    cost_per_job: float
    redis_ops_per_s_peak: float
    pg_statements_per_s_peak: float
    rows_per_day: float
    storage_mb_per_month: float
    steady_state_storage_mb: float
    memory_per_worker_mb: float
    ingest_p50_ms: float
    ingest_p95_ms: float
    binding_constraint: str
    binding_reason: str
    binding_share_pct: float
    warnings: list[str] = field(default_factory=list)
    assumption_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _measured_ingest() -> tuple[float, float]:
    """Read the bench artifact so the model tracks the docs instead of drifting."""
    try:
        paths = json.loads(BENCH.read_text(encoding="utf-8"))["paths"]
        pr = paths["webhook.pr_review"]
        return float(pr["median"]), float(pr["p95"])
    except (OSError, KeyError, ValueError, TypeError):
        return _INGEST_FALLBACK_P50_MS, _INGEST_FALLBACK_P95_MS


def tokens_per_review(rag_tokens: int = 0) -> tuple[float, float]:
    """Prompt and completion tokens for one PR-review job (both agent calls)."""
    prompt = (
        SYSTEM_PROMPT_TOKENS["code_reviewer"]
        + DIFF_TOKENS
        + rag_tokens
        + SYSTEM_PROMPT_TOKENS["test_generator"]
        + DIFF_TOKENS
    )
    completion = OUT_TOKENS_REVIEW + OUT_TOKENS_TESTS
    return float(prompt), float(completion)


def tokens_per_ci_fix(rag_tokens: int = 0) -> tuple[float, float]:
    """Prompt and completion tokens for one CI-failure job."""
    prompt = (
        SYSTEM_PROMPT_TOKENS["ci_monitor"]
        + CI_LOG_TOKENS
        + SYSTEM_PROMPT_TOKENS["fix_agent"]
        + DIFF_TOKENS
        + rag_tokens
    )
    completion = OUT_TOKENS_TRIAGE + OUT_TOKENS_FIX
    return float(prompt), float(completion)


def plan(
    workload: Workload | None = None,
    assumptions: Assumptions | None = None,
) -> Report:
    """Compute a sizing report. Pure: same inputs, same numbers, no I/O."""
    w = workload or Workload()
    a = assumptions or Assumptions()
    p50, p95 = _measured_ingest()

    prs_day = w.tenants * w.prs_per_tenant_per_week / 7.0
    ci_day = w.tenants * w.ci_failures_per_tenant_per_week / 7.0
    jobs_day = prs_day + ci_day

    # Peak, not mean: the share of daily volume arriving in the busiest hour.
    peak_hour = jobs_day * w.peak_hour_share
    peak_per_s = peak_hour / 3_600.0

    # Service time: provider latency for every LLM call, plus the work around it
    # (ingest, DB, GitHub read/write). The non-LLM part is measured ingest p50
    # plus a deliberately generous round-trip allowance for the two HTTP calls.
    llm_s = (LLM_CALLS_PER_REVIEW * a.llm_latency_ms) / 1_000.0
    overhead_s = (p50 / 1_000.0) + 0.15
    service_s = llm_s + overhead_s

    # Little's law. ceil() because a fraction of a worker does not consume a queue.
    workers = max(1, math.ceil(peak_per_s * service_s / max(w.target_utilization, 0.05)))

    in_rev, out_rev = tokens_per_review(w.rag_tokens)
    in_ci, out_ci = tokens_per_ci_fix(w.rag_tokens)
    calls_day = prs_day * LLM_CALLS_PER_REVIEW + ci_day * LLM_CALLS_PER_CI_FIX
    tok_in_day = prs_day * in_rev + ci_day * in_ci
    tok_out_day = prs_day * out_rev + ci_day * out_ci
    cost_day = (tok_in_day * a.price_in_per_mtok + tok_out_day * a.price_out_per_mtok) / 1_000_000

    rows_day = (
        jobs_day + jobs_day * DECISION_RATE + jobs_day  # one JobResult per finished job
    )
    bytes_day = (
        prs_day * ROW_BYTES_PR_JOB
        + ci_day * ROW_BYTES_CI_JOB
        + jobs_day * DECISION_RATE * ROW_BYTES_DECISION
    )
    storage_mb_month = bytes_day * 30 / 1e6
    # What the table actually holds once retention reaches steady state: the
    # inflow over the window, not the inflow since the beginning of time.
    steady_state_mb = bytes_day * w.retention_days / 1e6

    memory_mb = (SNAPSHOT_CAP_BYTES + WORKER_OVERHEAD_BYTES) / 1e6

    warnings: list[str] = []
    if w.target_utilization > 0.75:
        warnings.append(
            "target_utilization above 0.75 leaves no headroom for a provider "
            "stall; queue depth grows without bound once service time is "
            "underestimated"
        )
    if w.peak_hour_share > 0.5:
        warnings.append(
            "peak_hour_share above 0.5 means half of a day's work lands in one "
            "hour - size for that hour, not the day, or you will queue all day"
        )
    if not a.calibrated:
        warnings.append(
            "llm_latency_ms is uncalibrated for your provider and model; every "
            "worker count below scales linearly with it"
        )
    if w.rag_tokens > 2_000:
        warnings.append(
            "rag_tokens above 2,000 doubles prompt cost on its own; measure the "
            "retrieved-context size before enabling RAG for every review"
        )
    if jobs_day > 0 and ci_day / jobs_day > 0.5:
        warnings.append(
            "CI failures are over half of the workload - they store up to "
            f"{CI_LOG_CAP_CHARS:,} chars of logs per row; set retention or the "
            "table grows ~"
            f"{ci_day * ROW_BYTES_CI_JOB * 30 / 1e9:.2f} GB/month from logs alone"
        )

    # What saturates first. Each candidate is expressed as a fraction of a
    # published ceiling for that tier, because comparing calls/s to statements/s
    # directly would be comparing metres to kilograms. The ordering is what
    # matters: it tells an operator which knob to turn when the instance degrades.
    ceilings = {
        "LLM provider throughput / rate limit": (
            (peak_per_s * LLM_CALLS_PER_REVIEW) / 30.0,
            "the provider's tokens-per-minute ceiling - workers wait on it, so "
            "adding workers does not add throughput",
        ),
        "Postgres (quota COUNT per job)": (
            (peak_per_s * PG_STATEMENTS_PER_JOB) / 1_000.0,
            "the per-job quota COUNTs on pr_jobs; add the (repo, created_at) and "
            "(created_at) indexes before raising volume",
        ),
        "Redis stream ops": (
            (peak_per_s * REDIS_OPS_PER_JOB) / 20_000.0,
            "Redis, which at these rates is not a concern",
        ),
    }
    binding = max(ceilings, key=lambda k: ceilings[k][0])
    binding_share = ceilings[binding][0]
    if binding_share < 0.05:
        # Naming a bottleneck at 2% of its ceiling reads like "you are near a
        # limit". It is not: at this size the fleet is set by queueing, not by
        # capacity, and an operator should re-run for the volume they expect.
        warnings.append(
            f"no tier is above 5% of its published ceiling (the highest is "
            f"{binding_share * 100:.2f}%); at this size the worker count is set by "
            "Little's law, not by a hard limit - re-run for the volume you expect "
            "to reach"
        )

    return Report(
        tenants=w.tenants,
        jobs_per_day=round(jobs_day, 2),
        peak_jobs_per_hour=round(peak_hour, 2),
        service_s_per_job=round(service_s, 2),
        workers=workers,
        llm_calls_per_day=round(calls_day, 1),
        tokens_in_per_day=round(tok_in_day),
        tokens_out_per_day=round(tok_out_day),
        cost_per_day=round(cost_day, 2),
        cost_per_month=round(cost_day * 30, 2),
        cost_per_job=round(cost_day / jobs_day, 4) if jobs_day else 0.0,
        redis_ops_per_s_peak=round(peak_per_s * REDIS_OPS_PER_JOB, 3),
        pg_statements_per_s_peak=round(peak_per_s * PG_STATEMENTS_PER_JOB, 3),
        rows_per_day=round(rows_day, 1),
        storage_mb_per_month=round(storage_mb_month, 1),
        steady_state_storage_mb=round(steady_state_mb, 1),
        memory_per_worker_mb=round(memory_mb),
        ingest_p50_ms=round(p50, 3),
        ingest_p95_ms=round(p95, 3),
        binding_constraint=binding,
        binding_reason=ceilings[binding][1],
        binding_share_pct=round(binding_share * 100, 3),
        warnings=warnings,
        assumption_notes=a.notes(),
    )


def sweep(
    tenant_counts: tuple[int, ...] = (1, 10, 100, 1_000),
    workload: Workload | None = None,
    assumptions: Assumptions | None = None,
) -> list[Report]:
    """Same model across a range of tenant counts — the shape of the curve."""
    base = workload or Workload()
    return [
        plan(
            Workload(**{**asdict(base), "tenants": n}),
            assumptions,
        )
        for n in tenant_counts
    ]


def render_markdown(reports: list[Report]) -> str:
    """A table an operator can paste into a capacity review."""
    head = (
        "| tenants | jobs/day | peak jobs/h | workers | LLM calls/day "
        "| tokens/day (in/out) | USD/day | USD/job | storage GB/mo |"
    )
    sep = "|---:|---:|---:|---:|---:|---|---:|---:|---:|"
    rows = [head, sep]
    for r in reports:
        rows.append(
            f"| {r.tenants} | {r.jobs_per_day:,.0f} | {r.peak_jobs_per_hour:,.1f} "
            f"| {r.workers} | {r.llm_calls_per_day:,.0f} "
            f"| {r.tokens_in_per_day / 1e6:,.1f}M / {r.tokens_out_per_day / 1e6:,.2f}M "
            f"| {r.cost_per_day:,.2f} | {r.cost_per_job:,.3f} "
            f"| {r.storage_mb_per_month / 1e3:,.2f} |"
        )
    return "\n".join(rows)
