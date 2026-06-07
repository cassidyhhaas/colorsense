"""Unit tests for WP10 — the recommendation engine.

All inputs are built directly from :class:`RoleResults`; colors are made via
:func:`parse_css_color`. Network-free and deterministic. Threshold assertions use
a tiny epsilon to absorb floating-point noise.
"""

from __future__ import annotations

from colorsense.color.primitives import delta_e, parse_css_color
from colorsense.models import (
    Color,
    PaletteCandidate,
    PaletteRole,
    RoleResults,
    Theme,
)
from colorsense.recommend import (
    TEXT_CONTRAST_TARGET,
    UI_CONTRAST_TARGET,
    recommend,
)

EPS = 1e-6


def _color(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None
    return c


def _candidate(value: str, probability: float = 1.0) -> PaletteCandidate:
    return PaletteCandidate(color=_color(value), probability=probability, area=0.1)


def _roles(**by_role: list[PaletteCandidate]) -> RoleResults:
    mapping = {PaletteRole(name): cands for name, cands in by_role.items()}
    return RoleResults(mapping=mapping)


def _assert_all_pairs_pass(contrast: dict[str, float]) -> None:
    assert contrast["heading_text_on_heading_bg"] >= TEXT_CONTRAST_TARGET - EPS
    assert contrast["cta_text_on_cta_bg"] >= TEXT_CONTRAST_TARGET - EPS
    assert contrast["heading_bg_on_page"] >= UI_CONTRAST_TARGET - EPS
    assert contrast["cta_bg_on_page"] >= UI_CONTRAST_TARGET - EPS


def test_low_contrast_pair_corrected() -> None:
    # Mid-tone surfaces where a naive same-ish text would be low contrast.
    rec = recommend(
        _roles(
            secondary=[_candidate("#808080")],
            accent=[_candidate("#7f7f7f")],
        ),
        Theme.light,
        None,
    )
    assert rec.contrast["heading_text_on_heading_bg"] >= TEXT_CONTRAST_TARGET - EPS
    assert rec.contrast["cta_text_on_cta_bg"] >= TEXT_CONTRAST_TARGET - EPS


def test_near_white_brand_on_white_becomes_distinguishable() -> None:
    rec = recommend(
        _roles(secondary=[_candidate("#fafafa")], accent=[_candidate("#2563eb")]),
        Theme.light,
        None,
    )
    # Near-white banner must have been darkened until it stands out on white.
    assert rec.contrast["heading_bg_on_page"] >= UI_CONTRAST_TARGET - EPS
    # And text on the darkened banner is readable.
    assert rec.contrast["heading_text_on_heading_bg"] >= TEXT_CONTRAST_TARGET - EPS


def test_contrast_reported() -> None:
    rec = recommend(_roles(accent=[_candidate("#2563eb")]), Theme.light, None)
    expected_keys = {
        "heading_text_on_heading_bg",
        "cta_text_on_cta_bg",
        "heading_bg_on_page",
        "cta_bg_on_page",
        "cta_hover_bg_on_page",
    }
    assert rec.contrast  # non-empty
    assert expected_keys <= set(rec.contrast)
    for key in expected_keys:
        assert isinstance(rec.contrast[key], float)


def test_no_pair_fails_realistic_multi_role() -> None:
    rec = recommend(
        _roles(
            primary=[_candidate("#1d4ed8"), _candidate("#3b82f6")],
            secondary=[_candidate("#9333ea")],
            accent=[_candidate("#f97316")],
            neutral_light=[_candidate("#f5f5f5")],
            neutral_dark=[_candidate("#111827")],
        ),
        Theme.light,
        None,
    )
    _assert_all_pairs_pass(rec.contrast)


def test_hover_uses_distinct_hint() -> None:
    hint = _color("#1e40af")
    rec = recommend(_roles(accent=[_candidate("#2563eb")]), Theme.light, hint)
    assert rec.cta_hover_bg == hint


def test_hover_synthesized_when_none() -> None:
    rec = recommend(_roles(accent=[_candidate("#2563eb")]), Theme.light, None)
    assert delta_e(rec.cta_hover_bg, rec.cta_bg) > 0.02


def test_dark_theme_all_pairs_pass() -> None:
    rec = recommend(
        _roles(
            secondary=[_candidate("#9333ea")],
            accent=[_candidate("#f97316")],
        ),
        Theme.dark,
        None,
    )
    assert rec.theme is Theme.dark
    _assert_all_pairs_pass(rec.contrast)


def test_empty_roles_returns_valid_recommendation() -> None:
    rec = recommend(RoleResults(mapping={}), Theme.light, None)
    _assert_all_pairs_pass(rec.contrast)
    # Hover still recorded and perceptibly distinct.
    assert "cta_hover_bg_on_page" in rec.contrast
    assert delta_e(rec.cta_hover_bg, rec.cta_bg) > 0.02
