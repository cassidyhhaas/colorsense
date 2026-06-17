"""Unit tests for :mod:`colorsense.palette.inventory`."""

from __future__ import annotations

import itertools

import pytest

from colorsense.color.primitives import ciede2000, delta_e, parse_css_color
from colorsense.models import (
    ClassifiedElement,
    Color,
    ComponentType,
    Harvest,
    HarvestedElement,
    PropertyFamily,
    Rect,
    ScreenshotBin,
    Theme,
    Viewport,
)
from colorsense.palette.inventory import (
    _CTA_BG_GUARD_MAX_DE2000,
    _NEAR_BLACK_LIGHTNESS,
    DELTA_E_CLUSTER,
    DELTA_E_MATCH_BG,
    DELTA_E_MATCH_TEXT_BORDER,
    NEAR_WHITE_LIGHTNESS,
    NEAR_WHITE_MERGE_MAX_DE2000,
    _cluster_pool,
    _Entry,
    _entry_has_cta_action_mass,
    _forbids_cta_bg_merge,
    _forbids_near_white_merge,
    build_inventory,
)


def _color(value: str) -> Color:
    c = parse_css_color(value)
    assert c is not None
    return c


def _viewport() -> Viewport:
    return Viewport(width=1280, height=720, device_scale_factor=1.0)


def _harvest(bins: list[ScreenshotBin]) -> Harvest:
    return Harvest(
        url="https://example.test",
        theme=Theme.light,
        viewport=_viewport(),
        screenshot_bins=bins,
    )


def _element(
    bg: Color | None,
    text: Color | None = None,
    border: Color | None = None,
    bg_gradient_stops: tuple[Color, ...] = (),
) -> HarvestedElement:
    return HarvestedElement(
        tag="div",
        role=None,
        id=None,
        rect=Rect(x=0.0, y=0.0, width=10.0, height=10.0),
        position="static",
        bg=bg,
        text=text,
        border=border,
        bg_gradient_stops=bg_gradient_stops,
        is_iframe=False,
        cross_origin=False,
        shadow_host=False,
        clickable=False,
        has_hover_color_change=False,
        hover_bg=None,
        vendor_match=False,
        visible=True,
        aria_hidden=False,
    )


def _classified(
    bg: Color | None,
    dist: dict[ComponentType, float],
    text: Color | None = None,
    border: Color | None = None,
    bg_gradient_stops: tuple[Color, ...] = (),
) -> ClassifiedElement:
    return ClassifiedElement(
        element=_element(bg, text=text, border=border, bg_gradient_stops=bg_gradient_stops),
        component_dist=dist,
    )


def test_near_identical_colors_merge() -> None:
    near_a = _color("#3366cc")
    near_b = _color("#3367cc")
    distinct = _color("#ffffff")

    # Sanity: these two truly merge / stay-separate under the module thresholds.
    assert delta_e(near_a, near_b) <= DELTA_E_CLUSTER
    assert delta_e(near_a, distinct) > DELTA_E_CLUSTER

    harvest = _harvest(
        [
            ScreenshotBin(color=near_a, area_fraction=0.3),
            ScreenshotBin(color=near_b, area_fraction=0.4),
            ScreenshotBin(color=distinct, area_fraction=0.3),
        ]
    )

    clusters = build_inventory(harvest, [])

    assert len(clusters) == 2
    merged = max(clusters, key=lambda c: c.member_count)
    assert merged.member_count == 2
    assert merged.area_weight == pytest.approx(0.7, abs=1e-9)

    other = min(clusters, key=lambda c: c.member_count)
    assert other.member_count == 1
    assert other.area_weight == pytest.approx(0.3, abs=1e-9)
    assert other.color.hex == "#ffffff"


def test_component_mix_aggregates() -> None:
    surface = _color("#3366cc")
    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.5)])

    classified = [_classified(_color("#3366cc"), {ComponentType.cta_bg: 1.0})]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    mix = clusters[0].component_mix
    assert mix
    dominant = max(mix, key=lambda k: mix[k])
    assert dominant == ComponentType.cta_bg
    assert sum(mix.values()) == pytest.approx(1.0, abs=1e-9)


def test_component_mix_aggregates_multiple_types() -> None:
    surface = _color("#3366cc")
    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.5)])

    classified = [
        _classified(_color("#3366cc"), {ComponentType.cta_bg: 0.75}),
        _classified(_color("#3367cc"), {ComponentType.header_bg: 0.25}),
    ]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    mix = clusters[0].component_mix
    assert sum(mix.values()) == pytest.approx(1.0, abs=1e-9)
    dominant = max(mix, key=lambda k: mix[k])
    assert dominant == ComponentType.cta_bg


def test_component_mass_keeps_raw_unnormalized_sums() -> None:
    # component_mass preserves the RAW vote mass (cross-cluster magnitude, which the
    # usage view ranks by); component_mix is the same sums normalized to ~1.0.
    surface = _color("#3366cc")
    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.5)])
    classified = [
        _classified(_color("#3366cc"), {ComponentType.cta_bg: 0.75}),
        _classified(_color("#3367cc"), {ComponentType.header_bg: 0.25}),
        _classified(_color("#3366cc"), {ComponentType.cta_bg: 0.75}),
    ]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    mass = clusters[0].component_mass
    assert mass[ComponentType.cta_bg] == pytest.approx(1.5, abs=1e-9)
    assert mass[ComponentType.header_bg] == pytest.approx(0.25, abs=1e-9)
    # The mix is exactly the normalized mass.
    total = sum(mass.values())
    for comp, raw in mass.items():
        assert clusters[0].component_mix[comp] == pytest.approx(raw / total, abs=1e-9)


