"""Regression tests for two robustness fixes (network-free).

Fix 1 (M4): ``robots.txt`` ``User-agent: colorsense`` disallows must be honored under the
default wire UA (which begins with a browser token). These drive
:class:`PolitenessPolicy.can_fetch` with an injected ``robots_loader`` so no real network or
browser is touched.

Fix 2 (M3): a Playwright navigation/render failure must surface as the public
:class:`colorsense.harvest.RenderError`, not the version-private ``playwright._impl`` type.
This is exercised by monkeypatching the render seam so it raises a Playwright error, keeping
the test deterministic and browser-free.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError

import colorsense.harvest as harvest_mod
import colorsense.harvest.render as render_mod
from colorsense.config import Config, load_default_config
from colorsense.harvest import RenderError, RequestFilter, SharedBrowser, harvest_page
from colorsense.harvest.render import RenderSession
from colorsense.models import Harvest, Theme, Viewport
from colorsense.net.politeness import PolitenessPolicy, _default_robots_loader

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


# --- Fix 1: agent-specific robots disallows -------------------------------------------


def _loader_for(text: str | None):
    async def _loader(
        _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
    ) -> str | None:
        return text

    return _loader


async def test_agent_specific_disallow_blocks_under_default_ua() -> None:
    # A ``User-agent: colorsense`` group with ``Disallow: /`` must block, even though the
    # wire UA begins with "Mozilla/5.0" (the bug: prefix-matching the wire UA missed this).
    robots = "User-agent: colorsense\nDisallow: /\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("https://example.com/page") is False


async def test_wildcard_disallow_still_blocks() -> None:
    robots = "User-agent: *\nDisallow: /\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("https://example.com/page") is False


async def test_agent_specific_allow_permits() -> None:
    # Disallow everyone, but explicitly allow colorsense: agent-specific group must win.
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: colorsense\nDisallow:\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("https://example.com/page") is True


async def test_file_url_bypasses_robots() -> None:
    # file:// has no host/robots concept: always fetchable, even under a disallow-all loader.
    robots = "User-agent: colorsense\nDisallow: /\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("file:///tmp/x.html") is True


# --- Fix 2: RenderError wraps Playwright failures -------------------------------------


async def test_render_error_raised_on_navigation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://nonexistent.invalid/"
    original = PlaywrightError("net::ERR_NAME_NOT_RESOLVED at " + url)

    class _FailingSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        async def __aenter__(self) -> _FailingSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def goto(self, _url: str, **_kwargs: object) -> None:
            raise original

    monkeypatch.setattr(harvest_mod, "RenderSession", _FailingSession)

    config = load_default_config()
    with pytest.raises(RenderError) as excinfo:
        await harvest_page(url, Theme.light, config, VIEWPORT)

    err = excinfo.value
    assert isinstance(err, RenderError)  # catchable as the typed public class
    assert err.url == url
    assert err.__cause__ is original  # original Playwright error chained via ``from``


# --- Motion-neutralization CSS injection is best-effort --------------------------------
#
# Playwright's add_style_tag races its evaluation against ANY console error on the page
# mentioning "Content Security Policy" (Frame._raceWithCSPError in the driver), so on a
# busy page an *unrelated* violation — e.g. the site's own third-party tracker blocked by
# its connect-src — can spuriously reject the call. Seen live as
# ``RenderError: Page.add_style_tag: Connecting to 'https://...' violates the following
# Content Security Policy directive: "connect-src ..."`` on analyze("https://stripe.com").
# The injection is stabilization, not harvesting: goto() must retry once, then warn and
# continue rendering without the CSS — never fail the whole analysis.

_CSP_RACE_ERROR_TEXT = (
    "Page.add_style_tag: Connecting to 'https://dm.tracker.example/' violates the "
    'following Content Security Policy directive: "connect-src ..."'
)


class _MotionFakePage:
    """Fake Page for driving ``RenderSession.goto``; ``add_style_tag`` fails N times."""

    def __init__(self, style_failures: int) -> None:
        self._style_failures = style_failures
        self.style_attempts = 0
        self.injected_css: list[str] = []

    async def goto(self, _url: str, **_kwargs: object) -> None:
        return None

    async def wait_for_load_state(self, _state: str, **_kwargs: object) -> None:
        return None

    async def add_style_tag(self, *, content: str) -> None:
        self.style_attempts += 1
        if self.style_attempts <= self._style_failures:
            raise PlaywrightError(_CSP_RACE_ERROR_TEXT)
        self.injected_css.append(content)

    async def evaluate(self, _js: str, *_args: object) -> list[object]:
        return []


def _session_with_page(page: _MotionFakePage) -> RenderSession:
    session = RenderSession(Theme.light, VIEWPORT)
    session._page = page  # type: ignore[assignment]
    return session


async def test_goto_injects_motion_css_once_on_success(
    recwarn: pytest.WarningsRecorder,
) -> None:
    page = _MotionFakePage(style_failures=0)

    await _session_with_page(page).goto("https://example.test/")

    assert page.style_attempts == 1
    assert page.injected_css == [render_mod._DISABLE_MOTION_CSS]
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]


async def test_goto_retries_transient_style_injection_failure(
    recwarn: pytest.WarningsRecorder,
) -> None:
    # One spurious CSP-race rejection: the retry must land the CSS, with no warning and
    # no error escaping goto().
    page = _MotionFakePage(style_failures=1)

    await _session_with_page(page).goto("https://example.test/")

    assert page.style_attempts == 2
    assert page.injected_css == [render_mod._DISABLE_MOTION_CSS]
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]


async def test_goto_warns_and_continues_when_style_injection_keeps_failing() -> None:
    # Persistent failure (e.g. the page's CSP genuinely forbids inline styles): goto()
    # gives up after exactly one retry, emits a RuntimeWarning, and the render proceeds
    # without the CSS instead of raising.
    page = _MotionFakePage(style_failures=99)
    session = _session_with_page(page)

    with pytest.warns(RuntimeWarning, match="transition/animation-disabling CSS"):
        await session.goto("https://example.test/")

    assert page.style_attempts == 2  # initial attempt + one retry, then degrade
    assert page.injected_css == []
    assert session.consent_rects == []  # the rest of goto() still ran


# --- RenderSession teardown: every resource is closed exactly once --------------------


class _Closer:
    """A fake ``_context``/``_browser`` recording its ``close()`` calls."""

    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1


class _Playwright:
    """A fake ``_playwright`` recording its ``stop()`` calls."""

    def __init__(self) -> None:
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1


def _session_with_fakes() -> tuple[RenderSession, _Closer, _Closer, _Playwright]:
    """A RenderSession with its Playwright handles replaced by recording stubs."""
    session = RenderSession(Theme.light, VIEWPORT)
    context, browser, pw = _Closer(), _Closer(), _Playwright()
    session._context = context  # type: ignore[assignment]
    session._browser = browser  # type: ignore[assignment]
    session._playwright = pw  # type: ignore[assignment]
    return session, context, browser, pw


def _assert_torn_down_once(
    session: RenderSession, context: _Closer, browser: _Closer, pw: _Playwright
) -> None:
    # Each resource closed/stopped exactly once: a future edit dropping a `.close()`/`.stop()`
    # turns this red.
    assert context.closes == 1
    assert browser.closes == 1
    assert pw.stops == 1
    # Handles are cleared so a stale resource can't be re-used after teardown.
    assert session._context is None
    assert session._browser is None
    assert session._playwright is None
    assert session._page is None


async def test_aexit_closes_resources_on_normal_exit() -> None:
    session, context, browser, pw = _session_with_fakes()

    result = await session.__aexit__(None, None, None)

    assert result is None  # does not suppress (there is nothing to suppress)
    _assert_torn_down_once(session, context, browser, pw)


async def test_aexit_closes_resources_on_exception_path() -> None:
    # Driving __aexit__ with exception info must still tear every resource down once, and
    # must NOT suppress the in-flight exception (returns falsy so it propagates).
    session, context, browser, pw = _session_with_fakes()
    exc = RuntimeError("boom")

    result = await session.__aexit__(type(exc), exc, exc.__traceback__)

    assert not result  # falsy -> the original exception propagates
    _assert_torn_down_once(session, context, browser, pw)


async def test_aexit_swallows_teardown_errors_but_still_closes_rest() -> None:
    # A failing context.close() must not stop browser.close()/playwright.stop(): teardown
    # errors are suppressed so the original control flow is preserved.
    session, context, browser, pw = _session_with_fakes()

    async def _boom() -> None:
        context.closes += 1
        raise RuntimeError("close failed")

    context.close = _boom  # type: ignore[method-assign]

    await session.__aexit__(None, None, None)

    assert context.closes == 1  # attempted once
    assert browser.closes == 1  # still closed despite the context failure
    assert pw.stops == 1
    assert session._browser is None
    assert session._playwright is None


# --- Configured user_agent reaches the wire (robots GET + page render) -----------------


CUSTOM_UA = "colorsense-tests/1.0 (+https://example.test/contact)"


class _RecordingHarvester:
    """Harvester fake capturing the keyword-only seams it was invoked with."""

    def __init__(self) -> None:
        self.user_agents: list[str | None] = []
        self.request_filters: list[RequestFilter | None] = []

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
        self.user_agents.append(user_agent)
        self.request_filters.append(request_filter)
        return Harvest(url=url, theme=theme, viewport=viewport, screenshot_bins=[])


async def test_policy_passes_configured_user_agent_to_robots_loader() -> None:
    # The robots GET must identify as the policy's configured UA, not the module default
    # (the bug: ``_default_robots_loader`` hardcoded DEFAULT_USER_AGENT).
    seen: list[tuple[str, str]] = []

    async def recording_loader(
        url: str, user_agent: str, _request_filter: RequestFilter | None = None
    ) -> str | None:
        seen.append((url, user_agent))
        return None  # no rules => permitted

    policy = PolitenessPolicy(user_agent=CUSTOM_UA, robots_loader=recording_loader)
    assert await policy.can_fetch("https://example.com/page") is True
    assert seen == [("https://example.com/robots.txt", CUSTOM_UA)]


async def test_policy_passes_configured_user_agent_to_harvester() -> None:
    # The page render must go out under the configured UA too (the bug: fetch never
    # forwarded it, so navigation used the stock HeadlessChrome UA).
    harvester = _RecordingHarvester()

    async def no_robots(
        _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
    ) -> str | None:
        return None

    policy = PolitenessPolicy(user_agent=CUSTOM_UA, harvester=harvester, robots_loader=no_robots)
    config = load_default_config()
    await policy.fetch("https://example.com/page", Theme.light, config, VIEWPORT)
    assert harvester.user_agents == [CUSTOM_UA]


async def test_policy_passes_request_filter_to_harvester() -> None:
    # The configured egress predicate must reach the harvester (which installs it as a
    # browser route), and the default (None) must pass through as None.
    harvester = _RecordingHarvester()

    async def no_robots(
        _url: str, _user_agent: str, _request_filter: RequestFilter | None = None
    ) -> str | None:
        return None

    def only_example(url: str) -> bool:
        return url.startswith("https://example.com/")

    config = load_default_config()
    policy = PolitenessPolicy(
        harvester=harvester, robots_loader=no_robots, request_filter=only_example
    )
    await policy.fetch("https://example.com/page", Theme.light, config, VIEWPORT)
    assert harvester.request_filters == [only_example]

    default_policy = PolitenessPolicy(harvester=harvester, robots_loader=no_robots)
    await default_policy.fetch("https://example.com/page", Theme.light, config, VIEWPORT)
    assert harvester.request_filters == [only_example, None]


# --- _route_handler: egress filter abort/continue logic (browserless) -------------------


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    """A fake Playwright ``Route`` recording abort/continue calls."""

    def __init__(self, url: str) -> None:
        self.request = _FakeRequest(url)
        self.aborted = 0
        self.continued = 0

    async def abort(self) -> None:
        self.aborted += 1

    async def continue_(self) -> None:
        self.continued += 1


async def test_route_handler_continues_allowed_request() -> None:
    handler = render_mod._route_handler(lambda url: url.startswith("https://ok.test/"))
    route = _FakeRoute("https://ok.test/asset.css")
    await handler(route)  # type: ignore[arg-type]
    assert (route.continued, route.aborted) == (1, 0)


async def test_route_handler_aborts_blocked_request() -> None:
    handler = render_mod._route_handler(lambda url: url.startswith("https://ok.test/"))
    route = _FakeRoute("http://169.254.169.254/latest/meta-data/")
    await handler(route)  # type: ignore[arg-type]
    assert (route.continued, route.aborted) == (0, 1)


async def test_route_handler_fails_closed_on_predicate_error() -> None:
    # A buggy predicate must BLOCK, not permit: the raised error is swallowed and the
    # request aborted.
    def broken(_url: str) -> bool:
        raise RuntimeError("predicate boom")

    handler = render_mod._route_handler(broken)
    route = _FakeRoute("https://ok.test/page")
    await handler(route)  # type: ignore[arg-type]
    assert (route.continued, route.aborted) == (0, 1)


async def test_route_handler_supports_async_predicates() -> None:
    # The seam accepts async predicates too (e.g. the shipped block_private_networks()):
    # the handler awaits the verdict instead of treating the coroutine as truthy.
    async def only_ok(url: str) -> bool:
        return url.startswith("https://ok.test/")

    handler = render_mod._route_handler(only_ok)
    allowed = _FakeRoute("https://ok.test/asset.css")
    blocked = _FakeRoute("http://169.254.169.254/latest/meta-data/")
    await handler(allowed)  # type: ignore[arg-type]
    await handler(blocked)  # type: ignore[arg-type]
    assert (allowed.continued, allowed.aborted) == (1, 0)
    assert (blocked.continued, blocked.aborted) == (0, 1)


# --- evaluate_request_filter: the shared sync-or-async invocation helper ----------------


async def test_evaluate_request_filter_sync_verdicts() -> None:
    assert await render_mod.evaluate_request_filter(lambda _url: True, "https://a.test/") is True
    assert await render_mod.evaluate_request_filter(lambda _url: False, "https://a.test/") is False


async def test_evaluate_request_filter_async_verdicts() -> None:
    async def allow(_url: str) -> bool:
        return True

    async def deny(_url: str) -> bool:
        return False

    assert await render_mod.evaluate_request_filter(allow, "https://a.test/") is True
    assert await render_mod.evaluate_request_filter(deny, "https://a.test/") is False


async def test_evaluate_request_filter_sync_raise_fails_closed() -> None:
    def broken(_url: str) -> bool:
        raise RuntimeError("sync predicate boom")

    assert await render_mod.evaluate_request_filter(broken, "https://a.test/") is False


async def test_evaluate_request_filter_async_raise_fails_closed() -> None:
    async def broken(_url: str) -> bool:
        raise RuntimeError("async predicate boom")

    assert await render_mod.evaluate_request_filter(broken, "https://a.test/") is False


async def test_evaluate_request_filter_coerces_non_bool_results() -> None:
    # A sloppy predicate returning a truthy non-bool (e.g. a match object or string) is
    # coerced, never compared by identity.
    assert await render_mod.evaluate_request_filter(lambda _url: "yes", "https://a.test/") is True  # type: ignore[arg-type]
    assert await render_mod.evaluate_request_filter(lambda _url: "", "https://a.test/") is False  # type: ignore[arg-type]


async def test_default_robots_loader_sends_given_user_agent() -> None:
    # The default loader must put the UA it is *given* on the wire. The loader's private
    # ``_transport`` seam injects an httpx.MockTransport so the request headers can be
    # captured without any network.
    sent_uas: list[str] = []
    robots_text = "User-agent: *\nDisallow:\n"

    def handler(request: httpx.Request) -> httpx.Response:
        sent_uas.append(request.headers["user-agent"])
        return httpx.Response(200, text=robots_text)

    transport = httpx.MockTransport(handler)

    text = await _default_robots_loader(
        "https://example.test/robots.txt", CUSTOM_UA, _transport=transport
    )
    assert text == robots_text
    assert sent_uas == [CUSTOM_UA]


# --- RenderSession forwards user_agent to new_context ----------------------------------


class _UAFakePage:
    pass


class _UAFakeContext:
    """Fake context recording the route interceptors installed on it."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.ws_routes: list[tuple[str, object]] = []

    async def route(self, pattern: str, handler: object) -> None:
        self.routes.append((pattern, handler))

    async def route_web_socket(self, pattern: str, handler: object) -> None:
        self.ws_routes.append((pattern, handler))

    async def new_page(self) -> _UAFakePage:
        return _UAFakePage()

    async def close(self) -> None:
        return None


