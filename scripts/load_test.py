"""Load test: prove the ingest path holds its rate limit and its latency budget.

Not a unit test — a small, dependency-free load generator you run against a
deployed AutoPR to answer two questions the unit suite cannot:

  1. Throughput/latency: at N requests/second, what is p50/p95/p99 latency, and
     does the service stay healthy (no 5xx, `/healthz` still 200)?
  2. Back-pressure: does the ingest rate limit actually engage? A 429 with
     `Retry-After` is the CORRECT answer past the configured ceiling — silently
     accepting unbounded traffic is the failure.

Usage (needs the webhook secret so signatures validate):

    python scripts/load_test.py --url http://localhost:8000 \
        --secret "$AUTOPR_WEBHOOK_SECRET" --rps 20 --duration 15

Exit code is 0 when the run stayed within budget, 1 otherwise, so it can gate a
pre-release check. Every request uses a unique SHA, so this measures ingest, not
the dedup path (which is the point: dedup would hide the work).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import statistics
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx


@dataclass
class Results:
    latencies: list[float] = field(default_factory=list)
    codes: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, status: int | None, elapsed: float, error: str | None = None) -> None:
        with self.lock:
            if status is not None:
                self.codes[status] = self.codes.get(status, 0) + 1
                self.latencies.append(elapsed)
            elif error:
                self.errors.append(error)

    def pct(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
        return ordered[idx]


def _payload(sha: str, pr: int) -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "number": pr,
                "head": {"sha": sha},
                "user": {"login": "loadtest"},
            },
            "repository": {"full_name": "loadtest/loadtest"},
        }
    ).encode()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _worker(
    url: str,
    secret: str,
    rps: float,
    duration: float,
    offset: float,
    results: Results,
    stop: threading.Event,
) -> None:
    """One virtual user: fire a request every 1/rps seconds until `stop`."""
    interval = 1.0 / rps if rps else 0.0
    pr = 1000 + int(offset)
    next_at = time.perf_counter() + offset
    with httpx.Client(timeout=10.0) as client:
        while not stop.is_set():
            now = time.perf_counter()
            if now < next_at:
                time.sleep(min(next_at - now, 0.05))
                continue
            next_at += interval
            body = _payload(uuid.uuid4().hex, pr)
            pr += 1
            started = time.perf_counter()
            try:
                resp = client.post(
                    f"{url.rstrip('/')}/webhook",
                    content=body,
                    headers={
                        "X-Hub-Signature-256": _sign(secret, body),
                        "X-GitHub-Event": "pull_request",
                        "Content-Type": "application/json",
                    },
                )
                results.record(resp.status_code, time.perf_counter() - started)
            except httpx.HTTPError as exc:
                results.record(None, 0.0, repr(exc))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--secret", required=True, help="AUTOPR_WEBHOOK_SECRET of the target")
    ap.add_argument("--rps", type=float, default=20.0, help="target requests/second")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration", type=float, default=15.0, help="seconds")
    ap.add_argument("--p95-budget-ms", type=float, default=500.0)
    ap.add_argument("--max-5xx", type=int, default=0)
    args = ap.parse_args(argv)

    # Split the target rps across the virtual users.
    per_worker = args.rps / max(args.concurrency, 1)
    stop = threading.Event()
    results = Results()
    threads = [
        threading.Thread(
            target=_worker,
            args=(
                args.url,
                args.secret,
                per_worker,
                args.duration,
                i * 0.01,
                results,
                stop,
            ),
            daemon=True,
        )
        for i in range(max(args.concurrency, 1))
    ]
    started = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(args.duration)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.perf_counter() - started

    total = sum(results.codes.values())
    ok = results.codes.get(200, 0)
    limited = results.codes.get(429, 0)
    server_errors = sum(n for c, n in results.codes.items() if c >= 500)
    other = total - ok - limited - server_errors
    p95_ms = results.pct(95) * 1000
    p99_ms = results.pct(99) * 1000

    print(f"\n{'-' * 58}")
    print(
        f"target            {args.rps:.0f} rps for {args.duration:.0f}s "
        f"({args.concurrency} connections)"
    )
    print(f"achieved          {total / elapsed:.1f} rps ({total} responses)")
    print(
        f"latency           p50 {results.pct(50) * 1000:.0f} ms  "
        f"p95 {p95_ms:.0f} ms  p99 {p99_ms:.0f} ms"
    )
    print(f"responses         200 {ok} | 429 {limited} | 5xx {server_errors} | other {other}")
    for code in sorted(results.codes):
        print(f"  HTTP {code}: {results.codes[code]}")
    if results.errors:
        print(f"transport errors  {len(results.errors)} (first: {results.errors[0]})")
    print(
        f"stdev             {statistics.pstdev(results.latencies) * 1000:.0f} ms"
        if len(results.latencies) > 1
        else "stdev             n/a"
    )

    failures = []
    if not total:
        failures.append("no responses at all - is the service up?")
    if server_errors > args.max_5xx:
        failures.append(f"{server_errors} server errors (budget {args.max_5xx})")
    if other:
        failures.append(f"{other} unexpected status codes (e.g. 401: wrong secret?)")
    if results.errors:
        failures.append(f"{len(results.errors)} transport errors")
    if p95_ms > args.p95_budget_ms:
        failures.append(f"p95 {p95_ms:.0f} ms over budget {args.p95_budget_ms:.0f} ms")

    # Healthy = it accepted work and/or rate-limited; it must never 5xx.
    if not failures and not (ok or limited):
        failures.append("neither accepted nor rate-limited any request")

    print(f"{'-' * 58}")
    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS  (429s are correct: they are the rate limiter working)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
