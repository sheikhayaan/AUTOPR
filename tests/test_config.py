"""Settings-derivation tests.

Small, focused guards for the computed properties on ``Settings`` — the bits of
config that are *derived* rather than read straight from the environment, where a
silent regression would quietly mislead the UI or the CORS layer.
"""

from __future__ import annotations

import pytest

from app.config import settings


# --- github_web_url ----------------------------------------------------------
@pytest.mark.parametrize(
    ("api_url", "expected"),
    [
        # Public GitHub: the API host (api.github.com) is not the web host.
        ("https://api.github.com", "https://github.com"),
        # Enterprise: API lives under a path; the web UI is the bare host.
        ("https://ghe.example.com/api/v3", "https://ghe.example.com"),
        ("https://git.corp.internal/api/v3", "https://git.corp.internal"),
        # A trailing slash on the base must not leak into the derived origin.
        ("https://ghe.example.com/api/v3/", "https://ghe.example.com"),
    ],
)
def test_github_web_url_derivation(monkeypatch, api_url, expected):
    """The dashboard's PR links come from this derivation, not a hard-coded host."""
    monkeypatch.setattr(settings, "github_api_url", api_url, raising=False)
    assert settings.github_web_url == expected