class _UAFakeBrowser:
    """Fake browser recording every kwargs dict passed to ``new_context``."""

    def __init__(self) -> None:
        self.context_kwargs: list[dict[str, object]] = []
        self.contexts: list[_UAFakeContext] = []

    async def new_context(self, **kwargs: object) -> _UAFakeContext:
        self.context_kwargs.append(kwargs)
        context = _UAFakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        return None


class _UAFakeChromium:
    def __init__(self, browser: _UAFakeBrowser) -> None:
        self._browser = browser

    async def launch(self, **_kwargs: object) -> _UAFakeBrowser:
        return self._browser


class _UAFakePlaywright:
    def __init__(self, browser: _UAFakeBrowser) -> None:
        self.chromium = _UAFakeChromium(browser)

    async def stop(self) -> None:
        return None


class _UAFakePlaywrightStarter:
    def __init__(self, browser: _UAFakeBrowser) -> None:
        self._browser = browser

    async def start(self) -> _UAFakePlaywright:
        return _UAFakePlaywright(self._browser)


def _patch_playwright(monkeypatch: pytest.MonkeyPatch) -> _UAFakeBrowser:
    browser = _UAFakeBrowser()
    monkeypatch.setattr(render_mod, "async_playwright", lambda: _UAFakePlaywrightStarter(browser))
    return browser


