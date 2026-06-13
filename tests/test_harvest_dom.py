"""Browserless unit tests for the pure gradient-fill gate in :mod:`colorsense.harvest.dom`."""

from __future__ import annotations

from colorsense.color.primitives import parse_css_color
from colorsense.harvest.dom import (
    _MAX_GRADIENT_STOPS,
    _gradient_fill_stops,
    _is_interactive_pill,
)
from colorsense.models import Color

TRANSPARENT = Color(hex="#000000", lightness=0.0, chroma=0.0, hue=0.0, alpha=0.0)


def _bg(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None
    return c


def test_interactive_pill_requires_clickable_and_pill_shape() -> None:
    # A clickable rounded-full stadium (wider than tall) is an interactive pill.
    assert _is_interactive_pill(clickable=True, min_corner_radius=18.0, width=120.0, height=36.0)


def test_clickable_rectangle_is_not_an_interactive_pill() -> None:
    # A clickable gradient card (rounded rectangle, corners < height/2) is NOT a pill —
    # this is the stripe.com decorative-card case the gate must reject.
    assert not _is_interactive_pill(
        clickable=True, min_corner_radius=16.0, width=320.0, height=200.0
    )


def test_non_clickable_pill_is_not_an_interactive_pill() -> None:
    # A pill-shaped but non-clickable divider is rejected.
    assert not _is_interactive_pill(clickable=False, min_corner_radius=3.0, width=240.0, height=6.0)


def test_clickable_circle_is_not_an_interactive_pill() -> None:
    # A circle (width == height) is fully rounded but not wider-than-tall.
    assert not _is_interactive_pill(clickable=True, min_corner_radius=28.0, width=56.0, height=56.0)


def test_opaque_background_color_suppresses_gradient_stops() -> None:
    # A solid background paints the surface; the gradient is decorative on top.
    stops = _gradient_fill_stops(_bg("#101828"), ["rgb(124, 59, 237)", "rgb(60, 131, 246)"])
    assert stops == ()


def test_two_stop_gradient_over_transparent_bg_is_a_fill() -> None:
    stops = _gradient_fill_stops(TRANSPARENT, ["rgb(124, 59, 237)", "rgb(60, 131, 246)"])
    assert [c.hex for c in stops] == ["#7c3bed", "#3c83f6"]


def test_gradient_with_a_fully_transparent_stop_is_decorative() -> None:
    # Glow halos / fade masks / dot-grids always fade to rgba(0,0,0,0): not a fill.
    stops = _gradient_fill_stops(TRANSPARENT, ["rgb(124, 59, 237)", "rgba(0, 0, 0, 0)"])
    assert stops == ()


def test_no_gradient_colors_yields_no_stops() -> None:
    assert _gradient_fill_stops(TRANSPARENT, []) == ()


def test_none_background_treated_as_transparent() -> None:
    stops = _gradient_fill_stops(None, ["rgb(124, 59, 237)", "rgb(60, 131, 246)"])
    assert [c.hex for c in stops] == ["#7c3bed", "#3c83f6"]


def test_semi_transparent_stops_are_kept() -> None:
    # A translucent tinted bar (no fully-transparent stop) is still a fill; alpha is
    # carried so the inventory can weight it.
    raw = ["rgba(124, 59, 237, 0.2)", "rgba(60, 131, 246, 0.4)"]
    stops = _gradient_fill_stops(TRANSPARENT, raw)
    assert [c.hex for c in stops] == ["#7c3bed", "#3c83f6"]
    assert stops[0].alpha == 0.2


def test_repeated_stop_colors_are_deduped_by_hex() -> None:
    stops = _gradient_fill_stops(
        TRANSPARENT, ["rgb(124, 59, 237)", "rgb(124, 59, 237)", "rgb(60, 131, 246)"]
    )
    assert [c.hex for c in stops] == ["#7c3bed", "#3c83f6"]


def test_stop_count_is_capped() -> None:
    raw = [f"rgb({i}, 0, 0)" for i in range(1, 40)]  # 39 distinct opaque colors
    stops = _gradient_fill_stops(TRANSPARENT, raw)
    assert len(stops) == _MAX_GRADIENT_STOPS
