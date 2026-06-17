"""Unit tests for the usage builders (palette/usage.py): role-keyed projection + color index."""

from __future__ import annotations

import math

from colorsense.color.primitives import parse_css_color
from colorsense.models import (
    Color,
    ColorCluster,
    ComponentType,
    PropertyFamily,
    UsageRole,
)
from colorsense.palette.usage import (
    _AREA_RANKED_ROLES,
    COMPONENT_ROLE,
    MIN_EXEMPT_VOTE_MASS,
    MIN_PROBABILITY_SHARE,
    ROLE_COMPONENTS,
    build_color_index,
    build_usage,
)


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


# ---------------------------------------------------------------------------
# ROLE_COMPONENTS / partition
# ---------------------------------------------------------------------------


def test_role_components_partitions_every_routed_component_once() -> None:
    # COMPONENT_ROLE is the exact inverse of ROLE_COMPONENTS, one role per routed component.
    flat = [c for comps in ROLE_COMPONENTS.values() for c in comps]
    assert len(flat) == len(set(flat))  # no component routed twice
    assert set(COMPONENT_ROLE) == set(flat)
    for role, comps in ROLE_COMPONENTS.items():
        for comp in comps:
            assert COMPONENT_ROLE[comp] is role


def test_cta_text_and_third_party_are_unrouted() -> None:
    # Both are deliberately absent from every role and from the inverse map.
    assert ComponentType.cta_text not in COMPONENT_ROLE
    assert ComponentType.third_party not in COMPONENT_ROLE
    # Everything else IS routed.
    assert set(COMPONENT_ROLE) == set(ComponentType) - {
        ComponentType.cta_text,
        ComponentType.third_party,
    }


# ---------------------------------------------------------------------------
# Role-keyed projection (build_usage)
# ---------------------------------------------------------------------------


def test_empty_cluster_list_yields_empty_backfilled_palette() -> None:
    palette = build_usage([])
    assert set(palette.mapping) == set(UsageRole)
    for role in UsageRole:
        assert palette.mapping[role] == ()


def test_role_with_no_mass_backfills_to_empty() -> None:
    # Only page-bg mass anywhere: every other role must be backfilled to ().
    palette = build_usage([_cluster("#ffffff", 0.8, {ComponentType.page_bg: 2.0})])
    assert palette.mapping[UsageRole.page] != ()
    for role in UsageRole:
        if role is not UsageRole.page:
            assert palette.mapping[role] == ()


def test_surface_roles_ranked_by_area_not_vote_mass() -> None:
    # 30 repeated cards (high vote mass, small area) must NOT outrank an 86%-area page bg.
    # (Surface roles — page/surface/banner — are area-ranked; cta/action are not.)
    page = _cluster("#ffffff", 0.86, {ComponentType.page_bg: 1.0})
    cards = _cluster("#f6f8fa", 0.08, {ComponentType.card_bg: 30.0})
    palette = build_usage([page, cards])

    # page bg -> page role; cards -> surface role (no longer the same slot).
    page_role = palette.mapping[UsageRole.page]
    surface_role = palette.mapping[UsageRole.surface]
    assert [e.color.hex for e in page_role] == [_color("#ffffff").hex]
    assert [e.color.hex for e in surface_role] == [_color("#f6f8fa").hex]
    # A single cluster in each role normalizes to probability 1.0 (area-proportional within role).
    assert page_role[0].probability == 1.0
    assert surface_role[0].probability == 1.0


def test_banner_role_groups_header_nav_footer() -> None:
    header = _cluster("#0d1117", 0.05, {ComponentType.header_bg: 3.0})
    footer = _cluster("#161b22", 0.04, {ComponentType.footer_bg: 2.0})
    palette = build_usage([header, footer])
    banner = palette.mapping[UsageRole.banner]
    assert {e.color.hex for e in banner} == {_color("#0d1117").hex, _color("#161b22").hex}


