"""Redis Streams queue with consumer groups.

Why Streams (not a LIST)? A LIST (RPUSH/BLPOP) drops in-flight work on the
floor if a worker crashes between popping and finishing — the element is gone
from Redis with no record it was ever taken. Streams keep a per-consumer
Pending Entries List (PEL): a message read via XREADGROUP is not removed, it
stays "pending" against that consumer until XACK'd. If the consumer dies, the
entry is still in the PEL and another consumer can XAUTOCLAIM it after an idle
threshold. That is the durable, resumable, at-least-once delivery the Phase 1
spec asks for.

At-least-once means a message can be delivered twice (crash after work, before
ACK). We make that safe by requiring the *processing* to be idempotent — see
the worker's JobResult write. The queue's job is durability; the worker's job
is idempotency. Keeping them separate is the whole design.

This module wraps only the stream commands so the worker/tests stay readable
and so we can point ``redis`` at a real server or ``fakeredis`` in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import redis
from redis.exceptions import ResponseError

from app.config import settings


@dataclass(frozen=True)
class StreamMessage:
    id: str
    fields: dict[str, str]


class JobQueue:
    def __init__(
        self,
        client: redis.Redis,
        stream: str | None = None,
        group: str | None = None,
    ) -> None:
        self.client = client
        self.stream = stream or settings.stream_name
        self.group = group or settings.consumer_group

    # -- setup -----------------------------------------------------------
    def ensure_group(self) -> None:
        """Create the consumer group (and stream) if absent. Idempotent.

        MKSTREAM creates the stream so we can declare the group before any
        message exists. A second call raises BUSYGROUP, which we swallow.
        """
        try:
            self.client.xgroup_create(name=self.stream, groupname=self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # -- producer --------------------------------------------------------
    def publish(self, fields: dict[str, Any]) -> str:
        """XADD a job onto the stream, returning the entry id."""
        # _stringify yields dict[str, str]; redis-py types xadd's fields param as an
        # invariant Dict[FieldT, EncodableT], so a str-keyed dict needs a cast here.
        stream_fields = cast("dict[Any, Any]", _stringify(fields))
        return _as_str(self.client.xadd(self.stream, stream_fields))

    # -- consumer --------------------------------------------------------
    def consume(self, consumer: str, count: int = 1, block_ms: int = 5_000) -> list[StreamMessage]:
        """Read new (never-delivered) messages for this consumer.

        The special id ``>`` means "messages never delivered to any consumer
        in this group". Delivered-but-unacked messages are recovered via
        ``reclaim`` instead.
        """
        resp = self.client.xreadgroup(
            groupname=self.group,
            consumername=consumer,
            streams={self.stream: ">"},
            count=count,
            block=block_ms,
        )
        return _parse_xread(resp)

    def ack(self, message_id: str) -> int:
        """XACK — mark a message done so it leaves the PEL."""
        return int(self.client.xack(self.stream, self.group, message_id))

    def reclaim(
        self, consumer: str, min_idle_ms: int | None = None, count: int = 10
    ) -> list[StreamMessage]:
        """XAUTOCLAIM entries idle beyond the threshold to this consumer.

        This is crash recovery: a message stuck in a dead worker's PEL is
        transferred here so it gets processed again. Because processing is
        idempotent, reprocessing is safe.
        """
        idle = settings.reclaim_idle_ms if min_idle_ms is None else min_idle_ms
        # xautoclaim returns (next_cursor, claimed_messages, deleted_ids)
        result = self.client.xautoclaim(
            name=self.stream,
            groupname=self.group,
            consumername=consumer,
            min_idle_time=idle,
            start_id="0-0",
            count=count,
        )
        claimed = result[1] if len(result) >= 2 else []
        return _parse_entries(claimed)

    def pending_count(self) -> int:
        """Number of delivered-but-unacked messages (for tests / metrics)."""
        summary = self.client.xpending(self.stream, self.group)
        # xpending summary form: {'pending': N, 'min': ..., 'max': ..., 'consumers': ...}
        if isinstance(summary, dict):
            return int(summary.get("pending", 0))
        # Some clients return a list [count, min, max, consumers]
        return int(summary[0]) if summary else 0


# --- helpers -----------------------------------------------------------
def _stringify(fields: dict[str, Any]) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in fields.items()}


def _as_str(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _decode_fields(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, (bytes, bytearray)) else k
        val = v.decode() if isinstance(v, (bytes, bytearray)) else v
        out[key] = val
    return out


def _parse_entries(entries: list) -> list[StreamMessage]:
    messages: list[StreamMessage] = []
    for entry in entries:
        if entry is None:
            continue
        msg_id, fields = entry
        messages.append(StreamMessage(id=_as_str(msg_id), fields=_decode_fields(fields)))
    return messages


def _parse_xread(resp: Any) -> list[StreamMessage]:
    if not resp:
        return []
    # resp: [(stream_name, [(id, {fields}), ...]), ...]
    messages: list[StreamMessage] = []
    for _stream, entries in resp:
        messages.extend(_parse_entries(entries))
    return messages


def make_client(url: str | None = None) -> redis.Redis:
    """Build a Redis client with bounded timeouts and connection health checks.

    Every socket op is bounded (socket_timeout) so a dead server surfaces as a
    TimeoutError the caller can turn into a 503 rather than hanging a request
    thread; socket_connect_timeout bounds the initial connect; health_check_interval
    pings idle pooled connections so a silently-dropped one is detected and
    replaced; retry_on_timeout retries a single transient blip before raising.
    """
    return redis.Redis.from_url(
        url or settings.redis_url,
        socket_timeout=settings.redis_socket_timeout_s,
        socket_connect_timeout=settings.redis_connect_timeout_s,
        health_check_interval=settings.redis_health_check_interval_s,
        retry_on_timeout=True,
    )
