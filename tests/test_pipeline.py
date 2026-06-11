"""Pipeline + politeness tests.

The end-to-end cases render LOCAL fixture HTML via ``file://`` (no public network), the
same approach harvest uses. Politeness mechanics (robots gate, rate limiter, render cache) are
exercised with an injected fake harvester so they need neither Playwright nor real network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import file_policy

import colorsense
from colorsense import LIGHT_AND_DARK, analyze
from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.config import Config, load_default_config
from colorsense.harvest import RenderError, RequestFilter, SharedBrowser
from colorsense.models import (
    AnalysisResult,
    Color,
    Harvest,
    HarvestedElement,
    PaletteRole,
    Rect,
    ScreenshotBin,
    Theme,
    TokenRecord,
    TokenSemanticRole,
    Viewport,
)
from colorsense.net.politeness import (
    PolitenessPolicy,
    RobotsDisallowedError,
    UnsupportedSchemeError,
)
from colorsense.pipeline import (
    _COLLAPSE_DELTA_E,
    _collapse_themes,
    _dedupe_colors,
    _near_identical,
)

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


# Async robots_loader seams (the policy awaits the loader with the configured wire UA).
# One permits all (no rules), the other disallows everything.
async def _no_robots(
    _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
) -> str | None:
    return None


async def _disallow_all(
    _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
) -> str | None:
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


async def test_render_cache_unbounded_with_none(config: Config) -> None:
    # ``max_cache_entries=None`` is the documented "unbounded" spelling alongside ``0``:
    # nothing is ever evicted, so every distinct key stays cached and renders exactly once.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots, max_cache_entries=None)
    for i in range(10):
        await policy.fetch(f"https://example.test/u/{i}", Theme.light, config, VIEWPORT)
    assert len(policy._cache) == 10
    # Re-fetching all of them is pure cache hits — no extra renders.
    for i in range(10):
        await policy.fetch(f"https://example.test/u/{i}", Theme.light, config, VIEWPORT)
    assert len(harvester.calls) == 10


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
    # file:// has no host/robots concept: ``can_fetch`` (robots only) always permits it.
    # Whether it may be *fetched* is the separate scheme gate (see the tests below).
    policy = PolitenessPolicy(
        harvester=_CountingHarvester(),
        robots_loader=_disallow_all,
    )
    assert await policy.can_fetch("file:///tmp/x.html") is True


async def test_default_policy_rejects_file_urls(config: Config) -> None:
    # file:// is a local-file-read primitive, so it must be an explicit opt-in: the default
    # policy refuses it with the public UnsupportedSchemeError (with an opt-in hint) and
    # never reaches the harvester.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)
    with pytest.raises(UnsupportedSchemeError, match="allow_file_urls=True"):
        await policy.fetch("file:///tmp/x.html", Theme.light, config, VIEWPORT)
    assert harvester.calls == []


@pytest.mark.parametrize(
    "url", ["ftp://x", "data:text/html,hi", "javascript:alert(1)", "no-scheme"]
)
async def test_non_http_non_file_schemes_always_rejected(config: Config, url: str) -> None:
    # ftp/data/javascript/scheme-less are rejected even when file URLs are opted in.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots, allow_file_urls=True)
    with pytest.raises(UnsupportedSchemeError):
        await policy.fetch(url, Theme.light, config, VIEWPORT)
    assert harvester.calls == []


async def test_allow_file_urls_permits_file_fetch(config: Config) -> None:
    # The opt-in renders file:// URLs — still bypassing robots (disallow-all loader).
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(
        harvester=harvester, robots_loader=_disallow_all, allow_file_urls=True
    )
    url = "file:///tmp/x.html"
    await policy.fetch(url, Theme.light, config, VIEWPORT)
    assert harvester.calls == [(url, Theme.light)]


async def test_http_unaffected_by_file_url_default(config: Config) -> None:
    # http(s) fetches behave identically whether or not file URLs are allowed.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)
    await policy.fetch("https://example.test/a", Theme.light, config, VIEWPORT)
    await policy.fetch("http://example.test/b", Theme.light, config, VIEWPORT)
    assert [u for u, _ in harvester.calls] == ["https://example.test/a", "http://example.test/b"]


async def test_scheme_gate_runs_before_cache(config: Config) -> None:
    # The scheme is validated BEFORE the cache lookup: a file:// harvest cached while the
    # opt-in was on can never be served once file URLs are forbidden.
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots, allow_file_urls=True)
    url = "file:///tmp/x.html"
    await policy.fetch(url, Theme.light, config, VIEWPORT)  # populates the cache
    policy.allow_file_urls = False
    with pytest.raises(UnsupportedSchemeError):
        await policy.fetch(url, Theme.light, config, VIEWPORT)
    assert len(harvester.calls) == 1  # the cached entry was not served either


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


async def test_analyze_unwraps_taskgroup_to_robots_error(config: Config) -> None:
    # With multiple themes the fetches run inside a TaskGroup; a failing fetch must still
    # surface as the documented plain RobotsDisallowedError, never an ExceptionGroup.
    # ``pytest.raises(RobotsDisallowedError)`` would NOT catch an ExceptionGroup, so this
    # passing proves the unwrap.
    policy = PolitenessPolicy(
        harvester=_CountingHarvester(),
        robots_loader=_disallow_all,
    )
    with pytest.raises(RobotsDisallowedError) as excinfo:
        await analyze("https://example.test/", themes=LIGHT_AND_DARK, politeness=policy)
    assert not isinstance(excinfo.value, BaseExceptionGroup)


class _FailFastCancelAwareHarvester:
    """Light fails (after dark is in flight); dark blocks forever and records cancellation.

    The dark render waits on an Event that is never set, standing in for a long headless
    Chromium render. If ``analyze`` cancels siblings on first failure (TaskGroup semantics),
    the wait receives CancelledError; under plain ``gather`` it would be left running.
    """

    def __init__(self) -> None:
        self.dark_started = asyncio.Event()
        self.dark_cancelled = False
        self._never = asyncio.Event()

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
        if theme is Theme.dark:
            self.dark_started.set()
            try:
                await self._never.wait()
            except asyncio.CancelledError:
                self.dark_cancelled = True
                raise
            raise AssertionError("unreachable: the event is never set")
        # Only fail once the sibling render is genuinely in flight, so the test observes
        # whether the failure cancels it.
        await self.dark_started.wait()
        raise RenderError("simulated navigation failure")


async def test_failed_fetch_cancels_sibling_render(config: Config) -> None:
    harvester = _FailFastCancelAwareHarvester()
    clock = _Clock()

    async def sleeper(seconds: float) -> None:
        clock.t += seconds  # virtual time: the rate limiter never really sleeps

    policy = PolitenessPolicy(
        harvester=harvester, robots_loader=_no_robots, clock=clock, sleeper=sleeper
    )

    # The light theme's RenderError surfaces as itself (not ExceptionGroup) ...
    with pytest.raises(RenderError):
        await analyze("https://example.test/", themes=LIGHT_AND_DARK, politeness=policy)

    # ... and the in-flight dark render was cancelled rather than abandoned.
    assert harvester.dark_started.is_set()
    assert harvester.dark_cancelled is True


# ---------------------------------------------------------------------------
# Orchestration on a fully-faked Harvest (no browser): assert analyze() wires the
# stages together and segregates outputs correctly. The pipeline is pure given a
# Harvest, so an injected harvester returning a populated Harvest exercises the whole
# classify -> inventory -> roles -> reconcile -> assemble chain browserlessly.
# ---------------------------------------------------------------------------


def _color(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None, f"unparseable test color {value!r}"
    return c


def _bg_element(
    *, tag: str, bg: Color, class_tokens: list[str] | None = None, clickable: bool = False
) -> HarvestedElement:
    return HarvestedElement(
        tag=tag,
        role=None,
        id=None,
        class_tokens=class_tokens or [],
        rect=Rect(x=0.0, y=0.0, width=1280.0, height=200.0),
        position="static",
        bg=bg,
        text=_color("#111827"),
        border=None,
        is_iframe=False,
        cross_origin=False,
        shadow_host=False,
        clickable=clickable,
        has_hover_color_change=False,
        hover_bg=None,
        vendor_match=False,
        visible=True,
        aria_hidden=False,
    )


def _token(name: str, hex_value: str) -> TokenRecord:
    return TokenRecord(
        name=name,
        raw_value=hex_value,
        resolved=_color(hex_value),
        scope=":root",
    )


def _populated_harvest(url: str, theme: Theme, viewport: Viewport) -> Harvest:
    """A realistic single-theme Harvest: a light surface, a dark neutral, an accent,
    declared tokens (including a status/destructive token), and matching screenshot bins."""
    surface = _color("#ffffff")
    dark = _color("#111827")
    accent = _color("#2244aa")
    return Harvest(
        url=url,
        theme=theme,
        viewport=viewport,
        tokens=[
            _token("--color-primary", "#2244aa"),
            _token("--gray-100", "#f3f4f6"),
            _token("--gray-900", "#111827"),
            _token("--destructive", "#ef4444"),
        ],
        elements=[
            _bg_element(tag="body", bg=surface),
            _bg_element(tag="footer", bg=dark),
            _bg_element(tag="button", bg=accent, class_tokens=["btn", "cta"], clickable=True),
        ],
        screenshot_bins=[
            ScreenshotBin(color=surface, area_fraction=0.6),
            ScreenshotBin(color=dark, area_fraction=0.25),
            ScreenshotBin(color=accent, area_fraction=0.15),
        ],
    )


async def test_analyze_orchestrates_faked_harvest(config: Config) -> None:
    url = "https://example.test/page"

    async def harvester(
        u: str,
        theme: Theme,
        _cfg: Config,
        vp: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: RequestFilter | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        return _populated_harvest(u, theme, vp)

    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)
    result = await analyze(url, viewport=VIEWPORT, politeness=policy)

    assert isinstance(result, AnalysisResult)
    assert result.url == url
    assert result.viewport == VIEWPORT

    # Themes present: default flow is light-only, and it survives as the analyzed theme.
    assert set(result.themes) == {Theme.light}
    palette = result.themes[Theme.light]
    assert palette.theme is Theme.light
    assert result.metadata.themes_requested == (Theme.light,)
    assert result.metadata.themes_analyzed == (Theme.light,)
    assert result.metadata.single_theme is True

    # Roles populated: the mapping has every role key, and the area-truth bins yield real
    # candidates for the dominant surface roles.
    roles = palette.roles.mapping
    assert set(roles) == set(PaletteRole)
    nonempty = {role for role, cands in roles.items() if cands}
    assert nonempty, "expected at least one palette role to carry a candidate"
    # The dominant ~60% light surface should anchor the primary role.
    assert roles[PaletteRole.primary], "primary role should be populated from the 60% bin"
    primary_hexes = {cand.color.hex for cand in roles[PaletteRole.primary]}
    assert "#ffffff" in primary_hexes

    # Tokens carried through and classified (token names preserved, semantic roles assigned).
    token_names = {ct.record.name for ct in result.tokens}
    assert token_names == {"--color-primary", "--gray-100", "--gray-900", "--destructive"}
    by_name = {ct.record.name: ct for ct in result.tokens}
    assert by_name["--color-primary"].semantic_role is TokenSemanticRole.brand_primary
    assert by_name["--gray-100"].semantic_role is TokenSemanticRole.neutral
    assert by_name["--destructive"].semantic_role is TokenSemanticRole.status

    # Status colors are segregated OUT of the palette: the destructive red is reported as a
    # status color and must NOT appear as a palette-role candidate.
    status_hexes = {c.hex for c in result.status_colors}
    assert "#ef4444" in status_hexes
    all_role_hexes = {cand.color.hex for cands in roles.values() for cand in cands}
    assert "#ef4444" not in all_role_hexes

    # Clean Pydantic round-trip of the assembled result.
    restored = AnalysisResult.model_validate_json(result.model_dump_json())
    assert restored == result


# ---------------------------------------------------------------------------
# Theme collapse (no browser): _near_identical / _collapse_themes operate purely on
# Harvest screenshot bins, so hand-built bins exercise every collapse decision.
# ---------------------------------------------------------------------------


def _bins_harvest(theme: Theme, bins: list[tuple[str, float]]) -> Harvest:
    """A minimal Harvest carrying only screenshot bins (all collapse logic reads)."""
    return Harvest(
        url="https://example.test/",
        theme=theme,
        viewport=VIEWPORT,
        screenshot_bins=[
            ScreenshotBin(color=_color(hex_value), area_fraction=frac) for hex_value, frac in bins
        ],
    )


def test_collapse_identical_dominant_bins_drops_dark() -> None:
    # A site that ignores prefers-color-scheme renders byte-identical bins: dark collapses.
    bins = [("#ffffff", 0.6), ("#111827", 0.25), ("#2244aa", 0.15)]
    harvests = {
        Theme.light: _bins_harvest(Theme.light, bins),
        Theme.dark: _bins_harvest(Theme.dark, bins),
    }
    assert _near_identical(harvests[Theme.light], harvests[Theme.dark]) is True
    assert _collapse_themes([Theme.light, Theme.dark], harvests) == [Theme.light]


def test_no_collapse_for_genuinely_different_themes() -> None:
    # A real dark mode flips the large-area background bin (white -> near-black), pushing
    # the dominant-bin distance far past the threshold: both themes must survive.
    harvests = {
        Theme.light: _bins_harvest(Theme.light, [("#ffffff", 0.7), ("#2244aa", 0.3)]),
        Theme.dark: _bins_harvest(Theme.dark, [("#111827", 0.7), ("#2244aa", 0.3)]),
    }
    assert _near_identical(harvests[Theme.light], harvests[Theme.dark]) is False
    assert _collapse_themes([Theme.light, Theme.dark], harvests) == [Theme.light, Theme.dark]


def test_superset_dark_theme_does_not_collapse() -> None:
    # Guards the SYMMETRIC check in _near_identical: dark's top bins are a superset of
    # light's dominant colors plus a major new near-black bin. Every light bin has a close
    # match in dark, so the one-directional "a matches b" test passes — only the reverse
    # direction (dark's #111827 has no match in light) keeps these themes apart. Reverting
    # _near_identical to a one-directional a->b check would make this collapse and fail
    # the assertions below.
    light = _bins_harvest(Theme.light, [("#ffffff", 0.7), ("#2244aa", 0.3)])
    dark = _bins_harvest(Theme.dark, [("#111827", 0.5), ("#ffffff", 0.3), ("#2244aa", 0.2)])
    # Precondition making the one-directional check pass: every light bin matches in dark.
    for sb in light.screenshot_bins:
        assert min(delta_e(sb.color, ob.color) for ob in dark.screenshot_bins) <= _COLLAPSE_DELTA_E
    # ... but the new dominant dark bin has no match in light.
    assert _near_identical(light, dark) is False
    harvests = {Theme.light: light, Theme.dark: dark}
    assert _collapse_themes([Theme.light, Theme.dark], harvests) == [Theme.light, Theme.dark]


def test_empty_bins_never_collapse() -> None:
    # A render with no screenshot bins carries no evidence of sameness; _near_identical
    # must be conservative and keep both themes rather than collapsing on vacuous truth.
    populated = _bins_harvest(Theme.light, [("#ffffff", 1.0)])
    empty = _bins_harvest(Theme.dark, [])
    assert _near_identical(populated, empty) is False
    assert _near_identical(empty, populated) is False
    assert _near_identical(empty, _bins_harvest(Theme.light, [])) is False


def test_collapse_ignores_bins_beyond_top_four() -> None:
    # Only the _COLLAPSE_TOP_BINS largest bins participate: wildly different colors in
    # bins ranked 5+ (by area) must not prevent the collapse.
    shared = [("#ffffff", 0.4), ("#111827", 0.25), ("#2244aa", 0.15), ("#f3f4f6", 0.1)]
    light = _bins_harvest(Theme.light, [*shared, ("#ff0000", 0.05)])
    dark = _bins_harvest(Theme.dark, [*shared, ("#0000ff", 0.05)])
    assert _near_identical(light, dark) is True
    harvests = {Theme.light: light, Theme.dark: dark}
    assert _collapse_themes([Theme.light, Theme.dark], harvests) == [Theme.light]


def test_collapse_delta_e_boundary_within_threshold_matches() -> None:
    # Dominant bins that differ but stay within _COLLAPSE_DELTA_E still count as "the same
    # site" (e.g. a sub-threshold anti-aliasing/quantization wobble between renders).
    a, b = _color("#ffffff"), _color("#eeeeee")
    d = delta_e(a, b)
    assert 0.0 < d <= _COLLAPSE_DELTA_E, d  # the pair genuinely probes the boundary
    light = _bins_harvest(Theme.light, [("#ffffff", 1.0)])
    dark = _bins_harvest(Theme.dark, [("#eeeeee", 1.0)])
    assert _near_identical(light, dark) is True
    harvests = {Theme.light: light, Theme.dark: dark}
    assert _collapse_themes([Theme.light, Theme.dark], harvests) == [Theme.light]


# ---------------------------------------------------------------------------
# Orchestration gaps (no browser): theme dedupe/ordering, config_path flow-through,
# and the _dedupe_colors helper.
# ---------------------------------------------------------------------------


class _RecordingPopulatedHarvester:
    """Returns the populated fixture Harvest for every theme, recording each render."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Theme]] = []

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
        self.calls.append((url, theme))
        return _populated_harvest(url, theme, viewport)


