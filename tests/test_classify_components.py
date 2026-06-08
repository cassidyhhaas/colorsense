"""Unit tests for the rule-based component classifier."""

from __future__ import annotations

from colorsense.classify.components import classify_components
from colorsense.config import load_default_config
from colorsense.models import (
    Color,
    ComponentType,
    HarvestedElement,
    Rect,
    Viewport,
)

CONFIG = load_default_config()

VIEWPORT = Viewport(w=1280, h=800, device_scale_factor=1.0)


def _color(hex_value: str = "#123456") -> Color:
    """Build a minimal Color for fixtures (OKLCH coords are irrelevant here)."""
    return Color(hex=hex_value, lightness=0.5, chroma=0.1, hue=200.0)


def _element(
    *,
    tag: str = "div",
    role: str | None = None,
    element_id: str | None = None,
    class_tokens: list[str] | None = None,
    rect: Rect | None = None,
    position: str = "static",
    bg: Color | None = None,
    text: Color | None = None,
    border: Color | None = None,
    is_iframe: bool = False,
    cross_origin: bool = False,
    shadow_host: bool = False,
    clickable: bool = False,
    has_hover_color_change: bool = False,
    hover_bg: Color | None = None,
    vendor_match: bool = False,
    visible: bool = True,
    aria_hidden: bool = False,
) -> HarvestedElement:
    """Build a HarvestedElement with sensible defaults for classifier tests."""
    return HarvestedElement(
        tag=tag,
        role=role,
        id=element_id,
        class_tokens=class_tokens if class_tokens is not None else [],
        rect=rect if rect is not None else Rect(x=0.0, y=0.0, w=100.0, h=100.0),
        position=position,
        bg=bg,
        text=text,
        border=border,
        is_iframe=is_iframe,
        cross_origin=cross_origin,
        shadow_host=shadow_host,
        clickable=clickable,
        has_hover_color_change=has_hover_color_change,
        hover_bg=hover_bg,
        vendor_match=vendor_match,
        visible=visible,
        aria_hidden=aria_hidden,
    )


def _argmax(dist: dict[ComponentType, float]) -> ComponentType:
    return max(dist, key=lambda comp: dist[comp])


def test_header_top_bar_is_argmax_header_bg() -> None:
    """A full-width short top-bar <header> classifies dominantly as header_bg."""
    header = _element(
        tag="header",
        rect=Rect(x=0.0, y=0.0, w=1280.0, h=80.0),
    )
    [result] = classify_components([header], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.header_bg


def test_four_card_siblings_get_card_bg_via_repetition() -> None:
    """Four structurally-similar .card siblings each receive card_bg votes."""
    cards = [
        _element(
            tag="div",
            class_tokens=["card"],
            border=_color("#cccccc"),
            bg=_color("#ffffff"),
            rect=Rect(x=float(i * 200), y=300.0, w=180.0, h=180.0),
        )
        for i in range(4)
    ]
    results = classify_components(cards, CONFIG, VIEWPORT)
    assert len(results) == 4
    for result in results:
        assert ComponentType.card_bg in result.component_dist
        # card_bg should be dominant / high (class token + repetition).
        assert result.component_dist[ComponentType.card_bg] >= 0.5


def test_anchor_is_dominant_link() -> None:
    """An <a> element classifies dominantly as link."""
    anchor = _element(tag="a", clickable=True)
    [result] = classify_components([anchor], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.link


def test_iframe_is_third_party_and_suppresses_brand_components() -> None:
    """An iframe is third_party-dominant; brand components are heavily damped."""
    iframe = _element(
        tag="div",
        class_tokens=["card"],
        border=_color("#cccccc"),
        bg=_color("#ffffff"),
        is_iframe=True,
    )
    [result] = classify_components([iframe], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.third_party
    card_bg = result.component_dist.get(ComponentType.card_bg, 0.0)
    third_party = result.component_dist[ComponentType.third_party]
    assert card_bg < third_party


def test_container_wrapper_has_no_confident_brand_component() -> None:
    """A layout .container wrapper yields no confident brand component."""
    container = _element(
        tag="div",
        class_tokens=["container"],
        rect=Rect(x=0.0, y=200.0, w=1200.0, h=600.0),
    )
    [result] = classify_components([container], CONFIG, VIEWPORT)
    # Only the near-zero page_bg noise vote contributes; no brand component
    # should reach a confident probability.
    brand_components = {
        ComponentType(raw)
        for raw in CONFIG.component_classifier.brand_components
        if raw != "page_bg"
    }
    for comp in brand_components:
        assert result.component_dist.get(comp, 0.0) < 0.5


def test_distributions_sum_to_one_and_aria_hidden_is_empty() -> None:
    """Non-empty distributions sum to ~1.0; aria_hidden yields an empty dist."""
    header = _element(tag="header", rect=Rect(x=0.0, y=0.0, w=1280.0, h=80.0))
    anchor = _element(tag="a", clickable=True)
    hidden = _element(tag="header", aria_hidden=True)

    results = classify_components([header, anchor, hidden], CONFIG, VIEWPORT)
    header_res, anchor_res, hidden_res = results

    for res in (header_res, anchor_res):
        assert res.component_dist
        assert abs(sum(res.component_dist.values()) - 1.0) < 1e-6

    assert hidden_res.component_dist == {}


def test_no_viewport_uses_default() -> None:
    """Calling without a viewport still computes geometry-driven results."""
    header = _element(tag="header", rect=Rect(x=0.0, y=0.0, w=1280.0, h=80.0))
    [result] = classify_components([header], CONFIG)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.header_bg
