"""A library-shipped egress filter blocking private-network destinations.

:func:`block_private_networks` builds a predicate suitable for
``PolitenessPolicy(request_filter=...)``: it is applied by the library to **every** URL the
browser requests while rendering (the navigation and all sub-resources) *and* to the
policy's own server-side ``robots.txt`` GET (the robots URL and each redirect hop, vetted
before being requested), resolving each hostname and rejecting any URL whose resolution
includes a non-public address — loopback,
RFC 1918, link-local (including the cloud metadata endpoint 169.254.169.254), CGNAT
(100.64.0.0/10), unspecified, multicast, reserved, and their IPv6 equivalents. Resolution
failures fail **closed**. This is the shipped mechanism for the SECURITY.md §1
"filter egress in-library" item.

Honest limits — read before relying on this filter alone:

* **DNS rebinding is not fully defeated.** The predicate sees URL *strings*; Chromium
  resolves hostnames independently when it actually connects. A hostname can resolve to a
  public address when this filter checks it and to an internal one moments later when the
  browser connects. Per SECURITY.md, network isolation of the browser environment (egress
  firewalling, no route to internal ranges) remains the primary control; this filter is
  defense in depth, not a substitute.
* **Resolution runs off the event loop, but it still costs.** The predicate is **async**
  (only usable under a running loop, designed for the ``request_filter`` seam): on a cache
  miss the blocking ``getaddrinfo`` runs on a worker thread via ``asyncio.to_thread``, so a
  slow nameserver costs a worker thread plus latency for that host's requests only — never
  a loop stall. Per-host verdicts are TTL+LRU cached, and concurrent misses for one host
  coalesce into a single lookup, so a page fanning out requests to a novel hostname cannot
  exhaust the thread pool with duplicate resolutions. The coalescing futures are
  loop-bound: each predicate pins itself to the event loop of its first call and raises
  ``RuntimeError`` from any other loop — create one predicate per loop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], list[IPAddress]]
Clock = Callable[[], float]

# Mirrors the policy's own scheme gate. The browser also requests non-network URLs while
# rendering (data:, blob:, about:); aborting those is harmless for palette extraction and
# keeps this predicate a pure allowlist.
_FETCHABLE_SCHEMES = frozenset({"http", "https"})

DEFAULT_GUARD_TTL_SECONDS = 60.0
"""How long a hostname's public/non-public verdict is reused before re-resolving."""

DEFAULT_GUARD_MAX_ENTRIES = 1024
"""LRU bound on cached verdicts — a hostile page requesting many hostnames cannot grow
the cache without bound."""


def _default_resolver(host: str) -> list[IPAddress]:
    """Resolve ``host`` to all of its addresses via stdlib ``socket.getaddrinfo``.

    Blocking by design — the guard runs it inside ``asyncio.to_thread`` on a cache miss,
    so the ``Resolver`` seam stays a plain synchronous callable; the verdict is cached. IP
    literals pass straight through ``getaddrinfo`` without a network round trip. Raises
    ``OSError`` on resolution failure — the guard treats that as fail-closed.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    addresses: list[IPAddress] = []
    for info in infos:
        # sockaddr[0] is the textual address (str() pins that down for the typeshed union
        # covering non-INET families); IPv6 link-local entries can carry a "%scope" suffix
        # that ipaddress refuses, so strip it before parsing.
        addresses.append(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
    return addresses


def _is_public_address(ip: IPAddress) -> bool:
    """Whether ``ip`` is a globally routable destination we are willing to fetch.

    The explicit flags name the classic SSRF targets; ``is_global`` then sweeps up the
    ranges they miss — CGNAT (100.64.0.0/10), IETF protocol assignments, benchmarking
    nets, IPv6 ULA/site-local, and friends. Both checks must agree. IPv4-mapped IPv6
    addresses (``::ffff:a.b.c.d``) are classified as the *embedded* IPv4 address — some
    resolver stacks return them, and the connection goes to the embedded v4 target, so
    the wrapper's own flags must not be what decides.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public_address(ip.ipv4_mapped)
    if (
        ip.is_loopback  # 127.0.0.0/8, ::1
        or ip.is_private  # RFC 1918, IPv6 ULA, ...
        or ip.is_link_local  # 169.254.0.0/16 (cloud metadata), fe80::/10
        or ip.is_unspecified  # 0.0.0.0, ::
        or ip.is_multicast  # some multicast is "global scope" — never a fetch target
        or ip.is_reserved
    ):
        return False
    return ip.is_global


