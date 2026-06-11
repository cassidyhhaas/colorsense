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
    DesignToken,
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
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    UsageCategory,
    UsageEntry,
    UsagePalette,
    Viewport,
)


def _color(hex_: str = "#3366cc", lum: float = 0.5) -> Color:
    return Color(hex=hex_, lightness=lum, chroma=0.12, hue=260.0, alpha=1.0)


def _dummy_result(*, tokens: tuple[DesignToken, ...] | None = None) -> AnalysisResult:
    viewport = Viewport(width=1280, height=800, device_scale_factor=1.0)
    brand = _color("#3366cc", 0.55)
    white = _color("#ffffff", 0.99)
    dark = _color("#111111", 0.1)

    usage = UsagePalette(
        mapping={
            UsageCategory.surface: (
                UsageEntry(
                    color=white,
                    probability=0.8,
                    area=0.6,
                    components={ComponentType.page_bg: 1.0},
                ),
            ),
            UsageCategory.interactive: (
                UsageEntry(
                    color=brand,
                    probability=0.7,
                    area=0.05,
                    components={ComponentType.link: 0.6, ComponentType.cta_bg: 0.4},
                ),
            ),
        }
    )
    roles = RoleResults(
        mapping={
            PaletteRole.primary: (PaletteCandidate(color=white, probability=0.8, area=0.6),),
            PaletteRole.accent: (PaletteCandidate(color=brand, probability=0.7, area=0.05),),
        }
    )
    theme_palette = ThemePalette(
        theme=Theme.light,
        usage=usage,
        roles=roles,
        fit_score=0.82,
        divergence=(
            DivergenceItem(category=UsageCategory.surface, color=dark, note="declared but unused"),
        ),
        tokens=tokens,
    )

    return AnalysisResult(
        url="https://example.com",
        viewport=viewport,
        themes={Theme.light: theme_palette},
        third_party_colors=(_color("#00ff00", 0.8),),
        metadata=RunMetadata(
            themes_requested=(Theme.light, Theme.dark),
            themes_analyzed=(Theme.light,),
            user_agent="colorsense",
            respect_robots=True,
        ),
    )


