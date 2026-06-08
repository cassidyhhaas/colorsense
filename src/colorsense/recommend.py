"""Recommendation engine.

From reconciled palette roles (the reconcile output, a posterior
:class:`~colorsense.models.RoleResults`), produce a WCAG-enforced widget color
recommendation for a single theme: a heading banner plus CTA colors whose every
returned text/background pair meets a contrast minimum and whose surfaces are
visibly distinguishable from the theme's page background.

The pipeline is deterministic (no randomness):

1. Pick a ``page_bg`` for the theme (white for light, near-black for dark).
2. Select candidate ``heading_bg`` / ``cta_bg`` colors from the role candidates,
   with graceful fallbacks down to a brand default so empty input never raises.
3. Ensure each surface is distinguishable from ``page_bg`` (nudging it *away*
   from the page background until it meets :data:`UI_CONTRAST_TARGET`).
4. Compute readable ``heading_text`` / ``cta_text`` on the *final* surfaces
   (meeting :data:`TEXT_CONTRAST_TARGET`).
5. Derive a perceptibly distinct ``cta_hover_bg``.
6. Emit a contrast report of the final measured ratios.

The acceptance guarantee: no returned text/bg pair fails its threshold.
"""

from __future__ import annotations

from colorsense.color.primitives import (
    contrast_ratio,
    delta_e,
    is_neutral,
    nudge_lightness,
    parse_css_color,
    relative_luminance,
)
from colorsense.models import (
    Color,
    PaletteCandidate,
    PaletteRole,
    Recommendation,
    RoleResults,
    Theme,
)

__all__ = ["recommend"]

# ---------------------------------------------------------------------------
# Tunable constants (all documented).
# ---------------------------------------------------------------------------

#: WCAG AA contrast minimum for body text on its background.
TEXT_CONTRAST_TARGET: float = 4.5
#: Contrast minimum for a large/UI surface to read as distinct from the page.
UI_CONTRAST_TARGET: float = 3.0
#: OKLCH lightness step per nudge iteration when enforcing a target.
NUDGE_STEP: float = 0.05
#: Maximum nudge iterations before giving up on a target.
MAX_NUDGE_ITERS: int = 40
#: OKLCH lightness delta used to synthesize a CTA hover shade.
HOVER_STEP: float = 0.08
#: OKLCH chroma at/below which a color is treated as neutral (achromatic-ish).
NEUTRAL_CHROMA_MAX: float = 0.04
#: Minimum perceptual distance for a hover color to count as "distinct".
HOVER_DELTA_E_EPS: float = 0.02

#: Page background per theme (the widget host surface the banner/CTA sit on).
_LIGHT_PAGE_HEX: str = "#ffffff"
_DARK_PAGE_HEX: str = "#0b0b0b"
#: Brand default action color used when no usable candidate exists.
_BRAND_DEFAULT_HEX: str = "#2563eb"
_BLACK_HEX: str = "#000000"
_WHITE_HEX: str = "#ffffff"


def _must(value: Color | None) -> Color:
    """Assert a module-internal literal parsed successfully.

    Used only for hardcoded hex literals we control, so the ``None`` branch is
    unreachable in practice; it keeps mypy satisfied without ``# type: ignore``.
    """
    if value is None:  # pragma: no cover - defensive; literals always parse
        raise ValueError("internal color literal failed to parse")
    return value


def _page_bg(theme: Theme) -> Color:
    """Return the page background color for ``theme``."""
    return _must(parse_css_color(_LIGHT_PAGE_HEX if theme is Theme.light else _DARK_PAGE_HEX))


def _top(roles: RoleResults, role: PaletteRole) -> Color | None:
    """Return the top (highest-probability) candidate color of ``role``, if any."""
    candidates = roles.mapping.get(role)
    if not candidates:
        return None
    return candidates[0].color


def _all_candidates(roles: RoleResults) -> list[PaletteCandidate]:
    """Flatten every candidate across all roles into one list."""
    out: list[PaletteCandidate] = []
    for candidates in roles.mapping.values():
        out.extend(candidates)
    return out


def _highest_chroma(roles: RoleResults) -> Color | None:
    """Return the most chromatic candidate color across all roles, if any.

    Ties are broken deterministically by hex so the result is stable.
    """
    candidates = _all_candidates(roles)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.color.chroma, c.color.hex))
    return best.color


def _select_cta_bg(roles: RoleResults) -> Color:
    """Select the CTA (action) surface color.

    accent top -> highest-chroma across all roles -> brand default.
    """
    accent = _top(roles, PaletteRole.accent)
    if accent is not None:
        return accent
    chromatic = _highest_chroma(roles)
    if chromatic is not None:
        return chromatic
    return _must(parse_css_color(_BRAND_DEFAULT_HEX))


