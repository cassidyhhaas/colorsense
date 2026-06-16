"""Small internal, dependency-free utilities shared across the package.

Private module (leading underscore): nothing here is part of the public API and the
contents are free to change. Helpers land here only when the same shape of logic was
otherwise re-inlined at several call sites, where a single named helper removes the
duplication and pins one canonical behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable

__all__ = ["dedupe_by"]


def dedupe_by[T](
    items: Iterable[T],
    key: Callable[[T], Hashable],
    *,
    limit: int | None = None,
) -> list[T]:
    """Order-preserving dedupe: the first occurrence of each ``key(item)`` wins.

    Iterates ``items`` in order, keeping each item whose key has not been seen before and
    skipping subsequent items with a duplicate key, so the result preserves input order with
    only the earliest representative of each key.

    Optional ``limit`` caps the number of **unique** items kept: iteration stops once
    ``limit`` distinct keys have been collected (so the cap counts kept items, not items
    consumed — duplicates encountered before the cap do not advance it). ``None`` (the
    default) means unlimited.
    """
    seen: set[Hashable] = set()
    out: list[T] = []
    for item in items:
        item_key = key(item)
        if item_key in seen:
            continue
        seen.add(item_key)
        out.append(item)
        if limit is not None and len(out) >= limit:
            break
    return out