def test_gradient_cta_votes_both_stops_split_evenly() -> None:
    # A gradient CTA (transparent background-color, two opaque gradient stops) donates
    # its cta_bg mass to BOTH stops, split evenly — so a purple->blue button makes both
    # purple and blue candidates without out-voting a solid button (which keeps full mass).
    transparent = Color(hex="#000000", lightness=0.0, chroma=0.0, hue=0.0, alpha=0.0)
    purple = _color("#7c3bed")
    blue = _color("#3c83f6")
    harvest = _harvest(
        [
            ScreenshotBin(color=purple, area_fraction=0.5),
            ScreenshotBin(color=blue, area_fraction=0.5),
        ]
    )
    classified = [
        _classified(transparent, {ComponentType.cta_bg: 3.0}, bg_gradient_stops=(purple, blue))
    ]

    clusters = build_inventory(harvest, classified)

    p = next(c for c in clusters if c.color.hex == "#7c3bed")
    b = next(c for c in clusters if c.color.hex == "#3c83f6")
    assert p.component_mass[ComponentType.cta_bg] == pytest.approx(1.5, abs=1e-9)
    assert b.component_mass[ComponentType.cta_bg] == pytest.approx(1.5, abs=1e-9)


def test_gradient_stops_ignored_when_background_color_is_opaque() -> None:
    # A solid background-color takes precedence: the gradient stops are not voted.
    solid = _color("#101828")
    purple = _color("#7c3bed")
    harvest = _harvest([ScreenshotBin(color=solid, area_fraction=1.0)])
    classified = [_classified(solid, {ComponentType.card_bg: 1.0}, bg_gradient_stops=(purple,))]

    clusters = build_inventory(harvest, classified)

    assert [c.color.hex for c in clusters] == ["#101828"]
    assert clusters[0].component_mass[ComponentType.card_bg] == pytest.approx(1.0, abs=1e-9)


def test_semi_transparent_bg_vote_is_alpha_scaled() -> None:
    # A faint tint (bg-primary/10) keeps its intended saturated hex but votes in
    # proportion to how much it actually paints: mass is scaled by the bg alpha.
    tint = _color("rgba(124, 59, 237, 0.1)")
    assert tint.hex == "#7c3bed" and tint.alpha == pytest.approx(0.1)
    harvest = _harvest([ScreenshotBin(color=_color("#7c3bed"), area_fraction=1.0)])
    classified = [_classified(tint, {ComponentType.badge: 2.0})]

    clusters = build_inventory(harvest, classified)

    purple = next(c for c in clusters if c.color.hex == "#7c3bed")
    assert purple.component_mass[ComponentType.badge] == pytest.approx(0.2, abs=1e-9)


def test_semi_transparent_border_vote_is_alpha_scaled() -> None:
    # Like the bg channel, the border channel scales its vote by the border's alpha: a
    # near-transparent hairline border keeps its hex but votes in proportion to how much it
    # actually paints. This is what keeps a swarm of faint icon-container outlines from
    # out-voting the one opaque divider (the vercel #000000-over-#ebebeb case). The text
    # channel, by contrast, is NOT alpha-scaled.
    faint = _color("rgba(0, 0, 0, 0.08)")
    opaque = _color("rgba(0, 0, 0, 1.0)")
    assert faint.hex == "#000000" and faint.alpha == pytest.approx(0.08)
    harvest = _harvest([ScreenshotBin(color=_color("#ffffff"), area_fraction=1.0)])
    classified = [
        _classified(None, {ComponentType.border: 2.0}, border=faint),
        _classified(None, {ComponentType.border: 2.0}, border=opaque),
    ]

    clusters = build_inventory(harvest, classified)

    # Both borders are the same hex, so they cluster; the faint vote contributes 2.0*0.08
    # and the opaque one 2.0*1.0 -> 2.16 total (vs 4.0 if border were not alpha-scaled).
    black = next(c for c in clusters if c.color.hex == "#000000")
    assert black.component_mass[ComponentType.border] == pytest.approx(2.16, abs=1e-9)


def test_text_vote_is_not_alpha_scaled() -> None:
    # The text channel deliberately does NOT alpha-scale: a low-opacity glyph still reads as
    # its text color, so it votes at full mass (only bg and border are alpha-scaled).
    faint_text = _color("rgba(0, 0, 0, 0.3)")
    assert faint_text.alpha == pytest.approx(0.3)
    harvest = _harvest([ScreenshotBin(color=_color("#ffffff"), area_fraction=1.0)])
    classified = [_classified(None, {ComponentType.page_text: 2.0}, text=faint_text)]

    clusters = build_inventory(harvest, classified)

    black = next(c for c in clusters if c.color.hex == "#000000")
    assert black.component_mass[ComponentType.page_text] == pytest.approx(2.0, abs=1e-9)


