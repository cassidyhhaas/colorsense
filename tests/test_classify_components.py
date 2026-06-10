"""Unit tests for the rule-based component classifier."""

from __future__ import annotations

import math

import pytest

from colorsense.classify.components import _finalize_distribution, classify_components
from colorsense.config import load_default_config
from colorsense.models import (
    Color,
    ComponentType,
    HarvestedElement,
    Rect,
    Viewport,
)

CONFIG = load_default_config()

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


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
    has_box_shadow: bool = False,
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
        rect=rect if rect is not None else Rect(x=0.0, y=0.0, width=100.0, height=100.0),
        position=position,
        bg=bg,
        text=text,
        border=border,
        has_box_shadow=has_box_shadow,
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
        rect=Rect(x=0.0, y=0.0, width=1280.0, height=80.0),
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
            rect=Rect(x=float(i * 200), y=300.0, width=180.0, height=180.0),
        )
        for i in range(4)
    ]
    results = classify_components(cards, CONFIG, VIEWPORT)
    assert len(results) == 4
    for result in results:
        assert ComponentType.card_bg in result.component_dist
        # card_bg should be dominant / high (class token + repetition).
        assert result.component_dist[ComponentType.card_bg] >= 0.5


def test_repeated_plain_siblings_get_no_repetition_votes() -> None:
    """A repeated group with no border, box shadow, or background fails ``requires_any``.

    Regression: ungated ``borderTopColor`` used to make ``border`` non-None on every
    element, so any >=3 same-tag/shared-class group (e.g. plain ``<li>`` items) vacuously
    passed the repetition gate and received card votes.
    """
    items = [
        _element(
            tag="li",
            class_tokens=["item"],
            rect=Rect(x=0.0, y=float(100 + i * 30), width=300.0, height=24.0),
        )
        for i in range(4)
    ]
    results = classify_components(items, CONFIG, VIEWPORT)
    for result in results:
        assert ComponentType.card_bg not in result.component_dist


def test_repeated_box_shadow_siblings_get_repetition_votes() -> None:
    """The same plain group qualifies once each member carries a real box shadow."""
    items = [
        _element(
            tag="li",
            class_tokens=["item"],
            has_box_shadow=True,
            rect=Rect(x=0.0, y=float(100 + i * 30), width=300.0, height=24.0),
        )
        for i in range(4)
    ]
    results = classify_components(items, CONFIG, VIEWPORT)
    for result in results:
        assert ComponentType.card_bg in result.component_dist


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
        rect=Rect(x=0.0, y=200.0, width=1200.0, height=600.0),
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
    header = _element(tag="header", rect=Rect(x=0.0, y=0.0, width=1280.0, height=80.0))
    anchor = _element(tag="a", clickable=True)
    hidden = _element(tag="header", aria_hidden=True)

    results = classify_components([header, anchor, hidden], CONFIG, VIEWPORT)
    header_res, anchor_res, hidden_res = results

    for res in (header_res, anchor_res):
        assert res.component_dist
        assert abs(sum(res.component_dist.values()) - 1.0) < 1e-6

    assert hidden_res.component_dist == {}


# ---------------------------------------------------------------------------
# Geometry rules (thresholds from the default config, against the 1280x800
# viewport: top_band y<120, bottom_band y>=680, full_width w>=1152,
# hero_min_h h>=280, sticky_top_px 80, small_area w*h<=20480).
# ---------------------------------------------------------------------------


