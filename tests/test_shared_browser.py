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
from pathlib import Path

import pytest
from conftest import file_policy

import colorsense.harvest.render as render_mod
from colorsense import LIGHT_AND_DARK, analyze
from colorsense.color.primitives import parse_css_color
from colorsense.config import Config, load_default_config
from colorsense.harvest import RequestFilter, SharedBrowser, harvest_page
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


async def _no_robots(
    _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
) -> str | None:
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
        self.ws_routes: list[object] = []

    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def route(self, pattern: str, handler: object) -> None:
        self.routes.append((pattern, handler))

    async def route_web_socket(self, pattern: str, handler: object) -> None:
        self.ws_routes.append((pattern, handler))

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
        self.launch_kwargs: list[dict[str, object]] = []
        self.chromium = self  # launch() lives on .chromium in the real API

    def __call__(self) -> _FakePlaywrightStack:
        return self  # async_playwright() -> starter

    async def start(self) -> _FakePlaywrightStack:
        self.starts += 1
        return self  # the started Playwright (carries .chromium and .stop)

    async def launch(self, **kwargs: object) -> _FakeBrowser:
        self.launches += 1
        self.launch_kwargs.append(dict(kwargs))
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

    async with RenderSession(Theme.LIGHT, VIEWPORT, browser=external):  # type: ignore[arg-type]
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

    async with RenderSession(Theme.LIGHT, VIEWPORT, browser=external):  # type: ignore[arg-type]
        pass
    async with RenderSession(
        Theme.DARK,
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
# browser_args: extra Chromium launch arguments reach the launch call verbatim.
# ---------------------------------------------------------------------------

V8_CAP = "--js-flags=--max-old-space-size=512"


async def test_shared_browser_args_reach_launch_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The extras are appended after the library's own launch arguments (currently none
    # beyond headless=True) and passed verbatim — order preserved, nothing rewritten.
    stack = _patch_playwright(monkeypatch)
    async with SharedBrowser(browser_args=(V8_CAP, "--disable-dev-shm-usage")) as shared:
        await shared.get()
    (kwargs,) = stack.launch_kwargs
    assert kwargs == {"headless": True, "args": [V8_CAP, "--disable-dev-shm-usage"]}


async def test_shared_browser_default_args_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # No extras (the default) keeps the launch exactly as before: no behavior change.
    stack = _patch_playwright(monkeypatch)
    async with SharedBrowser() as shared:
        await shared.get()
    (kwargs,) = stack.launch_kwargs
    assert kwargs == {"headless": True, "args": []}


async def test_render_session_args_reach_owned_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dedicated-launch path (no external browser) honors browser_args too.
    stack = _patch_playwright(monkeypatch)
    async with RenderSession(Theme.LIGHT, VIEWPORT, browser_args=(V8_CAP,)):
        pass
    (kwargs,) = stack.launch_kwargs
    assert kwargs == {"headless": True, "args": [V8_CAP]}


async def test_render_session_rejects_args_with_external_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Launch args only exist at launch time: combining them with an already-launched
    # external browser is a contradiction and must fail loudly, not silently no-op.
    stack = _patch_playwright(monkeypatch)
    external = _FakeBrowser()
    with pytest.raises(ValueError, match="browser_args"):
        RenderSession(Theme.LIGHT, VIEWPORT, browser=external, browser_args=(V8_CAP,))  # type: ignore[arg-type]
    assert stack.starts == 0


async def test_harvest_page_rejects_args_with_shared_browser(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    # Same contradiction at the harvest_page seam — rejected as a plain ValueError BEFORE
    # any render, never wrapped into RenderError, and the handle is never resolved.
    stack = _patch_playwright(monkeypatch)
    async with SharedBrowser() as shared:
        with pytest.raises(ValueError, match="browser_args"):
            await harvest_page(
                URL, Theme.LIGHT, config, VIEWPORT, browser=shared, browser_args=(V8_CAP,)
            )
    assert (stack.starts, stack.launches) == (0, 0)


@pytest.mark.parametrize("bad", [("--ok", 512), (None,), "--bare-string"])
async def test_invalid_browser_args_raise_type_error(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    # Light validation only: non-string entries and a bare string (a forgotten one-tuple)
    # are rejected eagerly at construction; the flags themselves are never validated.
    stack = _patch_playwright(monkeypatch)
    with pytest.raises(TypeError, match="browser_args"):
        SharedBrowser(browser_args=bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="browser_args"):
        RenderSession(Theme.LIGHT, VIEWPORT, browser_args=bad)  # type: ignore[arg-type]
    assert stack.starts == 0


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
        request_filter: RequestFilter | None = None,
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
        await policy.fetch("https://example.test/a", Theme.LIGHT, config, VIEWPORT, browser=shared)
        await policy.fetch("https://example.test/b", Theme.LIGHT, config, VIEWPORT)

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

    await policy.fetch(URL, Theme.LIGHT, config, VIEWPORT)
    async with SharedBrowser() as shared:
        hit = await policy.fetch(URL, Theme.LIGHT, config, VIEWPORT, browser=shared)

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
        request_filter: RequestFilter | None = None,
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


async def test_analyze_forwards_browser_args_to_shared_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The analyze kwarg must land on the one SharedBrowser the call constructs — that
    # handle carries the args to the (single) Chromium launch every theme render shares.
    constructed: list[tuple[str, ...]] = []
    real_shared_browser = SharedBrowser

    def recording_shared_browser(*, browser_args: tuple[str, ...] = ()) -> SharedBrowser:
        constructed.append(browser_args)
        return real_shared_browser(browser_args=browser_args)

    monkeypatch.setattr("colorsense.pipeline.SharedBrowser", recording_shared_browser)
    stack = _patch_playwright(monkeypatch)
    harvester = _MiniHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    await analyze(URL, themes=LIGHT_AND_DARK, politeness=policy, browser_args=(V8_CAP,))

    assert constructed == [(V8_CAP,)]
    assert stack.starts == 0  # the fake harvester never resolved the handle


async def test_analyze_invalid_browser_args_raise_before_any_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _patch_playwright(monkeypatch)
    harvester = _MiniHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=_no_robots)

    with pytest.raises(TypeError, match="browser_args"):
        await analyze(URL, politeness=policy, browser_args=("--ok", 256))  # type: ignore[arg-type]

    assert harvester.browsers == []  # rejected before the policy/harvester saw anything
    assert stack.starts == 0


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
        light = await harvest_page(url, Theme.LIGHT, config, VIEWPORT, browser=shared)
        dark = await harvest_page(url, Theme.DARK, config, VIEWPORT, browser=shared)
        browser = await shared.get()  # the same underlying browser both renders used
        assert browser.is_connected()  # renders finished, the shared browser is still up
        assert browser.contexts == []  # each session closed its own context behind it

    assert not browser.is_connected()  # teardown closed it
    assert light.theme is Theme.LIGHT
    assert dark.theme is Theme.DARK
    # tokens.html declares CSS custom properties: both context renders genuinely harvested.
    assert light.tokens and dark.tokens


@pytest.mark.browser
async def test_analyze_succeeds_with_real_browser_args(fixtures_dir: Path) -> None:
    # Proof the plumbing survives a real Chromium launch: the V8-heap cap is a legitimate
    # flag, so the analysis must succeed exactly as without it.
    url = (fixtures_dir / "tokens.html").as_uri()
    result = await analyze(
        url,
        viewport=VIEWPORT,
        politeness=file_policy(),
        browser_args=("--js-flags=--max-old-space-size=256",),
    )
    assert result.url == url
    assert all(palette.colors for palette in result.themes.values())


@pytest.mark.browser
async def test_analyze_end_to_end_with_shared_browser(fixtures_dir: Path) -> None:
    # The default analyze() path (real harvest_page) now rides the shared browser; the
    # two-theme fixture must still yield both themes with full palettes.
    url = (fixtures_dir / "tokens.html").as_uri()
    result = await analyze(url, viewport=VIEWPORT, themes=LIGHT_AND_DARK, politeness=file_policy())
    assert set(result.themes) == {Theme.LIGHT, Theme.DARK}
    assert all(palette.colors for palette in result.themes.values())


# ---------------------------------------------------------------------------
# Release-review hardening: enter-failure cleanup and teardown/launch race.
# ---------------------------------------------------------------------------


class _GatedLaunchStack(_FakePlaywrightStack):
    """Launch blocks on an event so a teardown can race an in-flight first get()."""

    def __init__(self) -> None:
        super().__init__()
        self.launch_gate = asyncio.Event()
        self.launch_entered = asyncio.Event()

    async def launch(self, **kwargs: object) -> _FakeBrowser:
        self.launch_entered.set()
        await self.launch_gate.wait()
        return await super().launch(**kwargs)


class _FailingLaunchStack(_FakePlaywrightStack):
    async def launch(self, **kwargs: object) -> _FakeBrowser:
        raise RuntimeError("Executable doesn't exist (simulated launch failure)")


class _FailingContextBrowser(_FakeBrowser):
    async def new_context(self, **kwargs: object) -> _FakeContext:
        raise RuntimeError("new_context failed (simulated)")


class _FailingContextStack(_FakePlaywrightStack):
    async def launch(self, **kwargs: object) -> _FakeBrowser:
        self.launches += 1
        browser = _FailingContextBrowser()
        self.browsers.append(browser)
        return browser


async def test_render_session_enter_failure_stops_started_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A launch failure inside __aenter__ propagates without __aexit__ ever running, so
    # the just-started Playwright driver (a node subprocess in real life) leaked on every
    # failed attempt unless __aenter__ cleans up what it started before re-raising.
    stack = _FailingLaunchStack()
    monkeypatch.setattr(render_mod, "async_playwright", stack)

    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        await RenderSession(Theme.LIGHT, VIEWPORT).__aenter__()

    assert stack.starts == 1
    assert stack.stops == 1  # the started driver is stopped, not leaked


async def test_render_session_enter_failure_after_launch_closes_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Failing later (new_context) must release BOTH the launched browser and the driver
    # on the owned-browser path.
    stack = _FailingContextStack()
    monkeypatch.setattr(render_mod, "async_playwright", stack)

    with pytest.raises(RuntimeError, match="new_context failed"):
        await RenderSession(Theme.LIGHT, VIEWPORT).__aenter__()

    assert stack.browsers[0].closes == 1
    assert stack.stops == 1


async def test_render_session_enter_failure_never_closes_external_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On the external-browser path the session owns only its context: a setup failure
    # must not close the caller's browser.
    browser = _FailingContextBrowser()

    with pytest.raises(RuntimeError, match="new_context failed"):
        await RenderSession(Theme.LIGHT, VIEWPORT, browser=browser).__aenter__()  # type: ignore[arg-type]

    assert browser.closes == 0


async def test_shared_browser_teardown_waits_for_inflight_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # __aexit__ must serialize with get(): without the lock, a get() suspended in
    # chromium.launch could assign and return a fresh browser AFTER teardown closed
    # everything — a Chromium nobody would ever close.
    stack = _GatedLaunchStack()
    monkeypatch.setattr(render_mod, "async_playwright", stack)

    shared = SharedBrowser()
    get_task = asyncio.ensure_future(shared.get())
    await asyncio.wait_for(stack.launch_entered.wait(), 5)  # get() holds the lock in launch

    exit_task = asyncio.ensure_future(shared.__aexit__(None, None, None))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not exit_task.done()  # teardown is parked on the lock, not racing ahead

    stack.launch_gate.set()
    browser = await get_task
    await exit_task

    assert browser is stack.browsers[0]
    assert stack.browsers[0].closes == 1  # the in-flight launch's browser was closed
    assert stack.stops == 1
