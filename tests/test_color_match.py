"""Unit tests for :mod:`colorsense.color.match` — the shared radius-join helpers.

These lock the contract the module exists to centralize: radius-**inclusive** matching
(``<=``), argmin **last-wins-on-tie**, and the deliberate **first-vs-nearest** distinction.
The behavior-preservation signals (golden snapshots, the eval panel) do not exercise an
at-radius or tie input directly, so without these a future ``<=`` -> ``<`` (or
first-match -> argmin) slip would pass every other test. See the module docstring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from colorsense.color.match import any_within, first_within, nearest_within
from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.models import Color


def _c(value: str) -> Color:
    color = parse_css_color(value)
    assert color is not None
    return color


@dataclass(frozen=True)
class _Wrap:
    """A non-``Color`` candidate, to exercise the ``key`` extractor."""

    color: Color


TARGET = _c("#808080")
NEAR = _c("#7f7f7f")  # ~0.003 OKLab ΔE from TARGET (the closest)
FAR = _c("#000000")  # ~0.600 OKLab ΔE from TARGET (within radius 1.0, but farther)

_D_NEAR = delta_e(TARGET, NEAR)


# --- any_within ------------------------------------------------------------


def test_any_within_empty_is_false() -> None:
    assert any_within(TARGET, [], 1.0, key=lambda c: c) is False


def test_any_within_true_when_a_candidate_is_inside() -> None:
    assert any_within(TARGET, [FAR, NEAR], 1.0, key=lambda c: c) is True


def test_any_within_false_when_all_beyond() -> None:
    assert any_within(TARGET, [NEAR, FAR], math.nextafter(_D_NEAR, 0.0), key=lambda c: c) is False


def test_any_within_is_inclusive_of_the_radius() -> None:
    # Radius exactly equal to the distance -> matches (``<=``).
    assert any_within(TARGET, [NEAR], _D_NEAR, key=lambda c: c) is True
    # The largest float strictly below that distance -> excluded.
    assert any_within(TARGET, [NEAR], math.nextafter(_D_NEAR, 0.0), key=lambda c: c) is False


# --- nearest_within --------------------------------------------------------


def test_nearest_within_empty_is_none() -> None:
    assert nearest_within(TARGET, [], 1.0, key=lambda c: c) is None


def test_nearest_within_none_when_all_beyond() -> None:
    assert (
        nearest_within(TARGET, [NEAR, FAR], math.nextafter(_D_NEAR, 0.0), key=lambda c: c) is None
    )


def test_nearest_within_returns_argmin_index() -> None:
    # NEAR (index 1) is closer than FAR (index 0), both within radius.
    assert nearest_within(TARGET, [FAR, NEAR], 1.0, key=lambda c: c) == 1


def test_nearest_within_is_inclusive_of_the_radius() -> None:
    assert nearest_within(TARGET, [NEAR], _D_NEAR, key=lambda c: c) == 0
    assert nearest_within(TARGET, [NEAR], math.nextafter(_D_NEAR, 0.0), key=lambda c: c) is None


def test_nearest_within_last_wins_on_tie() -> None:
    # Two candidates at identical (minimal) distance -> the LAST index wins (``<=``).
    assert nearest_within(TARGET, [NEAR, NEAR], 1.0, key=lambda c: c) == 1


# --- first_within ----------------------------------------------------------


def test_first_within_none_when_all_beyond() -> None:
    assert first_within(TARGET, [NEAR, FAR], math.nextafter(_D_NEAR, 0.0), key=lambda c: c) is None


def test_first_within_returns_earliest_match_not_nearest() -> None:
    # FAR (earlier, farther) and NEAR (later, closer) are both within radius.
    assert first_within(TARGET, [FAR, NEAR], 1.0, key=lambda c: c) == 0


def test_first_within_diverges_from_nearest_within() -> None:
    # The load-bearing distinction the module documents: same input, different answer.
    candidates = [FAR, NEAR]
    assert first_within(TARGET, candidates, 1.0, key=lambda c: c) == 0
    assert nearest_within(TARGET, candidates, 1.0, key=lambda c: c) == 1


# --- key extractor (non-identity) ------------------------------------------


def test_key_extracts_color_from_wrapper() -> None:
    wrapped = [_Wrap(FAR), _Wrap(NEAR)]
    assert any_within(TARGET, wrapped, 1.0, key=lambda w: w.color) is True
    assert nearest_within(TARGET, wrapped, 1.0, key=lambda w: w.color) == 1
    assert first_within(TARGET, wrapped, 1.0, key=lambda w: w.color) == 0
