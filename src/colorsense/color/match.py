"""Shared perceptual color-matching helpers (radius joins over the OKLab metric).

The palette stages repeatedly answer the same shape of question — "is any candidate
color within ΔE radius *r* of this target?" / "which candidate is nearest within *r*?"
This module is the single home for those radius-join loops, so their metric and
tie-break semantics stay identical across every call site.

Matching metric
---------------
The matching metric is **OKLab ``deltaEOK``** (`colorsense.color.primitives.delta_e`),
baked in here as THE convention for radius joins. This is deliberate and measured: OKLab
outperforms CIEDE2000 on the full eval panel for these inventory/fusion/detect join loops,
so the helper hard-codes it rather than taking a pluggable metric. (CIEDE2000 remains the right
choice for color *identity* questions — see `primitives.ciede2000` — but not here.) The
radius constants themselves stay at the call sites, tuned to this OKLab scale.

Tie-break semantics
-------------------
`nearest_within` uses a ``<=`` comparison against a running best that starts at the radius,
so a candidate exactly at the radius matches and, among candidates at the minimal distance,
the **last** one wins.
Callers that need to filter the candidate set (and recover the original index) should
pre-filter into a list of ``(original_index, value)`` pairs and remap the returned index.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from colorsense.color.primitives import delta_e
from colorsense.models import Color

__all__ = ["any_within", "first_within", "nearest_within"]


def any_within[T](
    target: Color,
    candidates: Iterable[T],
    radius: float,
    *,
    key: Callable[[T], Color],
) -> bool:
    """Return ``True`` iff any candidate is within ``radius`` (OKLab ΔE) of ``target``.

    Matches the inline ``any(delta_e(target, key(c)) <= radius for c in candidates)``
    convention (inclusive of the radius).
    """
    return any(delta_e(target, key(c)) <= radius for c in candidates)


def nearest_within[T](
    target: Color,
    candidates: Sequence[T],
    radius: float,
    *,
    key: Callable[[T], Color],
) -> int | None:
    """Index of the candidate nearest ``target`` within ``radius``, else ``None``.

    Argmin over OKLab ΔE with the historical tie-break: the running best starts at
    ``radius`` (so a candidate exactly at the radius matches) and the comparison is
    ``<=`` (so among candidates at the minimal distance the **last** one wins). Returns
    the index into ``candidates``.
    """
    nearest_index: int | None = None
    nearest_distance = radius
    for index, candidate in enumerate(candidates):
        distance = delta_e(target, key(candidate))
        if distance <= nearest_distance:
            nearest_distance = distance
            nearest_index = index
    return nearest_index


def first_within[T](
    target: Color,
    candidates: Sequence[T],
    radius: float,
    *,
    key: Callable[[T], Color],
) -> int | None:
    """Index of the **first** candidate within ``radius`` (OKLab ΔE) of ``target``, else ``None``.

    First-match (early-break) semantics, deliberately distinct from `nearest_within`:
    when a target is within radius of two candidates this returns the earlier one, which
    matters for stable grouping (do not substitute nearest here).
    """
    for index, candidate in enumerate(candidates):
        if delta_e(target, key(candidate)) <= radius:
            return index
    return None
