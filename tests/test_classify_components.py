"""Unit tests for the rule-based component classifier."""

from __future__ import annotations

import math

import pytest

from colorsense.classify.components import (
    _finalize_distribution,
    _matches_interactivity,
    _matches_semantic_tag,
    classify_components,
)
from colorsense.config import VoteRule, WhenRule, load_default_config
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
    input_type: str | None = None,
    min_corner_radius: float = 0.0,
    bg_gradient_stops: tuple[Color, ...] = (),
    has_box_shadow: bool = False,
    has_text: bool = False,
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
        input_type=input_type,
        min_corner_radius=min_corner_radius,
        bg_gradient_stops=bg_gradient_stops,
        has_box_shadow=has_box_shadow,
        has_text=has_text,
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


def test_pill_chip_is_badge_not_card() -> None:
    """Regression (disconetwork.com): a fully-rounded, short, text-bearing chip is a badge.

    The site's status/category chips (``inline-flex rounded-full bg-success/10 ring-1``)
    are tiny pills that repeat in grids — they used to satisfy the repetition card
    detector (ring -> box_shadow, distinct bg) and flood ``card_bg`` with their accent
    colors. A pill (all four corners rounded via the min test, wider than tall) that
    paints a fill, carries text, and is a single text-line tall is routed to ``badge``
    (which maps to the accent / interactive palette).
    """
    chips = [
        _element(
            tag="span",
            class_tokens=["inline-flex", "rounded-full", "badge-chip"],
            bg=_color("#10b77f"),
            has_box_shadow=True,  # the ring
            has_text=True,
            min_corner_radius=9999.0,  # rounded-full, all four corners
            rect=Rect(x=float(i * 140), y=400.0, width=130.0, height=28.0),
        )
        for i in range(4)
    ]
    results = classify_components(chips, CONFIG, VIEWPORT)
    for result in results:
        assert _argmax(result.component_dist) is ComponentType.badge
        # The card detector must NOT fire on these repeated pills.
        assert ComponentType.card_bg not in result.component_dist


def test_one_corner_rounded_is_not_a_pill_or_badge() -> None:
    """A one-corner-rounded element (a tab/speech-bubble) is not a pill, so not a badge.

    This is the MAX->MIN corner-radius fix: only the top-left corner is fully rounded,
    so ``min_corner_radius`` is 0 even though the element is short, wide, painted, and
    text-bearing. The old MAX reducer would have read its one rounded corner as a pill
    and voted ``badge``; the MIN test (all four corners must be rounded) rejects it.
    """
    tab = _element(
        tag="span",
        class_tokens=["tab"],
        bg=_color("#f59e0b"),
        has_text=True,
        min_corner_radius=0.0,  # only one corner is rounded
        rect=Rect(x=10.0, y=300.0, width=120.0, height=28.0),
    )
    [result] = classify_components([tab], CONFIG, VIEWPORT)
    assert ComponentType.badge not in result.component_dist


def test_empty_pill_without_text_is_not_a_badge() -> None:
    """A fully-rounded short pill carrying no direct text is not a badge.

    The ``has_text`` gate keeps decorative chips / divider pills / switch tracks
    (``rounded-full`` with a painted fill but no text content) out of ``badge``.
    """
    track = _element(
        tag="div",
        bg=_color("#7c3bed"),
        has_text=False,
        min_corner_radius=9999.0,
        rect=Rect(x=0.0, y=200.0, width=64.0, height=24.0),
    )
    [result] = classify_components([track], CONFIG, VIEWPORT)
    assert ComponentType.badge not in result.component_dist


def test_tall_pill_is_not_a_badge_but_excluded_from_cards() -> None:
    """A tall fully-rounded pill (stat container) is not a badge, yet is still a pure pill.

    The ``h <= badge_max_h_px`` gate keeps two-line stat containers / toggles out of
    ``badge``. But shape-wise it IS a stadium, so ``_is_pill`` still excludes it from the
    repetition card detector — it never satisfies the card heuristic. With no badge vote
    and no card vote, the only votes it gets here are from text/fill presence, so it does
    not classify as either ``badge`` or ``card_bg``.
    """
    stats = [
        _element(
            tag="div",
            class_tokens=["stat"],
            bg=_color("#1f8ded"),
            border=_color("#1f8ded"),
            has_text=True,
            min_corner_radius=9999.0,  # fully rounded...
            rect=Rect(x=float(i * 200), y=300.0, width=180.0, height=44.0),  # ...but tall
        )
        for i in range(4)
    ]
    results = classify_components(stats, CONFIG, VIEWPORT)
    for result in results:
        assert ComponentType.badge not in result.component_dist
        assert ComponentType.card_bg not in result.component_dist


