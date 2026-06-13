"""Unit tests for palette role assignment (:mod:`colorsense.palette.roles`)."""

from __future__ import annotations

import itertools

import pytest

from colorsense.color.primitives import parse_css_color
from colorsense.models import (
    Color,
    ColorCluster,
    ComponentType,
    PaletteRole,
)
from colorsense.palette.roles import assign_roles

PROB_TOL = 1e-6


def _color(css: str) -> Color:
    c = parse_css_color(css)
    assert c is not None, f"unparseable test color: {css}"
    return c


def _cluster(
    css: str,
    area: float,
    mass: dict[ComponentType, float] | None = None,
    member_count: int = 1,
) -> ColorCluster:
    """Build a cluster from raw component vote ``mass`` (mix derived by normalizing).

    Role scoring reads the raw ``component_mass`` (cross-cluster magnitude matters);
    ``component_mix`` is carried for model completeness like the inventory does.
    """
    mass = mass or {}
    total = sum(mass.values())
    mix = {comp: m / total for comp, m in mass.items()} if total > 0 else {}
    return ColorCluster(
        color=_color(css),
        area_weight=area,
        member_count=member_count,
        component_mix=mix,
        component_mass=mass,
    )


def _top(mapping: dict, role: PaletteRole) -> Color:
    return mapping[role][0].color


def test_high_area_neutral_is_primary() -> None:
    clusters = [
        _cluster("#f3f4f6", 0.60, {ComponentType.page_bg: 1.0}),
        _cluster("#2563eb", 0.10, {ComponentType.cta_bg: 1.0}),
        _cluster("#fde68a", 0.30, {ComponentType.card_bg: 1.0}),
    ]
    results, _ = assign_roles(clusters)
    assert _top(results.mapping, PaletteRole.primary).hex == "#f3f4f6"


def test_high_chroma_low_area_button_is_accent() -> None:
    # Small-area vivid button beats a much larger neutral for the accent role.
    clusters = [
        _cluster("#f3f4f6", 0.70, {ComponentType.page_bg: 1.0}),
        _cluster("#e11d48", 0.05, {ComponentType.cta_bg: 0.7, ComponentType.link: 0.3}),
        _cluster("#1f2937", 0.25, {ComponentType.footer_bg: 1.0}),
    ]
    results, _ = assign_roles(clusters)
    accent = results.mapping[PaletteRole.accent]
    assert accent[0].color.hex == "#e11d48"
    # Despite tiny area, it beats the large neutral page background for accent.
    accent_hexes = [c.color.hex for c in accent]
    assert "#e11d48" in accent_hexes
    # The vivid color outranks the neutral surface in the accent ranking.
    pos = {c.color.hex: i for i, c in enumerate(accent)}
    assert pos["#e11d48"] < pos.get("#f3f4f6", len(accent))


def test_high_area_card_can_be_secondary() -> None:
    clusters = [
        _cluster("#f3f4f6", 0.55, {ComponentType.page_bg: 1.0}),
        _cluster("#fde68a", 0.35, {ComponentType.card_bg: 1.0}),
        _cluster("#e11d48", 0.10, {ComponentType.cta_bg: 1.0}),
    ]
    results, _ = assign_roles(clusters)
    secondary = results.mapping[PaletteRole.secondary]
    top2 = [c.color.hex for c in secondary[:2]]
    assert "#fde68a" in top2


def test_per_role_probabilities_sum_to_one() -> None:
    clusters = [
        _cluster("#f3f4f6", 0.60, {ComponentType.page_bg: 1.0}),
        _cluster("#fde68a", 0.30, {ComponentType.card_bg: 1.0}),
        _cluster("#e11d48", 0.10, {ComponentType.cta_bg: 1.0}),
        _cluster("#1f2937", 0.05, {ComponentType.footer_bg: 1.0}),
    ]
    results, _ = assign_roles(clusters)
    for role, cands in results.mapping.items():
        if cands:
            total = sum(c.probability for c in cands)
            assert abs(total - 1.0) < PROB_TOL, f"{role} probs sum to {total}"


def test_candidates_sorted_descending() -> None:
    clusters = [
        _cluster("#f3f4f6", 0.50, {ComponentType.page_bg: 1.0}),
        _cluster("#fde68a", 0.30, {ComponentType.card_bg: 1.0}),
        _cluster("#e11d48", 0.20, {ComponentType.cta_bg: 1.0}),
    ]
    results, _ = assign_roles(clusters)
    for cands in results.mapping.values():
        probs = [c.probability for c in cands]
        assert probs == sorted(probs, reverse=True)


