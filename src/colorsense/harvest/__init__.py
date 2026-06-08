"""Page rendering and design-token / color harvesting.

Public interface
----------------
* :func:`harvest_page` — async: render a URL under a theme and produce a frozen
  :class:`~colorsense.models.Harvest`.
* :class:`RenderSession` — the Playwright async context manager used internally (exported
  for advanced/manual use).
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Error as PlaywrightError

from colorsense.config import Config
from colorsense.harvest.dom import harvest_elements
from colorsense.harvest.render import DEFAULT_NAV_TIMEOUT_MS, RenderSession
from colorsense.harvest.screenshot import harvest_logo_colors, harvest_screenshot
from colorsense.harvest.states import probe_hover_states
from colorsense.harvest.tokens import harvest_tokens
from colorsense.models import Harvest, Theme, Viewport

__all__ = ["DEFAULT_NAV_TIMEOUT_MS", "RenderError", "RenderSession", "harvest_page"]


class RenderError(Exception):
    """A page failed to render or navigate.

    Raised when the underlying browser engine cannot load the target URL — e.g. DNS
    resolution failure, connection refused, TLS error, navigation timeout, or any other
    Playwright navigation/render failure. The version-private Playwright exception is wrapped
    so consumers have a single, stable, documented type to catch instead of reaching into
    ``playwright._impl``.

    The offending URL is available as :attr:`url`; the original Playwright error is chained
    via ``__cause__`` (``raise ... from err``).
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
) -> Harvest:
    """Render ``url`` under ``theme``/``viewport`` and harvest everything into a Harvest.

    Opens a single :class:`RenderSession`, navigates, then runs token, DOM, hover-state,
    screenshot, and logo harvesting against the one live page, and assembles the frozen
    :class:`~colorsense.models.Harvest` contract.

    ``nav_timeout_ms`` is the per-navigation timeout passed through to
    :meth:`RenderSession.goto` (defaults to :data:`DEFAULT_NAV_TIMEOUT_MS`); a navigation
    that exceeds it surfaces as :class:`RenderError`. It is keyword-only with a default so
    existing :data:`~colorsense.net.politeness.Harvester` callers/fakes remain compatible.

    The steps share one live page but overlap where it is safe to: token and DOM reads run
    together (both are read-only DOM queries), and the screenshot and logo fetch run
    together (the logo is a network fetch independent of rendering). Hover probing runs on
    its own — it forces ``:hover`` pseudo-state per element, which would otherwise leak into
    a concurrent screenshot. Concurrency *across* themes/URLs is the caller's job (see
    :meth:`PolitenessPolicy.fetch` and :func:`colorsense.analyze`, which render distinct
    themes concurrently).
    """
    vendor_prefixes = config.component_classifier.third_party.vendor_prefixes

    # The whole render body is wrapped so any Playwright navigation/render failure (DNS,
    # timeout, TLS, connection refused, evaluation error) surfaces as a public ``RenderError``
    # rather than the version-private ``playwright._impl`` type. The ``async with`` still
    # exits and tears the browser down on exception. ``RobotsDisallowedError`` is raised in
    # the politeness layer above this call, so it is never caught here.
    try:
        async with RenderSession(theme, viewport) as session:
            await session.goto(url, nav_timeout_ms=nav_timeout_ms)
            page = session.page

            tokens, (elements, selectors) = await asyncio.gather(
                harvest_tokens(page),
                harvest_elements(page, vendor_prefixes),
            )
            # Isolated: forces :hover per element, which would pollute a concurrent screenshot.
            elements = await probe_hover_states(page, elements, selectors)
            screenshot_bins, logo_colors = await asyncio.gather(
                harvest_screenshot(page, session.consent_rects, viewport.device_scale_factor),
                harvest_logo_colors(page),
            )
    except PlaywrightError as err:
        raise RenderError(url, str(err).splitlines()[0] if str(err) else None) from err

    return Harvest(
        url=url,
        theme=theme,
        viewport=viewport,
        tokens=tokens,
        elements=elements,
        screenshot_bins=screenshot_bins,
        logo_colors=logo_colors,
    )
