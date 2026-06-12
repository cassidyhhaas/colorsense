"""Unit tests for ``colorsense.net.guard.block_private_networks`` — pure, network-free.

The resolver is injected everywhere, so no DNS lookup ever happens; the tests assert the
*policy* (which addresses are fetchable, fail-closed behavior, caching, allowlist
narrowing) in isolation from the browser and the politeness machinery.
"""

from __future__ import annotations

import asyncio
import ipaddress
import threading
from collections.abc import Awaitable, Callable

import pytest

from colorsense import block_private_networks
from colorsense.net import guard as guard_module
from colorsense.net.guard import IPAddress, _is_public_address, _PrivateNetworkBlocker

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


def guard_for(mapping: dict[str, list[IPAddress]]) -> Callable[[str], Awaitable[bool]]:
    return block_private_networks(resolver=RecordingResolver(mapping))


async def wait_until(condition: Callable[[], bool], description: str, timeout: float = 5.0) -> None:
    """Yield to the loop until ``condition()`` holds; fail the test after ``timeout``.

    Bounded replacement for a bare ``while not condition(): await asyncio.sleep(0)`` spin:
    if the code under test regresses and the condition never becomes true, the test fails
    with a clear message instead of hanging forever.
    """

    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(poll(), timeout=timeout)
    except TimeoutError:  # only reached on regression
        pytest.fail(f"timed out after {timeout}s waiting until {description}")


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


async def test_public_host_allowed() -> None:
    guard = guard_for({"example.com": [PUBLIC_V4, PUBLIC_V6]})
    assert await guard("https://example.com/page") is True


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
async def test_hosts_resolving_to_non_public_addresses_rejected(resolved: list[IPAddress]) -> None:
    guard = guard_for({"evil.example": resolved})
    assert await guard("http://evil.example/") is False


async def test_ip_literal_metadata_endpoint_rejected() -> None:
    # IP literals resolve to themselves; no mapping entry needed with the real resolver,
    # but here the injected resolver supplies the literal explicitly.
    guard = guard_for({"169.254.169.254": [ipaddress.ip_address("169.254.169.254")]})
    assert await guard("http://169.254.169.254/latest/meta-data/") is False


async def test_ip_literals_pass_through_default_resolver() -> None:
    # The default getaddrinfo resolver maps literals to themselves with no network round
    # trip, so the guard classifies them directly: loopback rejected, public allowed.
    guard = block_private_networks()
    assert await guard("http://127.0.0.1/") is False
    assert await guard("http://[::1]/") is False
    assert await guard(f"http://{PUBLIC_V4}/") is True


async def test_non_http_schemes_rejected_without_resolving() -> None:
    resolver = RecordingResolver({})
    guard = block_private_networks(resolver=resolver)
    for url in ("ftp://example.com/", "file:///etc/passwd", "data:text/html,hi", "about:blank"):
        assert await guard(url) is False
    assert resolver.calls == []


async def test_userinfo_rejected_without_resolving() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(resolver=resolver)
    assert await guard("https://user:pass@example.com/") is False
    assert resolver.calls == []


async def test_missing_host_rejected() -> None:
    assert await guard_for({})("https:///nohost") is False


async def test_malformed_url_fails_closed() -> None:
    assert await guard_for({})("https://[::1/broken") is False


async def test_resolver_failure_fails_closed() -> None:
    guard = guard_for({})  # every lookup raises OSError
    assert await guard("https://does-not-resolve.example/") is False


async def test_empty_resolution_fails_closed() -> None:
    guard = guard_for({"empty.example": []})
    assert await guard("https://empty.example/") is False


# -- caching ------------------------------------------------------------------


async def test_verdict_cached_within_ttl_and_reresolved_after() -> None:
    now = 0.0
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(resolver=resolver, ttl=60.0, clock=lambda: now)
    assert await guard("https://example.com/a")
    assert await guard("https://example.com/b")
    assert resolver.calls == ["example.com"]  # second hit served from cache
    now = 61.0
    assert await guard("https://example.com/c")
    assert resolver.calls == ["example.com", "example.com"]  # TTL expiry re-resolves