def test_badge_height_gate_is_inclusive_at_the_boundary() -> None:
    """The ``h <= badge_max_h_px`` gate is inclusive: a pill exactly at the cap is a badge.

    Pins the boundary (cap is 36px): a 36px pill votes ``badge``; one pixel taller does
    not. Threshold precision is tested here rather than in the browser fixture, where
    box-model/font rendering makes an exact harvested height fragile.
    """
    cap = CONFIG.component_classifier.geometry.thresholds.badge_max_h_px
    at = _element(
        tag="span",
        bg=_color("#10b77f"),
        has_text=True,
        min_corner_radius=9999.0,
        rect=Rect(x=0.0, y=300.0, width=130.0, height=cap),
    )
    over = _element(
        tag="span",
        bg=_color("#10b77f"),
        has_text=True,
        min_corner_radius=9999.0,
        rect=Rect(x=0.0, y=300.0, width=130.0, height=cap + 1.0),
    )
    [at_result] = classify_components([at], CONFIG, VIEWPORT)
    [over_result] = classify_components([over], CONFIG, VIEWPORT)
    assert _argmax(at_result.component_dist) is ComponentType.badge
    assert ComponentType.badge not in over_result.component_dist


def test_low_radius_repeated_surfaces_stay_cards() -> None:
    """A size gate is not used: square-cornered repeated surfaces remain cards.

    Guards that the pill exclusion keys on shape (radius vs height), not size — a small
    repeated card with a modest radius is still a card, and a large one obviously is.
    """
    cards = [
        _element(
            tag="div",
            class_tokens=["tile"],
            bg=_color("#ffffff"),
            border=_color("#cccccc"),
            min_corner_radius=8.0,  # rounded corners, nowhere near half the height
            rect=Rect(x=float(i * 200), y=300.0, width=180.0, height=120.0),
        )
        for i in range(4)
    ]
    results = classify_components(cards, CONFIG, VIEWPORT)
    for result in results:
        assert ComponentType.card_bg in result.component_dist
        assert ComponentType.badge not in result.component_dist


def test_circular_avatar_is_not_a_badge() -> None:
    """A fully-rounded but square element (an avatar/icon) is not a pill, so not a badge.

    The ``width > height`` leg of the pill test excludes circles: a 56x56 ``rounded-full``
    avatar reaches radius >= height/2 but is not elongated, so it gets no badge vote.
    """
    avatar = _element(
        tag="div",
        bg=_color("#1f8ded"),
        min_corner_radius=28.0,  # 50% of 56
        rect=Rect(x=10.0, y=10.0, width=56.0, height=56.0),
    )
    [result] = classify_components([avatar], CONFIG, VIEWPORT)
    assert ComponentType.badge not in result.component_dist


def test_pill_shaped_cta_stays_interactive() -> None:
    """A pill-shaped <a> CTA stays dominantly interactive, not a badge.

    The badge vote (3.0) sits below the semantic ``a`` vote, so a fully-rounded, short,
    text-bearing link keeps its interactive label even though it matches every badge gate.
    """
    cta = _element(
        tag="a",
        class_tokens=["rounded-full"],
        bg=_color("#7c3bed"),
        clickable=True,
        has_text=True,
        min_corner_radius=9999.0,
        rect=Rect(x=100.0, y=100.0, width=160.0, height=32.0),
    )
    [result] = classify_components([cta], CONFIG, VIEWPORT)
    assert _argmax(result.component_dist) is ComponentType.link


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
    """An <input type=submit> is cta_bg-dominant.

    cta_bg collects input[submit] 3.5 + clickable 1.5 + input[submit|button] 2.0 = 7.0,
    crushing the bare-input input_bg 3.0 below the prune floor — a submit button is a
    CTA, not an input-background source.
    """
    submit = _element(
        tag="input",
        input_type="submit",
        clickable=True,
        rect=Rect(x=100.0, y=300.0, width=400.0, height=60.0),
    )
    [result] = classify_components([submit], CONFIG, VIEWPORT)
    assert result.component_dist == {ComponentType.cta_bg: 1.0}


