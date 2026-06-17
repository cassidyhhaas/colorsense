"""Single-flight / in-flight request de-duplication tests for ``PolitenessPolicy.fetch``.

Concurrent fetches for the *same* (url, theme, viewport) key must be coalesced into a single
render: one robots gate, one throttle, one harvester call, shared result. Distinct keys must
still render in parallel, leader failures must fan out to every follower without poisoning the
cache, and a completed coalesced render must be served from the cache afterward.

All tests are network/browser-free: the harvester, robots loader, clock, and sleeper are
injected fakes, and concurrency is driven deterministically via an ``asyncio.Event`` gate.
"""

from __future__ import annotations

import asyncio

import pytest

from colorsense.config import Config, load_default_config
from colorsense.harvest import RequestFilter, SharedBrowser
from colorsense.models import Harvest, Theme, Viewport
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)
URL = "https://example.test/page"


@pytest.fixture(scope="module")
def config() -> Config:
    return load_default_config()


async def _no_robots(
    _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
) -> str | None:
    """Robots loader that returns no rules, so every URL is permitted."""
    return None


async def _disallow_all(
    _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
) -> str | None:
    return "User-agent: *\nDisallow: /"


class _GatedHarvester:
    """Harvester that blocks on an Event until released, counting concurrent entrants.

    A caller that reaches the harvester increments ``calls`` and ``concurrent``, then awaits
    ``gate``; this lets a test observe how many callers were *genuinely in flight at once*
    before any of them completes. With coalescing in place only the leader ever enters.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def __call__(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: RequestFilter | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.entered.set()
        try:
            await self.gate.wait()
        finally:
            self.concurrent -= 1
        return Harvest(url=url, theme=theme, viewport=viewport, screenshot_bins=[])


class _FailingHarvester:
    """Harvester that blocks until released, then raises a sentinel error."""

    def __init__(self) -> None:
        self.calls = 0
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def __call__(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: RequestFilter | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        self.calls += 1
        self.entered.set()
        await self.gate.wait()
        raise RuntimeError("render boom")


async def test_concurrent_same_key_coalesces_to_one_render(config: Config) -> None:
    # Two concurrent fetches for the SAME key share a single render: the harvester is invoked
    # exactly once and both callers receive the same Harvest.
    harvester = _GatedHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    leader = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    # Wait until the leader is genuinely inside the (blocked) harvester before the follower
    # even starts, so the follower can only join via the in-flight Future.
    await harvester.entered.wait()
    follower = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    # Give the follower a chance to run and register as a follower.
    await asyncio.sleep(0)

    harvester.gate.set()
    first, second = await asyncio.gather(leader, follower)

    assert harvester.calls == 1
    assert harvester.max_concurrent == 1
    assert first is second  # same Harvest object shared by both callers


async def test_many_concurrent_same_key_single_render(config: Config) -> None:
    harvester = _GatedHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    leader = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    await harvester.entered.wait()
    followers = [
        asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT)) for _ in range(5)
    ]
    await asyncio.sleep(0)

    harvester.gate.set()
    results = await asyncio.gather(leader, *followers)

    assert harvester.calls == 1
    assert all(r is results[0] for r in results)


async def test_distinct_keys_render_in_parallel(config: Config) -> None:
    # Different themes => different cache keys => no false sharing: each key renders.
    harvester = _GatedHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    light = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    dark = asyncio.ensure_future(policy.fetch(URL, Theme.DARK, config, VIEWPORT))
    # Let both reach the harvester so they are genuinely concurrent.
    while harvester.concurrent < 2:
        await asyncio.sleep(0)

    harvester.gate.set()
    light_harvest, dark_harvest = await asyncio.gather(light, dark)

    assert harvester.calls == 2
    assert harvester.max_concurrent == 2  # truly parallel, not coalesced
    assert light_harvest.theme is Theme.LIGHT
    assert dark_harvest.theme is Theme.DARK


async def test_failure_propagates_to_all_and_is_not_cached(config: Config) -> None:
    # A leader failure fans the SAME exception out to every concurrent follower, the failure
    # is not cached, and the in-flight slot is cleaned up so a later fetch re-renders.
    failing = _FailingHarvester()
    policy = PolitenessPolicy(harvester=failing, robots_loader=_no_robots)

    leader = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    await failing.entered.wait()
    follower = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    await asyncio.sleep(0)

    failing.gate.set()
    results = await asyncio.gather(leader, follower, return_exceptions=True)

    assert failing.calls == 1  # only the leader rendered
    assert all(isinstance(r, RuntimeError) for r in results)
    assert results[0] is results[1]  # both callers received the identical exception object
    assert not policy._cache  # failure was not cached
    assert not policy._inflight  # slot released

    # A subsequent fetch of the same key re-invokes the harvester (nothing poisoned).
    ok = _GatedHarvester()
    ok.gate.set()
    policy._harvester = ok  # type: ignore[assignment]
    harvest = await policy.fetch(URL, Theme.LIGHT, config, VIEWPORT)
    assert ok.calls == 1
    assert harvest.url == URL


async def test_robots_disallow_propagates_and_is_not_cached(config: Config) -> None:
    # A RobotsDisallowedError from the leader's gate must reach a concurrent follower too,
    # without rendering and without caching.
    harvester = _GatedHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_disallow_all)

    a = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    b = asyncio.ensure_future(policy.fetch(URL, Theme.LIGHT, config, VIEWPORT))
    results = await asyncio.gather(a, b, return_exceptions=True)

    assert harvester.calls == 0  # gate rejected before any render
    assert all(isinstance(r, RobotsDisallowedError) for r in results)
    assert not policy._cache
    assert not policy._inflight


class _Clock:
    """Synchronous fake time source; advanced manually or by the fake sleeper."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class _ImmediateHarvester:
    """Harvester that returns at once, recording each URL it was asked to render."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: RequestFilter | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        self.urls.append(url)
        return Harvest(url=url, theme=theme, viewport=viewport, screenshot_bins=[])


async def test_robots_fetch_is_throttled_without_double_waiting(config: Config) -> None:
    # FIX 2: the robots.txt GET is the first request to a host and must respect the per-host
    # limiter. FIX (no double-wait): the robots GET and the page nav of the SAME fetch are one
    # logical visit, so the first fetch to a fresh host sleeps zero — not a full interval.
    robots_urls: list[str] = []

    async def recording_robots(
        url: str, _user_agent: str, _request_filter: RequestFilter | None = None
    ) -> str | None:
        robots_urls.append(url)
        return None  # fail-open: no rules => permitted

    clock = _Clock()
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    harvester = _ImmediateHarvester()
    policy = PolitenessPolicy(
        harvester=harvester,
        robots_loader=recording_robots,
        min_interval=2.0,
        clock=clock,
        sleeper=sleeper,
    )

    await policy.fetch("https://host.test/a", Theme.LIGHT, config, VIEWPORT)
    # First contact: robots GET + page nav share one reservation, so no sleep at all.
    assert robots_urls == ["https://host.test/robots.txt"]
    assert slept == []

    clock.t += 0.5  # only 0.5s passes before the next same-host fetch
    await policy.fetch("https://host.test/b", Theme.LIGHT, config, VIEWPORT)
    # robots.txt is cached per host, so the second fetch makes no second robots GET; the page
    # nav waits the remaining 1.5s of the interval — proof the host slot was reserved.
    assert robots_urls == ["https://host.test/robots.txt"]
    assert slept == [pytest.approx(1.5)]


async def test_different_hosts_do_not_serialize(config: Config) -> None:
    # FIX 1: a rate-limit wait for one host must not block a fetch to a DIFFERENT host. With
    # the old global throttle lock, host B's fetch would queue behind host A's sleep.
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_sleeper(_seconds: float) -> None:
        # Simulate host A being mid-wait: signal we started, then block until released.
        started.set()
        await release.wait()

    clock = _Clock()
    harvester = _ImmediateHarvester()
    policy = PolitenessPolicy(
        harvester=harvester,
        robots_loader=_no_robots,
        min_interval=5.0,
        clock=clock,
        sleeper=blocking_sleeper,
    )

    # Prime host A so its next fetch must wait, then launch that waiting fetch.
    await policy.fetch("https://a.test/1", Theme.LIGHT, config, VIEWPORT)
    a_again = asyncio.ensure_future(policy.fetch("https://a.test/2", Theme.LIGHT, config, VIEWPORT))
    await started.wait()  # host A is now parked inside the sleeper

    # Host B (never fetched) must complete WITHOUT waiting on host A's lock/sleep.
    b = await asyncio.wait_for(
        policy.fetch("https://b.test/1", Theme.LIGHT, config, VIEWPORT), timeout=1.0
    )
    assert b.url == "https://b.test/1"

    release.set()
    await a_again


async def test_concurrent_same_host_distinct_keys_chain(config: Config) -> None:
    # Same-host pacing must still hold across DISTINCT keys fetched concurrently: two callers
    # arriving together each reserve their own slot, so the second waits min_interval.
    clock = _Clock()
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    harvester = _ImmediateHarvester()
    policy = PolitenessPolicy(
        harvester=harvester,
        robots_loader=_no_robots,
        min_interval=3.0,
        clock=clock,
        sleeper=sleeper,
    )

    # Different themes => distinct cache keys => not coalesced; both throttle the same host.
    await asyncio.gather(
        policy.fetch("https://host.test/p", Theme.LIGHT, config, VIEWPORT),
        policy.fetch("https://host.test/p", Theme.DARK, config, VIEWPORT),
    )
    # First caller: no wait. Second caller: chains a full interval after the first's slot.
    assert slept == [pytest.approx(3.0)]


async def test_completed_coalesced_render_served_from_cache(config: Config) -> None:
    # After a coalesced render completes, a later fetch of the same key is a cache hit:
    # no new harvester call, and the in-flight map is empty.
    harvester = _GatedHarvester()
    harvester.gate.set()  # do not block; let the leader complete immediately
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    first, second = await asyncio.gather(
        policy.fetch(URL, Theme.LIGHT, config, VIEWPORT),
        policy.fetch(URL, Theme.LIGHT, config, VIEWPORT),
    )
    assert first is second
    in_flight_calls = harvester.calls
    assert in_flight_calls == 1
    assert not policy._inflight

    later = await policy.fetch(URL, Theme.LIGHT, config, VIEWPORT)
    assert harvester.calls == in_flight_calls  # served from cache, no new render
    assert later is first
    assert not policy._inflight


# ---------------------------------------------------------------------------
# robots.txt Crawl-delay honoring (deterministic via injected clock/sleeper/loader)
# ---------------------------------------------------------------------------


def _delay_policy(
    robots_text: str | None,
    *,
    min_interval: float,
    **kwargs: object,
) -> tuple[PolitenessPolicy, list[float]]:
    """A policy whose robots loader returns ``robots_text``, with a recording fake sleeper."""

    async def loader(
        _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
    ) -> str | None:
        return robots_text

    clock = _Clock()
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    policy = PolitenessPolicy(
        harvester=_ImmediateHarvester(),
        robots_loader=loader,
        min_interval=min_interval,
        clock=clock,
        sleeper=sleeper,
        **kwargs,  # type: ignore[arg-type]
    )
    return policy, slept


async def test_crawl_delay_raises_effective_interval(config: Config) -> None:
    # A robots Crawl-delay above min_interval governs same-host pacing. The delay is learned
    # from the FIRST fetch's robots GET (itself the host's first throttled request), so it
    # applies from the second fetch onward: first fetch sleeps zero, second waits 5s.
    robots = "User-agent: *\nCrawl-delay: 5\nDisallow:\n"
    policy, slept = _delay_policy(robots, min_interval=1.0)

    await policy.fetch("https://host.test/a", Theme.LIGHT, config, VIEWPORT)
    assert slept == []  # crawl delay not yet known when the first fetch was throttled

    await policy.fetch("https://host.test/b", Theme.LIGHT, config, VIEWPORT)
    assert slept == [pytest.approx(5.0)]  # the learned 5s delay, not min_interval's 1s


async def test_crawl_delay_clamped_to_max(config: Config) -> None:
    # A hostile/typo'd Crawl-delay must not stall the pipeline: it is capped by
    # max_crawl_delay (default 30s) before joining the limiter.
    robots = "User-agent: *\nCrawl-delay: 86400\nDisallow:\n"
    policy, slept = _delay_policy(robots, min_interval=1.0)

    await policy.fetch("https://host.test/a", Theme.LIGHT, config, VIEWPORT)
    await policy.fetch("https://host.test/b", Theme.LIGHT, config, VIEWPORT)
    assert slept == [pytest.approx(30.0)]

    # Consumers can raise the cap explicitly.
    policy2, slept2 = _delay_policy(robots, min_interval=1.0, max_crawl_delay=120.0)
    await policy2.fetch("https://host.test/a", Theme.LIGHT, config, VIEWPORT)
    await policy2.fetch("https://host.test/b", Theme.LIGHT, config, VIEWPORT)
    assert slept2 == [pytest.approx(120.0)]


async def test_no_crawl_delay_keeps_min_interval(config: Config) -> None:
    # robots rules without a Crawl-delay leave the limiter at min_interval, unchanged.
    robots = "User-agent: *\nDisallow:\n"
    policy, slept = _delay_policy(robots, min_interval=2.0)

    await policy.fetch("https://host.test/a", Theme.LIGHT, config, VIEWPORT)
    await policy.fetch("https://host.test/b", Theme.LIGHT, config, VIEWPORT)
    assert slept == [pytest.approx(2.0)]


async def test_min_interval_wins_over_smaller_crawl_delay(config: Config) -> None:
    # The effective interval is max(min_interval, crawl_delay): a tiny Crawl-delay never
    # *lowers* the consumer's configured pacing.
    robots = "User-agent: *\nCrawl-delay: 1\nDisallow:\n"
    policy, slept = _delay_policy(robots, min_interval=4.0)

    await policy.fetch("https://host.test/a", Theme.LIGHT, config, VIEWPORT)
    await policy.fetch("https://host.test/b", Theme.LIGHT, config, VIEWPORT)
    assert slept == [pytest.approx(4.0)]
