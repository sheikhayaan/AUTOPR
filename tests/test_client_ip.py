"""Who does the rate limiter — and the audit log — think is calling?

The failure this pins is subtle and only shows up in production: behind a reverse
proxy every request looks like the proxy, so the per-IP limiter silently becomes a
single global bucket. The naive fix (always read `X-Forwarded-For`) is worse than
the bug: any client could send a fresh value per request and never be limited at
all. These tests assert both directions, because a resolver that trusts the header
too eagerly is a bypass, not a fix.
"""

from __future__ import annotations

import pytest

from app.ratelimit import resolve_client_ip

NGINX = ("10.0.0.5",)
FLY = ("172.16.0.0/12",)


def test_direct_client_is_identified_by_its_socket_peer():
    assert resolve_client_ip("203.0.113.7", None) == "203.0.113.7"


def test_forwarded_header_is_ignored_from_an_untrusted_peer():
    """The spoofing case: a direct client sets the header to escape its bucket."""
    assert resolve_client_ip("203.0.113.7", "1.2.3.4") == "203.0.113.7"


def test_forwarded_header_is_read_when_the_peer_is_a_trusted_proxy():
    assert resolve_client_ip("10.0.0.5", "203.0.113.7", NGINX) == "203.0.113.7"


def test_a_multi_hop_chain_resolves_to_the_client_not_the_proxies():
    """`client, proxy1, proxy2` as appended by each hop — walk from the right."""
    chain = "203.0.113.7, 10.0.0.9, 10.0.0.5"
    assert resolve_client_ip("10.0.0.5", chain, ("10.0.0.5", "10.0.0.9")) == "203.0.113.7"


def test_a_forged_leftmost_entry_cannot_win_while_a_trusted_hop_is_last():
    """The attacker controls what they send; the proxy appends what it saw.

    So the rightmost untrusted hop is the real client, and everything left of it
    is text the attacker chose.
    """
    forged = "9.9.9.9, 203.0.113.7, 10.0.0.5"
    assert resolve_client_ip("10.0.0.5", forged, NGINX) == "203.0.113.7"


def test_cidr_entries_are_supported_for_platform_proxies():
    assert resolve_client_ip("172.16.3.4", "203.0.113.7", FLY) == "203.0.113.7"


def test_all_hops_trusted_falls_back_to_the_leftmost_entry():
    """Nothing in the chain identifies a client; better to share a bucket than
    to invent an identity."""
    assert (
        resolve_client_ip("10.0.0.5", "10.0.0.9, 10.0.0.5", ("10.0.0.5", "10.0.0.9")) == "10.0.0.9"
    )


@pytest.mark.parametrize("garbage", ["not-an-ip", "203.0.113.7:443", "", " , "])
def test_unparseable_forwarded_values_fall_back_to_the_peer(garbage):
    assert resolve_client_ip("10.0.0.5", garbage, NGINX) == "10.0.0.5"


def test_malformed_trusted_entry_does_not_widen_trust():
    """A typo in AUTOPR_TRUSTED_PROXIES must fail closed, not open."""
    assert resolve_client_ip("203.0.113.7", "1.2.3.4", ("10.0.0.0/99",)) == "203.0.113.7"


def test_missing_peer_is_unknown_not_empty():
    assert resolve_client_ip("", "203.0.113.7", NGINX) == "unknown"


def test_trusted_proxies_setting_parses_into_a_tuple():
    from app.config import Settings

    s = Settings(
        webhook_secret="x" * 32,
        api_token="y" * 32,
        allow_insecure=False,
        _env_file=None,
        trusted_proxies=" 10.0.0.5 , 172.16.0.0/12 ,, ",
    )
    assert s.trusted_proxy_list == ("10.0.0.5", "172.16.0.0/12")