def _design_token() -> DesignToken:
    return DesignToken(
        name="--color-primary",
        color=_color("#3366cc", 0.55),
        semantic_role=TokenSemanticRole.brand_primary,
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


def test_output_models_are_frozen() -> None:
    # The public result tree is immutable: reassigning any attribute on an output model
    # raises pydantic's frozen-instance ValidationError, and the value is unchanged.
    result = _dummy_result(tokens=(_design_token(),))
    with pytest.raises(ValidationError):
        result.url = "https://other.example"  # type: ignore[misc]
    assert result.url == "https://example.com"

    palette = result.themes[Theme.light]
    with pytest.raises(ValidationError):
        palette.fit_score = 1.0  # type: ignore[misc]
    assert palette.fit_score == 0.82

    entry = palette.usage.mapping[UsageCategory.surface][0]
    with pytest.raises(ValidationError):
        entry.probability = 0.1  # type: ignore[misc]

    usage_palette = palette.usage
    with pytest.raises(ValidationError):
        usage_palette.mapping = {}  # type: ignore[misc]

    candidate = palette.roles.mapping[PaletteRole.accent][0]
    with pytest.raises(ValidationError):
        candidate.probability = 0.1  # type: ignore[misc]

    role_results = palette.roles
    with pytest.raises(ValidationError):
        role_results.mapping = {}  # type: ignore[misc]

    assert palette.tokens is not None
    token = palette.tokens[0]
    with pytest.raises(ValidationError):
        token.name = "--other"  # type: ignore[misc]


def test_output_sequence_fields_are_tuples_not_appendable() -> None:
    # Sequence fields are tuples, so in-place mutation (``.append``) is impossible: a tuple
    # has no ``append``/``extend``, so the attempt raises AttributeError, not a silent edit.
    result = _dummy_result(tokens=(_design_token(),))
    assert isinstance(result.third_party_colors, tuple)
    palette = result.themes[Theme.light]
    assert isinstance(palette.divergence, tuple)
    with pytest.raises(AttributeError):
        palette.divergence.extend([])  # type: ignore[attr-defined]
    assert isinstance(palette.tokens, tuple)
    with pytest.raises(AttributeError):
        palette.tokens.append(palette.tokens[0])  # type: ignore[attr-defined]

    entries = palette.usage.mapping[UsageCategory.surface]
    assert isinstance(entries, tuple)
    with pytest.raises(AttributeError):
        entries.append(entries[0])  # type: ignore[attr-defined]

    candidates = palette.roles.mapping[PaletteRole.accent]
    assert isinstance(candidates, tuple)
    with pytest.raises(AttributeError):
        candidates.append(candidates[0])  # type: ignore[attr-defined]


def test_usage_palette_backfills_all_categories() -> None:
    # The after-validator guarantees every UsageCategory key, mapping to () when absent —
    # even for the bare constructor and a partially-populated mapping.
    empty = UsagePalette()
    assert set(empty.mapping) == set(UsageCategory)
    assert all(entries == () for entries in empty.mapping.values())

    partial = UsagePalette(
        mapping={
            UsageCategory.text: (UsageEntry(color=_color(), probability=1.0, area=0.0),),
        }
    )
    assert set(partial.mapping) == set(UsageCategory)
    assert partial.mapping[UsageCategory.text] != ()
    assert partial.mapping[UsageCategory.surface] == ()
    assert partial.mapping[UsageCategory.interactive] == ()
    assert partial.mapping[UsageCategory.border] == ()


def test_theme_palette_tokens_none_vs_empty() -> None:
    # None = tokens not requested (include_tokens=False); () = requested but none found.
    not_requested = _dummy_result(tokens=None).themes[Theme.light]
    assert not_requested.tokens is None

    requested_but_none = _dummy_result(tokens=()).themes[Theme.light]
    assert requested_but_none.tokens == ()
    assert requested_but_none.tokens is not None

    # The distinction survives a JSON round-trip.
    for original in (not_requested, requested_but_none):
        restored = ThemePalette.model_validate_json(original.model_dump_json())
        assert restored.tokens == original.tokens


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
        has_box_shadow=True,
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
    assert el.has_box_shadow is True
    # ``has_box_shadow`` defaults to False (mirroring the harvest-time default of
    # ``has_hover_color_change``) so pre-existing constructions stay valid.
    assert el.model_copy(update={"has_box_shadow": False}).has_box_shadow is False
    assert HarvestedElement.model_fields["has_box_shadow"].default is False
    sbin = ScreenshotBin(color=_color("#ffffff", 0.99), area_fraction=0.6)
    assert 0.0 <= sbin.area_fraction <= 1.0
    cluster = ColorCluster(
        color=_color(),
        area_weight=0.3,
        member_count=4,
        component_mix={ComponentType.card_bg: 1.0},
        component_mass={ComponentType.card_bg: 7.5},
    )
    assert cluster.member_count == 4
    assert cluster.component_mass == {ComponentType.card_bg: 7.5}


def test_internal_classified_token_carries_origin_and_usage_prior() -> None:
    token = ClassifiedToken(
        record=TokenRecord(
            name="--color-primary",
            raw_value="#3366cc",
            resolved=_color(),
            scope=":root",
        ),
        semantic_role=TokenSemanticRole.brand_primary,
        weight=5.0,
        usage_prior={UsageCategory.interactive: 0.5, UsageCategory.surface: 0.5},
        origin=TokenOrigin.name_rule,
    )
    assert token.origin is TokenOrigin.name_rule
    assert sum(token.usage_prior.values()) == 1.0
    # Origin defaults to fallback when unspecified.
    assert ClassifiedToken.model_fields["origin"].default is TokenOrigin.fallback


def test_analysis_result_json_round_trip() -> None:
    original = _dummy_result(tokens=(_design_token(),))
    payload = original.model_dump_json()
    restored = AnalysisResult.model_validate_json(payload)

    assert restored == original
    # Enum-keyed dicts survive the round trip.
    assert Theme.light in restored.themes
    palette = restored.themes[Theme.light]
    assert UsageCategory.surface in palette.usage.mapping
    surface_entry = palette.usage.mapping[UsageCategory.surface][0]
    assert surface_entry.color.hex == "#ffffff"
    assert surface_entry.components[ComponentType.page_bg] == 1.0
    assert palette.roles.mapping[PaletteRole.accent][0].color.hex == "#3366cc"
    assert palette.tokens is not None
    assert palette.tokens[0].semantic_role is TokenSemanticRole.brand_primary
    assert palette.divergence[0].category is UsageCategory.surface
    assert restored.metadata.user_agent == "colorsense"
    assert restored.metadata.themes_requested == (Theme.light, Theme.dark)
    # Sequence fields round-trip as tuples (typed ``tuple[X, ...]``), not lists.
    assert isinstance(restored.third_party_colors, tuple)
    assert isinstance(palette.divergence, tuple)
    assert isinstance(palette.tokens, tuple)
    assert isinstance(restored.metadata.themes_requested, tuple)
    assert isinstance(palette.usage.mapping[UsageCategory.surface], tuple)
    assert isinstance(palette.roles.mapping[PaletteRole.accent], tuple)


def test_public_api_exports() -> None:
    # The usage redesign's public surface: new names exported, internals removed.
    import colorsense

    for name in ("UsageCategory", "UsageEntry", "UsagePalette", "DesignToken", "ComponentType"):
        assert name in colorsense.__all__, name
        assert hasattr(colorsense, name)
    for name in ("ClassifiedToken", "TokenRecord"):
        assert name not in colorsense.__all__, name