async def test_negative_verdicts_cached_too() -> None:
    resolver = RecordingResolver({"internal.example": [ipaddress.ip_address("10.0.0.1")]})
    guard = block_private_networks(resolver=resolver)
    assert await guard("https://internal.example/") is False
    assert await guard("https://internal.example/again") is False
    assert resolver.calls == ["internal.example"]


async def test_cache_is_lru_bounded() -> None:
    resolver = RecordingResolver({f"h{i}.example": [PUBLIC_V4] for i in range(3)})
    guard = block_private_networks(resolver=resolver, max_entries=2)
    for i in range(3):
        assert await guard(f"https://h{i}.example/")
    # h0 was evicted by h2; touching it again must re-resolve.
    assert await guard("https://h0.example/")
    assert resolver.calls.count("h0.example") == 2


async def test_hostname_cache_key_is_case_insensitive() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(resolver=resolver)
    assert await guard("https://EXAMPLE.com/")
    assert await guard("https://example.COM/")
    assert resolver.calls == ["example.com"]  # one lowercase key, one resolution


# -- allowlist narrowing --------------------------------------------------------


async def test_allowlist_rejects_off_list_host_without_resolving() -> None:
    resolver = RecordingResolver({"other.example": [PUBLIC_V4]})
    guard = block_private_networks(allowed_hosts={"example.com"}, resolver=resolver)
    assert await guard("https://other.example/") is False
    assert resolver.calls == []  # rejected before any resolution


async def test_allowlist_is_compared_lowercase() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(allowed_hosts={"EXAMPLE.com"}, resolver=resolver)
    assert await guard("https://Example.COM/") is True


async def test_allowlisted_host_must_still_resolve_public() -> None:
    # The allowlist NARROWS, never widens: an allowlisted host resolving to an internal
    # address is still rejected.
    resolver = RecordingResolver({"example.com": [ipaddress.ip_address("10.0.0.5")]})
    guard = block_private_networks(allowed_hosts={"example.com"}, resolver=resolver)
    assert await guard("https://example.com/") is False
    assert resolver.calls == ["example.com"]


async def test_allowlisted_public_host_allowed() -> None:
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(allowed_hosts={"example.com"}, resolver=resolver)
    assert await guard("https://example.com/") is True


# -- off-loop resolution & single-flight coalescing -----------------------------


async def test_resolution_runs_off_the_event_loop() -> None:
    # The whole point of the async predicate: the (blocking) resolver must execute on a
    # worker thread, never on the loop thread that called the guard.
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def resolver(host: str) -> list[IPAddress]:
        seen.append(threading.current_thread())
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver)
    assert await guard("https://example.com/") is True
    assert seen and all(thread is not loop_thread for thread in seen)


async def test_concurrent_misses_for_one_host_coalesce_into_one_lookup() -> None:
    # Executor-exhaustion amplifier guard: N concurrent requests to one slow novel
    # hostname must dispatch ONE worker-thread lookup, with all callers sharing its
    # verdict. The resolver parks on an Event so the test deterministically observes all
    # five tasks pending behind a single in-flight resolution before releasing it.
    release = threading.Event()
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        calls.append(host)
        if not release.wait(timeout=10.0):  # pragma: no cover - hang guard
            raise OSError("test resolver was never released")
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver)
    tasks = [asyncio.create_task(guard(f"https://example.com/{i}")) for i in range(5)]
    # Let the leader reach the worker-thread dispatch.
    await wait_until(lambda: bool(calls), "the leader reaches the worker-thread dispatch")
    for _ in range(20):  # let every follower attach to the in-flight future
        await asyncio.sleep(0)
    assert all(not task.done() for task in tasks)
    release.set()
    assert await asyncio.gather(*tasks) == [True] * 5
    assert calls == ["example.com"]  # exactly one lookup served all five


async def test_leader_cancellation_fails_followers_closed() -> None:
    # Documented cancellation behavior: cancelling the task that owns the in-flight
    # lookup cancels the shared future, and followers fail CLOSED (False) rather than
    # inheriting the leader's CancelledError; the next request simply re-resolves.
    release = threading.Event()
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        calls.append(host)
        release.wait(timeout=10.0)
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver)
    leader = asyncio.create_task(guard("https://slow.example/"))
    # Leader is parked in the worker thread, inflight entry registered.
    await wait_until(lambda: bool(calls), "the leader parks in the worker thread")
    follower = asyncio.create_task(guard("https://slow.example/other"))
    for _ in range(20):  # follower attaches to the shared future
        await asyncio.sleep(0)
    leader.cancel()
    assert await follower is False  # fail closed, no CancelledError leak
    with pytest.raises(asyncio.CancelledError):
        await leader
    release.set()  # unblock the (still running) worker thread for clean teardown
    # Nothing was cached by the cancelled lookup: a later call re-resolves.
    assert await guard("https://slow.example/retry") is True
    assert calls == ["slow.example", "slow.example"]


