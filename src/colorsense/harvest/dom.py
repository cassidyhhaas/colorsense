"""Visible-DOM element harvesting with computed colors and structural flags.

Walks the rendered DOM in-page, capturing for each element its computed ``background-color`` /
``color`` / ``border-color`` (parsed to [`Color`][colorsense.Color]), bounding box, position,
tag/role/id/class tokens, the input ``type`` attribute (``<input>`` only), the opaque stops of
a gradient that fills it (only for clickable pill CTAs), its composited *effective*
background (the first fully-opaque ``background-color`` up the ancestor chain, plus whether
that ancestor is itself clickable), and structural flags
(iframe, cross-origin, shadow host, clickable, box shadow, direct text content, vendor match,
visible, aria-hidden). Hidden, zero-area, or aria-hidden elements are excluded from the returned
list (their flags are still computed on what is returned).

The border color is reported only when the element actually paints a border
(``border-top-width`` > 0): computed ``border-top-color`` resolves to a color for *every*
element regardless of width, so an ungated read would make ``border`` non-None on
virtually everything and feed meaningless (usually black) "border colors" downstream.

``has_hover_color_change`` / ``hover_bg`` are left at their defaults here; pseudo-state
probing fills them.

The element payload is capped at `_MAX_HARVEST_ELEMENTS` records (largest rendered area
wins, document order preserved): every record crosses from the renderer into the caller's
Python process, so the cap bounds host-process memory against a hostile page the same way
the screenshot capture/decode caps do (see ``harvest/screenshot.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TypedDict, cast

from playwright.async_api import Page

from colorsense._util import dedupe_by
from colorsense.color.primitives import is_painting, parse_css_color
from colorsense.harvest.render import EVAL_TIMEOUT_S
from colorsense.models import BoundingBox, Color, HarvestedElement, is_pill_shape

# Bound on the per-render element payload. Each record below materializes as a pydantic
# model in the *host* Python process — container limits bound the renderer, not the
# consumer — so without a cap a hostile page that synthesizes millions of visible elements
# (a few lines of JS) forces a multi-GB allocation here. Far above any genuine page (real
# pages run a few thousand visible elements); over budget, the largest-area elements are
# kept, since area dominates every downstream signal (screenshot fusion, component votes).
_MAX_HARVEST_ELEMENTS: int = 10_000

# Bound on the opaque gradient stops kept per element. A real fill gradient has a
# handful of stops; the cap stops a hostile many-stop gradient on thousands of elements
# from amplifying the O(n^2) inventory clustering. Far above any genuine gradient.
_MAX_GRADIENT_STOPS: int = 8


def _is_interactive_pill(
    *, clickable: bool, min_corner_radius: float, width: float, height: float
) -> bool:
    """Whether the element is a clickable pill (a CTA), the sole palette-bearing gradient fill.

    Only an interactive pill's gradient tracks the brand palette: a site's CTA/pill colors
    are consistently on-palette, but gradients on *card* backgrounds are decorative flavor
    that varies page to page (verified across stripe.com pages). ``clickable`` alone is
    insufficient — decorative gradient cards are often wrapped in links — and pill shape
    alone is insufficient — non-clickable rounded dividers are pills. Requiring both keeps
    rounded-full CTAs while rejecting rectangular cards (not pills) and decorative pill
    dividers (not clickable). The pill test is the shared `models.is_pill_shape` (all four
    corners fully rounded, wider than tall), which `classify.components._is_pill` also uses,
    so the two share one definition rather than being hand-synced.
    """
    return clickable and is_pill_shape(width, height, min_corner_radius)


def _gradient_fill_stops(bg: Color | None, raw_colors: Sequence[str]) -> tuple[Color, ...]:
    """Color stops of a gradient that *fills* the element, or ``()``.

    Applied only to elements that pass `_is_interactive_pill` (a CTA). Returns stops only
    when the gradient is the element's actual background fill: the computed
    ``background-color`` must paint nothing (``alpha == 0``, so a solid background takes
    precedence and the gradient is ignored), and the gradient must have **no
    fully-transparent stop** — decorative fades, glow halos, and dot-grid textures always
    fade to ``rgba(0, 0, 0, 0)``, so a transparent stop marks the gradient as ornamental
    rather than a fill. A merely *partly*-transparent stop is kept; the inventory later
    scales its mass by its alpha. Stops are deduped by opaque hex (a repeated brand color
    counts once) and capped at `_MAX_GRADIENT_STOPS`.
    """
    if is_painting(bg):
        return ()
    stops = [c for c in (parse_css_color(raw) for raw in raw_colors) if c is not None]
    if not stops or any(stop.alpha == 0.0 for stop in stops):
        return ()
    # Dedup by opaque hex, keeping the first (gradient source-order) occurrence, capped at
    # `_MAX_GRADIENT_STOPS` unique stops.
    deduped = dedupe_by(stops, key=lambda s: s.hex, limit=_MAX_GRADIENT_STOPS)
    return tuple(deduped)


class _RawElement(TypedDict):
    """Shape of one element record returned from the in-page JS."""

    selector: str
    tag: str
    role: str | None
    id: str | None
    class_tokens: list[str]
    bounding_box: dict[str, float]
    position: str
    bg: str
    text: str
    border: str
    input_type: str | None
    min_corner_radius: float
    bg_image_colors: list[str]
    effective_bg: str
    effective_bg_from_clickable: bool
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
(maxElements) => {
    const out = [];
    const pageOrigin = window.location.origin;

    // Ids are only usable as selectors when UNIQUE: duplicate ids are invalid HTML but
    // common in the wild, and the hover prober resolves selectors via DOM.querySelector
    // (first document-order match) — a duplicate-id selector would silently probe a
    // different element than the one harvested here.
    const idCounts = new Map();
    for (const el of document.querySelectorAll('[id]')) {
        idCounts.set(el.id, (idCounts.get(el.id) || 0) + 1);
    }
    const uniqueId = (node) => node.id !== '' && idCounts.get(node.id) === 1;

    // A selector that matches EXACTLY the element it was built for: either a unique id,
    // or a child-combinator chain anchored at a unique-id ancestor or the document root,
    // with :nth-child disambiguation wherever same-tag siblings exist. Returns '' (the
    // prober skips the element) on pathological nesting rather than an ambiguous chain.
    const cssSelector = (el) => {
        if (uniqueId(el)) return '#' + CSS.escape(el.id);
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1) {
            if (parts.length >= 32) return '';
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
            if (uniqueId(node)) { parts[0] = '#' + CSS.escape(node.id); break; }
            node = parent;
        }
        return parts.join(' > ');
    };

    // Whether a node renders as interactive (matches the per-element `clickable` rule
    // below). Used both for the element's own flag and to tag the ancestor that paints
    // an element's effective background.
    const isClickable = (node, s) => {
        const t = node.tagName.toLowerCase();
        const r = node.getAttribute('role');
        const it = (node.getAttribute('type') || '').toLowerCase();
        const submit = t === 'input' && ['submit', 'button', 'reset', 'image'].includes(it);
        return t === 'a' || t === 'button' || r === 'button'
            || node.hasAttribute('onclick') || submit || s.cursor === 'pointer';
    };

    // The element's COMPOSITED background: the first fully-opaque background-color found
    // walking the element itself and then its ancestors to the document root, plus whether
    // the node that contributed it is itself clickable/button-styled. An inline element's
    // own background-color is almost always transparent (alpha 0), so this recovers the
    // surface its text is actually painted on — distinguishing a genuine inline link (text
    // on a passive page/section surface) from a CTA-button label (text on the button's own
    // interactive fill). Background-images/gradients are ignored: solid fills are the case
    // this signal needs, and a gradient fill is already captured via bg_image_colors. ''
    // (-> None in Python) means no opaque background exists up the chain.
    const effectiveBg = (start) => {
        let node = start;
        while (node && node.nodeType === 1) {
            const s = getComputedStyle(node);
            const m = s.backgroundColor.match(/rgba?\(([^)]+)\)/);
            if (m) {
                const parts = m[1].split(',').map((x) => parseFloat(x));
                const alpha = parts.length > 3 ? parts[3] : 1;
                if (alpha >= 0.999) {
                    return {color: s.backgroundColor, fromClickable: isClickable(node, s)};
                }
            }
            node = node.parentElement;
        }
        return {color: '', fromClickable: false};
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
            && ['submit', 'button', 'reset', 'image'].includes(inputType);
        const clickable = tag === 'a' || tag === 'button' || role === 'button'
            || el.hasAttribute('onclick') || isSubmit || cursorPointer;

        // Only report a border color when a border is actually painted: computed
        // borderTopColor is a color for every element, so gate on a non-zero width and
        // send '' otherwise (parses to None in Python). Computed boxShadow is the
        // literal string 'none' when absent.
        const borderWidth = parseFloat(style.borderTopWidth) || 0;
        const borderColor = borderWidth > 0 ? style.borderTopColor : '';
        const hasBoxShadow = style.boxShadow !== 'none';

        // Color stops of a gradient background-image, in source order. Only gradient
        // layers carry rgb()/rgba() tokens we care about (a url() image has none); the
        // `gradient` guard skips non-gradient images cheaply. Python applies the fill
        // gate (background-color transparent + no fully-transparent stop) and the cap —
        // here we just export the raw computed color tokens (Chromium normalizes every
        // stop color to rgb()/rgba() form). Capped at 32 to bound the payload against a
        // pathological many-stop gradient.
        const bgImage = style.backgroundImage;
        let bgImageColors = [];
        if (bgImage && bgImage.includes('gradient')) {
            const matched = bgImage.match(/rgba?\([^)]*\)/g);
            if (matched) bgImageColors = matched.slice(0, 32);
        }

        // Smallest of the four computed corner radii in px. A true pill/stadium has
        // ALL FOUR corners fully rounded; the MIN is the radius guaranteed on every
        // corner, so `min >= height/2` means "fully rounded all around" — MAX would
        // false-match a single `rounded-tl-full` corner (a tab/speech-bubble) as a pill.
        // The `%` branch of resolveRadius is necessary because Chromium returns the
        // literal "%" string for a percentage radius (e.g. `"50%"`) from
        // getComputedStyle (empirically confirmed); we resolve it against `r.width`. A
        // single scalar can't carry CSS's per-axis horizontal/vertical resolution, but
        // the `width > height` gate downstream makes width-resolution correct for the
        // wide-short pill targets.
        const resolveRadius = (value, basis) => {
            const n = parseFloat(value) || 0;
            return value.endsWith('%') ? (n / 100) * basis : n;
        };
        const minCornerRadius = Math.min(
            resolveRadius(style.borderTopLeftRadius, r.width),
            resolveRadius(style.borderTopRightRadius, r.width),
            resolveRadius(style.borderBottomRightRadius, r.width),
            resolveRadius(style.borderBottomLeftRadius, r.width),
        );

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

        const effBg = effectiveBg(el);

        out.push({
            selector: cssSelector(el),
            tag: tag,
            role: role,
            id: el.id || null,
            class_tokens: classList,
            bounding_box: {x: r.x, y: r.y, w: r.width, h: r.height},
            position: style.position,
            bg: style.backgroundColor,
            text: style.color,
            border: borderColor,
            // Only meaningful on <input>: null for other tags and for inputs with
            // no/empty type attribute (the HTML default type is "text").
            input_type: (tag === 'input' && inputType !== '') ? inputType : null,
            min_corner_radius: minCornerRadius,
            bg_image_colors: bgImageColors,
            effective_bg: effBg.color,
            effective_bg_from_clickable: effBg.fromClickable,
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

    // Host-process memory bound (see _MAX_HARVEST_ELEMENTS in Python): keep the
    // largest-area records, preserving document order among the survivors.
    if (out.length > maxElements) {
        const ranked = out.map((rec, i) => [rec.bounding_box.w * rec.bounding_box.h, i]);
        ranked.sort((a, b) => (b[0] - a[0]) || (a[1] - b[1]));
        const keep = new Set(ranked.slice(0, maxElements).map((pair) => pair[1]));
        return out.filter((rec, i) => keep.has(i));
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
    pseudo-state probing can re-target the same elements. A selector is either uniquely
    resolvable to its element or the empty string (probing skips it); the payload is
    capped at `_MAX_HARVEST_ELEMENTS` records (largest area wins).
    """
    # ``wait_for`` bounds the evaluate (Playwright gives it no timeout of its own); a
    # wedged renderer surfaces as TimeoutError -> RenderError instead of a hung harvest.
    raw_elements = cast(
        list[_RawElement],
        await asyncio.wait_for(
            page.evaluate(_COLLECT_DOM_JS, _MAX_HARVEST_ELEMENTS), EVAL_TIMEOUT_S
        ),
    )

    elements: list[HarvestedElement] = []
    selectors: list[str] = []
    for raw in raw_elements:
        box = raw["bounding_box"]
        bg = parse_css_color(raw["bg"])
        gradient_stops: tuple[Color, ...] = ()
        if _is_interactive_pill(
            clickable=raw["clickable"],
            min_corner_radius=raw["min_corner_radius"],
            width=box["w"],
            height=box["h"],
        ):
            gradient_stops = _gradient_fill_stops(bg, raw["bg_image_colors"])
        elements.append(
            HarvestedElement(
                tag=raw["tag"],
                role=raw["role"],
                id=raw["id"],
                class_tokens=raw["class_tokens"],
                bounding_box=BoundingBox(x=box["x"], y=box["y"], width=box["w"], height=box["h"]),
                position=raw["position"],
                bg=bg,
                text=parse_css_color(raw["text"]),
                border=parse_css_color(raw["border"]),
                input_type=raw["input_type"],
                min_corner_radius=raw["min_corner_radius"],
                bg_gradient_stops=gradient_stops,
                effective_bg=parse_css_color(raw["effective_bg"]),
                effective_bg_from_clickable=raw["effective_bg_from_clickable"],
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
