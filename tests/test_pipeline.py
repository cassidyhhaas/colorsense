"""Pipeline + politeness tests.

The end-to-end cases render LOCAL fixture HTML via ``file://`` (no public network), the
same approach harvest uses. Politeness mechanics (robots gate, rate limiter, render cache) are
exercised with an injected fake harvester so they need neither Playwright nor real network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from colorsense import LIGHT_AND_DARK, analyze
from colorsense.config import Config, load_default_config
from colorsense.models import (
    AnalysisResult,
    Harvest,
    ScreenshotBin,
    Theme,
    Viewport,
)
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_default_config()


# ---------------------------------------------------------------------------
# Politeness: a fake harvester so these tests touch no browser / network.
# ---------------------------------------------------------------------------


class _CountingHarvester:
    """Records every (url, theme) it renders and returns a trivial Harvest."""

    def __init__(self, bins: list[ScreenshotBin] | None = None) -> None:
        self.calls: list[tuple[str, Theme]] = []
        self._bins = bins or []

    async def __call__(self, url: str, theme: Theme, config: Config, viewport: Viewport) -> Harvest:
        self.calls.append((url, theme))
        return Harvest(
            url=url,
            theme=theme,
            viewport=viewport,
            screenshot_bins=self._bins,
        )


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


# Async robots_loader seams (the policy awaits the loader). One permits all (no rules),
# the other disallows everything.
async def _no_robots(_url: str) -> str | None:
    return None


async def _disallow_all(_url: str) -> str | None:
    return "User-agent: *\nDisallow: /"


async def test_cache_returns_without_re_render(config: Config) -> None:
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)
    url = "https://example.test/page"

    first = await policy.fetch(url, Theme.light, config, VIEWPORT)
    second = await policy.fetch(url, Theme.light, config, VIEWPORT)

    assert second is first  # identical object straight from cache
    assert harvester.calls == [(url, Theme.light)]  # rendered exactly once

    # A different theme is a distinct cache key and re-renders.
    await policy.fetch(url, Theme.dark, config, VIEWPORT)
    assert harvester.calls == [(url, Theme.light), (url, Theme.dark)]


async def test_render_cache_is_lru_bounded(config: Config) -> None:
    # Insert more distinct keys than the cap, then prove (a) the cache stays bounded and
    # (b) the least-recently-used key was evicted: re-fetching it re-invokes the harvester.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots, max_cache_entries=3)

    urls = [f"https://example.test/page/{i}" for i in range(5)]
    for url in urls:
        await policy.fetch(url, Theme.light, config, VIEWPORT)

    # Bounded: never more than the cap regardless of how many distinct keys were inserted.
    assert len(policy._cache) == 3
    assert len(harvester.calls) == 5  # every distinct key rendered once on first insert

    # The 3 most-recent keys (page/2..4) are cache hits — no new render.
    for url in urls[2:]:
        await policy.fetch(url, Theme.light, config, VIEWPORT)
    assert len(harvester.calls) == 5  # still 5: all served from cache

    # The LRU key (page/0) was evicted, so re-fetching it re-invokes the harvester.
    await policy.fetch(urls[0], Theme.light, config, VIEWPORT)
    assert len(harvester.calls) == 6
    assert (urls[0], Theme.light) in harvester.calls


async def test_render_cache_hit_refreshes_recency(config: Config) -> None:
    # A cache hit must mark the entry most-recently-used so it survives later eviction.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots, max_cache_entries=2)
    a, b, c = (f"https://example.test/p/{x}" for x in "abc")

    await policy.fetch(a, Theme.light, config, VIEWPORT)
    await policy.fetch(b, Theme.light, config, VIEWPORT)
    # Touch ``a`` so ``b`` is now the LRU entry.
    await policy.fetch(a, Theme.light, config, VIEWPORT)
    # Inserting ``c`` should evict ``b`` (the LRU), not ``a``.
    await policy.fetch(c, Theme.light, config, VIEWPORT)

    assert len(harvester.calls) == 3  # a, b, c each rendered once so far
    # ``a`` survived: it is a hit (no new render).
    await policy.fetch(a, Theme.light, config, VIEWPORT)
    assert len(harvester.calls) == 3
    # ``b`` was evicted: re-fetch re-renders.
    await policy.fetch(b, Theme.light, config, VIEWPORT)
    assert len(harvester.calls) == 4


async def test_render_cache_unbounded_with_zero(config: Config) -> None:
    # ``max_cache_entries=0`` (or None) means unbounded: nothing is ever evicted.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots, max_cache_entries=0)
    for i in range(10):
        await policy.fetch(f"https://example.test/u/{i}", Theme.light, config, VIEWPORT)
    assert len(policy._cache) == 10


async def test_robots_disallow_blocks_fetch(config: Config) -> None:
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_disallow_all)
    url = "https://example.test/secret"

    assert await policy.can_fetch(url) is False
    with pytest.raises(RobotsDisallowedError):
        await policy.fetch(url, Theme.light, config, VIEWPORT)
    assert harvester.calls == []  # never rendered


async def test_respect_robots_false_bypasses_gate(config: Config) -> None:
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(
        harvester=harvester, robots_loader=_disallow_all, respect_robots=False
    )
    url = "https://example.test/secret"

    assert await policy.can_fetch(url) is True
    await policy.fetch(url, Theme.light, config, VIEWPORT)
    assert harvester.calls == [(url, Theme.light)]


async def test_file_url_bypasses_robots(config: Config) -> None:
    # file:// has no host/robots concept: always fetchable even under a disallow loader.
    policy = PolitenessPolicy(
        harvester=_CountingHarvester(),
        robots_loader=_disallow_all,
    )
    assert await policy.can_fetch("file:///tmp/x.html") is True


async def test_rate_limiter_spaces_same_host(config: Config) -> None:
    harvester = _CountingHarvester()
    clock = _Clock()
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    policy = PolitenessPolicy(
        harvester=harvester,
        robots_loader=_no_robots,
        min_interval=2.0,
        clock=clock,
        sleeper=sleeper,
    )

    await policy.fetch("https://host.test/a", Theme.light, config, VIEWPORT)
    clock.t += 0.5  # only 0.5s elapses before the next same-host fetch
    await policy.fetch("https://host.test/b", Theme.light, config, VIEWPORT)

    assert slept == [pytest.approx(1.5)]  # waited the remaining 1.5s of the interval


async def test_analyze_propagates_robots_block(config: Config) -> None:
    policy = PolitenessPolicy(
        harvester=_CountingHarvester(),
        robots_loader=_disallow_all,
    )
    with pytest.raises(RobotsDisallowedError):
        await analyze("https://example.test/", politeness=policy)


# ---------------------------------------------------------------------------
# End-to-end on local fixtures (real Playwright harvest of file:// HTML).
# ---------------------------------------------------------------------------


async def _analyze_fixture(name: str, fixtures_dir: Path, **kwargs: object) -> AnalysisResult:
    url = (fixtures_dir / name).as_uri()
    return await analyze(url, viewport=VIEWPORT, **kwargs)  # type: ignore[arg-type]


@pytest.mark.browser
async def test_end_to_end_light_and_dark(fixtures_dir: Path) -> None:
    # tokens.html has a `prefers-color-scheme: dark` block, so the two renders differ
    # and both themes survive collapse. Dark is opt-in, so request it explicitly.
    result = await _analyze_fixture("tokens.html", fixtures_dir, themes=LIGHT_AND_DARK)

    assert isinstance(result, AnalysisResult)
    assert set(result.themes) == {Theme.light, Theme.dark}
    assert result.metadata.single_theme is False
    assert 0.0 <= result.fit_score <= 1.0

    for theme, palette in result.themes.items():
        assert palette.theme is theme
        # Each surviving theme carries reconciled palette roles for consumers to use.
        assert palette.roles.mapping

    # Declared tokens were classified and carried onto the result.
    assert result.tokens
    token_names = {ct.record.name for ct in result.tokens}
    assert "--color-primary" in token_names

    # The result is a clean Pydantic round-trip.
    restored = AnalysisResult.model_validate_json(result.model_dump_json())
    assert restored == result


@pytest.mark.browser
async def test_single_theme_site_collapses(fixtures_dir: Path) -> None:
    # hover.html has no dark-mode block: when both themes are requested, the light and dark
    # renders are identical, so the pipeline collapses to one theme.
    result = await _analyze_fixture("hover.html", fixtures_dir, themes=LIGHT_AND_DARK)

    assert len(result.themes) == 1
    assert result.metadata.single_theme is True
    assert result.metadata.themes_requested == [Theme.light, Theme.dark]
    assert result.metadata.themes_analyzed == [Theme.light]


@pytest.mark.browser
async def test_default_is_light_only(fixtures_dir: Path) -> None:
    # The default flow renders light only — even on a site with a dark-mode block, dark is
    # not analyzed unless explicitly requested.
    result = await _analyze_fixture("tokens.html", fixtures_dir)

    assert set(result.themes) == {Theme.light}
    assert result.metadata.themes_requested == [Theme.light]
    assert result.metadata.themes_analyzed == [Theme.light]
    assert result.metadata.single_theme is True


@pytest.mark.browser
async def test_explicit_single_theme_request(fixtures_dir: Path) -> None:
    result = await _analyze_fixture("tokens.html", fixtures_dir, themes=(Theme.light,))
    assert set(result.themes) == {Theme.light}
    assert result.metadata.themes_requested == [Theme.light]


async def test_empty_themes_rejected(fixtures_dir: Path) -> None:
    url = (fixtures_dir / "tokens.html").as_uri()
    with pytest.raises(ValueError, match="at least one theme"):
        await analyze(url, themes=())