def test_fit_score_in_range_and_ordering() -> None:
    clean = [
        _cluster("#f3f4f6", 0.60, {ComponentType.page_bg: 1.0}),
        _cluster("#fde68a", 0.30, {ComponentType.card_bg: 1.0}),
        _cluster("#e11d48", 0.10, {ComponentType.cta_bg: 1.0}),
    ]
    _clean_results, clean_fit = assign_roles(clean)
    assert 0.0 <= clean_fit <= 1.0
    assert clean_fit > 0.8

    degenerate = [
        _cluster("#f3f4f6", 1.0, {ComponentType.page_bg: 1.0}),
    ]
    _, degen_fit = assign_roles(degenerate)
    assert 0.0 <= degen_fit <= 1.0
    assert clean_fit > degen_fit


def test_empty_clusters() -> None:
    results, fit = assign_roles([])
    # RoleResults backfills every role, so empty input yields all roles mapped to ().
    assert set(results.mapping) == set(PaletteRole)
    assert all(cands == () for cands in results.mapping.values())
    assert fit == 0.0


def test_assign_roles_is_input_order_independent() -> None:
    """Every permutation of the cluster list yields the identical role assignment.

    Pins the claimed hex tie-breaking / sorted-iteration determinism: a regression
    that lets Python list order leak into argmax or candidate ranking shows up as a
    different candidate order or drifted probabilities under some permutation.
    Probabilities are compared to 1e-12 (softmax sums the same floats in a
    different order, so the last few ulps may differ), candidate hex order exactly.
    """
    clusters = [
        _cluster("#f3f4f6", 0.60, {ComponentType.page_bg: 1.0}),
        _cluster("#fde68a", 0.25, {ComponentType.card_bg: 1.0}),
        _cluster("#e11d48", 0.10, {ComponentType.cta_bg: 1.0}),
        _cluster("#1f2937", 0.05, {ComponentType.footer_bg: 1.0}),
    ]
    base_results, base_fit = assign_roles(clusters)
    base = {
        role: [(c.color.hex, c.probability, c.area) for c in cands]
        for role, cands in base_results.mapping.items()
    }

    for perm in itertools.permutations(clusters):
        results, fit = assign_roles(list(perm))
        assert fit == pytest.approx(base_fit, abs=1e-12)
        assert set(results.mapping) == set(base)
        for role, expected in base.items():
            actual = [(c.color.hex, c.probability, c.area) for c in results.mapping[role]]
            assert [a[0] for a in actual] == [e[0] for e in expected], (role, perm)
            for (_, a_prob, a_area), (_, e_prob, e_area) in zip(actual, expected, strict=True):
                assert a_prob == pytest.approx(e_prob, abs=1e-12)
                assert a_area == e_area


def test_tiny_pure_structural_cluster_does_not_win_secondary() -> None:
    """Regression (disconetwork.com): mix purity without magnitude must not win secondary.

    A single 133x17px amber badge chip was a zero-area cluster with raw card_bg mass
    ~0.88 but component_mix purity 1.0, which maxed out the old mix-based secondary
    score and beat every well-evidenced surface. Raw-mass scoring (log1p of in-bucket
    mass, normalized by the per-bucket max across clusters) ranks the chip far down: a
    distinct light card surface carrying 40+ structural votes wins secondary instead.
    The dominant page background is the primary anchor and is excluded from secondary
    (see ``test_dominant_surface_excluded_from_secondary``), so the structural runner-up
    is the real second layer here.
    """
    clusters = [
        # The page surface: huge area + the largest primary mass -> primary anchor.
        _cluster("#ffffff", 0.55, {ComponentType.page_bg: 60.0}),
        # A distinct light card surface: the genuine second structural layer.
        _cluster("#e2e8f0", 0.30, {ComponentType.card_bg: 40.0}),
        # The badge chip: zero area, tiny raw mass, but 100% structural mix purity.
        _cluster("#f59e0b", 0.0, {ComponentType.card_bg: 0.88}),
        # Body text, so the set is not degenerate.
        _cluster("#050505", 0.15, {ComponentType.page_text: 30.0}),
    ]
    results, _ = assign_roles(clusters)
    secondary = results.mapping[PaletteRole.secondary]
    assert secondary[0].color.hex == "#e2e8f0"
    pos = {c.color.hex: i for i, c in enumerate(secondary)}
    # The chip ranks below the well-evidenced surface (or prunes out entirely).
    assert pos.get("#f59e0b", len(secondary)) > pos["#e2e8f0"]


