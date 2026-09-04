"""Groq LLM client factory shared across all agents.

Centralizes model config so we tune temperature/model in one place. All agents
call `get_llm()` rather than instantiating ChatGroq directly — this makes it
trivial to mock in tests (patch this one function) and to swap models later.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_groq import ChatGroq
from pydantic import SecretStr

from app.config import settings

# GPT-OSS 120B via Groq: strong reasoning, fast inference, generous free tier.
# (Groq retired llama-3.3-70b-versatile; this is the current large general model.)
# Overridable via AUTOPR_LLM_MODEL for easy swaps as Groq's catalog changes.
DEFAULT_MODEL = settings.llm_model or "openai/gpt-oss-120b"


@lru_cache(maxsize=8)
def get_llm(temperature: float = 0.0, model: str = DEFAULT_MODEL) -> ChatGroq:
    """Return a cached ChatGroq client.

    temperature=0.0 by default for deterministic, reviewable output. Cached by
    (temperature, model) so we reuse connections across agent calls within a
    worker process.
    """
    return ChatGroq(
        model=model,
        temperature=temperature,
        api_key=SecretStr(settings.groq_api_key),
        max_retries=2,
        # `request_timeout` is a pydantic field on ChatGroq (aliased to `timeout`,
        # populate-by-name). It's accepted and stored at runtime — verified in
        # tests/test_reliability.py — but mypy can't see aliased fields without
        # the pydantic plugin, so it mis-reads this as an unknown kwarg.
        request_timeout=settings.llm_timeout_s,  # type: ignore[call-arg]
    )
