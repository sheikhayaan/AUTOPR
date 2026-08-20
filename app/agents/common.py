"""Shared helpers for agents: retry-with-backoff and robust JSON parsing.

The spec requires "retry logic with backoff on any LLM call failure". We use
tenacity (already a LangChain dependency) for exponential backoff. LLMs also
frequently wrap JSON in markdown fences or add prose, so `parse_json` is
defensive.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

import structlog
from langchain_core.messages import BaseMessage
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger()

T = TypeVar("T")

# Exceptions worth retrying: transient network / rate-limit errors. We retry
# broadly (Exception) but cap attempts so a deterministic failure (bad key)
# doesn't loop forever — it fails after 3 tries and surfaces.
llm_retry = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda rs: log.warning(
        "llm.retry",
        attempt=rs.attempt_number,
        error=repr(rs.outcome.exception()) if rs.outcome else "unknown",
    ),
)


@llm_retry
def invoke_llm(llm, messages: list) -> BaseMessage:
    """Invoke an LLM with retry/backoff. Returns the response message."""
    return llm.invoke(messages)


def message_text(message: BaseMessage) -> str:
    """Return an LLM response's content as a plain string.

    langchain types ``BaseMessage.content`` as ``str | list[...]`` to allow
    multimodal content blocks. Our chat models always return text, but we
    normalize defensively so callers can ``.strip()``/parse without a type guard:
    a list is flattened to its string parts.
    """
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(part for part in content if isinstance(part, str))


def parse_json(text: str) -> Any:
    """Extract and parse JSON from an LLM response.

    Handles: raw JSON, ```json fenced blocks, and JSON embedded in prose.
    Raises ValueError if no valid JSON can be found.
    """
    text = text.strip()

    # 1. Try direct parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Try fenced code block ```json ... ``` or ``` ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. Try to find the first {...} or [...] span.
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue

    raise ValueError(f"No valid JSON found in LLM response: {text[:200]!r}")


def format_changed_files(changed_files: list[dict[str, str]], max_chars: int = 8000) -> str:
    """Render changed files (path + patch) into a prompt-friendly string."""
    parts: list[str] = []
    budget = max_chars
    for f in changed_files:
        path = f.get("path", "unknown")
        patch = f.get("patch", "")
        block = f"### File: {path}\n```diff\n{patch}\n```\n"
        if len(block) > budget:
            block = block[:budget] + "\n... (truncated)\n"
        parts.append(block)
        budget -= len(block)
        if budget <= 0:
            break
    return "\n".join(parts)
