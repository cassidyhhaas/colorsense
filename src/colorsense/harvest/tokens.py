"""CSSOM design-token harvesting.

Enumerates declared CSS custom properties (``--*``) across all *same-origin* stylesheets
(cross-origin sheets that throw on ``.cssRules`` are skipped), recursing into ``@media``
rules and capturing the media text. For each declaration we capture the raw value and its
scope selector, and separately read the value resolved against ``:root`` so it can be
parsed into a :class:`~colorsense.models.Color`.

``var(--x)`` aliases are detected from the raw value and recorded in ``alias_target``.

Alias-target naming convention
------------------------------
``alias_target`` carries the **leading ``--``** (e.g. ``"--accent"``), matching the
``TokenRecord.name`` convention so the alias graph can be joined on ``name``.
"""

from __future__ import annotations

import re
from typing import TypedDict, cast

from playwright.sync_api import Page

from colorsense.color.primitives import parse_css_color
from colorsense.models import TokenRecord

# Matches the first var(--name[, fallback]) reference in a raw value.
_VAR_REF_RE: re.Pattern[str] = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")


class _RawToken(TypedDict):
    """Shape of one token record returned from the in-page JS."""

    name: str
    raw_value: str
    scope: str
    media: str | None
    resolved: str


# JS enumerating custom properties from same-origin sheets + their :root-resolved values.
_COLLECT_TOKENS_JS: str = r"""
() => {
    const root = document.documentElement;
    const rootStyle = window.getComputedStyle(root);
    const out = [];

    const handleRule = (rule, media) => {
        // CSSStyleRule (type 1): scan its declaration block for --custom-props.
        if (rule.style && rule.selectorText !== undefined) {
            const style = rule.style;
            for (let i = 0; i < style.length; i++) {
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
                handleRule(inner, mediaText);
            }
        }
    };

    for (const sheet of document.styleSheets) {
        let rules = null;
        try {
            rules = sheet.cssRules;  // throws on cross-origin sheets
        } catch (e) {
            continue;  // skip cross-origin / inaccessible sheet
        }
        if (!rules) continue;
        for (const rule of rules) {
            handleRule(rule, null);
        }
    }
    return out;
}
"""


def _alias_target(raw_value: str) -> str | None:
    """Return the referenced custom-property name (with leading ``--``) or ``None``."""
    match = _VAR_REF_RE.search(raw_value)
    if match is None:
        return None
    return match.group(1)


def harvest_tokens(page: Page) -> list[TokenRecord]:
    """Collect declared CSS custom properties as :class:`TokenRecord` objects.

    Same-origin stylesheets only. Each record's ``resolved`` is the ``:root``-resolved
    value parsed via WP2 :func:`parse_css_color` (``None`` if non-color/unresolvable), and
    ``alias_target`` is set (with leading ``--``) when the raw value is a ``var(--x)``
    reference.
    """
    raw_tokens = cast(list[_RawToken], page.evaluate(_COLLECT_TOKENS_JS))

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
