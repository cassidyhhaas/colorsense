"""Unit tests for :mod:`colorsense.color.primitives`."""

from __future__ import annotations

import pytest

from colorsense.color.primitives import (
    composite_over,
    contrast_ratio,
    delta_e,
    is_neutral,
    nudge_lightness,
    parse_css_color,
    relative_luminance,
    to_hex,
)
from colorsense.models import Color


def _color(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None
    return c


WHITE = _color("#ffffff")
BLACK = _color("#000000")
BLUE = _color("#0000ff")
GRAY = _color("#808080")


def test_contrast_ratio_white_on_black_is_21() -> None:
    assert contrast_ratio(WHITE, BLACK) == pytest.approx(21.0, abs=1e-6)
    # Order-independent.
    assert contrast_ratio(BLACK, WHITE) == pytest.approx(21.0, abs=1e-6)


def test_delta_e_identity_is_zero() -> None:
    assert delta_e(BLUE, BLUE) == pytest.approx(0.0, abs=1e-9)


def test_delta_e_different_colors_positive() -> None:
    assert delta_e(WHITE, BLACK) > 0.0
    assert delta_e(BLUE, GRAY) > 0.0


def test_composite_half_alpha_black_over_white_is_midgray() -> None:
    fg = Color(hex="#000000", lightness=0.0, chroma=0.0, hue=0.0, alpha=0.5)
    result = composite_over(fg, WHITE)
    assert result.alpha == 1.0
    # Gamma-sRGB source-over of 50% black over white -> #808080.
    assert result.hex == "#808080"
    assert relative_luminance(result) == pytest.approx(0.214, abs=0.02)


def test_composite_opaque_fg_returns_fg() -> None:
    fg = _color("#123456")
    result = composite_over(fg, WHITE)
    assert result.hex == fg.hex
    assert result.alpha == 1.0


def test_composite_transparent_fg_returns_bg() -> None:
    fg = Color(hex="#ff0000", lightness=0.5, chroma=0.2, hue=29.0, alpha=0.0)
    result = composite_over(fg, WHITE)
    assert result.hex == WHITE.hex


@pytest.mark.parametrize("value", ["#fff", "#ffffff", "white", "rgb(255,255,255)"])
def test_parse_near_white(value: str) -> None:
    c = parse_css_color(value)
    assert c is not None
    assert c.hex == "#ffffff"
    assert c.lightness == pytest.approx(1.0, abs=1e-6)
    assert c.alpha == 1.0


def test_parse_transparent_alpha_zero() -> None:
    c = parse_css_color("transparent")
    assert c is not None
    assert c.alpha == 0.0


@pytest.mark.parametrize("value", ["banana", "none", ""])
def test_parse_non_color_returns_none(value: str) -> None:
    assert parse_css_color(value) is None


def test_parse_hsl_and_rgba() -> None:
    red = parse_css_color("hsl(0, 100%, 50%)")
    assert red is not None
    assert red.hex == "#ff0000"
    half = parse_css_color("rgba(0,0,0,0.5)")
    assert half is not None
    assert half.alpha == pytest.approx(0.5, abs=1e-6)
    # Alpha is not encoded into hex.
    assert half.hex == "#000000"


def test_relative_luminance_endpoints() -> None:
    assert relative_luminance(WHITE) == pytest.approx(1.0, abs=1e-6)
    assert relative_luminance(BLACK) == pytest.approx(0.0, abs=1e-6)


def test_is_neutral_gray_vs_saturated_blue() -> None:
    assert is_neutral(GRAY, 0.02) is True
    assert is_neutral(BLUE, 0.02) is False


def test_to_hex_roundtrip() -> None:
    assert to_hex(BLUE) == "#0000ff"
    assert to_hex(GRAY) == "#808080"


def test_nudge_lightness_light_increases_luminance() -> None:
    base = _color("#404040")
    lighter = nudge_lightness(base, "light", 0.2)
    assert relative_luminance(lighter) > relative_luminance(base)


def test_nudge_lightness_dark_decreases_luminance() -> None:
    base = _color("#c0c0c0")
    darker = nudge_lightness(base, "dark", 0.2)
    assert relative_luminance(darker) < relative_luminance(base)


def test_nudge_lightness_clamps_and_preserves_alpha() -> None:
    base = Color(hex="#ffffff", lightness=1.0, chroma=0.0, hue=0.0, alpha=0.5)
    lighter = nudge_lightness(base, "light", 0.5)
    assert lighter.lightness == pytest.approx(1.0, abs=1e-6)
    assert lighter.alpha == pytest.approx(0.5, abs=1e-6)
    darker = nudge_lightness(_color("#000000"), "dark", 0.5)
    assert darker.lightness == pytest.approx(0.0, abs=1e-6)


def test_nudge_lightness_invalid_direction() -> None:
    with pytest.raises(ValueError):
        nudge_lightness(WHITE, "sideways", 0.1)