def test_unmatched_element_creates_zero_area_entry() -> None:
    # No bins at all; an element's semantics must still be preserved.
    harvest = _harvest([])
    classified = [_classified(_color("#112233"), {ComponentType.cta_bg: 1.0})]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    assert clusters[0].area_weight == pytest.approx(0.0, abs=1e-9)
    assert clusters[0].color.hex == "#112233"
    assert max(clusters[0].component_mix) == ComponentType.cta_bg


def test_link_mass_routes_to_text_color_not_bg() -> None:
    # A link paints its typography color; its background is usually transparent, so
    # link mass routes to the TEXT channel (like *_text components).
    blue_text = _color("#0969da")
    bg = _color("#ffffff")
    harvest = _harvest([ScreenshotBin(color=bg, area_fraction=0.9)])
    classified = [_classified(bg, {ComponentType.link: 1.0}, text=blue_text)]

    clusters = build_inventory(harvest, classified)

    blue = next(c for c in clusters if c.color.hex == "#0969da")
    white = next(c for c in clusters if c.color.hex == "#ffffff")
    assert ComponentType.link in blue.component_mix
    assert ComponentType.link not in white.component_mix


def test_fully_transparent_channel_color_donates_no_mass() -> None:
    # alpha == 0 means the channel paints nothing: without the gate, transparent
    # backgrounds would pile votes onto a phantom #000000 zero-area cluster.
    transparent = Color(hex="#000000", lightness=0.0, chroma=0.0, hue=0.0, alpha=0.0)
    harvest = _harvest([ScreenshotBin(color=_color("#ffffff"), area_fraction=1.0)])
    classified = [_classified(transparent, {ComponentType.card_bg: 1.0})]

    clusters = build_inventory(harvest, classified)

    assert [c.color.hex for c in clusters] == ["#ffffff"]
    assert clusters[0].component_mass == {}


def test_element_far_from_all_bins_is_new_cluster() -> None:
    # NOTE: previously this test put page_text mass on the element's *bg*
    # channel; with channel routing, *_text components are carried by the
    # measured text color, so the far color is now the text channel.
    harvest = _harvest([ScreenshotBin(color=_color("#ffffff"), area_fraction=0.8)])
    far = _color("#000000")
    assert delta_e(far, _color("#ffffff")) > DELTA_E_MATCH_TEXT_BORDER

    classified = [_classified(None, {ComponentType.page_text: 1.0}, text=far)]
    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 2
    black = next(c for c in clusters if c.color.hex == "#000000")
    assert black.area_weight == pytest.approx(0.0, abs=1e-9)
    assert max(black.component_mix) == ComponentType.page_text


def test_element_without_bg_or_dist_is_ignored() -> None:
    harvest = _harvest([ScreenshotBin(color=_color("#3366cc"), area_fraction=0.5)])
    classified = [
        _classified(None, {ComponentType.cta_bg: 1.0}),
        _classified(_color("#3366cc"), {}),
    ]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    assert clusters[0].component_mix == {}


def test_text_mass_routes_to_text_color_not_bg() -> None:
    light_bg = _color("#ffffff")
    dark_text = _color("#111111")
    assert delta_e(light_bg, dark_text) > DELTA_E_MATCH_TEXT_BORDER

    harvest = _harvest([ScreenshotBin(color=light_bg, area_fraction=0.9)])
    classified = [
        _classified(
            light_bg,
            {ComponentType.page_bg: 0.6, ComponentType.page_text: 0.4},
            text=dark_text,
        )
    ]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 2
    white = next(c for c in clusters if c.color.hex == "#ffffff")
    black = next(c for c in clusters if c.color.hex == "#111111")
    # page_text mass lives on the TEXT color's cluster, not the bg cluster.
    assert ComponentType.page_text in black.component_mix
    assert ComponentType.page_text not in white.component_mix
    assert ComponentType.page_bg in white.component_mix


def test_border_mass_routes_to_border_color() -> None:
    bg = _color("#ffffff")
    border = _color("#3366cc")
    assert delta_e(bg, border) > DELTA_E_MATCH_TEXT_BORDER

    harvest = _harvest([ScreenshotBin(color=bg, area_fraction=0.9)])
    classified = [
        _classified(
            bg,
            {ComponentType.input_bg: 0.7, ComponentType.border: 0.3},
            border=border,
        )
    ]

    clusters = build_inventory(harvest, classified)

    blue = next(c for c in clusters if c.color.hex == "#3366cc")
    white = next(c for c in clusters if c.color.hex == "#ffffff")
    assert max(blue.component_mix) == ComponentType.border
    assert ComponentType.border not in white.component_mix
    assert ComponentType.input_bg in white.component_mix


def test_element_with_no_bg_still_contributes_text_mass() -> None:
    dark_text = _color("#222222")
    harvest = _harvest([])
    classified = [_classified(None, {ComponentType.cta_text: 1.0}, text=dark_text)]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    assert clusters[0].color.hex == "#222222"
    assert max(clusters[0].component_mix) == ComponentType.cta_text


