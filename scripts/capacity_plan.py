"""Print a capacity plan from the model in `app/capacity.py`.

    python scripts/capacity_plan.py
    python scripts/capacity_plan.py --tenants 1,10,100,1000 --llm-latency-ms 1800

Every number it prints is either measured in this repository or labelled
ASSUMPTION at the bottom. The two you are expected to calibrate are provider
latency and token price; everything else is pinned by code and by tests.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.capacity import Assumptions, Workload, plan, render_markdown, sweep  # noqa: E402


def _tenant_counts(text: str) -> tuple[int, ...]:
    try:
        counts = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as exc:  # pragma: no cover - argparse surfaces this
        raise argparse.ArgumentTypeError(f"--tenants wants integers: {exc}") from exc
    if not counts or any(n < 1 for n in counts):
        raise argparse.ArgumentTypeError("--tenants wants positive integers")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=_tenant_counts, default=(1, 10, 100, 1_000))
    parser.add_argument("--prs-per-tenant-week", type=float, default=20.0)
    parser.add_argument("--ci-failures-per-tenant-week", type=float, default=10.0)
    parser.add_argument("--peak-hour-share", type=float, default=0.20)
    parser.add_argument("--target-utilization", type=float, default=0.60)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--rag-tokens", type=int, default=0)
    parser.add_argument(
        "--llm-latency-ms",
        type=float,
        default=Assumptions().llm_latency_ms,
        help="time ten real reviews and pass the mean; until then this is an assumption",
    )
    parser.add_argument("--price-in-per-mtok", type=float, default=Assumptions().price_in_per_mtok)
    parser.add_argument(
        "--price-out-per-mtok", type=float, default=Assumptions().price_out_per_mtok
    )
    args = parser.parse_args(argv)

    workload = Workload(
        prs_per_tenant_per_week=args.prs_per_tenant_week,
        ci_failures_per_tenant_per_week=args.ci_failures_per_tenant_week,
        peak_hour_share=args.peak_hour_share,
        target_utilization=args.target_utilization,
        retention_days=args.retention_days,
        rag_tokens=args.rag_tokens,
    )
    # Calibrated only when the operator supplied it; the default stays labelled.
    assumptions = Assumptions(
        llm_latency_ms=args.llm_latency_ms,
        price_in_per_mtok=args.price_in_per_mtok,
        price_out_per_mtok=args.price_out_per_mtok,
        calibrated=args.llm_latency_ms != Assumptions().llm_latency_ms,
    )

    reports = sweep(args.tenants, workload, assumptions)
    one = plan(workload, assumptions)
    peak = max(reports, key=lambda r: r.tenants)

    print(render_markdown(reports))
    print()
    print(f"ingest p50/p95 (measured, in-process): {one.ingest_p50_ms} ms / {one.ingest_p95_ms} ms")
    print(f"service time per job:                 {one.service_s_per_job} s")
    print(f"memory per worker:                    {one.memory_per_worker_mb} MB")
    print(
        f"binding constraint at {peak.tenants} tenants: {peak.binding_constraint} "
        f"({peak.binding_share_pct}% of its published ceiling)"
    )
    print(f"  {peak.binding_reason}")

    # The "nothing is near a ceiling" warning is emitted once per volume with that
    # volume's percentage; collapse it to one line instead of one per row.
    warnings: dict[str, str] = {}
    for report in reports:
        for warning in report.warnings:
            warnings[warning.split(" (the highest is ")[0]] = warning
    if warnings:
        print("\nWarnings")
        for key in sorted(warnings):
            print(f"  - {warnings[key]}")

    print("\nAssumptions (not measured here - calibrate them)")
    for note in one.assumption_notes:
        print(f"  - {note}")
    print("\nBinding constraint is the ceiling that saturates first; see app/capacity.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
