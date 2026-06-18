"""A library-shipped egress filter blocking private-network destinations.

[`block_private_networks`][colorsense.block_private_networks] builds a predicate suitable for
``PolitenessPolicy(request_filter=...)``: it is applied by the library to every URL the
browser requests through ``context.route`` while rendering — the navigation, every redirect
hop, and all sub-resources including the page's own ``fetch``/XHR — *and* to the
policy's own server-side ``robots.txt`` GET (the robots URL and each redirect hop, vetted
before being requested). The two browser network paths ``context.route`` does **not**
intercept are closed off rather than filtered: WebSocket handshakes are refused outright
when a filter is configured (never connected upstream) and service workers are blocked
unconditionally at context creation (see ``harvest/render.py``). The predicate works by
resolving each hostname and rejecting any URL whose resolution
includes a non-public address — loopback,
RFC 1918, link-local (including the cloud metadata endpoint 169.254.169.254), CGNAT
(100.64.0.0/10), unspecified, multicast, reserved, and their IPv6 equivalents. Resolution
failures fail **closed**. This is the shipped mechanism for the SECURITY.md §1
"filter egress in-library" item.

The honest limits — DNS rebinding is not fully defeated (network isolation stays the
primary control), resolution runs off-loop on a small predicate-owned thread pool with a
fail-closed per-lookup timeout behind a TTL+LRU verdict cache with single-flight
coalescing, and each predicate serves one event loop at a time — are documented in full on
[`block_private_networks`][colorsense.block_private_networks], the public docstring users see.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
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

DEFAULT_GUARD_RESOLVE_TIMEOUT_SECONDS = 10.0
"""Per-lookup ceiling on a single DNS resolution; on expiry the URL fails **closed** and
the negative verdict is cached like any other. 10 seconds comfortably covers a healthy
OS resolver retrying once (typical per-attempt timeouts are ~5s) while bounding how long
a black-holed nameserver can hold a guard pool thread — and the request awaiting the
verdict — hostage."""

GUARD_RESOLVER_MAX_WORKERS = 8
"""Size of each predicate's own DNS-lookup thread pool. Small on purpose: per-host
single-flight coalescing already collapses duplicate lookups, so only *distinct* novel
hostnames compete for threads, and 8 keeps a burst of legitimate sub-resource hosts
moving while bounding what a hostile page fanning out unique slow hostnames can pin —
excess lookups queue inside the pool instead of consuming a thread each. Saturating the
pool slows the guard's own verdicts (briefly, given the resolve timeout), never the
event loop or anyone else's executor."""


