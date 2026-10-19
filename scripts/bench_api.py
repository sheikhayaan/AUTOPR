"""Measure the AutoPR API's real latency and write docs/assets/bench_result.json.

Everything the README says about performance comes from this file. It is not a
load test (that is ``scripts/load_test.py``, which needs a running deployment);
this is a *latency profile* of the request paths a human actually exercises,
measured in-process against the real FastAPI app with fakeredis and SQLite so it
runs anywhere with no infra and is reproducible on any machine.

Method, so the numbers can be read honestly:

* ``TestClient`` drives the real ASGI app: routing, Pydantic validation, HMAC
  verification, SQLAlchemy, the routing policy, and the Redis stream client are
  all the production code paths. What is *not* real is the transport (no socket,
  no TLS) and the backing stores (fakeredis, in-process SQLite instead of
  Redis + Postgres). Expect a deployed run to be slower; the README says so.
* Each path is timed with ``perf_counter`` around a single request, and the
  first 10% of samples are discarded as warm-up before percentiles are computed.

Usage:
    python scripts/bench_api.py [--out docs/assets/bench_result.json]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The per-IP limiter would reject our own throughput (120 webhooks/min by
# default) and we are not measuring it here. Quieten logs too, so the table is
# readable on stdout.
os.environ["AUTOPR_RATE_LIMIT_ENABLED"] = "false"
os.environ["AUTOPR_LOG_JSON"] = "false"
os.environ["AUTOPR_ALLOW_INSECURE"] = "1"
# Approve is timed, so run the bench in hand-off mode: the CAS, the state
# transition and the audit log all happen, but no GitHub write is attempted.
# The token is cleared so the bench does not need the operator's bearer token.
os.environ["AUTOPR_HANDOFF_MODE"] = "true"
os.environ["AUTOPR_API_TOKEN"] = ""

from app.config import settings  # noqa: E402
from app.main import app, get_db, get_queue  # noqa: E402
from app.models import Base  # noqa: E402
from app.queue import JobQueue  # noqa: E402
from app.routing import store  # noqa: E402
from app.routing.github import FakeGitHubClient  # noqa: E402
from app.routing.policy import route  # noqa: E402
from app.security import compute_signature  # noqa: E402

# --- fixtures -----------------------------------------------------------------


def _client() -> tuple[TestClient, sessionmaker]:
    """Wire the real app to an in-memory SQLite DB and a fakeredis stream."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    queue = JobQueue(fakeredis.FakeStrictRedis(), stream="bench:jobs", group="bench-workers")
    queue.ensure_group()

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_queue] = lambda: queue
    # The lifespan (which builds the real GitHub client) is bypassed, so provide
    # the recording client the app would have installed. In hand-off mode approve
    # never reaches it, but the dependency must resolve.
    app.state.github = FakeGitHubClient()
    return TestClient(app), Session


def _pr_body(sha: str, repo: str = "acme/api", pr: int = 7) -> bytes:
    """A minimal but realistic pull_request webhook body."""
    return json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "number": pr,
                "head": {"sha": sha},
                "user": {"login": "octocat"},
                "title": "Fix divide by zero in totals",
            },
            "repository": {"full_name": repo},
        }
    ).encode()


def _ci_body(sha: str, repo: str = "acme/api") -> bytes:
    """A minimal check_run.failed webhook body."""
    return json.dumps(
        {
            "action": "completed",
            "check_run": {
                "conclusion": "failure",
                "head_sha": sha,
                "output": {"summary": "1 failed: test_totals_zero"},
            },
            "repository": {"full_name": repo},
        }
    ).encode()


def _headers(body: bytes, event: str) -> dict:
    """Sign a body the way GitHub would."""
    return {
        "X-Hub-Signature-256": compute_signature(settings.webhook_secret, body),
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }


def _pr_state(risk: str = "high", repo: str = "acme/api", sha: str = "c0mm1t") -> dict:
    """The graph state the router reads, for the pure-policy measurement."""
    return {
        "repo": repo,
        "pr_number": 7,
        "commit_sha": sha,
        "changed_files": [{"path": "a.py", "patch": "+x"}],
        "review_findings": [{"file": "a.py", "line": 1, "severity": "error", "message": "x"}],
        "risk_score": risk,
        "summary": "s",
    }


def _seed_decisions(Session, n: int) -> None:
    """Put n human-gated decisions in the database for the read paths."""
    s = Session()
    try:
        for i in range(n):
            state = _pr_state(repo=f"acme/svc-{i % 5}", sha=f"sha{i:04d}")
            store.enqueue(s, route(state), state)
    finally:
        s.close()


# --- measurement --------------------------------------------------------------


def _timings(fn, n: int) -> list[float]:
    """Run fn n times, returning milliseconds per call (warm-up discarded)."""
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    warm = max(1, n // 10)
    return samples[warm:]


def _stats(samples: list[float]) -> dict:
    """Percentile summary in milliseconds, rounded to 3 decimals."""
    ordered = sorted(samples)

    def pct(p: float) -> float:
        idx = min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))
        return round(ordered[idx], 3)

    return {
        "n": len(ordered),
        "min": round(ordered[0], 3),
        "mean": round(statistics.fmean(ordered), 3),
        "median": round(statistics.median(ordered), 3),
        "p95": pct(95),
        "p99": pct(99),
        "max": round(ordered[-1], 3),
    }