def test_dominant_surface_excluded_from_secondary() -> None:
    """Regression (disconetwork.com / ds_site): the primary surface cannot also be secondary.

    The dominant page background accrues structural votes (cards/headers/nav/footer all
    painted in the page color) from sheer element count, so under raw-mass scoring it
    carries the largest secondary evidence *and* the largest area — it would win both
    primary and secondary, burying the actual ~30% structural color. The provisional
    primary cluster is excluded from secondary candidacy, so a distinct hero/header
    surface (one element, tiny mass, but a real structural band) wins secondary.
    """
    clusters = [
        # The page surface: large area + structural votes from many repeated elements.
        _cluster(
            "#ffffff",
            0.50,
            {
                ComponentType.page_bg: 10.0,
                ComponentType.card_bg: 3.0,
                ComponentType.header_bg: 1.0,
                ComponentType.nav_bg: 1.0,
                ComponentType.footer_bg: 1.0,
            },
        ),
        # A single chromatic hero band: big area, one element, tiny structural mass.
        _cluster("#2563eb", 0.33, {ComponentType.hero_bg: 1.0}),
        # A CTA so the set carries an accent-affine color too.
        _cluster("#e11d48", 0.10, {ComponentType.cta_bg: 1.0}),
    ]
    results, _ = assign_roles(clusters)
    secondary = results.mapping[PaletteRole.secondary]
    secondary_hexes = [c.color.hex for c in secondary]
    # The dominant surface is the primary anchor and never appears in secondary...
    assert _top(results.mapping, PaletteRole.primary).hex == "#ffffff"
    assert "#ffffff" not in secondary_hexes
    # ...so the genuine structural band wins it despite far less raw mass.
    assert secondary[0].color.hex == "#2563eb"


def test_single_cluster_yields_empty_secondary() -> None:
    """A one-color page has no second structural layer: secondary maps to ().

    The lone cluster is the primary anchor and is excluded from secondary candidacy,
    leaving secondary empty. This must not raise (``_fit_score`` reads ``cands[0]`` only
    when the list is non-empty) and the other roles still resolve.
    """
    clusters = [_cluster("#ffffff", 1.0, {ComponentType.page_bg: 1.0})]
    results, fit = assign_roles(clusters)
    assert results.mapping[PaletteRole.secondary] == ()
    assert results.mapping[PaletteRole.primary][0].color.hex == "#ffffff"
    assert set(results.mapping) == set(PaletteRole)
    assert 0.0 <= fit <= 1.0


def test_high_mass_diluted_chromatic_beats_low_mass_pure_for_accent() -> None:
    """Regression (disconetwork.com): accent evidence is magnitude, not mix purity.

    The brand purple's large accent-affine (link) mass was diluted across card_bg /
    page_text in its mix, while a minor green was link-pure with a fraction of the
    mass — and won accent under mix-based scoring. With raw mass the purple's far
    greater accent evidence must win.
    """
    clusters = [
        _cluster("#ffffff", 0.80, {ComponentType.page_bg: 80.0}),
        # Brand purple: big accent mass, diluted mix (link share only ~1/3).
        _cluster(
            "#7c3bed",
            0.0,
            {
                ComponentType.link: 15.0,
                ComponentType.card_bg: 20.0,
                ComponentType.page_text: 10.0,
            },
        ),
        # Minor green: link-pure mix but a fraction of the raw accent mass.
        _cluster("#10b77f", 0.0, {ComponentType.link: 2.0}),
    ]
    results, _ = assign_roles(clusters)
    accent = results.mapping[PaletteRole.accent]
    # Green pruning out of the accent list entirely also counts as losing.
    pos = {c.color.hex: i for i, c in enumerate(accent)}
    assert pos["#7c3bed"] < pos.get("#10b77f", len(accent))
    assert accent[0].color.hex == "#7c3bed"


def test_all_five_roles_present() -> None:
    clusters = [
        _cluster("#f3f4f6", 0.60, {ComponentType.page_bg: 1.0}),
        _cluster("#fde68a", 0.30, {ComponentType.card_bg: 1.0}),
        _cluster("#e11d48", 0.10, {ComponentType.cta_bg: 1.0}),
    ]
    results, _ = assign_roles(clusters)
    assert set(results.mapping) == set(PaletteRole)
    for cands in results.mapping.values():
        assert len(cands) >= 1