def test_text_ranked_by_log_damped_vote_mass() -> None:
    body = _cluster("#1f2328", 0.0, {ComponentType.page_text: 10.0})
    muted = _cluster("#59636e", 0.0, {ComponentType.page_text: 4.0, ComponentType.card_text: 1.0})
    palette = build_usage([body, muted])

    text = palette.mapping[UsageRole.text]
    # Ordering follows raw mass (log1p is monotonic); shares are log1p-compressed.
    assert [e.color.hex for e in text] == [_color("#1f2328").hex, _color("#59636e").hex]
    total = math.log1p(10.0) + math.log1p(5.0)
    assert math.isclose(text[0].probability, math.log1p(10.0) / total, abs_tol=1e-9)
    assert math.isclose(text[1].probability, math.log1p(5.0) / total, abs_tol=1e-9)


def test_cta_survives_in_its_own_role() -> None:
    # The redesign's payoff: the colored CTA gets its OWN role, so it no longer competes
    # with ~200 link votes for one "interactive" slot. As the only cta-mass cluster it
    # normalizes to 1.0 within the cta role (which is ranked by vote mass, not area).
    links_a = _cluster("#59636e", 0.0, {ComponentType.link: 93.0})
    links_b = _cluster("#0969da", 0.0, {ComponentType.link: 48.0})
    cta = _cluster("#1f883d", 0.02, {ComponentType.cta_bg: 1.0})
    palette = build_usage([links_a, links_b, cta])

    assert [e.color.hex for e in palette.mapping[UsageRole.cta]] == [_color("#1f883d").hex]
    assert palette.mapping[UsageRole.cta][0].probability == 1.0
    link_hexes = [e.color.hex for e in palette.mapping[UsageRole.link]]
    assert _color("#0969da").hex in link_hexes
    assert link_hexes[0] == _color("#59636e").hex  # mass-monotonic ordering within link


def test_cta_action_are_mass_ranked_not_area_ranked() -> None:
    # The taxonomy split: surfaces (page/surface/banner) rank by area; element colors
    # (cta/action/text/link/border) rank by vote mass. Guards against silent drift.
    assert {UsageRole.page, UsageRole.surface, UsageRole.banner} == _AREA_RANKED_ROLES
    assert UsageRole.cta not in _AREA_RANKED_ROLES
    assert UsageRole.action not in _AREA_RANKED_ROLES


def test_cta_brand_color_not_buried_by_high_area_page_background() -> None:
    # Regression for the area-ranked-cta bug: a huge-area page background that also carries
    # cta_bg mass (white "ghost"/secondary buttons share the page hex) must NOT bury the
    # small, zero-area brand CTA. Under the old area ranking the page bg (area 0.9) won the
    # cta role outright and the brand green was pruned below MIN_PROBABILITY_SHARE; under mass
    # ranking the higher-mass brand green wins and the page bg ranks below it. (The github case.)
    page = _cluster("#ffffff", 0.9, {ComponentType.page_bg: 1.0, ComponentType.cta_bg: 2.0})
    brand = _cluster("#08872b", 0.0, {ComponentType.cta_bg: 5.0})
    palette = build_usage([page, brand])

    cta = palette.mapping[UsageRole.cta]
    cta_hexes = [e.color.hex for e in cta]
    # Brand green survives AND outranks the high-area page bg (mass 5 > 2, area ignored).
    assert _color("#08872b").hex in cta_hexes
    assert cta_hexes[0] == _color("#08872b").hex
    # The page role itself is still area-ranked: white wins it.
    assert [e.color.hex for e in palette.mapping[UsageRole.page]] == [_color("#ffffff").hex]


def test_cta_winner_is_mass_deterministic_when_areas_tie() -> None:
    # The ds_site cross-OS flip, distilled: two zero-area CTAs (primary + secondary button)
    # whose screenshot bins flip across OSes. Area cannot decide (both 0 -> arbitrary hex
    # tiebreak), but DOM-derived vote mass is stable and picks the primary button on every
    # OS. Higher-mass amber (btn-primary) must beat lower-mass purple (btn-secondary)
    # regardless of hex order (purple's hex sorts first, so an area/hex tiebreak would pick
    # it — this asserts mass decides instead).
    amber = _cluster("#f59e0b", 0.0, {ComponentType.cta_bg: 0.865})
    purple = _cluster("#7c3aed", 0.0, {ComponentType.cta_bg: 0.737})
    palette = build_usage([amber, purple])

    cta = palette.mapping[UsageRole.cta]
    assert cta[0].color.hex == _color("#f59e0b").hex
    assert _color("#7c3aed").hex < _color("#f59e0b").hex  # purple would win a hex tiebreak


