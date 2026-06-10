"""Unit tests for ``examples.webservice.url_guard`` — pure, network-free.

Only :func:`validate_target_url` lives in the example now: the address-level egress
filtering it used to hand-roll is the library's ``block_private_networks`` (covered by
``tests/test_guard.py``). These tests assert the remaining app policy: cheap pre-call
navigation-URL validation.
"""

from __future__ import annotations

import pytest

from examples.webservice.url_guard import validate_target_url


def test_validate_accepts_plain_https() -> None:
    validate_target_url("https://example.com/path?q=1")  # no raise


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com",  # scheme-less
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "https:///nohost",
    ],
)
def test_validate_rejects_bad_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_target_url(url)


def test_validate_host_allowlist() -> None:
    allowed = frozenset({"example.com"})
    validate_target_url("https://EXAMPLE.com/x", allowed_hosts=allowed)  # case-insensitive
    with pytest.raises(ValueError, match="allowlist"):
        validate_target_url("https://other.example/", allowed_hosts=allowed)
