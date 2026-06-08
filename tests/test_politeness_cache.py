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
from colorsense.models import Harvest, Theme, Viewport
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)
URL = "https://example.test/page"


@pytest.fixture(scope="module")
def config() -> Config:
    return load_default_config()


async def _no_robots(_url: str) -> str | None:
    """Robots loader that returns no rules, so every URL is permitted."""
    return None


async def _disallow_all(_url: str) -> str | None:
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

    async def __call__(self, url: str, theme: Theme, config: Config, viewport: Viewport) -> Harvest:
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

    async def __call__(self, url: str, theme: Theme, config: Config, viewport: Viewport) -> Harvest:
        self.calls += 1
        self.entered.set()
        await self.gate.wait()
        raise RuntimeError("render boom")


async def test_concurrent_same_key_coalesces_to_one_render(config: Config) -> None:
    # Two concurrent fetches for the SAME key share a single render: the harvester is invoked
    # exactly once and both callers receive the same Harvest.
    harvester = _GatedHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    leader = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
    # Wait until the leader is genuinely inside the (blocked) harvester before the follower
    # even starts, so the follower can only join via the in-flight Future.
    await harvester.entered.wait()
    follower = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
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

    leader = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
    await harvester.entered.wait()
    followers = [
        asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT)) for _ in range(5)
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

    light = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
    dark = asyncio.ensure_future(policy.fetch(URL, Theme.dark, config, VIEWPORT))
    # Let both reach the harvester so they are genuinely concurrent.
    while harvester.concurrent < 2:
        await asyncio.sleep(0)

    harvester.gate.set()
    light_harvest, dark_harvest = await asyncio.gather(light, dark)

    assert harvester.calls == 2
    assert harvester.max_concurrent == 2  # truly parallel, not coalesced
    assert light_harvest.theme is Theme.light
    assert dark_harvest.theme is Theme.dark


async def test_failure_propagates_to_all_and_is_not_cached(config: Config) -> None:
    # A leader failure fans the SAME exception out to every concurrent follower, the failure
    # is not cached, and the in-flight slot is cleaned up so a later fetch re-renders.
    failing = _FailingHarvester()
    policy = PolitenessPolicy(harvester=failing, robots_loader=_no_robots)

    leader = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
    await failing.entered.wait()
    follower = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
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
    harvest = await policy.fetch(URL, Theme.light, config, VIEWPORT)
    assert ok.calls == 1
    assert harvest.url == URL


async def test_robots_disallow_propagates_and_is_not_cached(config: Config) -> None:
    # A RobotsDisallowedError from the leader's gate must reach a concurrent follower too,
    # without rendering and without caching.
    harvester = _GatedHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_disallow_all)

    a = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
    b = asyncio.ensure_future(policy.fetch(URL, Theme.light, config, VIEWPORT))
    results = await asyncio.gather(a, b, return_exceptions=True)

    assert harvester.calls == 0  # gate rejected before any render
    assert all(isinstance(r, RobotsDisallowedError) for r in results)
    assert not policy._cache
    assert not policy._inflight


async def test_completed_coalesced_render_served_from_cache(config: Config) -> None:
    # After a coalesced render completes, a later fetch of the same key is a cache hit:
    # no new harvester call, and the in-flight map is empty.
    harvester = _GatedHarvester()
    harvester.gate.set()  # do not block; let the leader complete immediately
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    first, second = await asyncio.gather(
        policy.fetch(URL, Theme.light, config, VIEWPORT),
        policy.fetch(URL, Theme.light, config, VIEWPORT),
    )
    assert first is second
    in_flight_calls = harvester.calls
    assert in_flight_calls == 1
    assert not policy._inflight

    later = await policy.fetch(URL, Theme.light, config, VIEWPORT)
    assert harvester.calls == in_flight_calls  # served from cache, no new render
    assert later is first
    assert not policy._inflight
