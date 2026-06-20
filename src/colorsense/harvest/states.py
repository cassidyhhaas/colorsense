"""Pseudo-state (hover/focus) color-change probing.

For each clickable candidate element we force its ``:hover``/``:focus`` pseudo-classes via
the Chrome DevTools Protocol (``CSS.forcePseudoState``), re-read the computed
``background-color``, and if it changed from the resting value we set
``has_hover_color_change=True`` and ``hover_bg`` to the parsed hover color.

Why CDP instead of a real mouse hover? Driving Playwright's `hover` per element is
catastrophically slow on real pages: each call runs actionability checks (scroll-into-view,
stability, pointer-event eligibility), and hovering one element can open menus/overlays that
then intercept pointer events on the *next* element, so its checks retry until the hover
timeout fires. Forcing the pseudo-state over CDP skips all of that — no mouse movement, no
page perturbation — and reads computed style, so it also works when the stylesheet is
cross-origin (CDN-hosted), which defeats a pure CSSOM ``:hover``-rule scan. On real sites
this is ~5-75x faster with identical output for CSS-driven hover (the common case for
buttons/links); the only effect it cannot see is purely JS-driven hover (e.g. a
class toggled on ``mouseenter``).

Per-element interaction is wrapped in try/except so a single uncooperative element cannot
abort the whole harvest, the whole pass degrades to a no-op if a CDP session cannot be
established, and the pass as a whole is bounded by `_PROBE_PASS_TIMEOUT_S` — CDP sends
have no Playwright timeout, so a page whose JS wedges the renderer main thread after load
would otherwise hang the harvest indefinitely (per-element isolation cannot bound a send
that never returns).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from playwright.async_api import CDPSession, Page

from colorsense.color.primitives import parse_css_color
from colorsense.models import Color, HarvestedElement

# Max elements to probe so a huge page can't blow up render time.
_MAX_PROBE: int = 80

# Wall-clock bound (seconds) on the whole hover-probe pass (CDP setup + every send). On a
# wedged renderer the pass is abandoned and the elements keep their (already correct)
# resting colors — hover data is a heuristic enrichment, never worth hanging the harvest.
_PROBE_PASS_TIMEOUT_S: float = 30.0

# Bound on the final CDP detach during cleanup (it, too, is an unbounded send).
_DETACH_TIMEOUT_S: float = 5.0

# Pseudo-classes forced while reading the hover background. ``focus``/``focus-visible`` are
# included to match the prior mouse path, which both hovered and focused each element.
_FORCED_PSEUDO: list[str] = ["hover", "focus", "focus-visible"]


async def probe_hover_states(
    page: Page,
    elements: list[HarvestedElement],
    selectors: list[str],
) -> list[HarvestedElement]:
    """Probe hover/focus color changes for clickable elements.

    ``elements`` and ``selectors`` are positionally aligned (as returned by
    `colorsense.harvest.dom.harvest_elements`).

    Args:
        page: The live Playwright page the elements were harvested from.
        elements: The harvested elements, in document order.
        selectors: CSS selectors positionally aligned with ``elements`` (an empty
            string marks an element the prober must skip).

    Returns:
        A new list with ``has_hover_color_change`` / ``hover_bg`` updated on clickable
        elements that change background color under forced ``:hover``/``:focus``; other
        elements are returned unchanged.

    """
    updated: list[HarvestedElement] = list(elements)

    client: CDPSession | None = None
    try:
        async with asyncio.timeout(_PROBE_PASS_TIMEOUT_S):
            client = await _open_cdp(page)
            if client is None:  # CDP unavailable — degrade to a no-op, don't abort.
                return updated

            root = await _document_root(client)
            if root is None:
                return updated

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

                hover_bg = await _read_hover_bg(client, root, selector)
                if hover_bg is None:
                    continue

                resting = element.bg
                # A change is "real" when there is no resting bg, or the hex/alpha differ.
                if (
                    resting is None
                    or hover_bg.hex != resting.hex
                    or hover_bg.alpha != resting.alpha
                ):
                    updated[index] = element.model_copy(
                        update={"has_hover_color_change": True, "hover_bg": hover_bg}
                    )
    except TimeoutError:
        # Wedged renderer: keep whatever was probed before the deadline — resting colors
        # are already correct, and hover data is best-effort by design.
        pass
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.detach(), _DETACH_TIMEOUT_S)

    return updated


async def _open_cdp(page: Page) -> CDPSession | None:
    """Open a CDP session with the DOM and CSS domains enabled.

    Args:
        page: The live Playwright page to attach the CDP session to.

    Returns:
        The enabled `CDPSession`, or ``None`` if CDP is unavailable (e.g. a non-Chromium
        engine) so the hover pass can degrade to a no-op.

    """
    try:
        client = await page.context.new_cdp_session(page)
        await client.send("DOM.enable")
        await client.send("CSS.enable")
    except Exception:  # CDP only exists on Chromium; never block the harvest on it.
        return None
    return client


async def _document_root(client: CDPSession) -> int | None:
    """Return the document root ``nodeId`` for ``DOM.querySelector`` calls.

    Only the root node is needed — ``DOM.querySelector`` resolves against the backend —
    so the default depth (1) is requested; ``depth: -1`` would serialize the entire DOM
    tree over the CDP transport just to discard it.

    Args:
        client: An open CDP session with the DOM domain enabled.

    Returns:
        The document root ``nodeId``, or ``None`` if the ``DOM.getDocument`` send fails.

    """
    try:
        doc = cast(dict[str, Any], await client.send("DOM.getDocument"))
        return int(doc["root"]["nodeId"])
    except Exception:
        return None


async def _read_hover_bg(client: CDPSession, root: int, selector: str) -> Color | None:
    """Force hover/focus on ``selector`` and return its parsed forced background color.

    The forced pseudo-state is always cleared afterward so one element's hover styling
    cannot leak into the next element's read.

    Args:
        client: An open CDP session with the DOM and CSS domains enabled.
        root: The document root ``nodeId`` to resolve ``selector`` against.
        selector: The CSS selector identifying the element to force and read.

    Returns:
        The parsed forced ``background-color``, or ``None`` on any failure (element gone,
        not resolvable, or a computed value that does not parse).

    """
    node_id: int | None = None
    try:
        found = cast(
            dict[str, Any],
            await client.send("DOM.querySelector", {"nodeId": root, "selector": selector}),
        )
        node_id = int(found.get("nodeId") or 0) or None
        if node_id is None:
            return None

        await client.send(
            "CSS.forcePseudoState",
            {"nodeId": node_id, "forcedPseudoClasses": _FORCED_PSEUDO},
        )
        computed = cast(
            dict[str, Any],
            await client.send("CSS.getComputedStyleForNode", {"nodeId": node_id}),
        )
    except Exception:  # one bad element must not abort the harvest
        await _clear_pseudo(client, node_id)
        return None

    await _clear_pseudo(client, node_id)

    raw = next(
        (
            prop["value"]
            for prop in computed.get("computedStyle", [])
            if prop["name"] == "background-color"
        ),
        None,
    )
    if not isinstance(raw, str):
        return None
    return parse_css_color(raw)


async def _clear_pseudo(client: CDPSession, node_id: int | None) -> None:
    """Best-effort reset of any forced pseudo-state on ``node_id``.

    Args:
        client: An open CDP session with the CSS domain enabled.
        node_id: The node whose forced pseudo-classes to clear; a no-op when ``None``.

    """
    if node_id is None:
        return
    with contextlib.suppress(Exception):
        await client.send("CSS.forcePseudoState", {"nodeId": node_id, "forcedPseudoClasses": []})
