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

from colorsense.config import Config
from colorsense.harvest.dom import harvest_elements
from colorsense.harvest.render import RenderSession
from colorsense.harvest.screenshot import harvest_logo_colors, harvest_screenshot
from colorsense.harvest.states import probe_hover_states
from colorsense.harvest.tokens import harvest_tokens
from colorsense.models import Harvest, Theme, Viewport

__all__ = ["RenderSession", "harvest_page"]


async def harvest_page(
    url: str,
    theme: Theme,
    config: Config,
    viewport: Viewport,
) -> Harvest:
    """Render ``url`` under ``theme``/``viewport`` and harvest everything into a Harvest.

    Opens a single :class:`RenderSession`, navigates, then runs token, DOM, hover-state,
    screenshot, and logo harvesting against the one live page, and assembles the frozen
    :class:`~colorsense.models.Harvest` contract.

    The steps share one live page but overlap where it is safe to: token and DOM reads run
    together (both are read-only DOM queries), and the screenshot and logo fetch run
    together (the logo is a network fetch independent of rendering). Hover probing runs on
    its own — it forces ``:hover`` pseudo-state per element, which would otherwise leak into
    a concurrent screenshot. Concurrency *across* themes/URLs is the caller's job (see
    :meth:`PolitenessPolicy.fetch` and :func:`colorsense.analyze`, which render distinct
    themes concurrently).
    """
    vendor_prefixes = config.component_classifier.third_party.vendor_prefixes

    async with RenderSession(theme, viewport) as session:
        await session.goto(url)
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

    return Harvest(
        url=url,
        theme=theme,
        viewport=viewport,
        tokens=tokens,
        elements=elements,
        screenshot_bins=screenshot_bins,
        logo_colors=logo_colors,
    )