def _default_resolver(host: str) -> list[IPAddress]:
    """Resolve ``host`` to all of its addresses via stdlib ``socket.getaddrinfo``.

    Blocking by design — the guard runs it on its own bounded lookup pool on a cache miss,
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
    """The predicate [`block_private_networks`][colorsense.block_private_networks] returns.

    See the factory docstring — including the single-event-loop-at-a-time contract its
    single-flight futures impose (enforcement mechanics in `_check_loop_affinity`).
    """

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] | None,
        resolver: Resolver,
        ttl: float,
        max_entries: int,
        clock: Clock,
        resolve_timeout: float,
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._resolver = resolver
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        self._resolve_timeout = resolve_timeout
        # The predicate-owned DNS lookup pool (lazily created on the first cache miss).
        # Deliberately NOT the loop's default to_thread executor: that pool is shared
        # with the pipeline's CPU phase and the embedding application, so letting guard
        # lookups land there would let a page fanning out distinct slow hostnames starve
        # unrelated work. Never explicitly shut down — it lives as long as the predicate
        # and its (idle) threads are reclaimed at interpreter shutdown. Unlike the
        # single-flight futures it is loop-independent, so it survives sequential
        # re-binding across event loops.
        self._executor: ThreadPoolExecutor | None = None
        # hostname -> (expiry, verdict). Most-recently-used keys at the end; overflow
        # evicts from the front. Negative verdicts are cached too — repeatedly re-resolving
        # a hostile hostname would hand the page a worker-thread-lookup amplifier. Read and
        # mutated ONLY on the event loop thread; the worker thread runs nothing but
        # _resolve_verdict.
        self._cache: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        # In-flight resolution coalescing (single-flight): concurrent misses for the same
        # host await one shared Future instead of each dispatching a worker-thread lookup —
        # otherwise a page fanning N requests at one slow novel hostname pins N executor
        # threads on the same getaddrinfo. The Futures are loop-bound; emptiness of this
        # dict is what lets _check_loop_affinity re-bind to a new loop.
        self._inflight: dict[str, asyncio.Future[bool]] = {}
        # The event loop currently served (see _check_loop_affinity).
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
        """Serve one event loop at a time: re-bind when idle, reject concurrent misuse.

        The single-flight Futures in ``self._inflight`` are bound to one event loop, so
        *concurrent* cross-loop use would corrupt them or hang waiters — that case raises.
        *Sequential* reuse across loops (back-to-back ``asyncio.run`` calls) is fine and
        supported: the ``finally`` cleanup in ``_host_is_public`` guarantees
        ``self._inflight`` is empty between runs, the TTL+LRU verdict cache is plain
        data, and the lookup pool is loop-independent, so the predicate simply re-binds
        to the new loop and keeps its warm cache (and pool).

        Detection is **best-effort**: concurrent multi-thread/multi-loop use was never
        supported, and this check reads unsynchronized state, so it catches the common
        misuse rather than guaranteeing detection. Raising here is fail-closed at the
        ``request_filter`` seam (``evaluate_request_filter`` turns it into ``False``, so
        misuse there shows up as requests from the other loop being aborted); the error
        itself is only visible to direct callers.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None or self._loop is loop:
            self._loop = loop
        elif not self._inflight:
            # Sequential handoff: the previous loop left no resolution in flight, so
            # nothing loop-bound survives. Adopt the new loop, keep the verdict cache.
            self._loop = loop
        else:
            raise RuntimeError(
                "block_private_networks(): this predicate is already serving another "
                "event loop with resolutions in flight; concurrent use from multiple "
                "event loops is unsupported (its single-flight futures are loop-bound) — "
                "create a separate predicate per event loop"
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
        # Leader: dispatch exactly one lookup for this host onto the predicate's own
        # bounded pool (never the loop's shared default executor — see __init__). Only
        # _resolve_verdict (pure: catches OSError/ValueError, returns a bool) runs in the
        # thread; the cache write/eviction happens back on the loop thread.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._inflight[host] = future
        try:
            try:
                verdict = await asyncio.wait_for(
                    loop.run_in_executor(self._lookup_pool(), self._resolve_verdict, host),
                    timeout=self._resolve_timeout,
                )
            except TimeoutError:
                # Lookup exceeded resolve_timeout: fail CLOSED, and cache the negative
                # verdict like any other (re-resolving a hostile hostname on every
                # request would hand the page an amplifier). The pool thread keeps
                # running until the resolver returns; its verdict is discarded.
                verdict = False
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
            # Stamp expiry from a fresh clock read: a resolution slower than the TTL
            # must not produce an entry that is already expired the moment it lands.
            self._cache[host] = (self._clock() + self._ttl, verdict)
            self._cache.move_to_end(host)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
            if not future.done():
                future.set_result(verdict)
            return verdict
        finally:
            self._inflight.pop(host, None)

    def _lookup_pool(self) -> ThreadPoolExecutor:
        """Return the predicate-owned DNS lookup pool, created lazily on the first cache miss.

        See the ``__init__`` comment for why this is a dedicated bounded pool rather
        than the loop's default ``to_thread`` executor, and for its lifecycle.
        """
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=GUARD_RESOLVER_MAX_WORKERS,
                thread_name_prefix="colorsense-guard-dns",
            )
        return self._executor

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
    resolve_timeout: float = DEFAULT_GUARD_RESOLVE_TIMEOUT_SECONDS,
) -> Callable[[str], Awaitable[bool]]:
    r"""Build a ``request_filter`` predicate that rejects non-public destinations.

    The returned **async** predicate (``await guard(url) -> bool``; ``True`` permits,
    ``False`` aborts; only usable under a running event loop, as the ``request_filter``
    seams are) is meant for ``PolitenessPolicy(request_filter=...)``, where the library applies
    it to every ``http(s)`` URL the browser requests while rendering — the navigation,
    every redirect hop, and all sub-resources, including the page's own ``fetch``/XHR
    calls — and to the policy's own ``robots.txt`` GET, whose initial URL and every
    redirect ``Location`` are vetted before each request goes out. The two browser network
    paths Playwright's ``context.route`` cannot intercept are closed rather than filtered:
    WebSocket connections are **refused outright** whenever a ``request_filter`` is
    configured (the handshake never reaches the network), and **service workers are always
    blocked** at context creation — strictly stronger than per-URL vetting for both. It
    implements the SECURITY.md §1 egress-filter item:

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
    in depth. Resolution runs **off the event loop** on a small thread pool the predicate
    itself owns (`GUARD_RESOLVER_MAX_WORKERS` threads, created lazily on the first
    cache miss and kept for the predicate's lifetime) — never the loop's shared default
    ``to_thread`` executor, so guard lookups cannot starve the pipeline's CPU phase or an
    embedding application's own thread-pool work. Concurrent misses for one host coalesce
    into a single lookup; fan-out to *distinct* novel hostnames beyond the pool size
    queues inside the guard's own pool (bounded threads, not one pinned thread per
    hostile hostname); each lookup is capped at ``resolve_timeout`` seconds, after which
    the URL fails **closed** and the negative verdict is cached. Verdicts land in a
    per-hostname TTL+LRU cache (negative verdicts cached too) — so a slow resolver costs
    bounded guard-pool time plus latency for that host only, never a loop stall. Honest
    limit: a timed-out lookup's thread still runs the resolver to completion inside the
    pool — the timeout bounds the caller's wait, not the thread's occupancy.

    **Single-event-loop-at-a-time contract:** the coalescing machinery uses loop-bound
    ``asyncio.Future``\ s, so each returned predicate must only be used from one event
    loop at a time. Reusing one predicate *sequentially* across loops (e.g. back-to-back
    ``asyncio.run`` calls) is supported — when idle it re-binds to the new loop and keeps
    its verdict cache (the lookup pool itself is loop-independent and carries over
    unchanged). *Concurrent* use from multiple event loops raises
    `RuntimeError` (detected best-effort). Direct callers see that error; through
    ``request_filter`` it fails closed instead, so misuse there manifests as requests from
    the other loop being aborted. Create a separate predicate per loop for concurrent use.

    Parameters
    ----------
    allowed_hosts:
        Optional exact (lowercase-compared) hostname allowlist applied *before* resolution:
        a host not on the list is rejected, and a host on the list must still resolve to
        only-public addresses. The allowlist narrows the filter, never widens it.
    resolver:
        ``host -> [addresses]`` seam, injectable for tests. Stays *synchronous* — the
        guard runs it on its own bounded lookup pool on a cache miss. Defaults to a
        blocking ``socket.getaddrinfo`` lookup; raising ``OSError`` fails closed.
    ttl:
        Seconds a hostname's verdict is reused before re-resolving. Defaults to
        `DEFAULT_GUARD_TTL_SECONDS` (60).
    max_entries:
        LRU bound on the verdict cache. Defaults to `DEFAULT_GUARD_MAX_ENTRIES`
        (1024).
    clock:
        Monotonic time source for the TTL, injectable for tests.
    resolve_timeout:
        Seconds a single lookup may take before the URL fails closed (and the negative
        verdict is cached). Defaults to `DEFAULT_GUARD_RESOLVE_TIMEOUT_SECONDS` (10).
    """
    hosts = None if allowed_hosts is None else frozenset(h.lower() for h in allowed_hosts)
    return _PrivateNetworkBlocker(
        allowed_hosts=hosts,
        resolver=resolver,
        ttl=ttl,
        max_entries=max_entries,
        clock=clock,
        resolve_timeout=resolve_timeout,
    )
