"""Playwright (async API) render session.

`RenderSession` is an async context manager that opens a page at a fixed
[`Viewport`][colorsense.Viewport] and color scheme — in its own headless Chromium, or
inside an externally supplied `Browser` — navigates robustly
to a URL (guarding ``networkidle`` so ``file://`` pages never hang), neutralizes
transitions/animations, step-scrolls to trigger lazy content, and detects consent/overlay
regions whose bounding rects can be masked out of the screenshot.

`SharedBrowser` is the lazy browser-lifecycle handle that lets multiple sessions
(e.g. the light and dark renders of one ``analyze()`` call) share a single Chromium
launch: each theme still gets its own `BrowserContext` —
contexts carry the color scheme, viewport, User-Agent, and egress route — so one browser
process suffices.

The Playwright `Page` is exposed as `RenderSession.page`
so the other harvest modules can run their own JS against the same live page. Built on the
**async** Playwright API so it runs natively on an asyncio event loop (e.g. inside a
FastAPI ``async def`` endpoint) and so sibling theme renders can overlap.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import Literal, Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    WebSocketRoute,
    async_playwright,
)

from colorsense.models import Rect, Theme, Viewport

RequestFilter = Callable[[str], bool] | Callable[[str], Awaitable[bool]]
"""An egress predicate over a request URL: ``True`` permits, ``False`` aborts.