async def test_render_session_forwards_user_agent_to_new_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _patch_playwright(monkeypatch)

    async with RenderSession(Theme.light, VIEWPORT, user_agent=CUSTOM_UA):
        pass

    assert len(browser.context_kwargs) == 1
    assert browser.context_kwargs[0]["user_agent"] == CUSTOM_UA


async def test_render_session_default_keeps_stock_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No configured UA => ``user_agent=None`` reaches new_context, i.e. Playwright's own
    # default (the stock UA) — never an empty string or a stale override.
    browser = _patch_playwright(monkeypatch)

    async with RenderSession(Theme.light, VIEWPORT):
        pass

    assert browser.context_kwargs[0]["user_agent"] is None


# --- Egress gate arms context.route cannot cover: service workers + WebSockets ----------


async def test_render_session_always_blocks_service_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Service-worker requests bypass context.route, so registration is blocked at context
    # creation — unconditionally, filter or no filter (SW are irrelevant to harvesting).
    browser = _patch_playwright(monkeypatch)

    async with RenderSession(Theme.light, VIEWPORT):
        pass
    async with RenderSession(Theme.light, VIEWPORT, request_filter=lambda _url: True):
        pass

    assert [kwargs["service_workers"] for kwargs in browser.context_kwargs] == ["block", "block"]


