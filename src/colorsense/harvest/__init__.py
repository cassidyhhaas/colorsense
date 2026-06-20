"""Page rendering and design-token / color harvesting.

The public interface:

* `harvest_page` — async: render a URL under a theme and produce a
  `Harvest`.
* `RenderSession` — the Playwright async context manager used internally (exported
  for advanced/manual use).
* `SharedBrowser` — lazy handle sharing one Chromium launch across several renders
  (each render still gets its own browser context); [`colorsense.analyze`][colorsense.analyze]
  uses one per call so sibling theme renders don't each pay a browser launch.
* [`RequestFilter`][colorsense.RequestFilter] — the type of a ``request_filter`` predicate: a sync
  **or** async ``url -> bool`` callable (sync runs inline on the event loop and must not block;
  async is awaited).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from colorsense.config import Config
from colorsense.harvest.dom import harvest_elements
from colorsense.harvest.render import (
    DEFAULT_NAV_TIMEOUT_MS,
    RenderSession,
    RequestFilter,
    SharedBrowser,
    normalize_browser_args,
)
from colorsense.harvest.screenshot import _OversizedCaptureError, harvest_screenshot
from colorsense.harvest.states import probe_hover_states
from colorsense.harvest.tokens import harvest_tokens
from colorsense.models import Harvest, Theme, Viewport

__all__ = [
    "DEFAULT_NAV_TIMEOUT_MS",
    "RenderError",
    "RenderSession",
    "RequestFilter",
    "SharedBrowser",
    "harvest_page",
]


class RenderError(Exception):
    """A page failed to render or to harvest cleanly.

    Raised when the underlying browser engine cannot load the target URL — e.g. DNS
    resolution failure, connection refused, TLS error, navigation timeout, or any other
    Playwright navigation/render failure. The version-private Playwright exception is wrapped
    so consumers have a single, stable, documented type to catch instead of reaching into
    ``playwright._impl``.

    Also raised when the harvest itself fails on a hostile or degenerate page: a page that
    tampers with DOM APIs can make the in-page harvest payloads come back malformed
    (surfacing as ``KeyError``/``TypeError``/``ValueError``/pydantic ``ValidationError``),
    and a capture whose decoded dimensions exceed the decompression-bomb cap is rejected.
    All of these are wrapped into this one public type.

    The original error is chained via ``__cause__`` (``raise ... from err``).

    Attributes:
        url: The offending URL that failed to render or harvest.

    """

    def __init__(self, url: str, message: str | None = None) -> None:
        """Build the error from the offending URL and an optional detail message.

        Args:
            url: The URL that failed to render or harvest.
            message: An optional detail line; a generic default is used when ``None``.

        """
        detail = message or "render/navigation failed"
        super().__init__(f"{detail} for {url!r}")
        self.url = url


async def harvest_page(
    url: str,
    theme: Theme,
    config: Config,
    viewport: Viewport,
    *,
    nav_timeout_ms: float = DEFAULT_NAV_TIMEOUT_MS,
    user_agent: str | None = None,
    request_filter: RequestFilter | None = None,
    browser: SharedBrowser | None = None,
    browser_args: Sequence[str] = (),
) -> Harvest:
    """Render ``url`` under ``theme``/``viewport`` and harvest everything into a Harvest.

    Opens a single `RenderSession`, navigates, then runs token, DOM, hover-state,
    and screenshot harvesting against the one live page, and assembles the
    `Harvest` contract.

    The steps share one live page but overlap where it is safe to: token and DOM reads run
    together (both are read-only DOM queries). Hover probing runs on its own — it forces
    ``:hover`` pseudo-state per element, which would otherwise leak into the subsequent
    screenshot. Concurrency *across* themes/URLs is the caller's job (see
    [`PolitenessPolicy.fetch`][colorsense.PolitenessPolicy.fetch] and
    [`colorsense.analyze`][colorsense.analyze], which render distinct themes concurrently).

    Calling ``harvest_page`` directly bypasses [`PolitenessPolicy`][colorsense.PolitenessPolicy]
    entirely — scheme validation, the robots gate, the rate limiter, and the cache all live
    in the policy, the only place networking policy is enforced.

    Args:
        url: The URL to render and harvest.
        theme: The color scheme to render under.
        config: The classifier config; its vendor prefixes drive DOM third-party flagging.
        viewport: The viewport (size and device scale factor) to render at.
        nav_timeout_ms: The per-navigation timeout passed through to `RenderSession.goto`
            (defaults to `DEFAULT_NAV_TIMEOUT_MS`); a navigation that exceeds it surfaces
            as [`RenderError`][colorsense.RenderError]. Keyword-only with a default so
            existing `Harvester` callers/fakes remain compatible.
        user_agent: When not ``None``, forwarded to `RenderSession` and set on the browser
            context, so the page navigation identifies itself with the configured UA
            instead of the stock headless-Chromium one.
            [`PolitenessPolicy.fetch`][colorsense.PolitenessPolicy.fetch] passes its
            configured ``user_agent`` through here, making the documented "identifiable
            User-Agent" hold on the actual render, not just the ``robots.txt`` GET.
        request_filter: When not ``None``, a [`RequestFilter`][colorsense.RequestFilter] —
            sync or async — forwarded to `RenderSession`, which installs it as a
            browser-context route: every request the render makes (the navigation *and* all
            sub-resources the page's own JS/markup triggers) is aborted unless the predicate
            returns ``True`` (an async predicate is awaited; a raising predicate fails
            closed and aborts). [`PolitenessPolicy.fetch`][colorsense.PolitenessPolicy.fetch]
            passes its configured ``request_filter`` through here.
        browser: When not ``None``, a `SharedBrowser` handle resolved lazily (launching on
            first use) and handed to `RenderSession`, so this render opens a fresh browser
            context inside the shared Chromium instead of launching its own. The handle's
            owner (e.g. one [`colorsense.analyze`][colorsense.analyze] call sharing a
            browser across its theme renders) is responsible for teardown; this function
            never closes it. ``None`` (the default) keeps the previous behavior: a dedicated
            browser per render. A failure launching the shared browser surfaces as
            [`RenderError`][colorsense.RenderError] like any other render failure.
        browser_args: Extra command-line arguments for the **dedicated** Chromium launch
            (the ``browser=None`` path), appended to the library's own launch arguments and
            passed verbatim — canonically ``("--js-flags=--max-old-space-size=512",)`` to
            cap the renderer's V8 heap (JS heap only, not total renderer memory; container
            limits remain the enforceable bound, see ``SECURITY.md`` §2). Launch arguments
            only exist at launch time, so combining a non-empty ``browser_args`` with a
            shared ``browser`` handle raises `ValueError` *before* any render — pass them to
            ``SharedBrowser(browser_args=...)`` instead (that is what
            ``analyze(browser_args=...)`` does).

    Returns:
        The assembled `Harvest` for the rendered page.

    Raises:
        ValueError: If a non-empty ``browser_args`` is combined with a shared ``browser``
            handle (launch arguments only apply to a dedicated launch).
        TypeError: If ``browser_args`` is a bare string or any entry is not a string.
        RenderError: On any render or harvest failure — Playwright navigation errors,
            malformed in-page payloads from a DOM-tampering page, or an oversized screenshot
            capture — with the original error chained (see
            [`RenderError`][colorsense.RenderError]).

    """
    vendor_prefixes = config.component_classifier.third_party.vendor_prefixes

    # Validate eagerly, OUTSIDE the try below: these are caller programming errors and must
    # surface as themselves (TypeError/ValueError), never wrapped into RenderError.
    extra_args = normalize_browser_args(browser_args)
    if browser is not None and extra_args:
        raise ValueError(
            "browser_args apply at launch time and cannot be combined with a shared "
            "browser handle; construct SharedBrowser(browser_args=...) instead"
        )

    # The whole render body is wrapped so the harvest's failure modes all surface as the one
    # public ``RenderError`` rather than version-private or incidental types:
    # * ``PlaywrightError`` — navigation/render failure (DNS, timeout, TLS, connection
    #   refused, evaluation error) from the version-private ``playwright._impl`` hierarchy.
    # * ``KeyError``/``TypeError``/``ValueError``/pydantic ``ValidationError`` — a hostile
    #   page that tampers with DOM APIs can make the in-page JS payloads come back
    #   malformed, blowing up payload parsing or model construction.
    # * ``_OversizedCaptureError`` — the captured screenshot exceeds the decode pixel cap.
    # * ``TimeoutError`` — a per-operation ``asyncio.wait_for`` bound on an essential
    #   harvest evaluate expired (a page whose JS wedged the renderer main thread; see
    #   ``render.EVAL_TIMEOUT_S``). The pipeline's ``max_total_seconds`` deadline is NOT
    #   this type at this layer — it cancels the harvest (``CancelledError``, a
    #   ``BaseException`` that passes through) and is translated at the pipeline.
    # Deliberately not a broad ``except Exception``: anything outside this set is a real
    # bug and should surface as itself. The ``async with`` still exits and tears the
    # browser down on exception. ``RobotsDisallowedError`` is raised in the politeness
    # layer above this call (and is a RuntimeError, outside this tuple anyway), so it is
    # never caught here.
    try:
        # Resolving the shared-browser handle is the lazy launch point, so it sits inside
        # the try: a Chromium launch failure wraps as RenderError exactly like a launch
        # failure inside RenderSession.__aenter__ on the dedicated-browser path.
        shared = await browser.get() if browser is not None else None
        async with RenderSession(
            theme,
            viewport,
            user_agent=user_agent,
            request_filter=request_filter,
            browser=shared,
            browser_args=extra_args,
        ) as session:
            await session.goto(url, nav_timeout_ms=nav_timeout_ms)
            page = session.page

            tokens, (elements, selectors) = await asyncio.gather(
                harvest_tokens(page),
                harvest_elements(page, vendor_prefixes),
            )
            # Isolated: forces :hover per element, which would pollute a concurrent screenshot.
            elements = await probe_hover_states(page, elements, selectors)
            screenshot_bins = await harvest_screenshot(
                page,
                session.consent_boxes,
                viewport.device_scale_factor,
                session.media_boxes,
            )
    except (
        PlaywrightError,
        _OversizedCaptureError,
        KeyError,
        TimeoutError,
        TypeError,
        ValueError,
        ValidationError,
    ) as err:
        raise RenderError(url, str(err).splitlines()[0] if str(err) else None) from err

    return Harvest(
        url=url,
        theme=theme,
        viewport=viewport,
        tokens=tokens,
        elements=elements,
        screenshot_bins=screenshot_bins,
    )