Either a plain synchronous callable or an async one. A sync predicate is invoked inline on
the event loop's request path, so it must not block (cheap string checks only); an async
predicate is awaited, free to move slow work (e.g. DNS resolution) off the loop — the
shipped [`colorsense.block_private_networks`][colorsense.block_private_networks] guard is
async for exactly that reason.
Both seams that apply a filter (the browser route handler and the robots loader) evaluate
it through `evaluate_request_filter`, so raising — sync or async — always fails
closed.
"""


async def evaluate_request_filter(request_filter: RequestFilter, url: str) -> bool:
    """Apply ``request_filter`` to ``url``; awaits async predicates; never raises (fail closed).

    The single place filter-invocation semantics live: the predicate is called, an
    awaitable result is awaited, and the outcome is coerced to ``bool``. ANY exception —
    from the call or from the await — yields ``False``, so a buggy filter can never
    silently wave traffic through. Module-level (and browser-free) so both seams that
    enforce a filter share one behavior and it stays unit-testable.

    Cancellation is the one deliberate exception to "never raises": ``CancelledError`` is
    a ``BaseException``, so cancelling the awaiting task mid-evaluation propagates out of
    this function rather than being coerced to ``False``. That is fail-closed too — the
    caller never receives a ``True`` it didn't earn, and the request never proceeds; see
    `_route_handler` for what that means on the browser route path.
    """
    try:
        verdict = request_filter(url)
        if inspect.isawaitable(verdict):
            verdict = await verdict
        return bool(verdict)
    except Exception:
        # Fail CLOSED: a broken predicate must block, not permit.
        return False


# Map our Theme to Playwright's color_scheme literal.
_COLOR_SCHEME: dict[Theme, Literal["light", "dark"]] = {
    Theme.light: "light",
    Theme.dark: "dark",
}

# Inject CSS killing transitions/animations so computed colors are deterministic.
_DISABLE_MOTION_CSS: str = "* { transition: none !important; animation: none !important; }"

# Max step-scroll iterations to trigger lazy content (cap so we never loop forever).
_MAX_SCROLL_STEPS: int = 20

# Default navigation timeout (ms) for ``page.goto``. Made explicit (rather than relying on
# Playwright's implicit 30s default) so the value is documented and overridable per render.
# A ``goto`` that exceeds it raises a Playwright ``TimeoutError``, which ``harvest_page``
# wraps as the public ``RenderError``.
DEFAULT_NAV_TIMEOUT_MS: float = 30_000.0

# Timeout (ms) guarding wait_for_load_state("networkidle") on pages that never idle.
# Kept short on purpose: ``goto(wait_until="load")`` has already fired the load event (all
# synchronous resources fetched), so this only waits out async/lazy chatter (analytics,
# below-fold images). Measured against real sites (stripe/github/bootstrap), dropping this
# from 3s to 1s left the harvested palette, tokens, and hover hits unchanged while saving
# ~1.5s/render; the subsequent step-scroll still triggers genuinely lazy content.
_NETWORKIDLE_TIMEOUT_MS: float = 1000.0

# JS that step-scrolls the full document height and returns the iteration count.
_STEP_SCROLL_JS: str = """
(maxSteps) => {
    const step = window.innerHeight;
    let pos = 0;
    let iterations = 0;
    const limit = Math.max(document.body ? document.body.scrollHeight : 0,
                           document.documentElement.scrollHeight);
    while (pos < limit && iterations < maxSteps) {
        window.scrollTo(0, pos);
        pos += step;
        iterations += 1;
    }
    window.scrollTo(0, 0);
    return iterations;
}
"""

# JS that finds consent/overlay banners and returns their bounding rects.
_CONSENT_RECTS_JS: str = r"""
() => {
    const keywords = /cookie|consent|gdpr|onetrust|cookiebot|usercentrics|privacy|banner/i;
    const out = [];
    const seen = new Set();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const push = (el) => {
        if (seen.has(el)) return;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return;
        seen.add(el);
        out.push({x: r.x, y: r.y, w: r.width, h: r.height});
    };
    for (const el of document.querySelectorAll('*')) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const idClass = (el.id + ' ' + (el.className && el.className.toString
            ? el.className.toString() : '')).trim();
        const matchesKeyword = keywords.test(idClass);
        const pos = style.position;
        const z = parseInt(style.zIndex, 10);
        const r = el.getBoundingClientRect();
        const fullWidthish = r.width >= vw * 0.8;
        const fixedSticky = pos === 'fixed' || pos === 'sticky';
        const highZ = Number.isFinite(z) && z >= 1000;
        const coversBand = fullWidthish && r.height > 0 && r.height < vh;
        if (matchesKeyword && (fixedSticky || highZ || fullWidthish)) {
            push(el);
        } else if (fixedSticky && (highZ || coversBand) && fullWidthish) {
            push(el);
        }
    }
    return out;
}
"""


def normalize_browser_args(browser_args: Sequence[str]) -> tuple[str, ...]:
    """Lightly validate extra Chromium launch arguments and return them as a tuple.

    Mechanism, not policy: entries are appended to the library's own launch arguments and
    passed **verbatim** to Chromium — no attempt is made to validate or allowlist the flags
    themselves. Only obviously broken input is rejected, *before* any browser is involved:

    * a bare string (almost certainly a forgotten one-tuple: pass ``("--flag",)``, not
      ``"--flag"``) raises `TypeError`;
    * any non-string entry raises `TypeError`.
    """
    if isinstance(browser_args, str):
        raise TypeError(
            "browser_args must be a sequence of strings, not a bare string "
            "(pass ('--flag',), not '--flag')"
        )
    for arg in browser_args:
        if not isinstance(arg, str):
            raise TypeError(
                f"browser_args entries must be strings, got {type(arg).__name__}: {arg!r}"
            )
    return tuple(browser_args)


def _route_handler(
    request_filter: RequestFilter,
) -> Callable[[Route], Awaitable[None]]:
    """Build the ``context.route`` handler enforcing ``request_filter`` on every request.

    The predicate sees the request URL (the navigation itself, every redirect hop, and
    every sub-resource the rendered page asks for: scripts, images, XHR/``fetch``).
    ``False`` aborts the request. What ``context.route`` does NOT intercept — WebSocket
    opening handshakes and service-worker traffic — is blocked outright instead of
    filtered (see `_refuse_web_socket` and the ``service_workers="block"`` context option),
    so no browser-initiated network path escapes both the filter and the block.
    Evaluation goes through `evaluate_request_filter`, so sync and async predicates
    are handled uniformly and a predicate that *raises* fails CLOSED — the request is
    aborted. Module-level so the abort/continue logic is unit testable without a browser.

    Cancellation corner: if the task awaiting the handler is cancelled mid-evaluation,
    ``CancelledError`` (a ``BaseException``) propagates past the fail-closed ``except``
    in `evaluate_request_filter` and out of the handler, so the route is neither
    continued nor aborted. That un-actioned route is still fail-closed — the request never
    proceeds — and the situation only arises at teardown/cancellation, when the Playwright
    ``Route`` may no longer be safely usable anyway, which is why the handler deliberately
    does NOT catch the cancellation to abort the route.
    """

    async def handle(route: Route) -> None:
        if await evaluate_request_filter(request_filter, route.request.url):
            await route.continue_()
        else:
            await route.abort()

    return handle


async def _refuse_web_socket(ws_route: WebSocketRoute) -> None:
    """Refuse a WebSocket connection outright (the WS arm of the egress gate).

    ``context.route`` does not intercept WebSocket opening handshakes, so the HTTP route
    handler above never sees a ``new WebSocket('ws://...')`` issued by the rendered page —
    left unrouted, that handshake would be a real GET the ``request_filter`` cannot vet.
    Rather than proxying the connection through the filter (which would require connecting
    to the real server before a verdict), this handler simply never calls
    ``connect_to_server`` — no egress occurs at all — and closes the page-side socket so
    the page observes a dead connection. A dead WebSocket is harmless for palette
    extraction, mirroring the data:/blob: abort rationale in ``net/guard.py``.
    Installed (against all URLs) only when a ``request_filter`` is configured, alongside
    the HTTP route.
    """
    await ws_route.close()


class SharedBrowser:
    """Lazily-launched headless Chromium shared by multiple `RenderSession`\\ s.

    Async context manager owning one Playwright + `Browser`
    pair. Nothing is launched until the first `get` call, so a caller whose renders
    are all served from a cache (or driven by an injected fake harvester) never pays a
    browser launch. Teardown closes the browser and stops Playwright exactly once —
    teardown errors are suppressed so an original in-flight exception propagates cleanly —
    and `get` refuses to relaunch after teardown.

    Usage::

        async with SharedBrowser() as shared:
            # each render opens its own context inside the one browser
            await harvest_page(url, Theme.light, config, viewport, browser=shared)
            await harvest_page(url, Theme.dark, config, viewport, browser=shared)

    Parameters
    ----------
    browser_args:
        Extra command-line arguments appended to the library's own launch arguments and
        passed **verbatim** to the Chromium launch (the library does not validate or
        allowlist the flags — mechanism, not policy). Canonical use case:
        ``("--js-flags=--max-old-space-size=512",)`` caps each renderer process's V8 heap
        at 512 MB. Note this bounds the **JS heap only**, not total renderer memory —
        container/cgroup limits remain the enforceable bound (see ``SECURITY.md`` §2).
        Non-string entries (or a bare string) raise `TypeError` at construction.
    """

    def __init__(self, *, browser_args: Sequence[str] = ()) -> None:
        self._browser_args = normalize_browser_args(browser_args)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._closed = False
        # Serializes first-use launching so concurrent get() calls (e.g. sibling theme
        # renders gathered by analyze()) share one launch instead of racing two.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._closed = True
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._browser = None
        self._playwright = None

    async def get(self) -> Browser:
        """Return the shared `Browser`, launching it on first use.

        Launch failures propagate as Playwright errors (callers such as
        `harvest_page` wrap them into the public
        [`RenderError`][colorsense.RenderError]). After teardown this raises
        `RuntimeError` rather than silently relaunching a browser nobody closes.
        """
        async with self._lock:
            if self._closed:
                raise RuntimeError("SharedBrowser.get() called after teardown")
            if self._browser is None:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True, args=list(self._browser_args)
                )
            return self._browser


class RenderSession:
    """Context manager wrapping a headless Chromium page at a fixed theme/viewport.

    Usage::

        async with RenderSession(theme, viewport) as session:
            await session.goto(url)
            page = session.page  # run module JS against it
            consent = session.consent_rects

    Parameters
    ----------
    user_agent:
        When not ``None``, the User-Agent string set on the browser context, so page
        navigations identify themselves with it instead of the stock headless-Chromium UA.
        The politeness layer passes its configured wire UA through here. ``None`` (the
        default) keeps Playwright's stock UA.
    request_filter:
        When not ``None``, a [`RequestFilter`][colorsense.RequestFilter] — a sync or async predicate
        over every request URL the browser makes (the navigation and all sub-resources), installed
        as a ``context.route`` interceptor: requests for which it returns ``False`` — or for which
        it raises (fail closed) — are aborted. A sync predicate runs inline on the event loop and
        must not block; an async one is awaited. WebSocket handshakes are not routable through
        ``context.route``, so when a filter is configured they are refused outright rather than
        filtered (see `_refuse_web_socket`); service workers are blocked unconditionally at
        context creation. ``None`` (the default) installs no routes at all, so the unfiltered
        path has zero interception overhead.
    browser:
        When not ``None``, an externally owned `Browser` the
        session opens its context inside instead of launching its own Chromium (the per-
        session knobs — color scheme, viewport, UA, egress route — all live on the
        `BrowserContext`, so sessions sharing one browser stay
        fully isolated). The session then owns only its context: teardown closes the
        context but never the external browser, whose lifecycle stays with the caller
        (see `SharedBrowser`). ``None`` (the default) launches and tears down a
        dedicated Playwright + Chromium pair as before.
    browser_args:
        Extra Chromium launch arguments for the session's **own** launch, appended to the
        library's launch arguments and passed verbatim (see
        `SharedBrowser` for the canonical V8-heap-cap use case and caveats). Launch
        arguments only exist at launch time, so combining a non-empty ``browser_args`` with
        an external ``browser`` (already launched, by someone else) raises
        `ValueError` — put the args on the `SharedBrowser` instead.
    """

    def __init__(
        self,
        theme: Theme,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: RequestFilter | None = None,
        browser: Browser | None = None,
        browser_args: Sequence[str] = (),
    ) -> None:
        self._browser_args = normalize_browser_args(browser_args)
        if browser is not None and self._browser_args:
            raise ValueError(
                "browser_args apply at launch time and cannot be combined with an external "
                "browser; pass them to SharedBrowser (or whoever launches the browser) instead"
            )
        self._theme = theme
        self._viewport = viewport
        self._user_agent = user_agent
        self._request_filter = request_filter
        self._owns_browser = browser is None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = browser
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._consent_rects: list[Rect] = []

    # -- context manager --------------------------------------------------

    async def __aenter__(self) -> Self:
        if self._owns_browser:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True, args=list(self._browser_args)
            )
        assert self._browser is not None  # external browser, or just launched above
        # ``user_agent=None`` is Playwright's own default (stock headless-Chromium UA), so a
        # configured UA overrides it and ``None`` passes through unchanged.
        # ``service_workers="block"`` is unconditional: service workers are irrelevant to
        # color harvesting, and by default their requests bypass ``context.route`` — an
        # unrouted path the egress filter would never see. Blocking registration removes
        # that path entirely.
        self._context = await self._browser.new_context(
            viewport={"width": self._viewport.width, "height": self._viewport.height},
            device_scale_factor=self._viewport.device_scale_factor,
            color_scheme=_COLOR_SCHEME[self._theme],
            user_agent=self._user_agent,
            service_workers="block",
        )
        # Egress filtering is opt-in: install the interceptors only when a filter exists, so
        # the default (None) path has zero routing overhead. WebSocket handshakes are not
        # seen by ``context.route``, so they get their own route that refuses to connect
        # (see _refuse_web_socket).
        if self._request_filter is not None:
            await self._context.route("**/*", _route_handler(self._request_filter))
            await self._context.route_web_socket("**/*", _refuse_web_socket)
        self._page = await self._context.new_page()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Always tear down, swallowing teardown errors so the original exception (if any)
        # propagates cleanly. An external browser is NOT closed — only the resources this
        # session owns (always its context; the browser/Playwright pair only when launched
        # here) are released, and the handles are cleared either way.
        closers = [self._context, self._browser] if self._owns_browser else [self._context]
        for closer in closers:
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    # -- accessors --------------------------------------------------------

    @property
    def page(self) -> Page:
        """The live Playwright page (only valid inside the ``with`` block)."""
        if self._page is None:
            raise RuntimeError("RenderSession.page accessed outside of an active session")
        return self._page

    @property
    def theme(self) -> Theme:
        """The theme this session renders under."""
        return self._theme

    @property
    def viewport(self) -> Viewport:
        """The viewport this session renders at."""
        return self._viewport

    @property
    def consent_rects(self) -> list[Rect]:
        """Bounding rects of detected consent/overlay banners (for masking)."""
        return list(self._consent_rects)

    # -- navigation -------------------------------------------------------

    async def goto(self, url: str, *, nav_timeout_ms: float = DEFAULT_NAV_TIMEOUT_MS) -> None:
        """Navigate to ``url`` and stabilize the page for harvesting.

        Performs ``goto(..., wait_until="load")``, a guarded ``networkidle`` wait, motion
        neutralization, step-scrolling to trigger lazy content, and consent-region
        detection. ``networkidle`` is guarded with a timeout/try-except so ``file://``
        pages that never report idle do not hang.

        Parameters
        ----------
        nav_timeout_ms:
            Per-navigation timeout in milliseconds, passed explicitly to ``page.goto``.
            Defaults to `DEFAULT_NAV_TIMEOUT_MS`. Exceeding it raises a Playwright
            ``TimeoutError`` (wrapped as [`RenderError`][colorsense.RenderError] upstream).
        """
        page = self.page
        await page.goto(url, wait_until="load", timeout=nav_timeout_ms)
        # file:// pages may never report networkidle; guard with a timeout.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS)

        await page.add_style_tag(content=_DISABLE_MOTION_CSS)

        # Step-scrolling is best-effort (triggers lazy content).
        with contextlib.suppress(Exception):
            await page.evaluate(_STEP_SCROLL_JS, _MAX_SCROLL_STEPS)

        self._consent_rects = await self._detect_consent_rects()

    async def _detect_consent_rects(self) -> list[Rect]:
        """Return bounding rects of consent/overlay banners without clicking them."""
        try:
            raw = await self.page.evaluate(_CONSENT_RECTS_JS)
        except Exception:  # detection is best-effort
            return []
        rects: list[Rect] = []
        if not isinstance(raw, list):
            return rects
        for item in raw:
            if not isinstance(item, dict):
                continue
            rects.append(
                Rect(
                    x=float(item["x"]),
                    y=float(item["y"]),
                    width=float(item["w"]),
                    height=float(item["h"]),
                )
            )
        return rects