async def test_duplicate_themes_are_deduped(config: Config) -> None:
    # Duplicates in ``themes=`` must not trigger duplicate renders: the harvester is
    # invoked once per unique theme, and metadata reflects the deduped request order.
    harvester = _RecordingPopulatedHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)
    url = "https://example.test/page"

    result = await analyze(
        url, themes=(Theme.light, Theme.dark, Theme.light, Theme.light), politeness=policy
    )

    assert harvester.calls == [(url, Theme.light), (url, Theme.dark)]
    assert result.metadata.themes_requested == (Theme.light, Theme.dark)
    # Identical bins for both renders -> the dark theme collapses into light.
    assert result.metadata.themes_analyzed == (Theme.light,)


def _dark_populated_harvest(url: str, theme: Theme, viewport: Viewport) -> Harvest:
    """A dark-dominant Harvest with tokens distinct from ``_populated_harvest``'s."""
    dark = _color("#111827")
    accent = _color("#2244aa")
    return Harvest(
        url=url,
        theme=theme,
        viewport=viewport,
        tokens=[_token("--dark-surface", "#111827"), _token("--color-primary", "#2244aa")],
        elements=[
            _bg_element(tag="body", bg=dark),
            _bg_element(tag="button", bg=accent, class_tokens=["btn"], clickable=True),
        ],
        screenshot_bins=[
            ScreenshotBin(color=dark, area_fraction=0.8),
            ScreenshotBin(color=accent, area_fraction=0.2),
        ],
    )