@pytest.mark.parametrize("input_type", ["text", "search", "email", "checkbox", None])
def test_non_button_input_gets_no_cta_votes(input_type: str | None) -> None:
    """A text-like (or untyped) input receives NO cta_bg votes from any family.

    Regression: input[submit] used to match EVERY <input>, so search/text inputs
    carried a spurious cta_bg 3.5 vote. A missing type attribute is NOT submit —
    the HTML default type is "text".
    """
    box = _element(
        tag="input",
        input_type=input_type,
        rect=Rect(x=100.0, y=300.0, width=300.0, height=60.0),
    )
    [result] = classify_components([box], CONFIG, VIEWPORT)
    assert result.component_dist == {ComponentType.input_bg: 1.0}


def test_clickable_text_input_gets_no_input_submit_button_vote() -> None:
    """A cursor:pointer text input must not pick up the input[submit|button] vote.

    The generic clickable votes (cta_bg 1.5, link 1.0) still apply but are crushed
    by input_bg 3.0 and pruned; with the spurious semantic 3.5 + interactivity 2.0
    votes restored, cta_bg would dominate instead.
    """
    # Rect kept above small_area so the small-clickable geometry rule stays out of frame.
    box = _element(
        tag="input",
        input_type="text",
        clickable=True,
        rect=Rect(x=100.0, y=300.0, width=400.0, height=60.0),
    )
    [result] = classify_components([box], CONFIG, VIEWPORT)
    assert result.component_dist == {ComponentType.input_bg: 1.0}
    assert ComponentType.cta_bg not in result.component_dist


# Pin the button-like membership for BOTH input predicates: the shared frozenset is
# submit/button/image/reset (all four paint as real buttons — the classifier scores
# visual roles, not form semantics); everything else, including None, is excluded.
_SEMANTIC_RULE = VoteRule(match="input[submit]", votes={"cta_bg": 3.5})
_INTERACTIVITY_RULE = WhenRule(when="input[submit|button]", votes={"cta_bg": 2.0})


@pytest.mark.parametrize(
    ("input_type", "buttonlike"),
    [
        ("submit", True),
        ("button", True),
        ("image", True),
        ("reset", True),
        ("text", False),
        ("search", False),
        (None, False),
    ],
)
def test_buttonlike_input_type_membership(input_type: str | None, buttonlike: bool) -> None:
    """Each type's membership decision holds for both input predicates."""
    el = _element(tag="input", input_type=input_type, clickable=True)
    assert _matches_semantic_tag(_SEMANTIC_RULE, el) is buttonlike
    assert _matches_interactivity(_INTERACTIVITY_RULE, el) is buttonlike


def test_input_submit_predicates_require_input_tag() -> None:
    """Neither input predicate matches a non-input even with a button-like type.

    <button> still satisfies the interactivity predicate via its own clickable gate.
    """
    div = _element(tag="div", input_type=None, clickable=True)
    assert _matches_semantic_tag(_SEMANTIC_RULE, div) is False
    assert _matches_interactivity(_INTERACTIVITY_RULE, div) is False
    button = _element(tag="button", clickable=True)
    assert _matches_interactivity(_INTERACTIVITY_RULE, button) is True
    unclickable_button = _element(tag="button", clickable=False)
    assert _matches_interactivity(_INTERACTIVITY_RULE, unclickable_button) is False


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


# ---------------------------------------------------------------------------
# Border-presence and text-presence feature families.
# ---------------------------------------------------------------------------


def test_border_presence_votes_border_on_bordered_card() -> None:
    """A bordered (non-input) card carries a surviving border component.

    The regression this family fixes: only the <input> semantic rule ever voted
    border, so pages without classified inputs measured zero border mass.
    """
    card = _element(tag="div", class_tokens=["card"], border=_color("#d1d9e0"))
    [result] = classify_components([card], CONFIG, VIEWPORT)
    assert _argmax(result.component_dist) is ComponentType.card_bg
    # YAML calibration: card_bg 3.0 vs border 2.5 -> border prob ~0.27, survives.
    assert result.component_dist.get(ComponentType.border, 0.0) > 0.05


def test_borderless_element_gets_no_border_vote() -> None:
    card = _element(tag="div", class_tokens=["card"], border=None)
    [result] = classify_components([card], CONFIG, VIEWPORT)
    assert ComponentType.border not in result.component_dist


def test_bordered_text_input_keeps_border_component() -> None:
    """The input border vote moved into border_presence; a bordered text input
    carries surviving border mass alongside its input_bg vote (and, post the
    input[submit] fix, no cta_bg at all)."""
    box = _element(tag="input", input_type="text", border=_color("#d1d9e0"))
    [result] = classify_components([box], CONFIG, VIEWPORT)
    assert _argmax(result.component_dist) is ComponentType.input_bg
    # YAML calibration: input_bg 3.0 vs border 2.5 -> border prob ~0.27, survives.
    assert result.component_dist.get(ComponentType.border, 0.0) > 0.05
    assert ComponentType.cta_bg not in result.component_dist


