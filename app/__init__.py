"""AutoPR — multi-agent PR automation.

Phase 1: idempotent GitHub webhook ingestion + durable Redis Streams queue
+ crash-safe worker. Later phases layer LangGraph agents, a sandboxed fix
verifier, risk routing, telemetry, load tests, and an eval harness on top.
"""

__version__ = "0.1.0"
