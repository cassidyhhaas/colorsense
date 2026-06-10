"""Unit tests for ``colorsense.net.guard.block_private_networks`` — pure, network-free.

The resolver is injected everywhere, so no DNS lookup ever happens; the tests assert the
*policy* (which addresses are fetchable, fail-closed behavior, caching, allowlist
narrowing) in isolation from the browser and the politeness machinery.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable

import pytest

from colorsense import block_private_networks
from colorsense.net.guard import IPAddress, _is_public_address

PUBLIC_V4 = ipaddress.ip_address("93.184.216.34")
PUBLIC_V6 = ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946")


class RecordingResolver:
    """Injectable resolver mapping hostname -> addresses; counts calls per host."""

    def __init__(self, mapping: dict[str, list[IPAddress]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[IPAddress]:
        self.calls.append(host)
        try:
            return self.mapping[host]
        except KeyError as err:
            raise OSError(f"no such host {host!r}") from err


def guard_for(mapping: dict[str, list[IPAddress]]) -> Callable[[str], bool]:
    return block_private_networks(resolver=RecordingResolver(mapping))


# -- address classification ---------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.8",  # RFC 1918
        "172.16.5.4",  # RFC 1918
        "192.168.1.1",  # RFC 1918
        "169.254.169.254",  # link-local: the cloud metadata endpoint
        "100.64.0.1",  # CGNAT 100.64.0.0/10
        "0.0.0.0",  # unspecified
        "224.0.0.251",  # multicast
        "240.0.0.1",  # reserved
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fd00::1",  # IPv6 ULA
        "::",  # IPv6 unspecified
        "ff02::1",  # IPv6 multicast
    ],
)
def test_non_public_addresses_rejected(address: str) -> None:
    assert not _is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6])
def test_public_addresses_accepted(address: IPAddress) -> None:
    assert _is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:127.0.0.1",  # mapped loopback
        "::ffff:169.254.169.254",  # mapped link-local: the cloud metadata endpoint
        "::ffff:10.0.0.1",  # mapped RFC 1918
    ],
)
def test_ipv4_mapped_non_public_addresses_rejected(address: str) -> None:
    # Classification must follow the *embedded* IPv4 address, not the v6 wrapper's flags:
    # resolver stacks can return ::ffff:a.b.c.d, and the connection goes to a.b.c.d.
    assert not _is_public_address(ipaddress.ip_address(address))


def test_ipv4_mapped_public_address_accepted() -> None:
    assert _is_public_address(ipaddress.ip_address(f"::ffff:{PUBLIC_V4}"))


# -- the guard predicate ------------------------------------------------------


def test_public_host_allowed() -> None:
    guard = guard_for({"example.com": [PUBLIC_V4, PUBLIC_V6]})
    assert guard("https://example.com/page") is True


@pytest.mark.parametrize(
    "resolved",
    [
        [ipaddress.ip_address("127.0.0.1")],
        [ipaddress.ip_address("10.1.2.3")],
        [ipaddress.ip_address("169.254.169.254")],
        [ipaddress.ip_address("::1")],
        # Split-horizon shape: one public record must not whitelist the internal one.
        [PUBLIC_V4, ipaddress.ip_address("192.168.0.10")],
    ],
)
def test_hosts_resolving_to_non_public_addresses_rejected(resolved: list[IPAddress]) -> None:
    guard = guard_for({"evil.example": resolved})
    assert guard("http://evil.example/") is False


def test_ip_literal_metadata_endpoint_rejected() -> None:
    # IP literals resolve to themselves; no mapping entry needed with the real resolver,
    # but here the injected resolver supplies the literal explicitly.
    guard = guard_for({"169.254.169.254": [ipaddress.ip_address("169.254.169.254")]})
    assert guard("http://169.254.169.254/latest/meta-data/") is False


def test_ip_literals_pass_through_default_resolver() -> None:
    # The default getaddrinfo resolver maps literals to themselves with no network round
    # trip, so the guard classifies them directly: loopback rejected, public allowed.
    guard = block_private_networks()
    assert guard("http://127.0.0.1/") is False
    assert guard("http://[::1]/") is False
    assert guard(f"http://{PUBLIC_V4}/") is True


def test_non_http_schemes_rejected_without_resolving() -> None:
    resolver = RecordingResolver({})
    guard = block_private_networks(resolver=resolver)
    for url in ("ftp://example.com/", "file:///etc/passwd", "data:text/html,hi", "about:blank"):
        assert guard(url) is False
    assert resolver.calls == []


def test_userinfo_rejected_without_resolving() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(resolver=resolver)
    assert guard("https://user:pass@example.com/") is False
    assert resolver.calls == []


def test_missing_host_rejected() -> None:
    assert guard_for({})("https:///nohost") is False


def test_malformed_url_fails_closed() -> None:
    assert guard_for({})("https://[::1/broken") is False


def test_resolver_failure_fails_closed() -> None:
    guard = guard_for({})  # every lookup raises OSError
    assert guard("https://does-not-resolve.example/") is False


def test_empty_resolution_fails_closed() -> None:
    guard = guard_for({"empty.example": []})
    assert guard("https://empty.example/") is False


# -- caching ------------------------------------------------------------------


def test_verdict_cached_within_ttl_and_reresolved_after() -> None:
    now = 0.0
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(resolver=resolver, ttl=60.0, clock=lambda: now)
    assert guard("https://example.com/a")
    assert guard("https://example.com/b")
    assert resolver.calls == ["example.com"]  # second hit served from cache
    now = 61.0
    assert guard("https://example.com/c")
    assert resolver.calls == ["example.com", "example.com"]  # TTL expiry re-resolves


def test_negative_verdicts_cached_too() -> None:
    resolver = RecordingResolver({"internal.example": [ipaddress.ip_address("10.0.0.1")]})
    guard = block_private_networks(resolver=resolver)
    assert guard("https://internal.example/") is False
    assert guard("https://internal.example/again") is False
    assert resolver.calls == ["internal.example"]


def test_cache_is_lru_bounded() -> None:
    resolver = RecordingResolver({f"h{i}.example": [PUBLIC_V4] for i in range(3)})
    guard = block_private_networks(resolver=resolver, max_entries=2)
    for i in range(3):
        assert guard(f"https://h{i}.example/")
    # h0 was evicted by h2; touching it again must re-resolve.
    assert guard("https://h0.example/")
    assert resolver.calls.count("h0.example") == 2


def test_hostname_cache_key_is_case_insensitive() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(resolver=resolver)
    assert guard("https://EXAMPLE.com/")
    assert guard("https://example.COM/")
    assert resolver.calls == ["example.com"]  # one lowercase key, one resolution


# -- allowlist narrowing --------------------------------------------------------


def test_allowlist_rejects_off_list_host_without_resolving() -> None:
    resolver = RecordingResolver({"other.example": [PUBLIC_V4]})
    guard = block_private_networks(allowed_hosts={"example.com"}, resolver=resolver)
    assert guard("https://other.example/") is False
    assert resolver.calls == []  # rejected before any resolution


def test_allowlist_is_compared_lowercase() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(allowed_hosts={"EXAMPLE.com"}, resolver=resolver)
    assert guard("https://Example.COM/") is True


def test_allowlisted_host_must_still_resolve_public() -> None:
    # The allowlist NARROWS, never widens: an allowlisted host resolving to an internal
    # address is still rejected.
    resolver = RecordingResolver({"example.com": [ipaddress.ip_address("10.0.0.5")]})
    guard = block_private_networks(allowed_hosts={"example.com"}, resolver=resolver)
    assert guard("https://example.com/") is False
    assert resolver.calls == ["example.com"]


def test_allowlisted_public_host_allowed() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(allowed_hosts={"example.com"}, resolver=resolver)
    assert guard("https://example.com/") is True