def test_channel_without_color_drops_only_that_channel() -> None:
    bg = _color("#ffffff")
    harvest = _harvest([ScreenshotBin(color=bg, area_fraction=1.0)])
    # text is None, so the page_text mass is dropped; the bg mass still lands.
    classified = [_classified(bg, {ComponentType.page_bg: 0.5, ComponentType.page_text: 0.5})]

    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    assert clusters[0].component_mix == {ComponentType.page_bg: pytest.approx(1.0)}


def test_clusters_sorted_by_area_descending() -> None:
    harvest = _harvest(
        [
            ScreenshotBin(color=_color("#ff0000"), area_fraction=0.1),
            ScreenshotBin(color=_color("#00ff00"), area_fraction=0.5),
            ScreenshotBin(color=_color("#0000ff"), area_fraction=0.3),
        ]
    )

    clusters = build_inventory(harvest, [])
    weights = [c.area_weight for c in clusters]
    assert weights == sorted(weights, reverse=True)


def test_determinism() -> None:
    harvest = _harvest(
        [
            ScreenshotBin(color=_color("#3366cc"), area_fraction=0.3),
            ScreenshotBin(color=_color("#3367cc"), area_fraction=0.4),
            ScreenshotBin(color=_color("#ffffff"), area_fraction=0.3),
        ]
    )
    classified = [
        _classified(_color("#3366cc"), {ComponentType.cta_bg: 1.0}),
        _classified(_color("#ffffff"), {ComponentType.page_bg: 1.0}),
    ]

    first = build_inventory(harvest, classified)
    second = build_inventory(harvest, classified)

    assert first == second


def test_build_inventory_permutation_invariant_on_well_separated_colors() -> None:
    """Permuting elements and bins leaves the output identical — for separated colors.

    CAVEAT (why the pinned property is deliberately weaker than full
    permutation-invariance): entry creation order can legitimately matter by
    design. Two elements whose colors are both far (beyond the channel's join
    radius) from every bin but between DELTA_E_CLUSTER and the radius of each
    other join one entry whose color is whichever element came first, changing
    the cluster's representative hex. Likewise nearest-entry ties (`<=` keeps
    the later index) depend on bin order for equidistant bins. So we pin the
    property the module does guarantee: when every pairwise color distance
    exceeds the largest join radius (DELTA_E_MATCH_BG), matching is unambiguous
    and the output is exactly permutation-invariant.

    All masses are dyadic (1.0), so float summation order cannot perturb the
    result and exact equality is safe.
    """
    white, blue, red, black = (
        _color("#ffffff"),
        _color("#0000ff"),
        _color("#ff0000"),
        _color("#000000"),
    )
    # Precondition: every pairwise distance is beyond the matching threshold.
    colors = [white, blue, red, black]
    for i in range(len(colors)):
        for j in range(i + 1, len(colors)):
            assert delta_e(colors[i], colors[j]) > DELTA_E_MATCH_BG

    bins = [
        ScreenshotBin(color=white, area_fraction=0.5),
        ScreenshotBin(color=blue, area_fraction=0.3),
        ScreenshotBin(color=red, area_fraction=0.2),
    ]
    elements = [
        _classified(white, {ComponentType.page_bg: 1.0}),
        _classified(blue, {ComponentType.cta_bg: 1.0}),
        _classified(blue, {ComponentType.link: 1.0}),
        # Far from every bin: creates a zero-area entry.
        _classified(None, {ComponentType.page_text: 1.0}, text=black),
    ]

    base = build_inventory(_harvest(bins), elements)
    assert len(base) == 4  # sanity: white, blue, red, black all present

    for element_perm in itertools.permutations(elements):
        assert build_inventory(_harvest(bins), list(element_perm)) == base
    for bin_perm in itertools.permutations(bins):
        assert build_inventory(_harvest(list(bin_perm)), elements) == base


def test_equal_area_clusters_sorted_by_hex() -> None:
    # Two clusters with identical area weights must order by hex (the secondary
    # sort key), independent of bin input order.
    red = ScreenshotBin(color=_color("#ff0000"), area_fraction=0.4)
    blue = ScreenshotBin(color=_color("#0000ff"), area_fraction=0.4)

    for bins in ([red, blue], [blue, red]):
        clusters = build_inventory(_harvest(bins), [])
        assert [c.color.hex for c in clusters] == ["#0000ff", "#ff0000"]


def test_thresholds_relationship() -> None:
    # Clustering threshold must not exceed either channel join radius, and the
    # text/border radius is deliberately the tighter of the two.
    assert DELTA_E_CLUSTER <= DELTA_E_MATCH_TEXT_BORDER <= DELTA_E_MATCH_BG


# ---------------------------------------------------------------------------
# Per-channel join radii (bg loose, text/border tight).
# ---------------------------------------------------------------------------

# GitHub's near-black body text vs. its dark code-block surface: 0.078 deltaEOK —
# between DELTA_E_MATCH_TEXT_BORDER (0.05) and DELTA_E_MATCH_BG (0.10). The live-probe
# regression: under a single 0.10 radius the text color was absorbed into the adjacent
# dark surface bin, erasing the body-text color from the usage view.
_DARK_SURFACE = "#0d1117"
_NEAR_BLACK_TEXT = "#1f2328"


