"""Page rendering and design-token / color harvesting.

Public interface
----------------
* :func:`harvest_page` — async: render a URL under a theme and produce a
  :class:`~colorsense.models.Harvest`.
* :class:`RenderSession` — the Playwright async context manager used internally (exported
  for advanced/manual use).
* :class:`SharedBrowser` — lazy handle sharing one Chromium launch across several renders
  (each render still gets its own browser context); :func:`colorsense.analyze` uses one
  per call so sibling theme renders don't each pay a browser launch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from colorsense.config import Config
from colorsense.harvest.dom import harvest_elements
from colorsense.harvest.render import DEFAULT_NAV_TIMEOUT_MS, RenderSession, SharedBrowser
from colorsense.harvest.screenshot import _OversizedCaptureError, harvest_screenshot
from colorsense.harvest.states import probe_hover_states
from colorsense.harvest.tokens import harvest_tokens
from colorsense.models import Harvest, Theme, Viewport

__all__ = [
    "DEFAULT_NAV_TIMEOUT_MS",
    "RenderError",
    "RenderSession",
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

    The offending URL is available as :attr:`url`; the original error is chained via
    ``__cause__`` (``raise ... from err``).
    """

    def __init__(self, url: str, message: str | None = None) -> None:
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
    request_filter: Callable[[str], bool] | None = None,
    browser: SharedBrowser | None = None,
) -> Harvest:
    """Render ``url`` under ``theme``/``viewport`` and harvest everything into a Harvest.

    Opens a single :class:`RenderSession`, navigates, then runs token, DOM, hover-state,
    and screenshot harvesting against the one live page, and assembles the
    :class:`~colorsense.models.Harvest` contract.

    ``nav_timeout_ms`` is the per-navigation timeout passed through to
    :meth:`RenderSession.goto` (defaults to :data:`DEFAULT_NAV_TIMEOUT_MS`); a navigation
    that exceeds it surfaces as :class:`RenderError`. It is keyword-only with a default so
    existing :class:`~colorsense.net.politeness.Harvester` callers/fakes remain compatible.

    Any render or harvest failure — Playwright navigation errors, malformed in-page
    payloads from a DOM-tampering page, or an oversized screenshot capture — surfaces as
    :class:`RenderError` with the original error chained (see :class:`RenderError`).

    ``user_agent``, when not ``None``, is forwarded to :class:`RenderSession` and set on the
    browser context, so the page navigation identifies itself with the configured UA instead
    of the stock headless-Chromium one. :meth:`PolitenessPolicy.fetch` passes its configured
    ``user_agent`` through here, making the documented "identifiable User-Agent" hold on the
    actual render, not just the ``robots.txt`` GET.

    ``request_filter``, when not ``None``, is forwarded to :class:`RenderSession`, which
    installs it as a browser-context route: every request the render makes (the navigation
    *and* all sub-resources the page's own JS/markup triggers) is aborted unless the
    predicate returns ``True`` (a raising predicate fails closed and aborts).
    :meth:`PolitenessPolicy.fetch` passes its configured ``request_filter`` through here.

    ``browser``, when not ``None``, is a :class:`SharedBrowser` handle resolved lazily
    (launching on first use) and handed to :class:`RenderSession`, so this render opens a
    fresh browser context inside the shared Chromium instead of launching its own. The
    handle's owner (e.g. one :func:`colorsense.analyze` call sharing a browser across its
    theme renders) is responsible for teardown; this function never closes it. ``None``
    (the default) keeps the previous behavior: a dedicated browser per render. A failure
    launching the shared browser surfaces as :class:`RenderError` like any other render
    failure.

    Calling ``harvest_page`` directly bypasses :class:`PolitenessPolicy` entirely — scheme
    validation, the robots gate, the rate limiter, and the cache all live in the policy,
    the only place networking policy is enforced.

    The steps share one live page but overlap where it is safe to: token and DOM reads run
    together (both are read-only DOM queries). Hover probing runs on its own — it forces
    ``:hover`` pseudo-state per element, which would otherwise leak into the subsequent
    screenshot. Concurrency *across* themes/URLs is the caller's job (see
    :meth:`PolitenessPolicy.fetch` and :func:`colorsense.analyze`, which render distinct
    themes concurrently).
    """
    vendor_prefixes = config.component_classifier.third_party.vendor_prefixes

    # The whole render body is wrapped so the harvest's failure modes all surface as the one
    # public ``RenderError`` rather than version-private or incidental types:
    # * ``PlaywrightError`` — navigation/render failure (DNS, timeout, TLS, connection
    #   refused, evaluation error) from the version-private ``playwright._impl`` hierarchy.
    # * ``KeyError``/``TypeError``/``ValueError``/pydantic ``ValidationError`` — a hostile
    #   page that tampers with DOM APIs can make the in-page JS payloads come back
    #   malformed, blowing up payload parsing or model construction.
    # * ``_OversizedCaptureError`` — the captured screenshot exceeds the decode pixel cap.
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
            theme, viewport, user_agent=user_agent, request_filter=request_filter, browser=shared
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
                page, session.consent_rects, viewport.device_scale_factor
            )
    except (
        PlaywrightError,
        _OversizedCaptureError,
        KeyError,
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
