"""Contract tests: construction and JSON round-trip of the shared models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from colorsense.models import (
    AnalysisResult,
    ClassifiedToken,
    Color,
    ColorCluster,
    ComponentType,
    DivergenceItem,
    HarvestedElement,
    PaletteCandidate,
    PaletteRole,
    Rect,
    RoleResults,
    RunMetadata,
    ScreenshotBin,
    Theme,
    ThemePalette,
    TokenRecord,
    TokenSemanticRole,
    Viewport,
)


def _color(hex_: str = "#3366cc", lum: float = 0.5) -> Color:
    return Color(hex=hex_, lightness=lum, chroma=0.12, hue=260.0, alpha=1.0)


def _dummy_result() -> AnalysisResult:
    viewport = Viewport(width=1280, height=800, device_scale_factor=1.0)
    brand = _color("#3366cc", 0.55)
    white = _color("#ffffff", 0.99)
    dark = _color("#111111", 0.1)

    roles = RoleResults(
        mapping={
            PaletteRole.primary: [
                PaletteCandidate(color=white, probability=0.8, area=0.6, evidence={"area": 0.6})
            ],
            PaletteRole.accent: [
                PaletteCandidate(color=brand, probability=0.7, area=0.05, evidence={"chroma": 0.9})
            ],
        }
    )
    theme_palette = ThemePalette(theme=Theme.light, roles=roles)

    token = ClassifiedToken(
        record=TokenRecord(
            name="--color-primary",
            raw_value="#3366cc",
            resolved=brand,
            scope=":root",
            media=None,
            alias_target=None,
        ),
        semantic_role=TokenSemanticRole.brand_primary,
        weight=5.0,
        palette_prior={
            PaletteRole.accent: 0.55,
            PaletteRole.secondary: 0.35,
            PaletteRole.primary: 0.10,
        },
        text_on_base=None,
    )

    return AnalysisResult(
        url="https://example.com",
        viewport=viewport,
        themes={Theme.light: theme_palette},
        tokens=[token],
        third_party_colors=[_color("#00ff00", 0.8)],
        status_colors=[_color("#cc0000", 0.4)],
        divergence=[
            DivergenceItem(role=PaletteRole.primary, color=dark, note="declared but unused")
        ],
        fit_score=0.82,
        metadata=RunMetadata(
            themes_requested=[Theme.light, Theme.dark],
            themes_analyzed=[Theme.light],
            single_theme=True,
            user_agent="colorsense",
            respect_robots=True,
        ),
    )


def test_value_objects_are_frozen() -> None:
    # Each value object is frozen: assigning to a field must raise pydantic's ValidationError
    # (specifically a frozen-instance error), not merely "something raised".
    color = _color()
    with pytest.raises(ValidationError):
        color.lightness = 0.9  # type: ignore[misc]
    assert color.lightness == 0.5  # value unchanged

    rect = Rect(x=1.0, y=2.0, width=3.0, height=4.0)
    with pytest.raises(ValidationError):
        rect.width = 99.0  # type: ignore[misc]
    assert rect.width == 3.0

    viewport = Viewport(width=1280, height=800, device_scale_factor=1.0)
    with pytest.raises(ValidationError):
        viewport.width = 640  # type: ignore[misc]
    assert viewport.width == 1280


def test_harvest_models_construct() -> None:
    el = HarvestedElement(
        tag="button",
        role=None,
        id="cta",
        class_tokens=["btn", "btn-primary"],
        rect=Rect(x=10, y=20, width=120, height=40),
        position="static",
        bg=_color("#3366cc"),
        text=_color("#ffffff", 0.99),
        border=None,
        is_iframe=False,
        cross_origin=False,
        shadow_host=False,
        clickable=True,
        has_hover_color_change=True,
        hover_bg=_color("#2a52a3", 0.45),
        vendor_match=False,
        visible=True,
        aria_hidden=False,
    )
    assert el.tag == "button"
    sbin = ScreenshotBin(color=_color("#ffffff", 0.99), area_fraction=0.6)
    assert 0.0 <= sbin.area_fraction <= 1.0
    cluster = ColorCluster(
        color=_color(),
        area_weight=0.3,
        member_count=4,
        component_mix={ComponentType.card_bg: 1.0},
    )
    assert cluster.member_count == 4


def test_analysis_result_json_round_trip() -> None:
    original = _dummy_result()
    payload = original.model_dump_json()
    restored = AnalysisResult.model_validate_json(payload)

    assert restored == original
    # Enum-keyed dicts survive the round trip.
    assert Theme.light in restored.themes
    assert restored.themes[Theme.light].roles.mapping[PaletteRole.accent][0].color.hex == "#3366cc"
    assert restored.tokens[0].palette_prior[PaletteRole.accent] == 0.55
    assert restored.metadata.user_agent == "colorsense"
    assert restored.metadata.single_theme is True
    assert restored.metadata.themes_requested == [Theme.light, Theme.dark]