def test_action_brand_color_not_buried_by_high_area_background() -> None:
    # Same as the cta case, for the action role (secondary buttons / badges).
    page = _cluster("#ffffff", 0.9, {ComponentType.page_bg: 1.0, ComponentType.badge: 1.0})
    brand = _cluster("#7c3aed", 0.0, {ComponentType.button_secondary: 4.0})
    palette = build_usage([page, brand])

    action_hexes = [e.color.hex for e in palette.mapping[UsageRole.action]]
    assert action_hexes[0] == _color("#7c3aed").hex


def test_dual_use_color_appears_in_both_roles_with_correct_masses() -> None:
    # The same gray paints text AND borders: it must appear in both roles, each carrying
    # only its own component masses.
    gray = _cluster("#d1d9e0", 0.01, {ComponentType.page_text: 2.0, ComponentType.border: 6.0})
    palette = build_usage([gray])

    text = palette.mapping[UsageRole.text]
    border = palette.mapping[UsageRole.border]
    assert len(text) == 1 and len(border) == 1
    assert text[0].color.hex == border[0].color.hex == _color("#d1d9e0").hex
    assert text[0].components == {ComponentType.page_text: 1.0}
    assert border[0].components == {ComponentType.border: 1.0}


def test_components_evidence_normalized_within_role() -> None:
    cluster = _cluster(
        "#f6f8fa",
        0.2,
        {ComponentType.card_bg: 7.0, ComponentType.modal_bg: 3.0, ComponentType.border: 5.0},
    )
    palette = build_usage([cluster])
    (surface_entry,) = palette.mapping[UsageRole.surface]
    assert math.isclose(surface_entry.components[ComponentType.card_bg], 0.7, abs_tol=1e-9)
    assert math.isclose(surface_entry.components[ComponentType.modal_bg], 0.3, abs_tol=1e-9)
    assert ComponentType.border not in surface_entry.components
    assert surface_entry.area == 0.2


def test_third_party_and_cta_text_map_to_no_role() -> None:
    palette = build_usage(
        [
            _cluster("#ff00aa", 0.05, {ComponentType.third_party: 9.0}),
            _cluster("#00ffaa", 0.01, {ComponentType.cta_text: 4.0}),
        ]
    )
    for role in UsageRole:
        assert palette.mapping[role] == ()


def test_zero_area_background_cluster_prunes_against_real_one() -> None:
    page = _cluster("#ffffff", 0.9, {ComponentType.page_bg: 1.0})
    # A zero-area surface cluster scores zero prominence; with a real surface it prunes.
    real = _cluster("#f6f8fa", 0.2, {ComponentType.card_bg: 5.0})
    ghost = _cluster("#123456", 0.0, {ComponentType.card_bg: 5.0})
    palette = build_usage([page, real, ghost])

    surface = palette.mapping[UsageRole.surface]
    assert [e.color.hex for e in surface] == [_color("#f6f8fa").hex]
    assert surface[0].probability == 1.0


def test_all_zero_area_backgrounds_keep_argmax_fallback() -> None:
    a = _cluster("#bbbbbb", 0.0, {ComponentType.card_bg: 5.0})
    b = _cluster("#aaaaaa", 0.0, {ComponentType.card_bg: 5.0})
    palette = build_usage([a, b])

    surface = palette.mapping[UsageRole.surface]
    assert len(surface) == 1
    assert surface[0].color.hex == _color("#aaaaaa").hex  # smallest hex wins the tie
    assert surface[0].probability == 1.0


def test_prune_below_min_share_with_renormalization() -> None:
    strong = _cluster("#111111", 0.0, {ComponentType.page_text: 99.0})
    weak = _cluster("#222222", 0.0, {ComponentType.page_text: 0.05})
    assert math.log1p(0.05) / (math.log1p(99.0) + math.log1p(0.05)) < MIN_PROBABILITY_SHARE
    palette = build_usage([strong, weak])

    text = palette.mapping[UsageRole.text]
    assert [e.color.hex for e in text] == [_color("#111111").hex]
    assert text[0].probability == 1.0


