"""Pre-call validation of user-supplied navigation URLs — app policy, not library policy.

This is the one SSRF control that stays hand-rolled here: deciding which *navigation* URLs
your service accepts (scheme, userinfo, an optional host allowlist) is application policy,
checked cheaply before a browser is ever involved so bad input gets a 400, not a render.

Everything address-level — resolving hostnames and rejecting private/loopback/link-local/
metadata destinations, on the navigation, every redirect hop, every sub-resource, and the
policy's own ``robots.txt`` fetch (including each of *its* redirect hops) — is the
library's job now: :func:`colorsense.block_private_networks` builds the egress
``request_filter`` that ``app.py`` installs on its :class:`~colorsense.PolitenessPolicy`.
See that factory's docstring for the honest limits (DNS rebinding, blocking resolution).
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Mirrors the library's own scheme gate: only http(s) navigations are accepted.
_FETCHABLE_SCHEMES = frozenset({"http", "https"})


def validate_target_url(url: str, *, allowed_hosts: frozenset[str] | None = None) -> None:
    """Pre-call validation of a user-supplied navigation URL; raises ``ValueError``.

    Checks only what is knowable without I/O: the scheme must be http(s), userinfo
    (``user:pass@host``) is rejected outright (a classic confusion vector — and colorsense
    has no business carrying credentials to a target), the host must be present, and —
    when ``allowed_hosts`` is configured — the host must be on it (compare lowercase).
    Address-level checks belong to the ``block_private_networks`` egress filter, which
    also covers every redirect hop and sub-resource, not just this initial URL.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in _FETCHABLE_SCHEMES:
        raise ValueError(f"unsupported scheme {parts.scheme!r}: only http(s) URLs are accepted")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs carrying userinfo (user:pass@host) are rejected")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no host")
    if allowed_hosts is not None and host.lower() not in allowed_hosts:
        raise ValueError(f"host {host!r} is not on the configured allowlist")
