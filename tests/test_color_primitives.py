"""Unit tests for :mod:`colorsense.color.primitives`."""

from __future__ import annotations

import pytest
from coloraide import Color as CAColor

from colorsense.color.primitives import (
    ciede2000,
    contrast_ratio,
    delta_e,
    parse_css_color,
    relative_luminance,
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


def test_delta_e_black_white_is_about_one() -> None:
    # OKLab lightness spans [0, 1]; black vs white is a pure-lightness distance of ~1.
    assert delta_e(BLACK, WHITE) == pytest.approx(1.0, abs=1e-6)


def test_ciede2000_identity_is_zero() -> None:
    assert ciede2000(BLUE, BLUE) == pytest.approx(0.0, abs=1e-9)


def test_ciede2000_matches_coloraide_2000() -> None:
    for a, b in ((WHITE, BLACK), (BLUE, GRAY), (WHITE, _color("#f0f6fc"))):
        expected = float(CAColor(a.hex).delta_e(CAColor(b.hex), method="2000"))
        assert ciede2000(a, b) == pytest.approx(expected, abs=1e-7), (a.hex, b.hex)


def test_ciede2000_is_more_accurate_than_oklab_near_white() -> None:
    """The motivating case: two near-whites OKLab collapses but CIEDE2000 keeps distinct.

    ``#ffffff`` vs Primer's ``#f0f6fc`` is OKLab ΔE ~0.03 (below the 0.05 cluster radius —
    they merge) yet CIEDE2000 ~4 (clearly distinct). This is exactly why color-IDENTITY
    comparisons (the CTA-label canvas-distinctness test) use CIEDE2000, not OKLab.
    """
    near_white = _color("#f0f6fc")
    assert delta_e(WHITE, near_white) < 0.05
    assert ciede2000(WHITE, near_white) > 3.0


# A meaningful spread: saturated hues around the wheel, grays, near-black/near-white, and
# alpha-carrying inputs (alpha is ignored by deltaEOK; the pairs prove it stays ignored).
# Each color is canonicalized through its hex (``_color(_color(v).hex)``): the cached OKLCH
# coords are computed from the *unquantized* sRGB input, while the coloraide reference below
# is built from the 8-bit hex — for non-hex-exact inputs (hsl) those differ by ~1e-3, which
# is quantization, not a delta_e formula error. The canonical form pins both sides to the
# same 8-bit color so the formula equivalence is testable at 1e-7. The original alpha is
# carried over so the grid still exercises alpha-bearing colors (deltaEOK ignores alpha).
def _canonical(value: str) -> Color:
    c = _color(value)
    return _color(c.hex).model_copy(update={"alpha": c.alpha})


_DELTA_E_GRID = [
    _canonical(v)
    for v in [
        "#ff0000",
        "#00ff00",
        "#0000ff",
        "#ffff00",
        "#00ffff",
        "#ff00ff",
        "#ff8800",
        "#8800ff",
        "#22aa66",
        "#aa2266",
        "hsl(15, 90%, 55%)",
        "hsl(195, 80%, 40%)",
        "hsl(285, 70%, 65%)",
        "#000000",
        "#ffffff",
        "#010101",
        "#fefefe",
        "#0a0a0a",
        "#f5f5f5",
        "#808080",
        "#404040",
        "#c0c0c0",
        "rgba(255, 0, 0, 0.5)",
        "rgba(0, 0, 255, 0.25)",
        "rgba(17, 24, 39, 0.8)",
        "rgba(128, 128, 128, 0.1)",
    ]
]


def test_delta_e_matches_coloraide_deltaeok() -> None:
    # The cached-OKLCH fast path must agree with coloraide's deltaEOK across the whole
    # pairwise grid (saturated hues, grays, near-black/white, alpha-carrying colors).
    for a in _DELTA_E_GRID:
        for b in _DELTA_E_GRID:
            expected = float(CAColor(a.hex).delta_e(CAColor(b.hex), method="ok"))
            assert delta_e(a, b) == pytest.approx(expected, abs=1e-7), (a.hex, b.hex)


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


@pytest.mark.parametrize(
    ("value", "expected_hex"),
    [
        ("rgb(300,0,0)", "#ff0000"),  # over-range channel clamps to 255
        ("rgb(-5,0,0)", "#000000"),  # negative channel clamps to 0
        ("rgb(0,300,0)", "#00ff00"),
        ("rgb(300,300,300)", "#ffffff"),
    ],
)
def test_parse_out_of_range_rgb_clamps(value: str, expected_hex: str) -> None:
    # Browsers clamp out-of-gamut rgb() per-channel rather than perceptually gamut-mapping
    # (which would yield e.g. #ff6c5b for rgb(300,0,0)).
    c = parse_css_color(value)
    assert c is not None
    assert c.hex == expected_hex


def test_parse_in_gamut_rgb_unchanged() -> None:
    # The clamp must not perturb a normal in-range color.
    c = parse_css_color("rgb(10,20,30)")
    assert c is not None
    assert c.hex == "#0a141e"


def test_relative_luminance_endpoints() -> None:
    assert relative_luminance(WHITE) == pytest.approx(1.0, abs=1e-6)
    assert relative_luminance(BLACK) == pytest.approx(0.0, abs=1e-6)