async def test_first_requested_theme_is_primary(config: Config) -> None:
    # ``themes=(dark, light)`` makes dark the primary theme: its tokens populate the
    # top-level fields, and the metadata preserves the requested order.
    async def harvester(
        u: str,
        theme: Theme,
        _cfg: Config,
        vp: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: RequestFilter | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        if theme is Theme.dark:
            return _dark_populated_harvest(u, theme, vp)
        return _populated_harvest(u, theme, vp)

    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)
    result = await analyze(
        "https://example.test/page", themes=(Theme.dark, Theme.light), politeness=policy
    )

    assert result.metadata.themes_requested == (Theme.dark, Theme.light)
    # The renders genuinely differ (dark-dominant vs light-dominant bins): no collapse.
    assert result.metadata.themes_analyzed == (Theme.dark, Theme.light)
    assert set(result.themes) == {Theme.dark, Theme.light}
    # Top-level tokens come from the PRIMARY (dark) harvest: --dark-surface is declared
    # only there, and --gray-100 only in the light harvest.
    token_names = {ct.record.name for ct in result.tokens}
    assert token_names == {"--dark-surface", "--color-primary"}
    assert 0.0 <= result.fit_score <= 1.0


async def test_config_path_flows_through_analyze(tmp_path: Path, config: Config) -> None:
    # A copy of the bundled YAML passed as ``config_path`` must load from THAT path and
    # drive the same classification as the bundled default.
    bundled = Path(colorsense.__file__).parent / "data" / "palette_config.yaml"
    copied = tmp_path / "palette_config.yaml"
    copied.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")

    async def harvester(
        u: str,
        theme: Theme,
        _cfg: Config,
        vp: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: RequestFilter | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        return _populated_harvest(u, theme, vp)

    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)
    result = await analyze("https://example.test/page", config_path=copied, politeness=policy)
    by_name = {ct.record.name: ct for ct in result.tokens}
    assert by_name["--color-primary"].semantic_role is TokenSemanticRole.brand_primary

    # The path genuinely flows through: a nonexistent config_path must fail the run
    # (were config_path ignored, this would silently succeed on the bundled default).
    with pytest.raises(ValueError, match="could not read config file"):
        await analyze(
            "https://example.test/page",
            config_path=tmp_path / "missing.yaml",
            politeness=policy,
        )