async def test_follower_own_cancellation_propagates() -> None:
    # A follower whose OWN task is cancelled raises CancelledError normally — and the
    # shared future survives, so the leader (and its verdict) are unaffected.
    release = threading.Event()
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        calls.append(host)
        release.wait(timeout=10.0)
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver)
    leader = asyncio.create_task(guard("https://slow.example/"))
    # Leader is parked in the worker thread, inflight entry registered.
    await wait_until(lambda: bool(calls), "the leader parks in the worker thread")
    follower = asyncio.create_task(guard("https://slow.example/other"))
    for _ in range(20):
        await asyncio.sleep(0)
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    release.set()
    assert await leader is True  # the shared lookup was not poisoned by the follower
    assert calls == ["slow.example"]


async def test_same_tick_leader_and_follower_cancellation_honors_the_follower() -> None:
    # The race branch: the leader's cancellation (which cancels the shared future) lands
    # in the same tick as the follower's own pending cancel. The follower's cancel must be
    # honored — CancelledError propagates — not swallowed into a False verdict.
    release = threading.Event()
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        calls.append(host)
        release.wait(timeout=10.0)
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver)
    leader = asyncio.create_task(guard("https://slow.example/"))
    # Leader is parked in the worker thread, inflight entry registered.
    await wait_until(lambda: bool(calls), "the leader parks in the worker thread")
    follower = asyncio.create_task(guard("https://slow.example/other"))
    for _ in range(20):
        await asyncio.sleep(0)
    # Cancel both without yielding in between: the leader (scheduled first) cancels the
    # shared future before the follower's shield wakes, so the follower observes a
    # cancelled future WITH its own cancel pending.
    leader.cancel()
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    with pytest.raises(asyncio.CancelledError):
        await leader
    release.set()


def test_sequential_reuse_across_loops_supported_and_keeps_verdict_cache() -> None:
    # One loop at a time, not one loop forever: the sequential asyncio.run pattern (house
    # style — cf. PolitenessPolicy._render_slots) keeps working. Between runs no
    # resolution is in flight, so the predicate re-binds to the new loop and carries its
    # plain-data verdict cache across.
    resolver = RecordingResolver({"example.com": [PUBLIC_V4]})
    guard = block_private_networks(resolver=resolver)
    assert asyncio.run(guard("https://example.com/")) is True
    assert asyncio.run(guard("https://example.com/again")) is True
    # Exactly one resolution: the second run was served from the carried-over cache.
    assert resolver.calls == ["example.com"]


async def test_concurrent_use_from_a_second_loop_raises() -> None:
    # Genuine concurrent cross-loop use — the case that would corrupt the loop-bound
    # single-flight futures or hang waiters: while a leader resolution is parked in the
    # worker thread (inflight entry registered on THIS loop), a call from a different
    # loop must raise a diagnosable RuntimeError instead of touching shared state.
    release = threading.Event()
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        calls.append(host)
        release.wait(timeout=10.0)
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver)
    leader = asyncio.create_task(guard("https://slow.example/"))
    try:
        # Leader is parked in the worker thread, inflight entry registered.
        await wait_until(lambda: bool(calls), "the leader parks in the worker thread")

        def call_from_another_loop() -> bool:
            return asyncio.run(guard("https://example.com/"))

        with pytest.raises(RuntimeError, match="separate predicate per event loop"):
            await asyncio.to_thread(call_from_another_loop)
    finally:
        release.set()  # always unblock the worker thread, even if the assertion fails
    assert await leader is True  # the refused call left the leader's loop state intact
    # The cross-loop call never reached the resolution machinery.
    assert calls == ["slow.example"]


# -- dedicated lookup pool & resolve timeout -------------------------------------


