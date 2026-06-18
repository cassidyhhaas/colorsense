"""Contract tests: construction and JSON round-trip of the shared models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from colorsense.models import (
    AnalysisResult,
    BoundingBox,
    ClassifiedToken,
    Color,
    ColorCluster,
    ColorUsage,
    ComponentType,
    DesignToken,
    DivergenceItem,
    HarvestedElement,
    PropertyFamily,
    RunMetadata,
    ScreenshotBin,
    Theme,
    ThemePalette,
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    Usage,
    UsageEntry,
    UsagePalette,
    UsageRole,
    Viewport,
    is_pill_shape,
)
from colorsense.palette.usage import USAGE_ROLE_BY_COMPONENT_TYPE


def _color(hex_: str = "#3366cc", lum: float = 0.5) -> Color:
    return Color(hex=hex_, lightness=lum, chroma=0.12, hue=260.0, alpha=1.0)


def _dummy_result(*, tokens: tuple[DesignToken, ...] | None = None) -> AnalysisResult:
    viewport = Viewport(width=1280, height=800, device_scale_factor=1.0)
    brand = _color("#3366cc", 0.55)
    white = _color("#ffffff", 0.99)
    dark = _color("#111111", 0.1)

    usage = UsagePalette(
        mapping={
            UsageRole.PAGE: (
                UsageEntry(
                    color=white,
                    probability=0.8,
                    area=0.6,
                    components={ComponentType.PAGE_BG: 1.0},
                ),
            ),
            UsageRole.CTA: (
                UsageEntry(
                    color=brand,
                    probability=0.7,
                    area=0.05,
                    components={ComponentType.CTA_BG: 1.0},
                ),
            ),
        }
    )
    colors = (
        ColorUsage(
            color=white,
            prominence=0.9,
            area=0.6,
            usages=(
                Usage(
                    role=UsageRole.PAGE,
                    property_family=PropertyFamily.BACKGROUND,
                    weight=1.0,
                    components={ComponentType.PAGE_BG: 1.0},
                ),
            ),
        ),
        ColorUsage(
            color=brand,
            prominence=0.3,
            area=0.05,
            usages=(
                Usage(
                    role=UsageRole.CTA,
                    property_family=PropertyFamily.BACKGROUND,
                    weight=0.7,
                    components={ComponentType.CTA_BG: 1.0},
                ),
                Usage(
                    role=UsageRole.LINK,
                    property_family=PropertyFamily.TEXT,
                    weight=0.3,
                    components={ComponentType.LINK: 1.0},
                ),
            ),
        ),
    )
    theme_palette = ThemePalette(
        theme=Theme.LIGHT,
        colors=colors,
        usage=usage,
        divergence=(DivergenceItem(role=UsageRole.PAGE, color=dark, note="declared but unused"),),
        tokens=tokens,
    )

    return AnalysisResult(
        url="https://example.com",
        viewport=viewport,
        themes={Theme.LIGHT: theme_palette},
        third_party_colors=(_color("#00ff00", 0.8),),
        metadata=RunMetadata(
            themes_requested=(Theme.LIGHT, Theme.DARK),
            themes_analyzed=(Theme.LIGHT,),
            user_agent="colorsense",
            respect_robots=True,
        ),
    )


def _design_token() -> DesignToken:
    return DesignToken(
        name="--color-primary",
        color=_color("#3366cc", 0.55),
        semantic_role=TokenSemanticRole.BRAND_PRIMARY,
    )


def test_usage_role_property_family_maps_every_role() -> None:
    # UsageRole.property_family is total over UsageRole and matches the documented rollup.
    assert UsageRole.TEXT.property_family is PropertyFamily.TEXT
    assert UsageRole.LINK.property_family is PropertyFamily.TEXT
    assert UsageRole.BORDER.property_family is PropertyFamily.BORDER
    for role in (
        UsageRole.PAGE,
        UsageRole.SURFACE,
        UsageRole.BANNER,
        UsageRole.CTA,
        UsageRole.ACTION,
    ):
        assert role.property_family is PropertyFamily.BACKGROUND
    # Total: defined for every role.
    assert {r.property_family for r in UsageRole} <= set(PropertyFamily)


def test_component_type_property_family_matches_routing() -> None:
    # ComponentType.property_family follows the fixed routing convention:
    # *_text plus link -> text, border -> border, everything else -> background.
    assert ComponentType.CTA_TEXT.property_family is PropertyFamily.TEXT
    assert ComponentType.LINK.property_family is PropertyFamily.TEXT
    assert ComponentType.BORDER.property_family is PropertyFamily.BORDER
    assert ComponentType.CTA_BG.property_family is PropertyFamily.BACKGROUND
    assert ComponentType.BADGE.property_family is PropertyFamily.BACKGROUND
    assert ComponentType.BUTTON_SECONDARY.property_family is PropertyFamily.BACKGROUND
    # Total: defined for every component type.
    assert {c.property_family for c in ComponentType} <= set(PropertyFamily)


def test_component_and_role_property_family_agree() -> None:
    # The latent coherence that justifies the unified type: every ComponentType that routes to
    # a UsageRole routes to one in the SAME PropertyFamily, so the component-side and role-side
    # rollups never disagree about which CSS property paints a color. (``cta_text`` and
    # ``third_party`` are deliberately unrouted — they key no usage role — so they are absent
    # from USAGE_ROLE_BY_COMPONENT_TYPE and have no role-side family to agree with.)
    for component, role in USAGE_ROLE_BY_COMPONENT_TYPE.items():
        assert component.property_family is role.property_family


def test_is_pill_shape() -> None:
    # Wide, fully-rounded stadium: min_corner_radius >= height/2 and width > height -> pill.
    assert is_pill_shape(width=120.0, height=40.0, min_corner_radius=20.0) is True
    # A circle (width == height) is excluded even when fully rounded.
    assert is_pill_shape(width=40.0, height=40.0, min_corner_radius=20.0) is False
    # Non-positive height -> not a shape at all.
    assert is_pill_shape(width=120.0, height=0.0, min_corner_radius=20.0) is False
    # Only partly rounded (radius below height/2) -> not a pill (a card/tab).
    assert is_pill_shape(width=120.0, height=40.0, min_corner_radius=19.9) is False
    # Tall box (width < height), fully rounded -> not a pill (not elongated horizontally).
    assert is_pill_shape(width=40.0, height=120.0, min_corner_radius=60.0) is False


def test_value_objects_are_frozen() -> None:
    # Each value object is frozen: assigning to a field must raise pydantic's ValidationError
    # (specifically a frozen-instance error), not merely "something raised".
    color = _color()
    with pytest.raises(ValidationError):
        color.lightness = 0.9  # type: ignore[misc]
    assert color.lightness == 0.5  # value unchanged

    box = BoundingBox(x=1.0, y=2.0, width=3.0, height=4.0)
    with pytest.raises(ValidationError):
        box.width = 99.0  # type: ignore[misc]
    assert box.width == 3.0

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

    palette = result.themes[Theme.LIGHT]
    color_usage = palette.colors[0]
    with pytest.raises(ValidationError):
        color_usage.prominence = 0.1  # type: ignore[misc]
    usage_slot = color_usage.usages[0]
    with pytest.raises(ValidationError):
        usage_slot.weight = 0.1  # type: ignore[misc]

    entry = palette.usage.mapping[UsageRole.PAGE][0]
    with pytest.raises(ValidationError):
        entry.probability = 0.1  # type: ignore[misc]

    usage_palette = palette.usage
    with pytest.raises(ValidationError):
        usage_palette.mapping = {}  # type: ignore[misc]

    assert palette.tokens is not None
    token = palette.tokens[0]
    with pytest.raises(ValidationError):
        token.name = "--other"  # type: ignore[misc]


def test_output_sequence_fields_are_tuples_not_appendable() -> None:
    # Sequence fields are tuples, so in-place mutation (``.append``) is impossible: a tuple
    # has no ``append``/``extend``, so the attempt raises AttributeError, not a silent edit.
    result = _dummy_result(tokens=(_design_token(),))
    assert isinstance(result.third_party_colors, tuple)
    palette = result.themes[Theme.LIGHT]
    assert isinstance(palette.divergence, tuple)
    with pytest.raises(AttributeError):
        palette.divergence.extend([])  # type: ignore[attr-defined]
    assert isinstance(palette.tokens, tuple)
    with pytest.raises(AttributeError):
        palette.tokens.append(palette.tokens[0])  # type: ignore[attr-defined]

    assert isinstance(palette.colors, tuple)
    with pytest.raises(AttributeError):
        palette.colors.append(palette.colors[0])  # type: ignore[attr-defined]
    assert isinstance(palette.colors[0].usages, tuple)

    entries = palette.usage.mapping[UsageRole.PAGE]
    assert isinstance(entries, tuple)
    with pytest.raises(AttributeError):
        entries.append(entries[0])  # type: ignore[attr-defined]


def test_usage_palette_backfills_all_roles() -> None:
    # The after-validator guarantees every UsageRole key, mapping to () when absent —
    # even for the bare constructor and a partially-populated mapping.
    empty = UsagePalette()
    assert set(empty.mapping) == set(UsageRole)
    assert all(entries == () for entries in empty.mapping.values())

    partial = UsagePalette(
        mapping={
            UsageRole.TEXT: (UsageEntry(color=_color(), probability=1.0, area=0.0),),
        }
    )
    assert set(partial.mapping) == set(UsageRole)
    assert partial.mapping[UsageRole.TEXT] != ()
    for role in UsageRole:
        if role is not UsageRole.TEXT:
            assert partial.mapping[role] == ()


def test_theme_palette_tokens_none_vs_empty() -> None:
    # None = tokens not requested (include_tokens=False); () = requested but none found.
    not_requested = _dummy_result(tokens=None).themes[Theme.LIGHT]
    assert not_requested.tokens is None

    requested_but_none = _dummy_result(tokens=()).themes[Theme.LIGHT]
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
        bounding_box=BoundingBox(x=10, y=20, width=120, height=40),
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
        component_mix={ComponentType.CARD_BG: 1.0},
        component_mass={ComponentType.CARD_BG: 7.5},
    )
    assert cluster.member_count == 4
    assert cluster.component_mass == {ComponentType.CARD_BG: 7.5}


def test_internal_classified_token_carries_origin_and_usage_intent() -> None:
    token = ClassifiedToken(
        record=TokenRecord(
            name="--color-primary",
            raw_value="#3366cc",
            resolved=_color(),
            scope=":root",
        ),
        semantic_role=TokenSemanticRole.BRAND_PRIMARY,
        weight=5.0,
        usage_intent={UsageRole.CTA: 0.5, UsageRole.SURFACE: 0.5},
        origin=TokenOrigin.NAME_RULE,
    )
    assert token.origin is TokenOrigin.NAME_RULE
    assert sum(token.usage_intent.values()) == 1.0
    # Origin defaults to fallback when unspecified.
    assert ClassifiedToken.model_fields["origin"].default is TokenOrigin.FALLBACK


def test_analysis_result_json_round_trip() -> None:
    original = _dummy_result(tokens=(_design_token(),))
    payload = original.model_dump_json()
    restored = AnalysisResult.model_validate_json(payload)

    assert restored == original
    # Enum-keyed dicts survive the round trip.
    assert Theme.LIGHT in restored.themes
    palette = restored.themes[Theme.LIGHT]
    assert UsageRole.PAGE in palette.usage.mapping
    page_entry = palette.usage.mapping[UsageRole.PAGE][0]
    assert page_entry.color.hex == "#ffffff"
    assert page_entry.components[ComponentType.PAGE_BG] == 1.0
    # Color-keyed index round-trips, including nested Usage slots and their family.
    assert palette.colors[0].color.hex == "#ffffff"
    brand_color = palette.colors[1]
    assert brand_color.usages[0].role is UsageRole.CTA
    assert brand_color.usages[1].property_family is PropertyFamily.TEXT
    assert palette.tokens is not None
    assert palette.tokens[0].semantic_role is TokenSemanticRole.BRAND_PRIMARY
    assert palette.divergence[0].role is UsageRole.PAGE
    assert restored.metadata.user_agent == "colorsense"
    assert restored.metadata.themes_requested == (Theme.LIGHT, Theme.DARK)
    # Sequence fields round-trip as tuples (typed ``tuple[X, ...]``), not lists.
    assert isinstance(restored.third_party_colors, tuple)
    assert isinstance(palette.divergence, tuple)
    assert isinstance(palette.tokens, tuple)
    assert isinstance(restored.metadata.themes_requested, tuple)
    assert isinstance(palette.colors, tuple)
    assert isinstance(palette.colors[1].usages, tuple)
    assert isinstance(palette.usage.mapping[UsageRole.PAGE], tuple)


def test_public_api_exports() -> None:
    # The usage-role redesign's public surface: new names exported, old/internals removed.
    import colorsense

    for name in (
        "UsageRole",
        "PropertyFamily",
        "Usage",
        "ColorUsage",
        "UsageEntry",
        "UsagePalette",
        "DesignToken",
        "ComponentType",
    ):
        assert name in colorsense.__all__, name
        assert hasattr(colorsense, name)
    # The 60/30/10 view and its taxonomy were removed along with the legacy internals.
    for name in (
        "UsageCategory",
        "RoleResults",
        "Composition",
        "PaletteRole",
        "PaletteCandidate",
        "ClassifiedToken",
        "TokenRecord",
    ):
        assert name not in colorsense.__all__, name
        assert not hasattr(colorsense, name)