def _select_heading_bg(roles: RoleResults, cta_bg: Color) -> Color:
    """Select the heading banner surface color.

    Prefer a chromatic structural/brand color (secondary -> accent -> primary),
    taking the first NON-neutral option; otherwise the strongest available brand
    color (highest chroma); final fallback is ``cta_bg``.
    """
    ordered = [
        _top(roles, PaletteRole.secondary),
        _top(roles, PaletteRole.accent),
        _top(roles, PaletteRole.primary),
    ]
    for candidate in ordered:
        if candidate is not None and not is_neutral(candidate, NEUTRAL_CHROMA_MAX):
            return candidate
    chromatic = _highest_chroma(roles)
    if chromatic is not None:
        return chromatic
    return cta_bg


def _enforce_text_on(bg: Color, target: float) -> Color:
    """Return a text color readable on ``bg`` (>= ``target`` contrast).

    Starts from the better of black/white on ``bg`` and, for robustness, nudges
    that starting color *away* from ``bg``'s luminance until the target is met.
    Falls back to the better of black/white if nudging never reaches the target
    (which effectively cannot happen: the better of black/white on any solid
    background is always >= ~4.58).
    """
    black = _must(parse_css_color(_BLACK_HEX))
    white = _must(parse_css_color(_WHITE_HEX))
    starting = black if contrast_ratio(black, bg) >= contrast_ratio(white, bg) else white

    if contrast_ratio(starting, bg) >= target:
        return starting

    toward = "dark" if relative_luminance(bg) > 0.5 else "light"
    text = starting
    for _ in range(MAX_NUDGE_ITERS):
        if contrast_ratio(text, bg) >= target:
            return text
        text = nudge_lightness(text, toward, NUDGE_STEP)
    if contrast_ratio(text, bg) >= target:
        return text
    return black if contrast_ratio(black, bg) >= contrast_ratio(white, bg) else white


def _ensure_distinguishable(surface: Color, page_bg: Color, target: float) -> Color:
    """Return a surface visibly distinct from ``page_bg`` (>= ``target`` contrast).

    If already distinct, ``surface`` is returned unchanged. Otherwise its
    lightness is nudged *away* from the page background (darker on a light page,
    lighter on a dark page) until the target is met or the lightness bound is hit.
    """
    if contrast_ratio(surface, page_bg) >= target:
        return surface

    toward = "dark" if relative_luminance(page_bg) > 0.5 else "light"
    result = surface
    for _ in range(MAX_NUDGE_ITERS):
        if contrast_ratio(result, page_bg) >= target:
            return result
        result = nudge_lightness(result, toward, NUDGE_STEP)
    return result


def _make_hover(cta_bg: Color, theme: Theme, hover_hint: Color | None) -> Color:
    """Return a CTA hover surface perceptibly distinct from the final ``cta_bg``.

    Uses ``hover_hint`` when supplied and perceptibly different; otherwise
    synthesizes a shade by nudging ``cta_bg`` (darker on light themes, lighter on
    dark themes). If a single step lands too close because of lightness clamping,
    the opposite direction is used so the result always differs perceptibly.
    """
    if hover_hint is not None and delta_e(hover_hint, cta_bg) > HOVER_DELTA_E_EPS:
        return hover_hint

    primary = "dark" if theme is Theme.light else "light"
    opposite = "light" if primary == "dark" else "dark"

    hover = nudge_lightness(cta_bg, primary, HOVER_STEP)
    if delta_e(hover, cta_bg) > HOVER_DELTA_E_EPS:
        return hover
    return nudge_lightness(cta_bg, opposite, HOVER_STEP)


def recommend(roles: RoleResults, theme: Theme, hover_hint: Color | None) -> Recommendation:
    """Produce a WCAG-enforced :class:`Recommendation` for ``theme``.

    Every returned text/background pair meets its contrast target and both
    surfaces are distinguishable from the theme's page background. Empty or
    degenerate ``roles`` never raise: brand-default fallbacks are used so the
    result is always valid.
    """
    page_bg = _page_bg(theme)

    cta_bg = _select_cta_bg(roles)
    heading_bg = _select_heading_bg(roles, cta_bg)

    # ORDER MATTERS: make surfaces distinct from the page FIRST, then compute the
    # text colors against the (possibly nudged) final surfaces.
    heading_bg = _ensure_distinguishable(heading_bg, page_bg, UI_CONTRAST_TARGET)
    cta_bg = _ensure_distinguishable(cta_bg, page_bg, UI_CONTRAST_TARGET)

    heading_text = _enforce_text_on(heading_bg, TEXT_CONTRAST_TARGET)
    cta_text = _enforce_text_on(cta_bg, TEXT_CONTRAST_TARGET)

    cta_hover_bg = _make_hover(cta_bg, theme, hover_hint)

    contrast = {
        "heading_text_on_heading_bg": contrast_ratio(heading_text, heading_bg),
        "cta_text_on_cta_bg": contrast_ratio(cta_text, cta_bg),
        "heading_bg_on_page": contrast_ratio(heading_bg, page_bg),
        "cta_bg_on_page": contrast_ratio(cta_bg, page_bg),
        "cta_hover_bg_on_page": contrast_ratio(cta_hover_bg, page_bg),
    }

    return Recommendation(
        theme=theme,
        heading_bg=heading_bg,
        heading_text=heading_text,
        cta_bg=cta_bg,
        cta_text=cta_text,
        cta_hover_bg=cta_hover_bg,
        contrast=contrast,
    )