def test_prune_emptying_role_keeps_argmax() -> None:
    # Many equal entries, each well below MIN_PROBABILITY_SHARE (1/60) AND below
    # MIN_EXEMPT_VOTE_MASS, so neither the share gate nor the mass-floor exemption keeps any:
    # the argmax fallback fires.
    n = 60
    mass = 0.1
    assert mass < MIN_EXEMPT_VOTE_MASS
    clusters = [_cluster(f"#0000{i:02x}", 0.0, {ComponentType.page_text: mass}) for i in range(n)]
    palette = build_usage(clusters)

    text = palette.mapping[UsageRole.text]
    assert len(text) == 1
    assert text[0].probability == 1.0
    assert text[0].color.hex == _color("#000000").hex


def test_mass_floor_exempts_genuine_low_share_element_color() -> None:
    # A role diluted by many entries: a genuine color's share falls below MIN_PROBABILITY_SHARE
    # purely from dilution, but its raw vote mass clears MIN_EXEMPT_VOTE_MASS, so the floor keeps
    # it. Mirrors the resend #46fea5 text recovery (a real accent diluted by the near-white split).
    dominant = _cluster("#111111", 0.0, {ComponentType.page_text: 99.0})
    fillers = [_cluster(f"#22{i:02x}00", 0.0, {ComponentType.page_text: 8.0}) for i in range(12)]
    accent = _cluster("#46fea5", 0.0, {ComponentType.page_text: MIN_EXEMPT_VOTE_MASS + 0.05})
    palette = build_usage([dominant, *fillers, accent])

    text = palette.mapping[UsageRole.text]
    hexes = [e.color.hex for e in text]
    accent_share = math.log1p(MIN_EXEMPT_VOTE_MASS + 0.05) / sum(
        math.log1p(sum(c.component_mass.values())) for c in [dominant, *fillers, accent]
    )
    assert accent_share < MIN_PROBABILITY_SHARE  # would be pruned on share alone
    assert _color("#46fea5").hex in hexes  # ...but the mass floor keeps it


def test_mass_floor_does_not_apply_to_area_ranked_surface_roles() -> None:
    # Surface (area-ranked) entries are NOT mass-floor-exempt: a zero-area card_bg with
    # high vote mass still prunes when a dominant-area surface owns the share.
    dominant = _cluster("#ffffff", 0.9, {ComponentType.card_bg: 1.0})
    massive_zero_area = _cluster("#0a0a0a", 0.0, {ComponentType.card_bg: 99.0})
    palette = build_usage([dominant, massive_zero_area])

    surface = palette.mapping[UsageRole.surface]
    assert [e.color.hex for e in surface] == [_color("#ffffff").hex]


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
    for entries in base.mapping.values():
        keys = [(-e.probability, e.color.hex) for e in entries]
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Color-keyed index (build_color_index)
# ---------------------------------------------------------------------------


def test_color_index_empty_for_no_clusters() -> None:
    assert build_color_index([]) == ()


def test_color_index_excludes_third_party_dominated_clusters() -> None:
    # A cluster with only third_party / cta_text mass has no routed usage and is dropped.
    index = build_color_index(
        [
            _cluster("#ffffff", 0.8, {ComponentType.page_bg: 1.0}),
            _cluster("#1f8ded", 0.05, {ComponentType.third_party: 9.0}),
        ]
    )
    assert [cu.color.hex for cu in index] == [_color("#ffffff").hex]


def test_color_index_per_color_usages_normalize() -> None:
    # A color used as cta (bg) and link (text): weights sum to ~1 and carry the family.
    gray = _cluster(
        "#0969da",
        0.02,
        {ComponentType.cta_bg: 3.0, ComponentType.link: 1.0},
    )
    (cu,) = build_color_index([gray])
    assert math.isclose(sum(u.weight for u in cu.usages), 1.0, abs_tol=1e-9)
    # Sorted by weight desc: cta (0.75) before link (0.25).
    assert [u.role for u in cu.usages] == [UsageRole.cta, UsageRole.link]
    assert math.isclose(cu.usages[0].weight, 0.75, abs_tol=1e-9)
    assert cu.usages[0].property_family is PropertyFamily.background
    assert cu.usages[1].property_family is PropertyFamily.text
    # property_family is always role.property_family.
    for u in cu.usages:
        assert u.property_family is u.role.property_family
    # Components within each slot normalize to 1.
    assert cu.usages[0].components == {ComponentType.cta_bg: 1.0}


