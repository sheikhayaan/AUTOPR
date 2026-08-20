"""HMAC signature verification tests (the 401 path)."""

from __future__ import annotations

from app.security import compute_signature, verify_signature

SECRET = "test-secret"
BODY = b'{"action":"opened","number":1}'


def test_valid_signature_accepted():
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY, sig) is True


def test_wrong_secret_rejected():
    sig = compute_signature("other-secret", BODY)
    assert verify_signature(SECRET, BODY, sig) is False


def test_tampered_body_rejected():
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, b'{"action":"closed"}', sig) is False


def test_missing_header_rejected():
    assert verify_signature(SECRET, BODY, None) is False


def test_malformed_header_rejected():
    # No sha256= prefix.
    assert verify_signature(SECRET, BODY, "deadbeef") is False
