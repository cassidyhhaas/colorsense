"""``PolitenessPolicy(max_concurrent_renders=...)`` tests — no browser, no network.

The semaphore must bound only genuine renders: cache hits and single-flight followers
never take a slot, a throttle wait does not hold one, ``None`` stays unbounded, and the
limiter is per-policy and per-event-loop. Concurrency is driven deterministically through
the same gated-harvester pattern as ``test_politeness_cache.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from colorsense.config import Config, load_default_config
from colorsense.harvest import SharedBrowser
from colorsense.models import Harvest, Theme, Viewport
from colorsense.net.politeness import PolitenessPolicy

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_default_config()


async def _no_robots(
    _url: str, _user_agent: str, _request_filter: Callable[[str], bool] | None = None
) -> str | None:
    return None


class _GatedHarvester:
    """Harvester that blocks on an Event until released, counting concurrent entrants."""

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
        request_filter: Callable[[str], bool] | None = None,
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


def _policy(harvester: _GatedHarvester, **kwargs: object) -> PolitenessPolicy:
    # min_interval=0 so the per-host throttle never interferes with the semaphore under test.
    return PolitenessPolicy(
        harvester=harvester,
        robots_loader=_no_robots,
        min_interval=0.0,
        **kwargs,  # type: ignore[arg-type]
    )


async def _spin(turns: int = 5) -> None:
    """Yield the loop a few times so blocked/queued tasks reach a stable state."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_semaphore_caps_simultaneous_renders(config: Config) -> None:
    # Four distinct keys, cap of two: at no point are more than two harvester calls in
    # flight, but all four eventually render.
    harvester = _GatedHarvester()
    policy = _policy(harvester, max_concurrent_renders=2)

    tasks = [
        asyncio.ensure_future(
            policy.fetch(f"https://h{i}.test/page", Theme.light, config, VIEWPORT)
        )
        for i in range(4)
    ]
    await _spin()
    # Only two entered; the other two are parked on the semaphore.
    assert harvester.concurrent == 2
    assert harvester.calls == 2

    harvester.gate.set()
    await asyncio.gather(*tasks)
    assert harvester.calls == 4
    assert harvester.max_concurrent == 2  # the cap was never exceeded


async def test_none_means_unbounded(config: Config) -> None:
    harvester = _GatedHarvester()
    policy = _policy(harvester)  # max_concurrent_renders defaults to None

    tasks = [
        asyncio.ensure_future(
            policy.fetch(f"https://h{i}.test/page", Theme.light, config, VIEWPORT)
        )
        for i in range(3)
    ]
    await _spin()
    assert harvester.concurrent == 3  # all in flight at once

    harvester.gate.set()
    await asyncio.gather(*tasks)
    assert harvester.max_concurrent == 3


async def test_cache_hits_bypass_the_slot(config: Config) -> None:
    # With the single slot held by an in-flight render, a fetch whose key is already
    # cached must return immediately — a cache hit never takes a slot.
    harvester = _GatedHarvester()
    policy = _policy(harvester, max_concurrent_renders=1)

    harvester.gate.set()
    cached = await policy.fetch("https://cached.test/", Theme.light, config, VIEWPORT)
    harvester.gate.clear()
    harvester.entered.clear()  # re-arm: the next entered.wait() must mean the BLOCKER entered

    blocker = asyncio.ensure_future(
        policy.fetch("https://blocker.test/", Theme.light, config, VIEWPORT)
    )
    await harvester.entered.wait()  # the blocker now holds the only slot

    hit = await asyncio.wait_for(
        policy.fetch("https://cached.test/", Theme.light, config, VIEWPORT), timeout=1.0
    )
    assert hit is cached

    harvester.gate.set()
    await blocker


async def test_followers_bypass_the_slot(config: Config) -> None:
    # Single-flight followers await the leader's future; with a cap of 1 a follower for
    # the SAME key must not deadlock waiting for a second slot.
    harvester = _GatedHarvester()
    policy = _policy(harvester, max_concurrent_renders=1)
    url = "https://example.test/page"

    leader = asyncio.ensure_future(policy.fetch(url, Theme.light, config, VIEWPORT))
    await harvester.entered.wait()  # the leader holds the only slot
    follower = asyncio.ensure_future(policy.fetch(url, Theme.light, config, VIEWPORT))
    await _spin()

    harvester.gate.set()
    first, second = await asyncio.gather(leader, follower)
    assert first is second
    assert harvester.calls == 1  # the follower never rendered (so never needed a slot)


async def test_throttle_wait_does_not_hold_a_slot(config: Config) -> None:
    # A fetch parked in a per-host rate-limit sleep must not occupy the render slot: a
    # fetch to a DIFFERENT host renders while host A is still waiting out its interval.
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_sleeper(_seconds: float) -> None:
        started.set()
        await release.wait()

    harvester = _GatedHarvester()
    harvester.gate.set()  # renders complete immediately; only the throttle blocks
    policy = PolitenessPolicy(
        harvester=harvester,
        robots_loader=_no_robots,
        min_interval=5.0,
        max_concurrent_renders=1,
        sleeper=blocking_sleeper,
    )

    await policy.fetch("https://a.test/1", Theme.light, config, VIEWPORT)
    a_again = asyncio.ensure_future(policy.fetch("https://a.test/2", Theme.light, config, VIEWPORT))
    await started.wait()  # host A is parked in the sleeper — BEFORE acquiring the slot

    b = await asyncio.wait_for(
        policy.fetch("https://b.test/1", Theme.light, config, VIEWPORT), timeout=1.0
    )
    assert b.url == "https://b.test/1"

    release.set()
    await a_again


async def test_two_policies_have_independent_limiters(config: Config) -> None:
    # The semaphore is per-policy state: with two cap-1 policies, one render through each
    # proceeds concurrently.
    h1, h2 = _GatedHarvester(), _GatedHarvester()
    p1 = _policy(h1, max_concurrent_renders=1)
    p2 = _policy(h2, max_concurrent_renders=1)

    t1 = asyncio.ensure_future(p1.fetch("https://one.test/", Theme.light, config, VIEWPORT))
    t2 = asyncio.ensure_future(p2.fetch("https://two.test/", Theme.light, config, VIEWPORT))
    await h1.entered.wait()
    await h2.entered.wait()  # both in flight at once — neither limiter blocked the other

    h1.gate.set()
    h2.gate.set()
    await asyncio.gather(t1, t2)


def test_policy_survives_sequential_event_loops() -> None:
    # The semaphore is created lazily inside the running loop and re-created when the loop
    # changes, so one policy serves sequential asyncio.run calls without binding errors.
    config = load_default_config()
    harvester = _GatedHarvester()
    harvester.gate.set()
    policy = _policy(harvester, max_concurrent_renders=1)

    async def run(i: int) -> Harvest:
        return await policy.fetch(f"https://loop{i}.test/", Theme.light, config, VIEWPORT)

    first = asyncio.run(run(1))
    second = asyncio.run(run(2))
    assert first.url == "https://loop1.test/"
    assert second.url == "https://loop2.test/"
    assert harvester.calls == 2


def test_max_concurrent_renders_validation() -> None:
    with pytest.raises(ValueError, match="max_concurrent_renders"):
        PolitenessPolicy(max_concurrent_renders=0)
    with pytest.raises(ValueError, match="max_concurrent_renders"):
        PolitenessPolicy(max_concurrent_renders=-1)
    PolitenessPolicy(max_concurrent_renders=1)  # the boundary value is accepted