def test_bordered_submit_input_stays_cta_dominated() -> None:
    """A bordered submit input keeps cta_bg dominant; its border mass prunes
    (YAML calibration: cta_bg 7.0 crushes the 2.5 border vote post-softmax)."""
    submit = _element(tag="input", input_type="submit", clickable=True, border=_color("#d1d9e0"))
    [result] = classify_components([submit], CONFIG, VIEWPORT)
    assert _argmax(result.component_dist) is ComponentType.cta_bg
    assert ComponentType.border not in result.component_dist


def test_bordered_cta_stays_cta_dominated() -> None:
    """A bordered primary button keeps cta_bg dominant; its border mass prunes.

    Guard on the family interaction: border_presence must not turn CTAs into
    border sources (cta votes ~>= 9 crush the 2.5 border vote post-softmax).
    """
    cta = _element(
        tag="button",
        class_tokens=["btn-primary"],
        clickable=True,
        border=_color("#1f883d"),
    )
    [result] = classify_components([cta], CONFIG, VIEWPORT)
    assert _argmax(result.component_dist) is ComponentType.cta_bg
    assert ComponentType.border not in result.component_dist


def test_repeated_transparent_bg_text_spans_are_not_repetition_cards() -> None:
    """Repeated text spans with the default transparent bg get NO repetition votes.

    ``distinct_bg_from_parent`` requires a bg that actually paints (alpha > 0): an
    ``alpha == 0`` computed ``background-color: transparent`` is not a background.
    Regression: repeated ``.muted`` metadata spans classified as repetition "cards",
    and the card_bg votes crushed their text_presence vote below the prune floor —
    un-measuring the muted text color the family exists to measure.
    """
    transparent = Color(hex="#000000", lightness=0.0, chroma=0.0, hue=0.0, alpha=0.0)
    spans = [
        _element(tag="span", class_tokens=["muted"], bg=transparent, has_text=True)
        for _ in range(4)
    ]
    results = classify_components(spans, CONFIG, VIEWPORT)
    for result in results:
        assert result.component_dist == {ComponentType.page_text: 1.0}


def test_text_presence_votes_page_text_on_plain_text_element() -> None:
    """A bare <p>/<span> with direct text — matched by NO semantic rule — votes
    page_text, so body-copy and muted-gray typography is finally measured."""
    p = _element(tag="p", has_text=True)
    [result] = classify_components([p], CONFIG, VIEWPORT)
    assert result.component_dist == {ComponentType.page_text: 1.0}


def test_text_presence_suppressed_on_clickable_elements() -> None:
    """A link with text gets NO page_text vote: clickable typography is interactive
    by definition and already routed via the link rules (see the YAML comment)."""
    link = _element(tag="a", clickable=True, has_text=True)
    [result] = classify_components([link], CONFIG, VIEWPORT)
    assert ComponentType.page_text not in result.component_dist
    assert _argmax(result.component_dist) is ComponentType.link


def test_text_presence_does_not_displace_semantic_card_bg() -> None:
    """Calibration guard: page_text 2.0 on a text-bearing card survives but stays
    below the card's semantic card_bg vote (3.0)."""
    card = _element(tag="div", class_tokens=["card"], has_text=True)
    [result] = classify_components([card], CONFIG, VIEWPORT)
    assert _argmax(result.component_dist) is ComponentType.card_bg
    assert 0.05 <= result.component_dist[ComponentType.page_text] < 0.5


def test_no_viewport_uses_default() -> None:
    """Calling without a viewport still computes geometry-driven results."""
    header = _element(tag="header", rect=Rect(x=0.0, y=0.0, width=1280.0, height=80.0))
    [result] = classify_components([header], CONFIG)
    assert result.component_dist
    assert _argmax(result.component_dist) is ComponentType.header_bg


def test_finalize_distribution_does_not_overflow_on_large_votes() -> None:
    """The softmax is max-shifted: stacked vote weights must not overflow math.exp.

    Unshifted exp(vote/T) overflows around vote/T > 709; probabilities are
    mathematically shift-invariant, so the winner and its share are unchanged.
    """
    accum = {ComponentType.cta_bg: 5_000.0, ComponentType.link: 4_999.0}
    probs = _finalize_distribution(accum, CONFIG)
    assert math.isclose(sum(probs.values()), 1.0, rel_tol=1e-9)
    assert probs[ComponentType.cta_bg] > probs.get(ComponentType.link, 0.0)
