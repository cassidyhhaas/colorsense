"""Unit tests for reconciliation (log-linear pooling of intent + usage)."""

from __future__ import annotations

import math

from colorsense.color.primitives import parse_css_color
from colorsense.models import (
    ClassifiedToken,
    Color,
    PaletteCandidate,
    PaletteRole,
    RoleResults,
    TokenRecord,
    TokenSemanticRole,
)
from colorsense.palette.reconcile import reconcile


def _color(css: str) -> Color:
    c = parse_css_color(css)
    assert c is not None, css
    return c


def _candidate(css: str, prob: float, area: float = 0.1) -> PaletteCandidate:
    return PaletteCandidate(color=_color(css), probability=prob, area=area)


def _token(
    name: str,
    css: str,
    prior: dict[PaletteRole, float],
    weight: float = 1.0,
    semantic_role: TokenSemanticRole = TokenSemanticRole.brand_accent,
) -> ClassifiedToken:
    return ClassifiedToken(
        record=TokenRecord(
            name=name,
            raw_value=css,
            resolved=_color(css),
            scope=":root",
        ),
        semantic_role=semantic_role,
        weight=weight,
        palette_prior=prior,
    )


def _prob_for(results: RoleResults, role: PaletteRole, css: str) -> float:
    target = _color(css)
    for cand in results.mapping.get(role, []):
        if cand.color.hex == target.hex:
            return cand.probability
    raise AssertionError(f"{css} not present in role {role}")


def test_intent_boost_breaks_tie_toward_declared() -> None:
    a = "#2563eb"
    b = "#e11d48"
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate(a, 0.5),
                _candidate(b, 0.5),
            ]
        }
    )
    # Both colors are declared for accent, but A carries far more intent mass; this
    # should tip the 0.5/0.5 usage tie toward A while keeping B present.
    tokens = [
        _token("--accent", a, {PaletteRole.accent: 0.85, PaletteRole.primary: 0.15}),
        _token("--accent-2", b, {PaletteRole.accent: 0.15, PaletteRole.primary: 0.85}),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=0.4)

    p_a = _prob_for(posterior, PaletteRole.accent, a)
    p_b = _prob_for(posterior, PaletteRole.accent, b)
    assert p_a > p_b
    assert p_a > 0.5


def test_declared_but_unused_appears_in_divergence() -> None:
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [_candidate("#2563eb", 1.0)],
        }
    )
    unused = "#10b981"
    tokens = [
        _token("--primary", unused, {PaletteRole.primary: 1.0}),
    ]
    _, divergence = reconcile(usage, tokens, alpha=0.4)

    target = _color(unused)
    hits = [d for d in divergence if d.color.hex == target.hex and "unused" in d.note]
    assert hits, divergence
    assert hits[0].role == PaletteRole.primary


def test_alpha_zero_is_pure_usage() -> None:
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate("#2563eb", 0.7),
                _candidate("#e11d48", 0.3),
            ]
        }
    )
    # Strong intent for a token-only color that should be ignored at alpha=0.
    tokens = [
        _token("--accent", "#10b981", {PaletteRole.accent: 1.0}, weight=5.0),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=0.0)

    cands = posterior.mapping[PaletteRole.accent]
    hexes = {c.color.hex for c in cands}
    assert hexes == {_color("#2563eb").hex, _color("#e11d48").hex}

    p_blue = _prob_for(posterior, PaletteRole.accent, "#2563eb")
    p_rose = _prob_for(posterior, PaletteRole.accent, "#e11d48")
    # Ratio preserved: 0.7 / 0.3.
    assert math.isclose(p_blue / p_rose, 0.7 / 0.3, rel_tol=1e-4)


def test_alpha_one_is_pure_intent() -> None:
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate("#2563eb", 0.9),
                _candidate("#e11d48", 0.1),
            ]
        }
    )
    # Token favors the rose color strongly for accent.
    tokens = [
        _token("--accent", "#e11d48", {PaletteRole.accent: 1.0}, weight=3.0),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=1.0)

    cands = posterior.mapping[PaletteRole.accent]
    argmax = max(cands, key=lambda c: c.probability)
    assert argmax.color.hex == _color("#e11d48").hex


def test_every_role_distribution_normalized() -> None:
    usage = RoleResults(
        mapping={
            PaletteRole.primary: [
                _candidate("#2563eb", 0.6),
                _candidate("#1d4ed8", 0.4),
            ],
            PaletteRole.accent: [
                _candidate("#e11d48", 0.5),
                _candidate("#10b981", 0.5),
            ],
        }
    )
    tokens = [
        _token("--brand", "#2563eb", {PaletteRole.primary: 1.0}),
        _token("--pop", "#e11d48", {PaletteRole.accent: 1.0}),
    ]
    posterior, _ = reconcile(usage, tokens, alpha=0.4)

    # Every PaletteRole is always present (roles with no candidates map to []); a
    # non-empty role's candidate probabilities form a normalized distribution.
    assert set(posterior.mapping) == set(PaletteRole)
    for role, cands in posterior.mapping.items():
        if not cands:
            continue
        total = sum(c.probability for c in cands)
        assert math.isclose(total, 1.0, abs_tol=1e-6), (role, total)


def test_used_but_undeclared_appears_in_divergence() -> None:
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate("#2563eb", 0.8),
                _candidate("#e11d48", 0.2),
            ]
        }
    )
    # No tokens declared at all -> prominent usage color is undeclared.
    tokens: list[ClassifiedToken] = []
    _, divergence = reconcile(usage, tokens, alpha=0.4)

    target = _color("#2563eb")
    hits = [d for d in divergence if d.color.hex == target.hex and d.note == "used but undeclared"]
    assert hits, divergence
    assert hits[0].role == PaletteRole.accent


def test_alpha_out_of_range_is_clamped() -> None:
    usage = RoleResults(mapping={PaletteRole.accent: [_candidate("#2563eb", 1.0)]})
    # Should behave like alpha=1.0 / alpha=0.0 rather than raising.
    posterior_hi, _ = reconcile(usage, [], alpha=5.0)
    posterior_lo, _ = reconcile(usage, [], alpha=-5.0)
    assert PaletteRole.accent in posterior_hi.mapping
    assert PaletteRole.accent in posterior_lo.mapping