def test_hero_rule_full_width_tall_top_block() -> None:
    """A full-width tall block in the top band gets hero_bg via geometry alone."""
    hero = _element(rect=Rect(x=0.0, y=0.0, width=1280.0, height=400.0))
    [result] = classify_components([hero], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.hero_bg


def test_hero_rule_negative_just_under_min_height() -> None:
    """One px under hero_min_h (280px at 800px viewport) the hero rule must not fire."""
    not_hero = _element(rect=Rect(x=0.0, y=0.0, width=1280.0, height=279.0))
    [result] = classify_components([not_hero], CONFIG, VIEWPORT)
    assert ComponentType.hero_bg not in result.component_dist


def test_footer_rule_full_width_bottom_band() -> None:
    """A full-width block at/below the bottom band (y>=680) gets footer_bg."""
    footer = _element(rect=Rect(x=0.0, y=680.0, width=1280.0, height=120.0))
    [result] = classify_components([footer], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.footer_bg


def test_footer_rule_negative_just_above_bottom_band() -> None:
    """One px above the bottom band the footer rule must not fire."""
    not_footer = _element(rect=Rect(x=0.0, y=679.0, width=1280.0, height=120.0))
    [result] = classify_components([not_footer], CONFIG, VIEWPORT)
    assert ComponentType.footer_bg not in result.component_dist


def test_fixed_sticky_rule_near_top() -> None:
    """A fixed/sticky element above sticky_top_px (80) gets nav_bg via geometry."""
    for position in ("fixed", "sticky"):
        bar = _element(position=position, rect=Rect(x=0.0, y=79.0, width=200.0, height=50.0))
        [result] = classify_components([bar], CONFIG, VIEWPORT)
        assert result.component_dist, position
        assert _argmax(result.component_dist) is ComponentType.nav_bg, position


def test_fixed_sticky_rule_negative_at_threshold() -> None:
    """At exactly sticky_top_px (y=80, not <80) the fixed/sticky rule must not fire."""
    bar = _element(position="fixed", rect=Rect(x=0.0, y=80.0, width=200.0, height=50.0))
    [result] = classify_components([bar], CONFIG, VIEWPORT)
    assert ComponentType.nav_bg not in result.component_dist


def test_small_clickable_rule_votes_link() -> None:
    """A small clickable (area exactly at small_area: 160*128=20480) is link-dominant.

    The small-area rule adds link votes on top of the generic clickable votes,
    flipping the argmax from cta_bg to link.
    """
    chip = _element(clickable=True, rect=Rect(x=100.0, y=300.0, width=160.0, height=128.0))
    [result] = classify_components([chip], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.link


def test_small_clickable_rule_negative_just_over_area() -> None:
    """Just over small_area (160*129>20480) only the clickable votes remain (cta_bg wins)."""
    block = _element(clickable=True, rect=Rect(x=100.0, y=300.0, width=160.0, height=129.0))
    [result] = classify_components([block], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.cta_bg


# ---------------------------------------------------------------------------
# Third-party vote paths and the third_party_present suppressor.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["cross_origin", "shadow_host", "vendor_match"])
def test_third_party_flags_each_vote_third_party(flag: str) -> None:
    """cross_origin / shadow_host / vendor_match each contribute third_party votes."""
    element = _element(**{flag: True})  # type: ignore[arg-type]
    [result] = classify_components([element], CONFIG, VIEWPORT)
    assert result.component_dist == {ComponentType.third_party: 1.0}


@pytest.mark.parametrize("flag", ["cross_origin", "shadow_host", "vendor_match"])
def test_third_party_flags_each_suppress_brand_components(flag: str) -> None:
    """Each third-party flag triggers the brand-component damping suppressor.

    Without the suppressor the .card class votes (3.0) would beat shadow_host's
    third_party votes (2.5), so a damping regression flips the argmax.
    """
    element = _element(
        tag="div",
        class_tokens=["card"],
        border=_color("#cccccc"),
        bg=_color("#ffffff"),
        **{flag: True},  # type: ignore[arg-type]
    )
    [result] = classify_components([element], CONFIG, VIEWPORT)
    assert result.component_dist, flag
    assert _argmax(result.component_dist) is ComponentType.third_party, flag
    card_bg = result.component_dist.get(ComponentType.card_bg, 0.0)
    assert card_bg < result.component_dist[ComponentType.third_party], flag


# ---------------------------------------------------------------------------
# zero_area_or_hidden suppressor.
# ---------------------------------------------------------------------------


def test_invisible_element_yields_empty_distribution() -> None:
    """visible=False zeroes the distribution even when strong rules match.

    The harvester filters invisible elements, but the classifier must still
    suppress them (defense in depth via the zero_area_or_hidden suppressor).
    """
    invisible = _element(
        tag="header",
        rect=Rect(x=0.0, y=0.0, width=1280.0, height=80.0),
        visible=False,
    )
    [result] = classify_components([invisible], CONFIG, VIEWPORT)
    assert result.component_dist == {}


# ---------------------------------------------------------------------------
# Semantic-tag and interactivity predicates.
# ---------------------------------------------------------------------------


def test_role_banner_is_header_bg() -> None:
    """role=banner matches the role= semantic rule (header_bg-dominant)."""
    banner = _element(tag="div", role="banner")
    [result] = classify_components([banner], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.header_bg


def test_input_submit_semantic_rule() -> None:
    """An <input> matches both input[submit] (cta_bg) and the bare input rule."""
    submit = _element(tag="input", rect=Rect(x=100.0, y=300.0, width=300.0, height=60.0))
    [result] = classify_components([submit], CONFIG, VIEWPORT)
    assert _argmax(result.component_dist) is ComponentType.cta_bg
    assert ComponentType.input_bg in result.component_dist


def test_hover_color_change_votes_cta() -> None:
    """has_hover_color_change alone contributes cta_bg votes."""
    hover = _element(has_hover_color_change=True)
    [result] = classify_components([hover], CONFIG, VIEWPORT)
    assert result.component_dist == {ComponentType.cta_bg: 1.0}


def test_input_submit_button_interactivity_outvotes_nav_class() -> None:
    """The input[submit|button] votes flip the argmax for a clickable button.

    The .navbar class contributes nav_bg 5.5 (navbar 3.0 + substring nav 2.5);
    semantic button (3.5) + clickable (1.5) reach only cta_bg 5.0 — the extra
    input[submit|button] votes (2.0) push cta_bg to 7.0. A regression in that
    predicate makes nav_bg the argmax.
    """
    button = _element(
        tag="button",
        clickable=True,
        class_tokens=["navbar"],
        rect=Rect(x=100.0, y=300.0, width=400.0, height=60.0),
    )
    [result] = classify_components([button], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.cta_bg


# ---------------------------------------------------------------------------
# Class-token matching against the element id.
# ---------------------------------------------------------------------------


def test_class_token_rules_match_element_id() -> None:
    """An element with no classes but a 'navbar' id matches class-token rules."""
    bar = _element(
        tag="div",
        element_id="main-navbar",
        rect=Rect(x=100.0, y=300.0, width=400.0, height=60.0),
    )
    [result] = classify_components([bar], CONFIG, VIEWPORT)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.nav_bg


# ---------------------------------------------------------------------------
# _finalize_distribution prune-to-argmax fallback.
# ---------------------------------------------------------------------------


def test_finalize_distribution_prune_fallback_keeps_single_argmax() -> None:
    """When pruning removes every component, the single argmax survives at 1.0.

    Tested directly: engineering 21 sub-threshold softmax probabilities through
    the public rule set is impractical. Equal votes on all components plus a
    nudge on cta_bg put every probability below min_component_prob (verified as
    a precondition so a config change cannot make this vacuous).
    """
    cc = CONFIG.component_classifier
    accum = dict.fromkeys(ComponentType, 1.0)
    accum[ComponentType.cta_bg] = 1.02

    # Precondition: the raw softmax leaves everything below the prune threshold.
    exps = {comp: math.exp(vote / cc.softmax_temperature) for comp, vote in accum.items()}
    total = sum(exps.values())
    assert max(exps.values()) / total < cc.min_component_prob

    assert _finalize_distribution(accum, CONFIG) == {ComponentType.cta_bg: 1.0}


def test_no_viewport_uses_default() -> None:
    """Calling without a viewport still computes geometry-driven results."""
    header = _element(tag="header", rect=Rect(x=0.0, y=0.0, width=1280.0, height=80.0))
    [result] = classify_components([header], CONFIG)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.header_bg
