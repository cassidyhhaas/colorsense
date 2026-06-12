"""Shared prune/renormalize/argmax-fallback step for probability rankings.

Every palette ranking stage ends the same way: normalize raw prominence weights into a
probability distribution, prune entries below a minimum share, renormalize the
survivors, and — if pruning would empty a non-empty input — keep the single argmax at
probability 1.0 instead. This module implements that step once, so the three call sites
(``palette/usage.py``, ``palette/roles.py``, ``palette/reconcile.py``) cannot drift:
every argmax fallback breaks exact-probability ties by the caller-supplied ``tie_key``
— the color ``hex`` at all call sites, smallest winning, which is the codebase's
determinism convention.

``classify/components.py`` has a similar-looking softmax-prune block but ranks
``ComponentType`` keys, not colors — there is no hex to tie-break on, and ``classify/``
does not depend on ``palette/`` — so it deliberately keeps a local copy (with a pointer
back here).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

__all__ = ["prune_distribution"]


def prune_distribution[T](
    items: Sequence[T],
    weights: Sequence[float],
    *,
    min_share: float,
    tie_key: Callable[[T], str],
) -> list[tuple[T, float]]:
    """Normalize ``weights`` over ``items``, prune below ``min_share``, renormalize.

    ``items`` and ``weights`` are parallel; the result is ``(item, probability)`` pairs
    in input order (callers apply their own final ranking sort). Degenerate cases keep
    a single deterministic argmax at probability 1.0 rather than emptying a non-empty
    input:

    * pruning removed everything → the highest-probability item wins, exact ties broken
      by the smallest ``tie_key``;
    * the weights sum to zero (e.g. an all-zero-area surface set) → every item ties, so
      the smallest ``tie_key`` wins outright.

    An empty ``items`` yields ``[]``.
    """
    if not items:
        return []

    total = sum(weights)
    if total <= 0.0:
        return [(min(items, key=tie_key), 1.0)]

    probs = [w / total for w in weights]
    kept = [(item, p) for item, p in zip(items, probs, strict=True) if p >= min_share]
    if not kept:
        best, _ = min(zip(items, probs, strict=True), key=lambda ip: (-ip[1], tie_key(ip[0])))
        return [(best, 1.0)]

    kept_total = sum(p for _, p in kept)
    return [(item, p / kept_total) for item, p in kept]
