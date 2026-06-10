"""Shared-browser tests: one Chromium launch across the renders of one ``analyze()`` call.

The seam is threaded end to end: ``analyze`` owns a lazy :class:`SharedBrowser` handle,
``PolitenessPolicy.fetch`` forwards it to the harvester opaquely (keyword-only, default
``None`` — the policy knows nothing about browser lifecycle), ``harvest_page`` resolves it,
and :class:`RenderSession` opens a per-theme browser context inside the shared browser
instead of launching its own.

Everything except the ``@pytest.mark.browser`` cases is browser/network-free: the Playwright
stack is replaced with counting fakes (the same pattern as ``test_render_errors``), and the
politeness/pipeline seams are driven with recording fake harvesters.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import file_policy

import colorsense.harvest.render as render_mod
from colorsense import LIGHT_AND_DARK, analyze
from colorsense.color.primitives import parse_css_color
from colorsense.config import Config, load_default_config
from colorsense.harvest import SharedBrowser, harvest_page
from colorsense.harvest.render import RenderSession
from colorsense.models import (
    Harvest,
    HarvestedElement,
    Rect,
    ScreenshotBin,
    Theme,
    Viewport,
)
from colorsense.net.politeness import PolitenessPolicy

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)
URL = "https://example.test/page"


@pytest.fixture(scope="module")
def config() -> Config:
    return load_default_config()


async def _no_robots(_url: str, _user_agent: str) -> str | None:
    return None


# ---------------------------------------------------------------------------
# Counting fakes for the whole async_playwright() stack (no real browser).
# ---------------------------------------------------------------------------


class _FakePage:
    pass


class _FakeContext:
    def __init__(self) -> None:
        self.closes = 0
        self.routes: list[object] = []

    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def route(self, pattern: str, handler: object) -> None:
        self.routes.append((pattern, handler))

    async def close(self) -> None:
        self.closes += 1


class _FakeBrowser:
    """Fake Browser recording each new_context kwargs dict and its close() calls."""

    def __init__(self) -> None:
        self.closes = 0
        self.contexts: list[tuple[_FakeContext, dict[str, object]]] = []

    async def new_context(self, **kwargs: object) -> _FakeContext:
        context = _FakeContext()
        self.contexts.append((context, kwargs))
        return context

    async def close(self) -> None:
        self.closes += 1


class _FakePlaywrightStack:
    """Stands in for ``async_playwright`` itself, counting starts/launches/stops."""

    def __init__(self) -> None:
        self.starts = 0
        self.launches = 0
        self.stops = 0
        self.browsers: list[_FakeBrowser] = []
        self.chromium = self  # launch() lives on .chromium in the real API

    def __call__(self) -> _FakePlaywrightStack:
        return self  # async_playwright() -> starter

    async def start(self) -> _FakePlaywrightStack:
        self.starts += 1
        return self  # the started Playwright (carries .chromium and .stop)

    async def launch(self, **_kwargs: object) -> _FakeBrowser:
        self.launches += 1
        browser = _FakeBrowser()
        self.browsers.append(browser)
        return browser

    async def stop(self) -> None:
        self.stops += 1


def _patch_playwright(monkeypatch: pytest.MonkeyPatch) -> _FakePlaywrightStack:
    stack = _FakePlaywrightStack()
    monkeypatch.setattr(render_mod, "async_playwright", stack)
    return stack


# ---------------------------------------------------------------------------
# SharedBrowser lifecycle (browserless, via the fake Playwright stack).
# ---------------------------------------------------------------------------


async def test_shared_browser_is_lazy_and_launches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing is launched until the first get(); repeated get() returns the SAME browser
    # without relaunching; teardown closes the browser and stops Playwright exactly once.
    stack = _patch_playwright(monkeypatch)

    async with SharedBrowser() as shared:
        assert stack.starts == 0  # entering the context launches nothing
        first = await shared.get()
        second = await shared.get()

    assert first is second
    assert (stack.starts, stack.launches, stack.stops) == (1, 1, 1)
    assert stack.browsers[0].closes == 1


async def test_shared_browser_concurrent_gets_share_one_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sibling theme renders call get() concurrently; the internal lock must coalesce them
    # onto a single launch (the fakes await, so unguarded callers could interleave).
    stack = _patch_playwright(monkeypatch)
    async with SharedBrowser() as shared:
        a, b = await asyncio.gather(shared.get(), shared.get())

    assert a is b
    assert stack.launches == 1


async def test_shared_browser_unused_never_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    # An analyze() run whose fetches are all cache hits (or driven by a fake harvester)
    # never calls get(): entering and exiting the handle must cost no launch and no stop.
    stack = _patch_playwright(monkeypatch)
    async with SharedBrowser():
        pass
    assert (stack.starts, stack.launches, stack.stops) == (0, 0, 0)


async def test_shared_browser_get_after_teardown_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After teardown the handle must refuse to relaunch (a relaunched browser would have
    # no owner left to close it).
    stack = _patch_playwright(monkeypatch)
    async with SharedBrowser() as shared:
        await shared.get()
    with pytest.raises(RuntimeError, match="after teardown"):
        await shared.get()
    assert stack.launches == 1  # no silent relaunch


async def test_shared_browser_closes_on_exception_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The teardown-on-exception guarantee: a failure inside the block (e.g. a sibling
    # render error propagating out of analyze's TaskGroup) still closes the browser and
    # stops Playwright, and the original exception propagates unsuppressed.
    stack = _patch_playwright(monkeypatch)
    with pytest.raises(RuntimeError, match="boom"):
        async with SharedBrowser() as shared:
            await shared.get()
            raise RuntimeError("boom")
    assert stack.browsers[0].closes == 1
    assert stack.stops == 1


# ---------------------------------------------------------------------------
# RenderSession with an external browser: context-only ownership.
# ---------------------------------------------------------------------------


async def test_render_session_external_browser_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With an external browser the session must launch no Playwright of its own, and its
    # teardown must close only the context it created — never the borrowed browser.
    stack = _patch_playwright(monkeypatch)
    external = _FakeBrowser()

    async with RenderSession(Theme.light, VIEWPORT, browser=external):  # type: ignore[arg-type]
        pass

    assert stack.starts == 0  # no dedicated Playwright/Chromium was launched
    assert external.closes == 0  # the borrowed browser stays open for the next render
    context, kwargs = external.contexts[0]
    assert context.closes == 1  # the session's own context WAS torn down
    assert kwargs["color_scheme"] == "light"


async def test_two_sessions_share_browser_with_isolated_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The light and dark renders of one analyze() call share the browser but must each get
    # their own context carrying their own color scheme (and egress route, when filtered).
    stack = _patch_playwright(monkeypatch)
    external = _FakeBrowser()

    def allow_all(_url: str) -> bool:
        return True

    async with RenderSession(Theme.light, VIEWPORT, browser=external):  # type: ignore[arg-type]
        pass
    async with RenderSession(
        Theme.dark,
        VIEWPORT,
        request_filter=allow_all,
        browser=external,  # type: ignore[arg-type]
    ):
        pass

    assert stack.starts == 0
    assert external.closes == 0
    assert [kwargs["color_scheme"] for _, kwargs in external.contexts] == ["light", "dark"]
    assert all(context.closes == 1 for context, _ in external.contexts)
    # The egress filter is per-context: only the session that configured one installed it.
    assert [len(context.routes) for context, _ in external.contexts] == [0, 1]


# ---------------------------------------------------------------------------
# PolitenessPolicy.fetch threads the handle through opaquely.
# ---------------------------------------------------------------------------


class _BrowserRecordingHarvester:
    """Records the ``browser`` handle each render was invoked with."""

    def __init__(self) -> None:
        self.browsers: list[SharedBrowser | None] = []

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
        self.browsers.append(browser)
        return Harvest(url=url, theme=theme, viewport=viewport, screenshot_bins=[])


async def test_fetch_forwards_browser_handle_to_harvester(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    # fetch passes the handle through verbatim — and never resolves it itself: the policy
    # knows nothing about browser lifecycle, so the handle must still be unlaunched after.
    stack = _patch_playwright(monkeypatch)
    harvester = _BrowserRecordingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    async with SharedBrowser() as shared:
        await policy.fetch("https://example.test/a", Theme.light, config, VIEWPORT, browser=shared)
        await policy.fetch("https://example.test/b", Theme.light, config, VIEWPORT)

    assert harvester.browsers == [shared, None]  # forwarded when given, None otherwise
    assert stack.starts == 0  # the policy never touched the handle


async def test_cache_hit_skips_render_and_never_launches(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    # Caching semantics are unchanged by the new parameter: a hit returns without invoking
    # the harvester, so the (lazy) handle passed alongside is never resolved.
    stack = _patch_playwright(monkeypatch)
    harvester = _BrowserRecordingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    await policy.fetch(URL, Theme.light, config, VIEWPORT)
    async with SharedBrowser() as shared:
        hit = await policy.fetch(URL, Theme.light, config, VIEWPORT, browser=shared)

    assert hit.url == URL
    assert harvester.browsers == [None]  # one render total; the hit never reached the seam
    assert stack.starts == 0


# ---------------------------------------------------------------------------
# analyze() shares ONE handle across its theme renders.
# ---------------------------------------------------------------------------


def _mini_harvest(url: str, theme: Theme, viewport: Viewport) -> Harvest:
    """The smallest analyzable Harvest: one surface element and one screenshot bin."""
    white = parse_css_color("#ffffff")
    assert white is not None
    text = parse_css_color("#111827")
    element = HarvestedElement(
        tag="body",
        role=None,
        id=None,
        class_tokens=[],
        rect=Rect(x=0.0, y=0.0, width=1280.0, height=800.0),
        position="static",
        bg=white,
        text=text,
        border=None,
        is_iframe=False,
        cross_origin=False,
        shadow_host=False,
        clickable=False,
        has_hover_color_change=False,
        hover_bg=None,
        vendor_match=False,
        visible=True,
        aria_hidden=False,
    )
    return Harvest(
        url=url,
        theme=theme,
        viewport=viewport,
        elements=[element],
        screenshot_bins=[ScreenshotBin(color=white, area_fraction=1.0)],
    )


class _MiniHarvester(_BrowserRecordingHarvester):
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
        self.browsers.append(browser)
        return _mini_harvest(url, theme, viewport)


async def test_analyze_passes_one_shared_handle_to_all_theme_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both theme renders of one analyze() call must receive the SAME SharedBrowser handle
    # (that is the whole point: one launch for the pair) — and with a fake harvester the
    # lazy handle is never resolved, so analyze() costs no browser launch here.
    stack = _patch_playwright(monkeypatch)
    harvester = _MiniHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    await analyze(URL, themes=LIGHT_AND_DARK, politeness=policy)

    assert len(harvester.browsers) == 2  # one render per theme
    assert isinstance(harvester.browsers[0], SharedBrowser)
    assert harvester.browsers[0] is harvester.browsers[1]
    assert stack.starts == 0  # lazy: the fake harvester never resolved the handle


async def test_analyze_uses_fresh_handle_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each analyze() call owns (and tears down) its own browser: handles must not leak
    # across calls, where a closed shared browser would poison the next run.
    _patch_playwright(monkeypatch)
    harvester = _MiniHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots, max_cache_entries=0)

    await analyze("https://example.test/one", politeness=policy)
    await analyze("https://example.test/two", politeness=policy)

    assert len(harvester.browsers) == 2
    assert harvester.browsers[0] is not harvester.browsers[1]


# ---------------------------------------------------------------------------
# Real-browser proof: two themes, one Chromium (local file:// fixture only).
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_harvest_page_renders_both_themes_in_one_browser(fixtures_dir: Path) -> None:
    config = load_default_config()
    url = (fixtures_dir / "tokens.html").as_uri()

    async with SharedBrowser() as shared:
        light = await harvest_page(url, Theme.light, config, VIEWPORT, browser=shared)
        dark = await harvest_page(url, Theme.dark, config, VIEWPORT, browser=shared)
        browser = await shared.get()  # the same underlying browser both renders used
        assert browser.is_connected()  # renders finished, the shared browser is still up
        assert browser.contexts == []  # each session closed its own context behind it

    assert not browser.is_connected()  # teardown closed it
    assert light.theme is Theme.light
    assert dark.theme is Theme.dark
    # tokens.html declares CSS custom properties: both context renders genuinely harvested.
    assert light.tokens and dark.tokens


@pytest.mark.browser
async def test_analyze_end_to_end_with_shared_browser(fixtures_dir: Path) -> None:
    # The default analyze() path (real harvest_page) now rides the shared browser; the
    # two-theme fixture must still yield both themes with full palettes.
    url = (fixtures_dir / "tokens.html").as_uri()
    result = await analyze(url, viewport=VIEWPORT, themes=LIGHT_AND_DARK, politeness=file_policy())
    assert set(result.themes) == {Theme.light, Theme.dark}
    assert all(palette.roles.mapping for palette in result.themes.values())