def test_text_color_near_dark_surface_bin_forms_distinct_entry() -> None:
    surface, text = _color(_DARK_SURFACE), _color(_NEAR_BLACK_TEXT)
    gap = delta_e(surface, text)
    assert DELTA_E_MATCH_TEXT_BORDER < gap <= DELTA_E_MATCH_BG  # the regression window

    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.2)])
    classified = [_classified(None, {ComponentType.page_text: 1.0}, text=text)]
    clusters = build_inventory(harvest, classified)

    # Two distinct clusters: the text did NOT merge into the dark surface bin.
    assert len(clusters) == 2
    text_cluster = next(c for c in clusters if c.color.hex == text.hex)
    assert text_cluster.area_weight == pytest.approx(0.0, abs=1e-9)
    assert max(text_cluster.component_mix) == ComponentType.page_text
    bin_cluster = next(c for c in clusters if c.color.hex == surface.hex)
    assert ComponentType.page_text not in bin_cluster.component_mix


def test_border_channel_uses_tight_radius() -> None:
    surface, border = _color(_DARK_SURFACE), _color(_NEAR_BLACK_TEXT)
    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.2)])
    classified = [_classified(None, {ComponentType.border: 1.0}, border=border)]
    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 2
    border_cluster = next(c for c in clusters if c.color.hex == border.hex)
    assert max(border_cluster.component_mix) == ComponentType.border


def test_bg_channel_keeps_loose_radius_at_same_distance() -> None:
    # The SAME color pair on the BG channel still merges: screenshot quantization and
    # anti-aliasing smear backgrounds, so bg keeps the generous 0.10 join radius.
    surface, bg = _color(_DARK_SURFACE), _color(_NEAR_BLACK_TEXT)
    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.2)])
    classified = [_classified(bg, {ComponentType.card_bg: 1.0})]
    clusters = build_inventory(harvest, classified)

    assert len(clusters) == 1
    assert clusters[0].color.hex == surface.hex
    assert max(clusters[0].component_mix) == ComponentType.card_bg


# ---------------------------------------------------------------------------
# Family-segregated clustering: text/border colors never adopt a bg bin's hex.
# ---------------------------------------------------------------------------


# A text/border color WITHIN the tight join radius of the big bg bin: this is the actual
# family-bleed window. Under the OLD single-pool algorithm the text would join (or cluster
# into) the higher-area bin and adopt ITS hex; family segregation keeps them apart.
_BLEED_TEXT = "#10141a"  # deltaEOK 0.0136 from _DARK_SURFACE — inside DELTA_E_MATCH_TEXT_BORDER


def test_text_color_near_large_bg_bin_keeps_own_hex() -> None:
    surface, text = _color(_DARK_SURFACE), _color(_BLEED_TEXT)
    # Inside the tight radius => the OLD code bled the text onto the bin's hex; the NEW
    # family-segregated code must keep the text's own hex.
    assert delta_e(surface, text) <= DELTA_E_MATCH_TEXT_BORDER

    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.9)])
    classified = [_classified(None, {ComponentType.page_text: 1.0}, text=text)]
    clusters = build_inventory(harvest, classified)

    text_cluster = next(c for c in clusters if ComponentType.page_text in c.component_mix)
    assert text_cluster.color.hex == text.hex  # did NOT adopt the bin's hex
    assert text_cluster.area_weight == pytest.approx(0.0, abs=1e-9)


def test_border_color_near_bg_bin_keeps_own_hex() -> None:
    # Same guarantee for the border channel: a border color near a bg bin keeps its hex.
    surface, border = _color(_DARK_SURFACE), _color(_BLEED_TEXT)
    assert delta_e(surface, border) <= DELTA_E_MATCH_TEXT_BORDER

    harvest = _harvest([ScreenshotBin(color=surface, area_fraction=0.9)])
    classified = [_classified(None, {ComponentType.border: 1.0}, border=border)]
    clusters = build_inventory(harvest, classified)

    border_cluster = next(c for c in clusters if ComponentType.border in c.component_mix)
    assert border_cluster.color.hex == border.hex
    assert border_cluster.area_weight == pytest.approx(0.0, abs=1e-9)


def test_background_output_unchanged_for_simple_page_bg_input() -> None:
    # A page-bg-only input must yield the SAME background cluster as the old single-pool
    # behavior: one cluster, page bin's hex, full area, page_bg mass.
    page = _color("#ffffff")
    harvest = _harvest([ScreenshotBin(color=page, area_fraction=1.0)])
    classified = [_classified(page, {ComponentType.page_bg: 1.0})]

    clusters = build_inventory(harvest, classified)

    assert [c.color.hex for c in clusters] == ["#ffffff"]
    assert clusters[0].area_weight == pytest.approx(1.0, abs=1e-9)
    assert clusters[0].component_mix == {ComponentType.page_bg: pytest.approx(1.0)}


