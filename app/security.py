"""GitHub webhook signature verification.

GitHub signs each webhook body with HMAC-SHA256 keyed by the shared secret
and sends it as ``X-Hub-Signature-256: sha256=<hex>``. We recompute the HMAC
over the *raw* request body (not the re-serialized JSON — key order/whitespace
would differ) and compare in constant time.
"""

from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Return True iff ``header`` is a valid signature for ``body``.

    Uses ``hmac.compare_digest`` for a constant-time comparison so we don't
    leak information about the expected signature via timing.
    """
    if not header or not header.startswith(_PREFIX):
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, header)
