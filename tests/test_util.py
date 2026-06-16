"""Unit tests for :mod:`colorsense._util` — the shared internal utilities.

These lock the contract `dedupe_by` centralizes: order preservation, first-occurrence-wins,
and a ``limit`` that caps the number of **unique** items kept (not raw items consumed). The
call sites it replaces (pipeline token/color dedup, gradient-stop dedup) are exercised only
indirectly by goldens, so these pin the cap-on-unique-count distinction directly.
"""

from __future__ import annotations

from colorsense._util import dedupe_by


def test_dedupe_by_preserves_order_and_first_wins() -> None:
    # Keys collide on the lowercased value; the FIRST occurrence of each key is kept, in
    # input order.
    items = ["Apple", "banana", "APPLE", "Cherry", "BANANA"]
    assert dedupe_by(items, key=str.lower) == ["Apple", "banana", "Cherry"]


def test_dedupe_by_identity_key() -> None:
    assert dedupe_by([3, 1, 3, 2, 1], key=lambda n: n) == [3, 1, 2]


def test_dedupe_by_empty() -> None:
    assert dedupe_by([], key=lambda n: n) == []


def test_dedupe_by_limit_none_is_unlimited() -> None:
    assert dedupe_by([1, 2, 3, 4], key=lambda n: n, limit=None) == [1, 2, 3, 4]


def test_dedupe_by_limit_caps_unique_count_not_raw_count() -> None:
    # Duplicates before the cap do NOT advance it: the limit counts UNIQUE kept items. Here
    # the leading duplicate ``1``s collapse to one kept item, so a limit of 2 yields the
    # first two DISTINCT keys (1, 2), never stopping early on the raw duplicates.
    items = [1, 1, 1, 2, 2, 3, 4]
    assert dedupe_by(items, key=lambda n: n, limit=2) == [1, 2]


def test_dedupe_by_limit_exceeding_unique_count_keeps_all() -> None:
    assert dedupe_by([1, 1, 2], key=lambda n: n, limit=10) == [1, 2]