def test_text_border_representative_is_max_in_family_mass() -> None:
    # Within a text/border cluster the representative is the member with the largest
    # in-family vote mass (hex tiebreak), NOT area (text/border have no area). Build the
    # pool directly: through build_inventory two near text colors nearest-join into one
    # entry, so the multi-entry case is exercised at the _cluster_pool level.
    low = _color("#3366cc")
    high = _color("#3367cc")  # near enough to cluster with `low`
    assert delta_e(low, high) <= DELTA_E_CLUSTER

    low_entry = _Entry(low, 0.0)
    low_entry.component_mix[ComponentType.page_text] = 1.0
    high_entry = _Entry(high, 0.0)
    high_entry.component_mix[ComponentType.page_text] = 5.0

    clusters = _cluster_pool([low_entry, high_entry], PropertyFamily.text)

    assert len(clusters) == 1
    # `high` carries more in-family mass, so it is the representative hex (not max area:
    # both areas are 0). The bg path would instead break the tie by smallest hex (`low`).
    assert clusters[0].color.hex == high.hex
    assert _cluster_pool([low_entry, high_entry], PropertyFamily.background)[0].color.hex == low.hex


def test_background_representative_is_max_area() -> None:
    # Within a background cluster the representative is still the max-area member.
    small = _color("#3366cc")
    big = _color("#3367cc")
    assert delta_e(small, big) <= DELTA_E_CLUSTER

    harvest = _harvest(
        [
            ScreenshotBin(color=small, area_fraction=0.2),
            ScreenshotBin(color=big, area_fraction=0.6),
        ]
    )
    clusters = build_inventory(harvest, [])

    assert len(clusters) == 1
    assert clusters[0].color.hex == big.hex  # max-area member wins


def test_segregated_determinism_same_input_identical_output() -> None:
    # Family segregation must stay deterministic: same input -> identical cluster list.
    harvest = _harvest(
        [
            ScreenshotBin(color=_color("#0d1117"), area_fraction=0.7),
            ScreenshotBin(color=_color("#ffffff"), area_fraction=0.3),
        ]
    )
    classified = [
        _classified(None, {ComponentType.page_text: 1.0}, text=_color("#1f2328")),
        _classified(None, {ComponentType.border: 1.0}, border=_color("#30363d")),
        _classified(_color("#0d1117"), {ComponentType.page_bg: 1.0}),
    ]

    first = build_inventory(harvest, classified)
    second = build_inventory(harvest, classified)
    assert first == second


# --------------------------------------------------------------------------- #
# Near-white guard (text/border pools): OKLab collapses perceptually-distinct
# near-white text colors; CIEDE2000 keeps them apart. See `_forbids_near_white_merge`.
# --------------------------------------------------------------------------- #

# GitHub's canonical case: dominant white body text vs Primer's near-white `--fgColor-default`.
_WHITE = "#ffffff"
_PRIMER_NEAR_WHITE = "#f0f6fc"


def test_near_white_guard_predicate_distinguishes_the_github_pair() -> None:
    white = _color(_WHITE)
    primer = _color(_PRIMER_NEAR_WHITE)

    # OKLab would merge them (within the cluster radius); CIEDE2000 says clearly distinct.
    assert delta_e(white, primer) <= DELTA_E_CLUSTER
    assert ciede2000(white, primer) > NEAR_WHITE_MERGE_MAX_DE2000
    assert white.lightness >= NEAR_WHITE_LIGHTNESS
    assert primer.lightness >= NEAR_WHITE_LIGHTNESS

    assert _forbids_near_white_merge(white, primer)
    # Symmetric, and a color never forbids merging with itself.
    assert _forbids_near_white_merge(primer, white)
    assert not _forbids_near_white_merge(white, white)


def test_near_white_guard_ignores_colors_below_the_regime() -> None:
    # A near-white and a mid-gray: not both near-white, so the guard never engages
    # (the gray is excluded by lightness before any CIEDE2000 call).
    white = _color(_WHITE)
    gray = _color("#9198a1")
    assert gray.lightness < NEAR_WHITE_LIGHTNESS
    assert not _forbids_near_white_merge(white, gray)


def test_near_white_guard_allows_anti_alias_variants_to_merge() -> None:
    # The radius is a *denoising* radius, looser than the 1.0 identity floor: two near-white
    # colors within NEAR_WHITE_MERGE_MAX_DE2000 still merge (anti-alias variants must collapse).
    white = _color(_WHITE)
    faint = _color("#fcfdff")  # ~1.1 ΔE2000 from white — an anti-alias-scale variant
    assert ciede2000(white, faint) <= NEAR_WHITE_MERGE_MAX_DE2000
    assert not _forbids_near_white_merge(white, faint)


def test_text_pool_splits_distinct_near_whites() -> None:
    # Two text elements paint white and Primer near-white. Without the guard they collapse
    # onto one text entry (OKLab); with it they stay as two distinct text colors.
    harvest = _harvest([ScreenshotBin(color=_color("#0d1117"), area_fraction=1.0)])
    classified = [
        _classified(None, {ComponentType.page_text: 1.0}, text=_color(_WHITE)),
        _classified(None, {ComponentType.page_text: 1.0}, text=_color(_PRIMER_NEAR_WHITE)),
    ]

    clusters = build_inventory(harvest, classified)
    text_hexes = {
        c.color.hex
        for c in clusters
        if any(comp == ComponentType.page_text for comp in c.component_mass)
    }
    assert text_hexes == {_WHITE, _PRIMER_NEAR_WHITE}


