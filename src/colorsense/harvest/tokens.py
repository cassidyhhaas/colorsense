"""CSSOM design-token harvesting.

Enumerates declared CSS custom properties (``--*``) across the document's *same-origin*
stylesheets — ``document.styleSheets`` plus constructed sheets adopted via
``document.adoptedStyleSheets`` (the standard token-shipping mechanism for
web-component-heavy design systems) — recursing into ``@media`` rules and capturing the
media text. Cross-origin sheets that throw on ``.cssRules`` are skipped, and sheets
adopted inside *shadow roots* are not visited (the DOM walk likewise flags shadow hosts
without descending into them). For each declaration we capture the raw value and its
scope selector, and separately read the value resolved against ``:root`` so it can be
parsed into a [`Color`][colorsense.Color].

The payload is capped at `_MAX_HARVEST_TOKENS` declarations: like the DOM-element and
screenshot caps, this bounds host-process memory against a hostile page (one that
declares millions of custom properties) — collection stops at the cap, keeping the
earliest declarations in stylesheet order.

``var(--x)`` aliases are detected from the raw value and recorded in ``alias_target``,
which carries the **leading ``--``** (e.g. ``"--accent"``), matching the
``TokenRecord.name`` convention so the alias graph can be joined on ``name``.
"""

from __future__ import annotations

import asyncio
import re
from typing import TypedDict, cast

from playwright.async_api import Page

from colorsense.color.primitives import parse_css_color
from colorsense.harvest.render import EVAL_TIMEOUT_S
from colorsense.models import TokenRecord

# Matches the first var(--name[, fallback]) reference in a raw value.
_VAR_REF_RE: re.Pattern[str] = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")

# Bound on the per-render token payload. Same rationale as ``dom._MAX_HARVEST_ELEMENTS``:
# every declaration materializes as a pydantic model in the host Python process, so a
# hostile stylesheet declaring millions of --props must not translate into a multi-GB
# allocation here. Far above real design systems (the largest run low thousands of
# declarations across themes/scopes); collection stops at the cap in stylesheet order.
_MAX_HARVEST_TOKENS: int = 5_000


class _RawToken(TypedDict):
    """Shape of one token record returned from the in-page JS."""

    name: str
    raw_value: str
    scope: str
    media: str | None
    resolved: str


# JS enumerating custom properties from same-origin sheets + their :root-resolved values.
_COLLECT_TOKENS_JS: str = r"""
(maxTokens) => {
    const root = document.documentElement;
    const rootStyle = window.getComputedStyle(root);
    const out = [];

    const handleRule = (rule, media) => {
        if (out.length >= maxTokens) return;
        // CSSStyleRule (type 1): scan its declaration block for --custom-props.
        if (rule.style && rule.selectorText !== undefined) {
            const style = rule.style;
            for (let i = 0; i < style.length && out.length < maxTokens; i++) {
                const prop = style[i];
                if (prop && prop.startsWith('--')) {
                    const raw = style.getPropertyValue(prop).trim();
                    let resolved = '';
                    try {
                        resolved = rootStyle.getPropertyValue(prop).trim();
                    } catch (e) {
                        resolved = '';
                    }
                    out.push({
                        name: prop,
                        raw_value: raw,
                        scope: rule.selectorText,
                        media: media,
                        resolved: resolved,
                    });
                }
            }
        }
        // CSSMediaRule (type 4): recurse, capturing its media text.
        if (rule.media && rule.cssRules) {
            const mediaText = rule.conditionText || rule.media.mediaText;
            for (const inner of rule.cssRules) {
                if (out.length >= maxTokens) return;
                handleRule(inner, mediaText);
            }
        }
    };

    // Document sheets plus constructed sheets adopted at the document level
    // (adoptedStyleSheets is not part of document.styleSheets per spec).
    const sheets = [...document.styleSheets, ...(document.adoptedStyleSheets || [])];
    for (const sheet of sheets) {
        if (out.length >= maxTokens) break;
        let rules = null;
        try {
            rules = sheet.cssRules;  // throws on cross-origin sheets
        } catch (e) {
            continue;  // skip cross-origin / inaccessible sheet
        }
        if (!rules) continue;
        for (const rule of rules) {
            if (out.length >= maxTokens) break;
            handleRule(rule, null);
        }
    }
    return out;
}
"""


def _alias_target(raw_value: str) -> str | None:
    """Return the first ``var(--x)`` reference's target name, or ``None``.

    Args:
        raw_value: The raw declared value of a custom property.

    Returns:
        The referenced custom-property name with its leading ``--`` (e.g.
        ``"--accent"``), or ``None`` when the value has no ``var()`` reference.
    """
    match = _VAR_REF_RE.search(raw_value)
    if match is None:
        return None
    return match.group(1)


async def harvest_tokens(page: Page) -> list[TokenRecord]:
    """Collect declared CSS custom properties as `TokenRecord` objects.

    Same-origin stylesheets only (document sheets plus document-level
    ``adoptedStyleSheets``; see the module docstring for scope and the payload cap). Each
    record's ``resolved`` is the ``:root``-resolved value parsed via `parse_css_color`
    (``None`` if non-color/unresolvable), and ``alias_target`` is set (with leading
    ``--``) when the raw value is a ``var(--x)`` reference.

    Args:
        page: The live Playwright page to enumerate custom properties from.

    Returns:
        One `TokenRecord` per declared custom property, in stylesheet order, capped at
        `_MAX_HARVEST_TOKENS`.
    """
    # Bounded like the DOM evaluate: a wedged renderer must fail, not hang.
    raw_tokens = cast(
        list[_RawToken],
        await asyncio.wait_for(
            page.evaluate(_COLLECT_TOKENS_JS, _MAX_HARVEST_TOKENS), EVAL_TIMEOUT_S
        ),
    )

    records: list[TokenRecord] = []
    for token in raw_tokens:
        raw_value = token["raw_value"]
        resolved_str = token["resolved"]
        records.append(
            TokenRecord(
                name=token["name"],
                raw_value=raw_value,
                resolved=parse_css_color(resolved_str) if resolved_str else None,
                scope=token["scope"],
                media=token["media"],
                alias_target=_alias_target(raw_value),
            )
        )
    return records
