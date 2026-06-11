"""Visible-DOM element harvesting with computed colors and structural flags.

Walks the rendered DOM in-page, capturing for each element its computed
``background-color`` / ``color`` / ``border-color`` (parsed to :class:`Color`), bounding rect,
position, tag/role/id/class tokens, and structural flags (iframe, cross-origin, shadow
host, clickable, box shadow, direct text content, vendor match, visible, aria-hidden).
Hidden, zero-area, or
aria-hidden elements are excluded from the returned list (their flags are still computed
on what is returned).

The border color is reported only when the element actually paints a border
(``border-top-width`` > 0): computed ``border-top-color`` resolves to a color for *every*
element regardless of width, so an ungated read would make ``border`` non-None on
virtually everything and feed meaningless (usually black) "border colors" downstream.

``has_hover_color_change`` / ``hover_bg`` are left at their defaults here; pseudo-state
probing fills them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict, cast

from playwright.async_api import Page

from colorsense.color.primitives import parse_css_color
from colorsense.models import HarvestedElement, Rect


class _RawElement(TypedDict):
    """Shape of one element record returned from the in-page JS."""

    selector: str
    tag: str
    role: str | None
    id: str | None
    class_tokens: list[str]
    rect: dict[str, float]
    position: str
    bg: str
    text: str
    border: str
    has_box_shadow: bool
    has_text: bool
    is_iframe: bool
    cross_origin: bool
    shadow_host: bool
    clickable: bool
    visible: bool
    aria_hidden: bool
    vendor_blob: str


# JS walking visible elements and reporting computed colors + structural facts.
# Vendor matching is finished in Python against the config prefixes; JS exports the raw
# lowercased id/class/src blob used for matching.
_COLLECT_DOM_JS: str = r"""
() => {
    const out = [];
    const pageOrigin = window.location.origin;

    const cssSelector = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && parts.length < 8) {
            let part = node.tagName.toLowerCase();
            const parent = node.parentElement;
            if (parent) {
                const sameTag = Array.from(parent.children).filter(
                    (c) => c.tagName === node.tagName);
                if (sameTag.length > 1) {
                    part += ':nth-child(' + (Array.from(parent.children).indexOf(node) + 1) + ')';
                }
            }
            parts.unshift(part);
            if (node.id) { parts[0] = '#' + CSS.escape(node.id); break; }
            node = parent;
        }
        return parts.join(' > ');
    };

    for (const el of document.querySelectorAll('*')) {
        const tag = el.tagName.toLowerCase();
        if (tag === 'script' || tag === 'style' || tag === 'meta'
            || tag === 'head' || tag === 'link' || tag === 'title' || tag === 'noscript') {
            continue;
        }
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const ariaHidden = el.getAttribute('aria-hidden') === 'true';
        const zeroArea = r.width <= 0 || r.height <= 0;
        const hidden = style.display === 'none' || style.visibility === 'hidden';
        const visible = !hidden && !zeroArea;

        // Exclude hidden / zero-area / aria-hidden from the returned list.
        if (!visible || ariaHidden) continue;

        const classList = Array.from(el.classList);
        const isIframe = tag === 'iframe';
        let crossOrigin = false;
        if (isIframe) {
            const src = el.getAttribute('src') || '';
            try {
                const u = new URL(src, window.location.href);
                crossOrigin = u.origin !== pageOrigin;
            } catch (e) {
                crossOrigin = false;
            }
        }
        const shadowHost = !!el.shadowRoot;

        const role = el.getAttribute('role');
        const cursorPointer = style.cursor === 'pointer';
        const inputType = (el.getAttribute('type') || '').toLowerCase();
        const isSubmit = tag === 'input'
            && ['submit', 'button', 'reset'].includes(inputType);
        const clickable = tag === 'a' || tag === 'button' || role === 'button'
            || el.hasAttribute('onclick') || isSubmit || cursorPointer;

        // Only report a border color when a border is actually painted: computed
        // borderTopColor is a color for every element, so gate on a non-zero width and
        // send '' otherwise (parses to None in Python). Computed boxShadow is the
        // literal string 'none' when absent.
        const borderWidth = parseFloat(style.borderTopWidth) || 0;
        const borderColor = borderWidth > 0 ? style.borderTopColor : '';
        const hasBoxShadow = style.boxShadow !== 'none';

        // True iff the element has at least one DIRECT child text node with
        // non-whitespace content. Descendant text deliberately does not count:
        // otherwise every ancestor wrapper of any text would carry the flag.
        let hasText = false;
        for (const child of el.childNodes) {
            if (child.nodeType === 3 && child.nodeValue.trim() !== '') {
                hasText = true;
                break;
            }
        }

        const vendorBlob = (
            (el.id || '') + ' ' + classList.join(' ') + ' ' + (el.getAttribute('src') || '')
        ).toLowerCase();

        out.push({
            selector: cssSelector(el),
            tag: tag,
            role: role,
            id: el.id || null,
            class_tokens: classList,
            rect: {x: r.x, y: r.y, w: r.width, h: r.height},
            position: style.position,
            bg: style.backgroundColor,
            text: style.color,
            border: borderColor,
            has_box_shadow: hasBoxShadow,
            has_text: hasText,
            is_iframe: isIframe,
            cross_origin: crossOrigin,
            shadow_host: shadowHost,
            clickable: clickable,
            visible: visible,
            aria_hidden: ariaHidden,
            vendor_blob: vendorBlob,
        });
    }
    return out;
}
"""


def _vendor_match(blob: str, vendor_prefixes: Sequence[str]) -> bool:
    """Return ``True`` if the lowercased id/class/src blob contains any vendor prefix."""
    return any(prefix.lower() in blob for prefix in vendor_prefixes)


async def harvest_elements(
    page: Page,
    vendor_prefixes: Sequence[str],
) -> tuple[list[HarvestedElement], list[str]]:
    """Harvest visible DOM elements with computed colors and structural flags.

    Returns the elements alongside a parallel list of CSS selectors (one per element) so
    pseudo-state probing can re-target the same elements.
    """
    raw_elements = cast(list[_RawElement], await page.evaluate(_COLLECT_DOM_JS))

    elements: list[HarvestedElement] = []
    selectors: list[str] = []
    for raw in raw_elements:
        rect = raw["rect"]
        elements.append(
            HarvestedElement(
                tag=raw["tag"],
                role=raw["role"],
                id=raw["id"],
                class_tokens=raw["class_tokens"],
                rect=Rect(x=rect["x"], y=rect["y"], width=rect["w"], height=rect["h"]),
                position=raw["position"],
                bg=parse_css_color(raw["bg"]),
                text=parse_css_color(raw["text"]),
                border=parse_css_color(raw["border"]),
                has_box_shadow=raw["has_box_shadow"],
                has_text=raw["has_text"],
                is_iframe=raw["is_iframe"],
                cross_origin=raw["cross_origin"],
                shadow_host=raw["shadow_host"],
                clickable=raw["clickable"],
                has_hover_color_change=False,
                hover_bg=None,
                vendor_match=_vendor_match(raw["vendor_blob"], vendor_prefixes),
                visible=raw["visible"],
                aria_hidden=raw["aria_hidden"],
            )
        )
        selectors.append(raw["selector"])
    return elements, selectors