class _PrivateNetworkBlocker:
    """The predicate :func:`block_private_networks` returns; see the factory docstring.

    **Single-event-loop contract:** the single-flight machinery hands out
    ``asyncio.Future``\\ s created on the loop the predicate first runs under, and futures
    are loop-bound, so one instance must only ever be awaited from that one loop. The first
    call pins the loop; a call from any other loop raises :class:`RuntimeError` (which the
    ``request_filter`` seam treats as fail-closed). Create a separate predicate per loop.
    """

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] | None,
        resolver: Resolver,
        ttl: float,
        max_entries: int,
        clock: Clock,
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._resolver = resolver
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        # hostname -> (expiry, verdict). Most-recently-used keys at the end; overflow
        # evicts from the front. Negative verdicts are cached too — repeatedly re-resolving
        # a hostile hostname would hand the page a worker-thread-lookup amplifier. Read and
        # mutated ONLY on the event loop thread; the worker thread runs nothing but
        # _resolve_verdict.
        self._cache: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        # In-flight resolution coalescing (single-flight): concurrent misses for the same
        # host await one shared Future instead of each dispatching a worker-thread lookup —
        # otherwise a page fanning N requests at one slow novel hostname pins N executor
        # threads on the same getaddrinfo. Futures are loop-bound, so the predicate pins
        # itself to the loop of its first call (below) and refuses to run anywhere else.
        self._inflight: dict[str, asyncio.Future[bool]] = {}
        # The event loop this predicate is bound to, pinned on first use. Enforced (not
        # just documented) so cross-loop misuse surfaces as a diagnosable RuntimeError —
        # which the request_filter seam fails closed on — instead of a confusing
        # "Future attached to a different loop" deep in the single-flight path.
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __call__(self, url: str) -> bool:
        self._check_loop_affinity()
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            # Malformed URL (e.g. broken IPv6 bracket syntax): fail closed.
            return False
        if parts.scheme.lower() not in _FETCHABLE_SCHEMES:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        if not host:
            return False
        host = host.lower()
        # The allowlist narrows, never widens: a host off the list is rejected outright
        # (before any resolution), and a host ON the list must still resolve public.
        if self._allowed_hosts is not None and host not in self._allowed_hosts:
            return False
        return await self._host_is_public(host)

    def _check_loop_affinity(self) -> None:
        """Pin the predicate to the loop of its first call; reject any other loop.

        The single-flight Futures in ``self._inflight`` (and the loop-thread-only cache)
        are bound to one event loop, so cross-loop use would corrupt shared state or hang
        waiters. Raising here is fail-closed at the ``request_filter`` seam
        (``evaluate_request_filter`` turns it into ``False``, so misuse there shows up as
        every request from the wrong loop being aborted); the error itself is only
        visible to direct callers.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError(
                "block_private_networks(): this predicate is bound to the event loop it "
                "first ran on and cannot be used from another loop (its single-flight "
                "futures are loop-bound); create a separate predicate per event loop"
            )

    async def _host_is_public(self, host: str) -> bool:
        now = self._clock()
        cached = self._cache.get(host)
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(host)
            return cached[1]  # fast path: cache hit returns without awaiting
        existing = self._inflight.get(host)
        if existing is not None:
            # Follower: share the leader's single lookup. ``shield`` keeps this follower's
            # OWN cancellation from cancelling the shared Future other waiters still use.
            # Cancellation behavior (deterministic, fail closed): if the LEADER is
            # cancelled it cancels the Future, and followers return False — fail closed is
            # always safe here, and the next request for the host simply re-resolves. A
            # follower whose own task is cancelled still raises CancelledError normally.
            try:
                return await asyncio.shield(existing)
            except asyncio.CancelledError:
                if not existing.cancelled():
                    raise  # the follower's own task was cancelled — propagate
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    # The leader's cancellation landed in the same tick as this follower's
                    # own pending cancel; the latter must be honored, not swallowed.
                    raise
                return False
        # Leader: dispatch exactly one worker-thread lookup for this host. Only
        # _resolve_verdict (pure: catches OSError/ValueError, returns a bool) runs in the
        # thread; the cache write/eviction happens back on the loop thread.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._inflight[host] = future
        try:
            verdict = await asyncio.to_thread(self._resolve_verdict, host)
        except BaseException:
            # Leader cancelled — or a buggy custom resolver raised something outside the
            # OSError/ValueError set _resolve_verdict catches: cancel the shared Future so
            # followers observe it and fail closed (see above), then re-raise (the seam's
            # evaluate_request_filter turns a raising predicate into False). On
            # cancellation the lookup thread keeps running to completion; its verdict is
            # simply discarded.
            if not future.done():
                future.cancel()
            raise
        else:
            self._cache[host] = (now + self._ttl, verdict)
            self._cache.move_to_end(host)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
            if not future.done():
                future.set_result(verdict)
            return verdict
        finally:
            self._inflight.pop(host, None)

    def _resolve_verdict(self, host: str) -> bool:
        try:
            addresses = self._resolver(host)
        except (OSError, ValueError):
            # Resolution failure (or unparseable address) fails CLOSED: an unresolvable
            # hostname is not a fetchable target, and a resolver error must never default
            # to "allow".
            return False
        # Every resolved address must be public: a hostname with one public and one
        # internal A record is exactly the split-horizon shape an attacker would use.
        return bool(addresses) and all(_is_public_address(ip) for ip in addresses)


def block_private_networks(
    *,
    allowed_hosts: Iterable[str] | None = None,
    resolver: Resolver = _default_resolver,
    ttl: float = DEFAULT_GUARD_TTL_SECONDS,
    max_entries: int = DEFAULT_GUARD_MAX_ENTRIES,
    clock: Clock = time.monotonic,
) -> Callable[[str], Awaitable[bool]]:
    """Build a ``request_filter`` predicate that rejects non-public destinations.

    The returned **async** predicate (``await guard(url) -> bool``; ``True`` permits,
    ``False`` aborts; only usable under a running event loop, as the ``request_filter``
    seams are) is meant for ``PolitenessPolicy(request_filter=...)``, where the library applies
    it to **every** URL the browser requests while rendering — the navigation, every
    redirect hop, and all sub-resources, including the page's own ``fetch`` calls — and to
    the policy's own ``robots.txt`` GET, whose initial URL and every redirect ``Location``
    are vetted before each request goes out. It implements the SECURITY.md §1
    egress-filter item:

    * only ``http(s)`` URLs pass; URLs carrying userinfo (``user:pass@host``) are rejected;
    * the hostname is resolved (stdlib ``getaddrinfo``; IP literals pass through without a
      network round trip) and the URL is rejected if **any** resolved address is non-public:
      loopback, RFC 1918/private, link-local (including the 169.254.169.254 cloud metadata
      endpoint), CGNAT 100.64.0.0/10, unspecified, multicast, reserved, and their IPv6
      equivalents (IPv6 zone suffixes are stripped before classification);
    * malformed URLs, resolution failures, and empty resolutions all fail **closed**.

    The library treats a *raising* predicate as fail-closed, but this guard never relies on
    that — it catches its own failure modes and returns ``False`` explicitly.

    Honest residual gap: a URL-string predicate cannot fully defeat **DNS rebinding** —
    Chromium resolves hostnames independently when it connects, so a hostname can flip from
    public to internal between this check and the connection. Network isolation of the
    browser environment remains the primary control per SECURITY.md; this filter is defense
    in depth. Resolution runs **off the event loop**: a cache miss dispatches the blocking
    ``getaddrinfo`` to a worker thread via ``asyncio.to_thread``, concurrent misses for one
    host coalesce into a single lookup, and verdicts land in a per-hostname TTL+LRU cache
    (negative verdicts cached too) — so a slow resolver costs a worker thread plus latency
    for that host only, never a loop stall.

    **Single-event-loop contract:** the coalescing machinery uses loop-bound
    ``asyncio.Future``\\ s, so each returned predicate must only be used from one event
    loop — the loop of its first call, which it pins itself to. Awaiting it from a
    different loop (e.g. reusing one predicate across separate ``asyncio.run`` calls)
    raises :class:`RuntimeError`. Direct callers see that error; through ``request_filter``
    it fails closed instead, so misuse there manifests as every request from the wrong
    loop being aborted. Create a separate predicate per loop. (Stricter than 0.3.0, where
    *sequential* cross-loop reuse happened to be safe.)

    Parameters
    ----------
    allowed_hosts:
        Optional exact (lowercase-compared) hostname allowlist applied *before* resolution:
        a host not on the list is rejected, and a host on the list must still resolve to
        only-public addresses. The allowlist narrows the filter, never widens it.
    resolver:
        ``host -> [addresses]`` seam, injectable for tests. Stays *synchronous* — the
        guard runs it inside ``asyncio.to_thread`` on a cache miss. Defaults to a blocking
        ``socket.getaddrinfo`` lookup; raising ``OSError`` fails closed.
    ttl:
        Seconds a hostname's verdict is reused before re-resolving. Defaults to
        :data:`DEFAULT_GUARD_TTL_SECONDS` (60).
    max_entries:
        LRU bound on the verdict cache. Defaults to :data:`DEFAULT_GUARD_MAX_ENTRIES`
        (1024).
    clock:
        Monotonic time source for the TTL, injectable for tests.
    """
    hosts = None if allowed_hosts is None else frozenset(h.lower() for h in allowed_hosts)
    return _PrivateNetworkBlocker(
        allowed_hosts=hosts,
        resolver=resolver,
        ttl=ttl,
        max_entries=max_entries,
        clock=clock,
    )
