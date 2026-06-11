"""Unit tests for the measured usage-palette builder (palette/usage.py)."""

from __future__ import annotations

import math

from colorsense.color.primitives import parse_css_color
from colorsense.models import Color, ColorCluster, ComponentType, UsageCategory
from colorsense.palette.usage import COMPONENT_USAGE, MIN_SHARE, build_usage


def _color(css: str) -> Color:
    c = parse_css_color(css)
    assert c is not None, css
    return c


def _cluster(
    css: str,
    area: float,
    mass: dict[ComponentType, float],
) -> ColorCluster:
    total = sum(mass.values())
    mix = {comp: val / total for comp, val in mass.items()} if total > 0 else {}
    return ColorCluster(
        color=_color(css),
        area_weight=area,
        member_count=1,
        component_mix=mix,
        component_mass=dict(mass),
    )


def test_empty_cluster_list_yields_empty_backfilled_palette() -> None:
    palette = build_usage([])
    assert set(palette.mapping) == set(UsageCategory)
    for category in UsageCategory:
        assert palette.mapping[category] == ()


def test_category_with_no_mass_backfills_to_empty() -> None:
    # Only surface mass anywhere: text/interactive/border must be backfilled to ().
    palette = build_usage([_cluster("#ffffff", 0.8, {ComponentType.page_bg: 2.0})])
    assert palette.mapping[UsageCategory.surface] != ()
    assert palette.mapping[UsageCategory.text] == ()
    assert palette.mapping[UsageCategory.interactive] == ()
    assert palette.mapping[UsageCategory.border] == ()


def test_surface_ranked_by_area_not_vote_mass() -> None:
    # The design rationale: 30 repeated cards (high vote mass, small area) must NOT
    # outrank an 86%-area page background (one vote, dominant area).
    page = _cluster("#ffffff", 0.86, {ComponentType.page_bg: 1.0})
    cards = _cluster("#f6f8fa", 0.08, {ComponentType.card_bg: 30.0})
    palette = build_usage([page, cards])

    surface = palette.mapping[UsageCategory.surface]
    assert [e.color.hex for e in surface][:2] == [_color("#ffffff").hex, _color("#f6f8fa").hex]
    assert surface[0].probability > surface[1].probability
    # Probabilities are area shares: 0.86 / (0.86 + 0.08).
    assert math.isclose(surface[0].probability, 0.86 / 0.94, abs_tol=1e-9)


def test_text_ranked_by_log_damped_vote_mass() -> None:
    body = _cluster("#1f2328", 0.0, {ComponentType.page_text: 10.0})
    muted = _cluster("#59636e", 0.0, {ComponentType.page_text: 4.0, ComponentType.card_text: 1.0})
    palette = build_usage([body, muted])

    text = palette.mapping[UsageCategory.text]
    # Ordering follows raw mass (log1p is monotonic); shares are the log1p-compressed
    # prominences, NOT the raw mass ratio 10:5.
    assert [e.color.hex for e in text] == [_color("#1f2328").hex, _color("#59636e").hex]
    total = math.log1p(10.0) + math.log1p(5.0)
    assert math.isclose(text[0].probability, math.log1p(10.0) / total, abs_tol=1e-9)
    assert math.isclose(text[1].probability, math.log1p(5.0) / total, abs_tol=1e-9)


def test_single_cta_survives_against_many_links() -> None:
    # The github.com regression for log1p damping: one high-confidence CTA (mass 1.0)
    # against link clusters with masses ~93/55/48 (~200 link votes). Under raw-mass
    # shares the CTA fell to ~0.005 (< MIN_SHARE) and the brand accent vanished from
    # the interactive category; log1p compression keeps it above the pruning floor.
    links_a = _cluster("#59636e", 0.0, {ComponentType.link: 93.0})
    links_b = _cluster("#002a36", 0.1, {ComponentType.link: 55.0})
    links_c = _cluster("#0969da", 0.0, {ComponentType.link: 48.0})
    cta = _cluster("#1f883d", 0.0, {ComponentType.cta_bg: 1.0})
    raw_share = 1.0 / (93.0 + 55.0 + 48.0 + 1.0)
    assert raw_share < MIN_SHARE  # the old behavior would have pruned the CTA

    palette = build_usage([links_a, links_b, links_c, cta])
    interactive = palette.mapping[UsageCategory.interactive]
    hexes = [e.color.hex for e in interactive]
    assert _color("#1f883d").hex in hexes
    # Ordering is still mass-monotonic: the heavy link clusters outrank the CTA.
    assert hexes[0] == _color("#59636e").hex


def test_dual_use_color_appears_in_both_categories_with_correct_masses() -> None:
    # The same gray paints text AND borders: it must appear in both categories, each
    # carrying only its own component masses.
    gray = _cluster("#d1d9e0", 0.01, {ComponentType.page_text: 2.0, ComponentType.border: 6.0})
    palette = build_usage([gray])

    text = palette.mapping[UsageCategory.text]
    border = palette.mapping[UsageCategory.border]
    assert len(text) == 1 and len(border) == 1
    assert text[0].color.hex == border[0].color.hex == _color("#d1d9e0").hex
    # components is the per-category mass normalized within the category.
    assert text[0].components == {ComponentType.page_text: 1.0}
    assert border[0].components == {ComponentType.border: 1.0}


