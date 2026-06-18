"""Networking policy and the SSRF egress guard — the only layer that touches the wire.

`politeness.py` (`PolitenessPolicy`) is the single home for networking policy: the
robots.txt gate, per-host rate limit, render cache, scheme gate, and the egress hook.
`guard.py` ships `block_private_networks()`, the SSRF filter applied to every browser
request and the robots fetch itself.
"""