def test_near_white_guard_survives_union_find_transitivity() -> None:
    # Union-find is transitive, so a near-white "bridge" color close to two guard-forbidden
    # colors must NOT chain them into one cluster. A and C are forbidden (CIEDE2000 > 3.0);
    # B sits between them and is mergeable with each. They must still end up in two clusters.
    a = _color("#ebebeb")
    b = _color("#ebebef")  # the bridge
    c = _color("#ebebf3")
    assert _forbids_near_white_merge(a, c)
    assert not _forbids_near_white_merge(a, b)
    assert not _forbids_near_white_merge(b, c)
    assert delta_e(a, b) <= DELTA_E_CLUSTER and delta_e(b, c) <= DELTA_E_CLUSTER

    entries = [_Entry(a, 0.0), _Entry(b, 0.0), _Entry(c, 0.0)]
    clusters = _cluster_pool(entries, PropertyFamily.text)
    reps = {cluster.color.hex for cluster in clusters}

    assert len(clusters) == 2  # A and C never co-cluster, even through the bridge
    assert "#ebebeb" in reps and "#ebebf3" in reps


def test_near_white_anti_alias_variants_still_collapse() -> None:
    # The guard must not over-fragment: three mutually-near near-white variants (all pairwise
    # within the denoising radius) still collapse to a single text cluster.
    variants = [_color("#ffffff"), _color("#fefefe"), _color("#fdfdfd")]
    for first, second in itertools.combinations(variants, 2):
        assert not _forbids_near_white_merge(first, second)

    entries = [_Entry(v, 0.0) for v in variants]
    clusters = _cluster_pool(entries, PropertyFamily.text)
    assert len(clusters) == 1
    assert clusters[0].member_count == 3


def test_background_pool_still_merges_near_whites() -> None:
    # The guard is text/border-only: the background pool keeps the pure OKLab radius, so the
    # same near-white pair that splits in the text pool still merges as screenshot bins.
    harvest = _harvest(
        [
            ScreenshotBin(color=_color(_WHITE), area_fraction=0.6),
            ScreenshotBin(color=_color(_PRIMER_NEAR_WHITE), area_fraction=0.4),
        ]
    )
    clusters = build_inventory(harvest, [])
    assert len(clusters) == 1
    assert clusters[0].member_count == 2
    assert clusters[0].color.hex == _WHITE  # max-area member wins


# --- Near-black CTA/action background guard ---------------------------------------------
# disco's dark CTA anchors paint `#030711`, OKLab-near the `#050505` footer screenshot bin but
# CIEDE2000-distinct. Without the guard the CTA bg mass is absorbed into the footer bin.
_NB_CTA_BG = "#030711"
_NB_SURFACE = "#050505"


def test_cta_bg_guard_predicate_distinguishes_the_disco_pair() -> None:
    cta = _color(_NB_CTA_BG)
    surface = _color(_NB_SURFACE)

    # OKLab would merge them (within the cluster radius); CIEDE2000 says clearly distinct.
    assert delta_e(cta, surface) <= DELTA_E_CLUSTER
    assert ciede2000(cta, surface) > _CTA_BG_GUARD_MAX_DE2000
    assert cta.lightness <= _NEAR_BLACK_LIGHTNESS
    assert surface.lightness <= _NEAR_BLACK_LIGHTNESS

    assert _forbids_cta_bg_merge(cta, surface)
    assert _forbids_cta_bg_merge(surface, cta)  # symmetric
    assert not _forbids_cta_bg_merge(cta, cta)  # never forbids itself


def test_cta_bg_guard_is_near_black_only_not_near_white() -> None:
    # Deliberate asymmetry vs the near-white text guard: the near-white surface-variant cloud is
    # where OKLab's denoising is load-bearing, so the CTA bg guard never engages near white even
    # for a CIEDE2000-distinct pair (measured: a symmetric variant regresses the panel).
    white = _color(_WHITE)
    primer = _color(_PRIMER_NEAR_WHITE)
    assert ciede2000(white, primer) > _CTA_BG_GUARD_MAX_DE2000  # distinct...
    assert not _forbids_cta_bg_merge(white, primer)  # ...yet the near-black guard ignores it

    # A near-black and a mid-gray: not both near-black, so the guard never engages.
    assert not _forbids_cta_bg_merge(_color(_NB_CTA_BG), _color("#9198a1"))


def test_cta_bg_guard_allows_near_black_anti_alias_variants_to_merge() -> None:
    # The radius is a denoising radius: genuine near-black surface variants still merge.
    for a, b in (("#000000", "#010101"), ("#08090b", _NB_SURFACE)):
        first, second = _color(a), _color(b)
        assert ciede2000(first, second) <= _CTA_BG_GUARD_MAX_DE2000
        assert not _forbids_cta_bg_merge(first, second)


def test_entry_has_cta_action_mass() -> None:
    cta = _Entry(_color(_NB_CTA_BG), 0.0)
    cta.component_mix[ComponentType.cta_bg] = 1.0
    surface = _Entry(_color(_NB_SURFACE), 0.1)
    surface.component_mix[ComponentType.footer_bg] = 1.0
    assert _entry_has_cta_action_mass(cta)
    assert not _entry_has_cta_action_mass(surface)