def test_color_index_prominence_sorts_area_primary_but_keeps_accents() -> None:
    # The big page background ranks first by area; a zero-area brand CTA is NOT buried
    # last simply for having no area — its vote mass lifts it above a near-zero-mass color.
    page = _cluster("#ffffff", 0.9, {ComponentType.page_bg: 1.0})
    cta = _cluster("#1f883d", 0.0, {ComponentType.cta_bg: 40.0})
    faint = _cluster("#abcdef", 0.001, {ComponentType.border: 0.1})
    index = build_color_index([page, cta, faint])
    hexes = [cu.color.hex for cu in index]
    assert hexes[0] == _color("#ffffff").hex  # area-dominant first
    # The high-mass zero-area CTA outranks the near-zero faint color.
    assert hexes.index(_color("#1f883d").hex) < hexes.index(_color("#abcdef").hex)
    # prominence is monotonically non-increasing.
    proms = [cu.prominence for cu in index]
    assert proms == sorted(proms, reverse=True)


def test_color_index_deterministic_under_permutation() -> None:
    clusters = [
        _cluster("#ffffff", 0.7, {ComponentType.page_bg: 1.0}),
        _cluster("#f6f8fa", 0.2, {ComponentType.card_bg: 8.0}),
        _cluster("#0969da", 0.01, {ComponentType.link: 6.0}),
        _cluster("#1f883d", 0.01, {ComponentType.cta_bg: 2.0}),
    ]
    base = build_color_index(clusters)
    for permuted in (list(reversed(clusters)), clusters[2:] + clusters[:2]):
        assert build_color_index(permuted) == base


# ---------------------------------------------------------------------------
# Exact-hex re-merge: family-segregated clusters of the same hex collapse to one atom.
# ---------------------------------------------------------------------------


def test_color_index_same_hex_text_and_border_collapse_to_one_atom() -> None:
    # Family segregation can emit the SAME hex as a text cluster AND a border cluster.
    # The color-keyed index must show ONE atom listing both usages.
    text_cluster = _cluster("#1a1a1a", 0.0, {ComponentType.page_text: 4.0})
    border_cluster = _cluster("#1a1a1a", 0.0, {ComponentType.border: 2.0})

    index = build_color_index([text_cluster, border_cluster])

    atoms = [cu for cu in index if cu.color.hex == "#1a1a1a"]
    assert len(atoms) == 1
    roles = {u.role for u in atoms[0].usages}
    assert roles == {UsageRole.text, UsageRole.border}


def test_color_index_same_hex_merges_area_and_mass() -> None:
    # Merged atom: area = max member area, masses summed across families.
    bg_cluster = _cluster("#ffffff", 0.8, {ComponentType.page_bg: 1.0})
    text_cluster = _cluster("#ffffff", 0.0, {ComponentType.page_text: 3.0})

    index = build_color_index([bg_cluster, text_cluster])

    (atom,) = [cu for cu in index if cu.color.hex == "#ffffff"]
    assert atom.area == 0.8  # max of (0.8, 0.0)
    roles = {u.role for u in atom.usages}
    assert roles == {UsageRole.page, UsageRole.text}


def test_color_index_near_but_distinct_hexes_stay_two_atoms() -> None:
    # Two perceptually near but DISTINCT hexes (e.g. a border vs a bg color) are NOT
    # merged — exact-hex grouping preserves family-distinct colors.
    border = _cluster("#e5e5ea", 0.0, {ComponentType.border: 2.0})
    bg = _cluster("#ffffff", 0.9, {ComponentType.page_bg: 1.0})

    index = build_color_index([border, bg])

    hexes = {cu.color.hex for cu in index}
    assert hexes == {"#e5e5ea", "#ffffff"}