def main() -> int:
    """Run every measured path, print a table, and write the JSON artifact."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="docs/assets/bench_result.json")
    ap.add_argument("--scale", type=float, default=1.0, help="multiplier on sample counts")
    args = ap.parse_args()

    client, Session = _client()
    _seed_decisions(Session, 40)

    def n(base: int) -> int:
        return max(20, int(base * args.scale))

    counter = {"pr": 0, "ci": 0}

    def post_pr() -> None:
        counter["pr"] += 1
        body = _pr_body(sha=f"pr{counter['pr']:05d}", pr=counter["pr"])
        r = client.post("/webhook", content=body, headers=_headers(body, "pull_request"))
        assert r.status_code == 200, r.text

    def post_ci() -> None:
        counter["ci"] += 1
        body = _ci_body(sha=f"ci{counter['ci']:05d}")
        r = client.post("/webhook", content=body, headers=_headers(body, "check_run"))
        assert r.status_code == 200, r.text

    dup_body = _pr_body(sha="dupsha", pr=999)
    dup_headers = _headers(dup_body, "pull_request")
    client.post("/webhook", content=dup_body, headers=dup_headers)

    def post_duplicate() -> None:
        r = client.post("/webhook", content=dup_body, headers=dup_headers)
        assert r.status_code == 200 and r.json()["status"] == "duplicate", r.text

    def approve_one() -> None:
        pending = client.get("/reviews/pending").json()["pending"]
        if not pending:
            return
        r = client.post(f"/reviews/{pending[0]['id']}/approve")
        assert r.status_code in (200, 409), r.text

    def policy_route() -> None:
        route(_pr_state())

    paths: dict[str, dict] = {}

    print("measuring …")
    buckets: dict[str, tuple] = {
        "webhook.pr_review": (post_pr, n(200), "ingest a pull_request delivery"),
        "webhook.ci_failure": (post_ci, n(120), "ingest a failed check_run"),
        "webhook.duplicate": (post_duplicate, n(120), "duplicate delivery (no-op path)"),
        "read.stats": (lambda: client.get("/stats"), n(150), "GET /stats"),
        "read.jobs": (
            lambda: client.get("/jobs?limit=20&offset=0"),
            n(150),
            "GET /jobs page",
        ),
        "read.reviews": (
            lambda: client.get("/reviews?limit=20&offset=0"),
            n(150),
            "GET /reviews page",
        ),
        "read.pending": (lambda: client.get("/reviews/pending"), n(150), "GET /reviews/pending"),
        "review.approve": (approve_one, n(80), "POST /reviews/{id}/approve"),
        "policy.route": (policy_route, n(300), "routing policy, no HTTP"),
    }
    for name, (fn, count, desc) in buckets.items():
        samples = _timings(fn, count)
        paths[name] = {
            **_stats(samples),
            "description": desc,
            "samples_ms": [round(x, 3) for x in samples],
        }
        st = paths[name]
        print(
            f"  {name:<20} n={st['n']:<4} "
            f"p50={st['median']:>7.3f}  p95={st['p95']:>7.3f}  max={st['max']:>8.3f} ms"
        )

    # One real end-to-end ingest, timed step by step, for the "where time goes" table.
    body = _pr_body(sha="e2e0001", pr=1001)
    hdrs = _headers(body, "pull_request")
    t0 = time.perf_counter()
    sig = compute_signature(settings.webhook_secret, body)
    t1 = time.perf_counter()
    r = client.post("/webhook", content=body, headers={**hdrs, "X-Hub-Signature-256": sig})
    t2 = time.perf_counter()
    assert r.status_code == 200
    breakdown = {
        "hmac_sha256_ms": round((t1 - t0) * 1000, 4),
        "http_ingest_ms": round((t2 - t1) * 1000, 4),
        "body_bytes": len(body),
    }

    pending = client.get("/reviews/pending").json()
    result_extra = {
        "pending_after_run": pending["count"],
        "pending_actions": sorted({p["action"] for p in pending["pending"]}),
    }

    result = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": (
            "in-process ASGI (TestClient) + fakeredis + in-memory SQLite; "
            "real import of the app, real HMAC, real SQLAlchemy, real routing policy"
        ),
        "caveat": (
            "no socket, no TLS, no network to Redis/Postgres/GitHub; a deployed run "
            "is slower. Use scripts/load_test.py against a live deployment for that."
        ),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": __import__("os").cpu_count(),
        },
        "paths": paths,
        "e2e_breakdown": breakdown,
        "corpus": {"decisions_seeded": 40, "jobs_ingested": counter["pr"] + counter["ci"]},
        **result_extra,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}  ({out.stat().st_size} bytes)")
    app.dependency_overrides.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