def test_cta_bg_splits_from_near_black_surface_bin() -> None:
    # The disco scenario end-to-end (attribution + cluster guard): a clickable dark CTA whose bg
    # is `#030711` must surface as its own background cluster carrying cta_bg mass, not be absorbed
    # into the `#050505` footer bin that OKLab would merge it into.
    harvest = _harvest([ScreenshotBin(color=_color(_NB_SURFACE), area_fraction=0.4)])
    classified = [_classified(_color(_NB_CTA_BG), {ComponentType.cta_bg: 1.0})]

    clusters = build_inventory(harvest, classified)
    cta_clusters = [c for c in clusters if ComponentType.cta_bg in c.component_mass]
    assert len(cta_clusters) == 1
    assert cta_clusters[0].color.hex == _NB_CTA_BG
    # The surface bin keeps its area and does NOT pick up the cta_bg mass.
    surface = [c for c in clusters if c.color.hex == _NB_SURFACE]
    assert surface and ComponentType.cta_bg not in surface[0].component_mass


def test_near_black_page_bg_still_merges_into_surface_bin() -> None:
    # Scoping proof: the guard is CTA/action-only. The SAME `#030711`/`#050505` pair that splits
    # above merges when the element's mass is page_bg (not cta/action) — page/surface attribution
    # keeps the pure OKLab radius so the denoiser stays intact.
    harvest = _harvest([ScreenshotBin(color=_color(_NB_SURFACE), area_fraction=0.4)])
    classified = [_classified(_color(_NB_CTA_BG), {ComponentType.page_bg: 1.0})]

    clusters = build_inventory(harvest, classified)
    bg_clusters = [c for c in clusters if c.area_weight > 0.0 or c.component_mass]
    assert len(bg_clusters) == 1  # one merged background cluster
    assert bg_clusters[0].color.hex == _NB_SURFACE  # max-area member wins
    assert ComponentType.page_bg in bg_clusters[0].component_mass


def test_near_black_mixed_cta_and_page_mass_is_guarded() -> None:
    # Accepted edge (see `_CTA_ACTION_BG_COMPONENTS`): the gate is presence-based, so an element the
    # softmax scores as mostly page_bg but partly cta_bg still routes its WHOLE near-black bg vote
    # through the guard — it splits off the distinct surface bin rather than merging the page share.
    # Pinned deliberately: surfacing a distinct dark CTA is preferred over a dominance threshold
    # that would drop genuine dark CTAs with split classifier mass; the element vote carries no area
    # so it cannot displace the area-ranked surface bin in page/surface.
    harvest = _harvest([ScreenshotBin(color=_color(_NB_SURFACE), area_fraction=0.4)])
    classified = [
        _classified(_color(_NB_CTA_BG), {ComponentType.page_bg: 0.9, ComponentType.cta_bg: 0.1})
    ]

    clusters = build_inventory(harvest, classified)
    # The CTA color splits off as its own entry, carrying both components of the mixed vote.
    cta_clusters = [c for c in clusters if c.color.hex == _NB_CTA_BG]
    assert len(cta_clusters) == 1
    assert ComponentType.cta_bg in cta_clusters[0].component_mass
    assert ComponentType.page_bg in cta_clusters[0].component_mass
    # The area-ranked surface bin keeps its area and is untouched as the page/surface winner.
    surface = next(c for c in clusters if c.color.hex == _NB_SURFACE)
    assert surface.area_weight == pytest.approx(0.4, abs=1e-9)


def test_cta_bg_guard_survives_union_find_transitivity() -> None:
    # Transitivity safety (mirrors the near-white test): A (a CTA bg) and C are guard-forbidden;
    # bridge B is mergeable with each. A must not chain to C through B. The guard only blocks
    # because a member of the offending pair carries CTA/action mass.
    a, b, c = _color("#000000"), _color("#000002"), _color("#000008")
    assert _forbids_cta_bg_merge(a, c)
    assert not _forbids_cta_bg_merge(a, b)
    assert not _forbids_cta_bg_merge(b, c)
    assert delta_e(a, b) <= DELTA_E_CLUSTER and delta_e(b, c) <= DELTA_E_CLUSTER

    # Equal (zero) areas so the representative tiebreak falls to the smallest hex, isolating the
    # transitivity behaviour from area-based representative selection.
    entry_a = _Entry(a, 0.0)
    entry_a.component_mix[ComponentType.cta_bg] = 1.0  # the CTA that must keep its identity
    entries = [entry_a, _Entry(b, 0.0), _Entry(c, 0.0)]
    clusters = _cluster_pool(entries, PropertyFamily.background)

    reps = {cluster.color.hex for cluster in clusters}
    assert len(clusters) == 2  # A and C never co-cluster, even through the bridge
    assert "#000000" in reps and "#000008" in reps


def test_background_pool_merges_distinct_near_blacks_without_cta_mass() -> None:
    # Without any CTA/action mass the background pool keeps the pure OKLab radius: the same
    # forbidden-by-distance near-black pair still merges (the guard never engages).
    harvest = _harvest(
        [
            ScreenshotBin(color=_color(_NB_SURFACE), area_fraction=0.6),
            ScreenshotBin(color=_color(_NB_CTA_BG), area_fraction=0.4),
        ]
    )
    clusters = build_inventory(harvest, [])
    assert len(clusters) == 1
    assert clusters[0].member_count == 2
    assert clusters[0].color.hex == _NB_SURFACE  # max-area member wins