def test_components_evidence_normalized_within_category() -> None:
    cluster = _cluster(
        "#f6f8fa",
        0.2,
        {ComponentType.card_bg: 7.0, ComponentType.modal_bg: 3.0, ComponentType.border: 5.0},
    )
    palette = build_usage([cluster])
    (surface_entry,) = palette.mapping[UsageCategory.surface]
    assert math.isclose(surface_entry.components[ComponentType.card_bg], 0.7, abs_tol=1e-9)
    assert math.isclose(surface_entry.components[ComponentType.modal_bg], 0.3, abs_tol=1e-9)
    assert ComponentType.border not in surface_entry.components
    assert surface_entry.area == 0.2


def test_third_party_mass_maps_to_no_category() -> None:
    palette = build_usage([_cluster("#ff00aa", 0.05, {ComponentType.third_party: 9.0})])
    for category in UsageCategory:
        assert palette.mapping[category] == ()
    # And the routing table itself never mentions third_party.
    assert ComponentType.third_party not in COMPONENT_USAGE


def test_zero_area_surface_cluster_prunes_against_real_surface() -> None:
    # An element-only surface color (no screenshot bin match -> area 0) scores zero
    # prominence and prunes naturally when a real-area surface exists.
    page = _cluster("#ffffff", 0.9, {ComponentType.page_bg: 1.0})
    ghost = _cluster("#123456", 0.0, {ComponentType.card_bg: 5.0})
    palette = build_usage([page, ghost])

    surface = palette.mapping[UsageCategory.surface]
    assert [e.color.hex for e in surface] == [_color("#ffffff").hex]
    assert surface[0].probability == 1.0


def test_all_zero_area_surfaces_keep_argmax_fallback() -> None:
    # If EVERY surface cluster has zero area, the category must not vanish: the argmax
    # fallback keeps exactly one entry at probability 1.0, deterministically (by hex).
    a = _cluster("#bbbbbb", 0.0, {ComponentType.card_bg: 5.0})
    b = _cluster("#aaaaaa", 0.0, {ComponentType.card_bg: 5.0})
    palette = build_usage([a, b])

    surface = palette.mapping[UsageCategory.surface]
    assert len(surface) == 1
    assert surface[0].color.hex == _color("#aaaaaa").hex  # smallest hex wins the tie
    assert surface[0].probability == 1.0


def test_prune_below_min_share_with_renormalization() -> None:
    # A genuinely tiny vote mass still prunes under log1p damping (log1p(0.05) ~ 0.05,
    # a < MIN_SHARE share against log1p(99) ~ 4.6); survivors renormalize to sum 1.
    strong = _cluster("#111111", 0.0, {ComponentType.page_text: 99.0})
    weak = _cluster("#222222", 0.0, {ComponentType.page_text: 0.05})
    assert math.log1p(0.05) / (math.log1p(99.0) + math.log1p(0.05)) < MIN_SHARE
    palette = build_usage([strong, weak])

    text = palette.mapping[UsageCategory.text]
    assert [e.color.hex for e in text] == [_color("#111111").hex]
    assert text[0].probability == 1.0


def test_prune_emptying_category_keeps_argmax() -> None:
    # 60 equal-mass text colors, each share ~1/60 < MIN_SHARE: pruning would empty the
    # category, so the argmax (smallest hex among ties) is kept at probability 1.0.
    n = 60
    clusters = [_cluster(f"#0000{i:02x}", 0.0, {ComponentType.page_text: 1.0}) for i in range(n)]
    palette = build_usage(clusters)

    text = palette.mapping[UsageCategory.text]
    assert len(text) == 1
    assert text[0].probability == 1.0
    assert text[0].color.hex == _color("#000000").hex


def test_deterministic_ordering_under_permutation() -> None:
    clusters = [
        _cluster("#ffffff", 0.7, {ComponentType.page_bg: 1.0}),
        _cluster("#f6f8fa", 0.2, {ComponentType.card_bg: 8.0}),
        _cluster("#1f2328", 0.0, {ComponentType.page_text: 9.0}),
        _cluster("#59636e", 0.0, {ComponentType.page_text: 3.0, ComponentType.border: 4.0}),
        _cluster("#0969da", 0.01, {ComponentType.link: 6.0}),
        _cluster("#1f883d", 0.01, {ComponentType.cta_bg: 2.0}),
    ]
    base = build_usage(clusters)
    for permuted in (list(reversed(clusters)), clusters[3:] + clusters[:3]):
        assert build_usage(permuted) == base
    # Within every category, entries are sorted by (-probability, hex).
    for entries in base.mapping.values():
        keys = [(-e.probability, e.color.hex) for e in entries]
        assert keys == sorted(keys)


def test_every_component_type_except_third_party_is_routed() -> None:
    # The routing table is total over the taxonomy minus third_party — a new
    # ComponentType must be deliberately routed (or excluded) here.
    assert set(COMPONENT_USAGE) == set(ComponentType) - {ComponentType.third_party}
