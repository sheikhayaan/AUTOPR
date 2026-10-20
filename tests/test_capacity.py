"""Tests for the capacity model.

The module's whole claim is that its arithmetic is auditable, so these tests do
not check "it returns a number". They check the two things that actually matter:

1. **The measured constants really are measured.** The ingest numbers must come
   from `docs/assets/bench_result.json` (and follow it when it is regenerated),
   not from a hard-coded copy that silently drifts from the docs.
2. **Every unmeasurable input is still labelled as an assumption** in the output.
   A capacity report that quietly implies it measured provider latency is worse
   than no report, because somebody will size a fleet with it.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from app import capacity
from app.capacity import Assumptions, Workload, plan, sweep, tokens_per_ci_fix, tokens_per_review


def test_ingest_numbers_come_from_the_bench_artifact() -> None:
    """The model reads the measured JSON, not a stale copy of it."""
    artifact = json.loads(capacity.BENCH.read_text(encoding="utf-8"))["paths"]
    pr = artifact["webhook.pr_review"]

    p50, p95 = capacity._measured_ingest()

    assert p50 == pytest.approx(pr["median"])
    assert p95 == pytest.approx(pr["p95"])
    # ...and the fallback is not what produced them, or regenerating the bench
    # would change nothing here and this test would pass for the wrong reason.
    assert (
        p50 != capacity._INGEST_FALLBACK_P50_MS or pr["median"] == capacity._INGEST_FALLBACK_P50_MS
    )


def test_missing_artifact_falls_back_without_crashing(tmp_path, monkeypatch) -> None:
    """A checkout without the generated artifact still plans, using named constants."""
    monkeypatch.setattr(capacity, "BENCH", tmp_path / "absent.json")

    p50, p95 = capacity._measured_ingest()

    assert (p50, p95) == (capacity._INGEST_FALLBACK_P50_MS, capacity._INGEST_FALLBACK_P95_MS)


def test_report_tracks_the_artifact_when_it_changes(tmp_path, monkeypatch) -> None:
    """Bumping the measured p50 in the JSON moves the report's ingest figure."""
    fake = tmp_path / "bench.json"
    fake.write_text(
        json.dumps({"paths": {"webhook.pr_review": {"median": 99.5, "p95": 123.0}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(capacity, "BENCH", fake)

    report = plan()

    assert report.ingest_p50_ms == pytest.approx(99.5)
    assert report.ingest_p95_ms == pytest.approx(123.0)


def test_workers_follow_littles_law() -> None:
    """workers = ceil(peak arrival rate x service time / target utilisation)."""
    workload = Workload(tenants=50, target_utilization=0.6)
    assumptions = Assumptions(llm_latency_ms=2_000.0)

    report = plan(workload, assumptions)

    jobs_day = 50 * 20.0 / 7.0 + 50 * 10.0 / 7.0
    peak_per_s = jobs_day * workload.peak_hour_share / 3_600.0
    service_s = (
        capacity.LLM_CALLS_PER_REVIEW * assumptions.llm_latency_ms / 1_000.0
        + report.ingest_p50_ms / 1_000.0
        + 0.15
    )
    expected = max(1, math.ceil(peak_per_s * service_s / workload.target_utilization))

    assert report.workers == expected
    assert report.service_s_per_job == pytest.approx(service_s, abs=0.01)


def test_lower_target_utilisation_needs_more_workers() -> None:
    """Headroom is a cost: it must show up as a bigger fleet, not a smaller one."""
    busy = plan(Workload(tenants=200, target_utilization=0.9))
    safe = plan(Workload(tenants=200, target_utilization=0.3))

    assert safe.workers > busy.workers


def test_never_plans_zero_workers() -> None:
    """One tenant with one PR a week still needs a worker; a fraction cannot consume."""
    assert (
        plan(
            Workload(tenants=1, prs_per_tenant_per_week=1, ci_failures_per_tenant_per_week=0)
        ).workers
        == 1
    )


def test_volume_scales_linearly_and_workers_do_not_shrink() -> None:
    """Doubling tenants doubles the work and can never reduce the fleet."""
    small = sweep(tenant_counts=(10, 20))
    # jobs_per_day is rounded to 2 dp for display, so compare at that precision.
    assert small[0].jobs_per_day * 2 == pytest.approx(small[1].jobs_per_day, abs=0.02)
    assert small[1].workers >= small[0].workers

    curve = sweep(tenant_counts=(1, 10, 100, 1_000))
    assert [r.tenants for r in curve] == [1, 10, 100, 1_000]
    assert [r.workers for r in curve] == sorted(r.workers for r in curve)


def test_tokens_and_cost_arithmetic() -> None:
    """Cost is token counts times prices, recomputed here from the raw formulas."""
    workload = Workload(tenants=10)
    assumptions = Assumptions(price_in_per_mtok=1.0, price_out_per_mtok=2.0)

    report = plan(workload, assumptions)

    # Recompute independently, from the unrounded job counts (not the rounded
    # ones in the report): jobs_per_day = tenants * (pr + ci) / 7.
    prs_day = workload.tenants * workload.prs_per_tenant_per_week / 7.0
    ci_day = workload.tenants * workload.ci_failures_per_tenant_per_week / 7.0
    in_rev, out_rev = tokens_per_review()
    in_ci, out_ci = tokens_per_ci_fix()
    tok_in = prs_day * in_rev + ci_day * in_ci
    tok_out = prs_day * out_rev + ci_day * out_ci
    cost_day = (tok_in * 1.0 + tok_out * 2.0) / 1_000_000

    assert report.tokens_in_per_day == pytest.approx(round(tok_in), abs=0.5)
    assert report.tokens_out_per_day == pytest.approx(round(tok_out), abs=0.5)
    assert report.cost_per_day == pytest.approx(cost_day, abs=0.005)  # 2 dp
    # The monthly figure comes from the unrounded daily cost, so it is more precise
    # than 30x the displayed daily number (which is rounded to cents first).
    assert report.cost_per_month == pytest.approx(cost_day * 30, abs=0.02)
    assert report.cost_per_job == pytest.approx(cost_day / report.jobs_per_day, abs=5e-4)
    # Output tokens are priced above input tokens, so the mix matters.
    assert report.cost_per_day > tok_in / 1_000_000


def test_token_estimates_match_their_parts() -> None:
    """A CI job prompts with logs; a review does not. Both scale with RAG context."""
    review_in, review_out = tokens_per_review()
    assert review_in == pytest.approx(
        capacity.SYSTEM_PROMPT_TOKENS["code_reviewer"]
        + capacity.DIFF_TOKENS
        + capacity.SYSTEM_PROMPT_TOKENS["test_generator"]
        + capacity.DIFF_TOKENS
    )
    assert review_out == capacity.OUT_TOKENS_REVIEW + capacity.OUT_TOKENS_TESTS

    # RAG context is added to the prompt of each job type, exactly once.
    assert tokens_per_review(rag_tokens=500)[0] - review_in == pytest.approx(500)
    assert tokens_per_ci_fix(rag_tokens=500)[0] - tokens_per_ci_fix()[0] == pytest.approx(500)
    # CI logs are the reason a CI job is the more expensive prompt.
    assert tokens_per_ci_fix()[0] > tokens_per_review()[0]


def test_storage_is_steady_state_not_cumulative() -> None:
    """Retention caps the table; 'GB/month' and 'GB held' are different numbers."""
    report = plan(Workload(retention_days=30))

    # steady state = one month of inflow, by construction
    assert report.steady_state_storage_mb == pytest.approx(report.storage_mb_per_month, rel=1e-6)

    week = plan(Workload(retention_days=7))
    assert week.steady_state_storage_mb < week.storage_mb_per_month


def test_every_report_labels_its_assumptions() -> None:
    """The honesty property: unmeasured inputs say so, in the output itself."""
    report = plan()

    assert report.assumption_notes, "a report with no assumption notes is lying about precision"
    assert all("ASSUMPTION" in note or "calibrated" in note for note in report.assumption_notes)
    assert any("llm_latency_ms" in note for note in report.assumption_notes)
    assert any("price_in/out" in note for note in report.assumption_notes)
    # Uncalibrated latency must also produce a warning, not just a footnote.
    assert any("uncalibrated" in warning for warning in report.warnings)


def test_calibrated_assumptions_drop_the_calibration_warning() -> None:
    """Once the operator measures latency, the warning goes away — and only it."""
    uncalibrated = plan()
    calibrated = plan(assumptions=Assumptions(llm_latency_ms=1_800.0, calibrated=True))

    assert any("uncalibrated" in warning for warning in uncalibrated.warnings)
    assert not any("uncalibrated" in warning for warning in calibrated.warnings)
    assert calibrated.assumption_notes  # prices are still assumptions


@pytest.mark.parametrize(
    ("workload", "needle"),
    [
        (Workload(target_utilization=0.9), "headroom"),
        (Workload(peak_hour_share=0.6), "peak_hour_share"),
        (Workload(rag_tokens=5_000), "RAG"),
        (Workload(ci_failures_per_tenant_per_week=100), "logs"),
    ],
)
def test_pathological_input_warns(workload: Workload, needle: str) -> None:
    """Each knob that quietly breaks a fleet gets a warning when pushed too far."""
    report = plan(workload)

    assert any(needle in warning for warning in report.warnings)


def test_binding_constraint_names_a_known_ceiling() -> None:
    """The ordering is the useful part, so it must always resolve to a named tier."""
    known = (
        "LLM provider throughput / rate limit",
        "Postgres (quota COUNT per job)",
        "Redis stream ops",
    )

    for report in sweep(tenant_counts=(1, 10, 1_000)):
        assert report.binding_constraint in known
        # The tier name and the advice for turning that knob are separate fields.
        assert report.binding_reason and report.binding_reason != report.binding_constraint


def test_binding_share_says_how_full_that_tier_is() -> None:
    """Naming a bottleneck is only useful with 'how close are you to it'."""
    small = plan(Workload(tenants=1))
    large = plan(Workload(tenants=10_000))

    assert 0 <= small.binding_share_pct < 100
    assert large.binding_share_pct > small.binding_share_pct
    # At demo scale nothing is near a ceiling, and the report says so rather than
    # implying the queue is about to fall over.
    assert small.binding_share_pct < 5
    assert any("no tier is above 5%" in warning for warning in small.warnings)
    assert not any("no tier is above 5%" in warning for warning in large.warnings)


def test_plan_is_pure_and_deterministic() -> None:
    """Same inputs, same numbers — no clock, no I/O beyond the artifact."""
    assert plan().to_dict() == plan().to_dict()
    assert plan(Workload(tenants=7)).to_dict() != plan(Workload(tenants=8)).to_dict()


def test_markdown_renders_one_row_per_report() -> None:
    """The paste-into-a-review table has no NaN, no None and the right shape."""
    reports = sweep(tenant_counts=(10, 100))
    markdown = capacity.render_markdown(reports)
    lines = markdown.splitlines()

    assert len(lines) == len(reports) + 2  # header + separator
    assert not re.search(r"(nan|none)", markdown.lower())
    assert lines[0].startswith("| tenants")
    assert all(line.count("|") == lines[0].count("|") for line in lines)


def test_cli_prints_a_plan_and_flags_uncalibrated_inputs(capsys) -> None:
    """The script is the way an operator reaches the model; it must label itself."""
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "scripts" / "capacity_plan.py"
    spec = importlib.util.spec_from_file_location("capacity_plan", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["capacity_plan"] = module
    spec.loader.exec_module(module)

    assert module.main(["--tenants", "1,10", "--llm-latency-ms", "1800"]) == 0
    out = capsys.readouterr().out

    assert "| tenants | jobs/day |" in out
    assert "ASSUMPTION" in out
    assert "price_in/out" in out
    # Latency was supplied, so it is reported as calibrated, not flagged unmeasured.
    assert "was supplied by the operator" in out
    assert "binding constraint at 10 tenants" in out
