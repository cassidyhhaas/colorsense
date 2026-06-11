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
    mix: dict[ComponentType, float] | None = None,
    member_count: int = 1,
) -> ColorCluster:
    return ColorCluster(
        color=_color(css),
        area_weight=area,
        member_count=member_count,
        component_mix=mix or {},
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
