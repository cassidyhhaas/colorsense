"""Pseudo-state (hover/focus) color-change probing.

For each clickable candidate element we hover (and focus) it via Playwright, re-read the
computed ``background-color``, and if it changed from the resting value we set
``has_hover_color_change=True`` and ``hover_bg`` to the parsed hover color.

Per-element interaction is wrapped in try/except so a single uncooperative element cannot
abort the whole harvest.
"""

from __future__ import annotations

import contextlib

from playwright.async_api import Page

from colorsense.color.primitives import parse_css_color
from colorsense.models import Color, HarvestedElement

# Max elements to probe so a huge page can't blow up render time.
_MAX_PROBE: int = 80


async def probe_hover_states(
    page: Page,
    elements: list[HarvestedElement],
    selectors: list[str],
) -> list[HarvestedElement]:
    """Probe hover/focus color changes for clickable elements.

    ``elements`` and ``selectors`` are positionally aligned (as returned by
    :func:`colorsense.harvest.dom.harvest_elements`). Returns a new list with
    ``has_hover_color_change`` / ``hover_bg`` updated on clickable elements that change
    background color on hover; other elements are returned unchanged.
    """
    updated: list[HarvestedElement] = list(elements)
    probed = 0

    for index, element in enumerate(elements):
        if not element.clickable:
            continue
        if probed >= _MAX_PROBE:
            break
        selector = selectors[index] if index < len(selectors) else None
        if not selector:
            continue
        probed += 1

        hover_bg = await _read_hover_bg(page, selector)
        if hover_bg is None:
            continue

        resting = element.bg
        # A change is "real" when there is no resting bg, or the hex/alpha differ.
        if resting is None or hover_bg.hex != resting.hex or hover_bg.alpha != resting.alpha:
            updated[index] = element.model_copy(
                update={"has_hover_color_change": True, "hover_bg": hover_bg}
            )

    # Reset interaction state (move mouse away) — best effort.
    with contextlib.suppress(Exception):
        await page.mouse.move(0, 0)

    return updated


async def _read_hover_bg(page: Page, selector: str) -> Color | None:
    """Hover/focus the element at ``selector`` and return its parsed hover bg color.

    Returns ``None`` on any failure (element gone, detached, not hoverable) or if the
    computed background-color does not parse.
    """
    try:
        locator = page.locator(selector).first
        await locator.hover(timeout=1000)
        with contextlib.suppress(Exception):
            await locator.focus(timeout=500)
        raw = await locator.evaluate("(el) => window.getComputedStyle(el).backgroundColor")
    except Exception:  # one bad element must not abort the harvest
        return None
    if not isinstance(raw, str):
        return None
    return parse_css_color(raw)