async def test_lookups_run_on_the_predicates_own_pool_not_the_default_executor() -> None:
    # Isolation from the loop's shared to_thread executor (which the pipeline's CPU phase
    # and any embedding app use): guard lookups must land on the predicate-owned pool,
    # recognizable by its thread_name_prefix.
    seen: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        seen.append(threading.current_thread().name)
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver)
    assert await guard("https://example.com/") is True
    assert seen and all(name.startswith("colorsense-guard-dns") for name in seen)


async def test_slow_resolution_times_out_fails_closed_and_caches_negative() -> None:
    # A lookup exceeding resolve_timeout fails CLOSED, the negative verdict is cached
    # (re-resolving a hostile hostname per request would be an amplifier), and the
    # single-flight bookkeeping is cleaned up.
    release = threading.Event()
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        calls.append(host)
        release.wait(timeout=10.0)
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver, resolve_timeout=0.05)
    assert isinstance(guard, _PrivateNetworkBlocker)
    try:
        assert await guard("https://blackhole.example/") is False  # fail closed on timeout
        assert guard._inflight == {}  # single-flight state cleaned up
        # Negative verdict cached: the follow-up call must not dispatch a new lookup.
        assert await guard("https://blackhole.example/again") is False
        assert calls == ["blackhole.example"]
    finally:
        release.set()  # unblock the (still running) pool thread for clean teardown


async def test_followers_observe_a_timed_out_lookups_negative_verdict() -> None:
    # Timeout must not corrupt single-flight state: followers parked on the shared
    # future observe the leader's fail-closed verdict, not a hang or an error.
    release = threading.Event()
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        calls.append(host)
        release.wait(timeout=10.0)
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver, resolve_timeout=0.05)
    assert isinstance(guard, _PrivateNetworkBlocker)
    leader = asyncio.create_task(guard("https://blackhole.example/"))
    try:
        await wait_until(lambda: bool(calls), "the leader parks in the pool thread")
        follower = asyncio.create_task(guard("https://blackhole.example/other"))
        for _ in range(20):  # follower attaches to the shared future
            await asyncio.sleep(0)
        assert await leader is False
        assert await follower is False
        assert guard._inflight == {}
        assert calls == ["blackhole.example"]  # one lookup served both
    finally:
        release.set()


async def test_distinct_host_fanout_is_bounded_by_the_pool_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The finding this guards against: a hostile page fanning requests at many DISTINCT
    # slow hostnames must not pin one thread per host — concurrent resolver entries are
    # capped at the pool size and the excess queues.
    monkeypatch.setattr(guard_module, "GUARD_RESOLVER_MAX_WORKERS", 2)
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def resolver(host: str) -> list[IPAddress]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            if not release.wait(timeout=10.0):  # pragma: no cover - hang guard
                raise OSError("test resolver was never released")
            return [PUBLIC_V4]
        finally:
            with lock:
                active -= 1

    guard = block_private_networks(resolver=resolver, resolve_timeout=30.0)
    tasks = [asyncio.create_task(guard(f"https://h{i}.example/")) for i in range(5)]
    # Both pool threads must fill up with the first two lookups...
    await wait_until(lambda: max_active == 2, "both pool threads are occupied")
    # ...and the remaining three distinct-host lookups queue instead of spawning threads.
    for _ in range(50):
        await asyncio.sleep(0)
    assert max_active == 2
    release.set()
    assert await asyncio.gather(*tasks) == [True] * 5
    assert max_active == 2  # the queued lookups reused the two threads, never a third


async def test_cache_expiry_is_stamped_after_resolution_completes() -> None:
    # A resolution slower than the TTL must not produce a born-expired cache entry: the
    # expiry is stamped from the clock AFTER the lookup lands, so the verdict is reusable
    # for a full TTL from completion.
    now = 0.0
    calls: list[str] = []

    def resolver(host: str) -> list[IPAddress]:
        nonlocal now
        calls.append(host)
        now += 120.0  # the resolution itself takes twice the TTL
        return [PUBLIC_V4]

    guard = block_private_networks(resolver=resolver, ttl=60.0, clock=lambda: now)
    assert await guard("https://example.com/") is True
    assert await guard("https://example.com/again") is True  # served from cache
    assert calls == ["example.com"]