def test_dedupe_colors_preserves_first_seen_order() -> None:
    # _dedupe_colors keys on hex: duplicates are dropped, first-seen order is preserved.
    colors = [
        _color("#2244aa"),
        _color("#ffffff"),
        _color("#2244aa"),
        _color("#111827"),
        _color("#ffffff"),
    ]
    assert [c.hex for c in _dedupe_colors(colors)] == ["#2244aa", "#ffffff", "#111827"]
    assert _dedupe_colors([]) == []


# ---------------------------------------------------------------------------
# End-to-end on local fixtures (real Playwright harvest of file:// HTML).
# ---------------------------------------------------------------------------


async def _analyze_fixture(name: str, fixtures_dir: Path, **kwargs: object) -> AnalysisResult:
    url = (fixtures_dir / name).as_uri()
    # file:// is opt-in by default, so fixture analyses need the file-enabled policy.
    kwargs.setdefault("politeness", file_policy())
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
    assert result.metadata.themes_requested == (Theme.light, Theme.dark)
    assert result.metadata.themes_analyzed == (Theme.light,)


@pytest.mark.browser
async def test_default_is_light_only(fixtures_dir: Path) -> None:
    # The default flow renders light only — even on a site with a dark-mode block, dark is
    # not analyzed unless explicitly requested.
    result = await _analyze_fixture("tokens.html", fixtures_dir)

    assert set(result.themes) == {Theme.light}
    assert result.metadata.themes_requested == (Theme.light,)
    assert result.metadata.themes_analyzed == (Theme.light,)
    assert result.metadata.single_theme is True


@pytest.mark.browser
async def test_explicit_single_theme_request(fixtures_dir: Path) -> None:
    result = await _analyze_fixture("tokens.html", fixtures_dir, themes=(Theme.light,))
    assert set(result.themes) == {Theme.light}
    assert result.metadata.themes_requested == (Theme.light,)


async def test_empty_themes_rejected(fixtures_dir: Path) -> None:
    url = (fixtures_dir / "tokens.html").as_uri()
    with pytest.raises(ValueError, match="at least one theme"):
        await analyze(url, themes=())
