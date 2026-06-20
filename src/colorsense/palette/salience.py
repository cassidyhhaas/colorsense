"""Per-instance salience and role-level aggregation for detection-plus-ranking.

Pure, side-effect-free helpers implementing the salience model of the redesign
(tuning-spec §1-§3): the per-instance salience ``sigma_i = p_role * pi_i``, its bounded
prominence modulators (position, sibling-relative size, contrast), the role-parameterized
aggregation ``S_measured = sigma_(1) + lambda_r * sum sigma_(i)^beta_r``, and the bounded
intent multiplier ``f = 1 + alpha * q_intent``.

All quantities are unitless and **anchored to viewport fraction**, not pixels, so
thresholds calibrated against them are resolution-independent (tuning-spec §0). Every
constant lives in the passed config object ([`DetectionConfig`][colorsense.config.DetectionConfig]
and its sub-models); nothing is hard-coded here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from colorsense.models import BoundingBox, Viewport

if TYPE_CHECKING:
    from colorsense.config import (
        ContrastModulatorConfig,
        DetectionConfig,
        PositionModulatorConfig,
        SiblingModulatorConfig,
    )

__all__ = [
    "aggregate_salience",
    "area_fraction",
    "clamp",
    "contrast_modulator",
    "instance_prominence",
    "intent_multiplier",
    "position_modulator",
    "sibling_modulator",
    "vertical_fraction",
]


def clamp(x: float, lo: float, hi: float) -> float:
    """Clamp ``x`` to the closed interval ``[lo, hi]``.

    Args:
        x: The value to clamp.
        lo: Lower bound (inclusive).
        hi: Upper bound (inclusive).

    Returns:
        ``lo`` if ``x < lo``, ``hi`` if ``x > hi``, else ``x``.

    """
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def area_fraction(box: BoundingBox, viewport: Viewport) -> float:
    """Bounding-box area as a fraction of the viewport, clamped to ``(0, 1]``.

    The magnitude carrier ``a_i`` of instance prominence (tuning-spec §1.2): a raw
    area share, clamped so a zero/negative box never produces a non-positive prominence
    and an oversized box never exceeds the full viewport.

    Args:
        box: The instance's layout rectangle in CSS pixels.
        viewport: The rendering viewport.

    Returns:
        ``(box.width * box.height) / (viewport.width * viewport.height)`` clamped to
        ``(0, 1]``.

    """
    viewport_area = viewport.width * viewport.height
    raw = (box.width * box.height) / viewport_area
    # Clamp to (0, 1]: the lower bound is the smallest positive float so a_i never
    # zeroes out prominence, the upper bound is the full viewport.
    return clamp(raw, 1e-12, 1.0)


def vertical_fraction(box: BoundingBox, viewport: Viewport) -> float:
    """Element center-y as a fraction of the first-viewport height, clamped to ``[0, 1]``.

    Drives the above-the-fold modulator ``m_pos``: ``0.0`` is the very top of the page,
    ``1.0`` is the fold (and everything below it, since the value is clamped).

    Args:
        box: The instance's layout rectangle in CSS pixels.
        viewport: The rendering viewport (its ``height`` is the first-viewport height).

    Returns:
        ``clamp((box.y + box.height / 2) / viewport.height, 0.0, 1.0)``.

    """
    center_y = box.y + box.height / 2.0
    return clamp(center_y / viewport.height, 0.0, 1.0)


def position_modulator(y_frac: float, cfg: PositionModulatorConfig) -> float:
    """Above-the-fold position modulator ``m_pos`` (tuning-spec §1.2).

    Rewards elements high on the page and damps those below the fold; bounded and
    centered near ``1`` so it nudges but cannot manufacture prominence.

    Args:
        y_frac: Element center-y as a fraction of the first-viewport height (from
            `vertical_fraction`), in ``[0, 1]``.
        cfg: The position-modulator config (intercept, slope, min, max).

    Returns:
        ``clamp(cfg.intercept - cfg.slope * y_frac, cfg.min, cfg.max)``.

    """
    return clamp(cfg.intercept - cfg.slope * y_frac, cfg.min, cfg.max)


def sibling_modulator(a_i: float, median_sibling_area: float, cfg: SiblingModulatorConfig) -> float:
    """Sibling-relative size modulator ``m_sib`` (tuning-spec §1.2).

    Rewards an element that is much larger than its sibling interactive elements. The
    gentle ``cfg.exponent`` (default ``0.25``) keeps the influence mild; a non-positive
    median (no siblings) yields the neutral ``1.0``.

    Args:
        a_i: The instance's own area fraction (from `area_fraction`).
        median_sibling_area: Median area fraction of the instance's sibling interactive
            elements; ``<= 0`` means no comparison is possible.
        cfg: The sibling-modulator config (exponent, min, max).

    Returns:
        ``1.0`` if ``median_sibling_area <= 0``; otherwise
        ``clamp((a_i / median_sibling_area)^cfg.exponent, cfg.min, cfg.max)``.

    """
    if median_sibling_area <= 0.0:
        return 1.0
    return clamp((a_i / median_sibling_area) ** cfg.exponent, cfg.min, cfg.max)


def contrast_modulator(cr: float, cfg: ContrastModulatorConfig) -> float:
    """Contrast modulator ``m_con`` (tuning-spec §1.2).

    A tiebreak-strength signal centered near ``1`` around the WCAG AA threshold
    (``cr = 4.5 -> ~0.925`` with the defaults): higher contrast nudges prominence up.

    Args:
        cr: WCAG contrast ratio of the element against its effective background (use
            `colorsense.color.primitives.contrast_ratio`).
        cfg: The contrast-modulator config (intercept, slope, pivot, min, max).

    Returns:
        ``clamp(cfg.intercept + cfg.slope * (cr - cfg.pivot), cfg.min, cfg.max)``.

    """
    return clamp(cfg.intercept + cfg.slope * (cr - cfg.pivot), cfg.min, cfg.max)


def instance_prominence(
    a_i: float,
    *,
    y_frac: float,
    median_sibling_area: float,
    contrast: float | None,
    surface: bool,
    cfg: DetectionConfig,
) -> float:
    """Per-instance prominence ``pi_i`` (tuning-spec §1.2).

    For element roles, ``pi_i = a_i * m_pos * m_sib * m_con``: area carries the magnitude
    and the three bounded modulators nudge it. For surface roles (``page``/``surface``/
    ``banner``) all modulators are ``1`` — area *is* the prominence — so ``pi_i = a_i``.

    Args:
        a_i: The instance's own area fraction (from `area_fraction`).
        y_frac: Element center-y as a fraction of the first-viewport height.
        median_sibling_area: Median area fraction of sibling interactive elements.
        contrast: WCAG contrast ratio against the effective background, or ``None`` when
            contrast is unavailable (the painted color or its effective background is
            missing). ``None`` yields the neutral ``m_con = 1.0`` so a missing contrast
            neither rewards nor penalizes the instance.
        surface: Whether this is a surface role (disables all modulators).
        cfg: The detection config carrying the modulator sub-configs.

    Returns:
        ``a_i`` for surface roles, else ``a_i * m_pos * m_sib * m_con``.

    """
    if surface:
        return a_i
    m_pos = position_modulator(y_frac, cfg.position)
    m_sib = sibling_modulator(a_i, median_sibling_area, cfg.sibling)
    m_con = 1.0 if contrast is None else contrast_modulator(contrast, cfg.contrast)
    return a_i * m_pos * m_sib * m_con


def aggregate_salience(saliences_desc: Sequence[float], lambda_r: float, beta_r: float) -> float:
    """Role-level measured salience ``S_measured`` (tuning-spec §2).

    Peak-dominant with saturating corroboration::

        S = sigma_(1) + lambda_r * sum_{i >= 2} sigma_(i) ^ beta_r

    The peak term ``sigma_(1)`` makes one prominent instance outweigh many tiny ones; the
    concave tail (``beta_r <= 1``) adds confidence from additional instances with diminishing
    returns, so headcount cannot overwhelm peak prominence.

    Args:
        saliences_desc: Per-instance saliences sigma_i, **sorted descending** (the first
            element is the peak). May be empty.
        lambda_r: Corroboration weight lambda_r for role ``r``, in ``[0, 1]``.
        beta_r: Concavity exponent beta_r for role ``r``, in ``(0, 1]``.

    Returns:
        The aggregated salience, or ``0.0`` for an empty input.

    """
    if not saliences_desc:
        return 0.0
    peak = saliences_desc[0]
    tail = math.fsum(math.pow(s, beta_r) for s in saliences_desc[1:])
    return peak + lambda_r * tail


def intent_multiplier(q_intent: float, alpha: float) -> float:
    """Bounded intent corroboration multiplier ``f`` (tuning-spec §3).

    ``f = 1 + alpha * q_intent`` with ``q_intent`` clamped to ``[0, 1]``, so ``f`` is bounded
    to ``[1, 1 + alpha]``: a matching token can re-rank or rescue a color at the margin but can
    never veto (a missing token gives ``f = 1``, never a penalty) nor manufacture one (no
    measured salience means nothing to multiply).

    Args:
        q_intent: The matched token's usage-intent share for the role; clamped to ``[0, 1]``.
        alpha: The intent boost cap (the maximum boost is ``1 + alpha``).

    Returns:
        ``1.0 + alpha * clamp(q_intent, 0.0, 1.0)`` — in ``[1, 1 + alpha]``.

    """
    return 1.0 + alpha * clamp(q_intent, 0.0, 1.0)
