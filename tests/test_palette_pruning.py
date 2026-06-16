"""Unit tests for the shared prune/renormalize/argmax step (palette/_pruning.py)."""

from __future__ import annotations

import math

from colorsense.palette._pruning import prune_distribution


def _hex_key(item: str) -> str:
    return item


def test_empty_items_yield_empty_list() -> None:
    assert prune_distribution([], [], min_share=0.02, tie_key=_hex_key) == []


def test_normalizes_weights_into_probabilities_in_input_order() -> None:
    result = prune_distribution(
        ["#bbbbbb", "#aaaaaa"], [3.0, 1.0], min_share=0.02, tie_key=_hex_key
    )
    assert result == [("#bbbbbb", 0.75), ("#aaaaaa", 0.25)]


def test_prunes_below_min_share_and_renormalizes_survivors() -> None:
    # 0.01 share is pruned; the two survivors renormalize back onto the simplex.
    result = prune_distribution(
        ["#111111", "#222222", "#333333"],
        [0.66, 0.33, 0.01],
        min_share=0.02,
        tie_key=_hex_key,
    )
    assert [item for item, _ in result] == ["#111111", "#222222"]
    assert math.isclose(sum(p for _, p in result), 1.0, abs_tol=1e-12)
    assert math.isclose(result[0][1], 0.66 / 0.99, abs_tol=1e-12)


def test_prune_emptying_input_keeps_single_argmax() -> None:
    # 60 entries, every share < min_share: the single largest-weight item is kept at 1.0.
    items = [f"#0000{i:02x}" for i in range(60)]
    weights = [1.0] * 59 + [2.0]  # strict argmax, no tie
    result = prune_distribution(items, weights, min_share=0.02, tie_key=_hex_key)
    assert result == [("#00003b", 1.0)]


def test_argmax_fallback_breaks_exact_ties_by_smallest_tie_key() -> None:
    # Two exactly-tied maxima, larger hex FIRST in input order: positional order must
    # not win — the smallest tie_key does (the codebase's hex convention).
    items = ["#cccccc", "#aaaaaa"] + [f"#0000{i:02x}" for i in range(58)]
    weights = [2.0, 2.0] + [1.9] * 58  # every share < 0.02 after normalization
    assert all(w / sum(weights) < 0.02 for w in weights)
    result = prune_distribution(items, weights, min_share=0.02, tie_key=_hex_key)
    assert result == [("#aaaaaa", 1.0)]


def test_zero_total_weight_keeps_smallest_tie_key_at_one() -> None:
    # All-zero weights: every item ties, so the smallest tie_key wins outright.
    result = prune_distribution(
        ["#bbbbbb", "#aaaaaa", "#cccccc"], [0.0, 0.0, 0.0], min_share=0.02, tie_key=_hex_key
    )
    assert result == [("#aaaaaa", 1.0)]


def test_protected_entry_survives_share_prune_and_renormalizes() -> None:
    # A below-min_share entry flagged protected survives and renormalizes alongside the
    # share survivor; the unprotected below-min_share entry is still pruned.
    result = prune_distribution(
        ["#111111", "#222222", "#333333"],
        [0.66, 0.33, 0.01],
        min_share=0.02,
        tie_key=_hex_key,
        protected=[False, False, True],
    )
    assert [item for item, _ in result] == ["#111111", "#222222", "#333333"]
    assert math.isclose(sum(p for _, p in result), 1.0, abs_tol=1e-12)


def test_protected_does_not_resurrect_zero_total_input() -> None:
    # The argmax/zero-total fallback still owns the all-zero case; protected flags cannot
    # divide by a zero total.
    result = prune_distribution(
        ["#bbbbbb", "#aaaaaa"],
        [0.0, 0.0],
        min_share=0.02,
        tie_key=_hex_key,
        protected=[True, True],
    )
    assert result == [("#aaaaaa", 1.0)]


def test_generic_items_with_caller_supplied_key() -> None:
    # The helper is generic over the item type; only tie_key needs to produce a string.
    items = [("cluster-b", "#ff0000"), ("cluster-a", "#00ff00")]
    result = prune_distribution(items, [0.0, 0.0], min_share=0.02, tie_key=lambda item: item[1])
    assert result == [(("cluster-a", "#00ff00"), 1.0)]