async def test_render_session_installs_ws_refusal_route_with_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Configuring a request_filter must install BOTH interceptors: the HTTP route enforcing
    # the filter and the route_web_socket refusing every WebSocket (whose handshakes the
    # HTTP route never sees).
    browser = _patch_playwright(monkeypatch)

    async with RenderSession(Theme.light, VIEWPORT, request_filter=lambda _url: True):
        pass

    (context,) = browser.contexts
    assert [pattern for pattern, _ in context.routes] == ["**/*"]
    assert context.ws_routes == [("**/*", render_mod._refuse_web_socket)]


async def test_render_session_installs_no_routes_without_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default (no filter) path stays interception-free: zero routes of either kind.
    browser = _patch_playwright(monkeypatch)

    async with RenderSession(Theme.light, VIEWPORT):
        pass

    (context,) = browser.contexts
    assert context.routes == []
    assert context.ws_routes == []


async def test_refuse_web_socket_closes_without_connecting() -> None:
    # The refusal handler must close the page-side socket and never call
    # connect_to_server — refusal means zero egress, not a filtered proxy.
    class _FakeWebSocketRoute:
        def __init__(self) -> None:
            self.closes = 0
            self.connects = 0

        async def close(self) -> None:
            self.closes += 1

        def connect_to_server(self) -> None:
            self.connects += 1

    ws_route = _FakeWebSocketRoute()
    await render_mod._refuse_web_socket(ws_route)  # type: ignore[arg-type]
    assert ws_route.closes == 1
    assert ws_route.connects == 0
