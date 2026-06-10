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


def _candidate_lists(results: RoleResults) -> dict[PaletteRole, list[tuple[str, float]]]:
    """The full per-role (hex, probability) candidate lists, for whole-posterior equality."""
    return {
        role: [(c.color.hex, c.probability) for c in cands]
        for role, cands in results.mapping.items()
    }


def test_alpha_out_of_range_is_clamped() -> None:
    # A setup where alpha genuinely matters: usage and intent disagree about the accent,
    # so the alpha=0 and alpha=1 posteriors differ — proving the comparisons below are
    # not vacuous. Out-of-range alphas must clamp to the boundary posteriors exactly.
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate("#2563eb", 0.7),
                _candidate("#e11d48", 0.3),
            ]
        }
    )
    tokens = [_token("--accent", "#e11d48", {PaletteRole.accent: 1.0})]

    at_zero = _candidate_lists(reconcile(usage, tokens, alpha=0.0)[0])
    at_one = _candidate_lists(reconcile(usage, tokens, alpha=1.0)[0])
    assert at_zero != at_one  # alpha is load-bearing in this setup

    assert _candidate_lists(reconcile(usage, tokens, alpha=-0.5)[0]) == at_zero
    assert _candidate_lists(reconcile(usage, tokens, alpha=5.0)[0]) == at_one


def test_near_colors_join_across_usage_and_tokens() -> None:
    # The ΔE nearest-color join: #fa0202 is within DELTA_E_MATCH of the used #ff0000, so
    # the token must pool INTO the usage candidate (both signals on one color) instead of
    # surfacing as a separate token-only candidate.
    used_red = "#ff0000"
    declared_red = "#fa0202"
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate(used_red, 0.7),
                _candidate("#0000ff", 0.3),
            ]
        }
    )
    tokens = [_token("--brand-red", declared_red, {PaletteRole.accent: 1.0})]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    cands = posterior.mapping[PaletteRole.accent]
    hexes = {c.color.hex for c in cands}
    # No separate token-only candidate: the declared color merged into the usage color.
    assert _color(declared_red).hex not in hexes
    assert _color(used_red).hex in hexes

    joined = next(c for c in cands if c.color.hex == _color(used_red).hex)
    # The pooled posterior reflects BOTH signals.
    assert joined.evidence["p_usage"] == 0.7
    assert joined.evidence["p_intent"] == 1.0
    # Intent backing lifts the red above its pure-usage 0.7.
    assert joined.probability > 0.7

    # And the joined color is neither "declared but unused" nor "used but undeclared".
    assert not any(d.color.hex == _color(declared_red).hex for d in divergence)
    assert not any(d.color.hex == _color(used_red).hex for d in divergence)


def test_near_identical_tokens_aggregate_into_one_intent_group() -> None:
    # #2563eb and #2a66ec are within DELTA_E_MATCH: _aggregate_intent must fold them into
    # ONE intent group (one joined candidate, one divergence entry), with rep_name taken
    # from the heavier-weighted token.
    usage = RoleResults(mapping={PaletteRole.accent: [_candidate("#10b981", 1.0)]})
    tokens = [
        _token("--a-light-blue", "#2563eb", {PaletteRole.primary: 1.0}, weight=1.0),
        _token("--b-heavy-blue", "#2a66ec", {PaletteRole.primary: 1.0}, weight=3.0),
    ]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    # One group -> exactly one token-only candidate for primary, not two.
    primary = posterior.mapping[PaletteRole.primary]
    assert len(primary) == 1

    # One group -> exactly one declared-but-unused entry; rep_name is the heavier token.
    unused = [d for d in divergence if "unused" in d.note]
    assert len(unused) == 1
    assert unused[0].note == "declared '--b-heavy-blue' unused in render"
    assert unused[0].role == PaletteRole.primary


def test_colors_outside_delta_e_threshold_stay_separate() -> None:
    # #2563eb vs #10b981 are far outside DELTA_E_MATCH: two intent groups, two separate
    # token-only candidates, two separate divergence entries.
    usage = RoleResults(mapping={PaletteRole.accent: [_candidate("#e11d48", 1.0)]})
    tokens = [
        _token("--blue", "#2563eb", {PaletteRole.primary: 1.0}),
        _token("--green", "#10b981", {PaletteRole.primary: 1.0}),
    ]
    posterior, divergence = reconcile(usage, tokens, alpha=0.4)

    primary_hexes = {c.color.hex for c in posterior.mapping[PaletteRole.primary]}
    assert primary_hexes == {_color("#2563eb").hex, _color("#10b981").hex}
    unused_hexes = {d.color.hex for d in divergence if "unused" in d.note}
    assert unused_hexes == {_color("#2563eb").hex, _color("#10b981").hex}


def test_weak_candidates_pruned_and_survivors_renormalized() -> None:
    # At alpha=0 the posterior equals the usage distribution, so the 0.01 candidate falls
    # below MIN_POSTERIOR_PROB (0.02) and is pruned; the two survivors renormalize from
    # 0.495 each to 0.5 each (summing to ~1.0).
    weak = "#aaaaaa"
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate("#2563eb", 0.495),
                _candidate("#e11d48", 0.495),
                _candidate(weak, 0.01),
            ]
        }
    )
    posterior, _ = reconcile(usage, [], alpha=0.0)

    cands = posterior.mapping[PaletteRole.accent]
    hexes = {c.color.hex for c in cands}
    assert _color(weak).hex not in hexes  # pruned
    assert hexes == {_color("#2563eb").hex, _color("#e11d48").hex}
    assert math.isclose(sum(c.probability for c in cands), 1.0, abs_tol=1e-9)
    for cand in cands:
        assert math.isclose(cand.probability, 0.5, abs_tol=1e-9)


def test_pruning_that_empties_role_keeps_argmax_at_one() -> None:
    # 60 candidates, all below MIN_POSTERIOR_PROB: naive pruning would empty the role, so
    # the single argmax candidate must be kept at probability 1.0 instead.
    n = 60
    strongest = "#00ff00"
    weak_share = (1.0 - 0.019) / (n - 1)  # every candidate < MIN_POSTERIOR_PROB (0.02)
    candidates = [_candidate(strongest, 0.019)] + [
        _candidate(f"#0000{i:02x}", weak_share) for i in range(n - 1)
    ]
    assert all(c.probability < 0.02 for c in candidates)
    usage = RoleResults(mapping={PaletteRole.accent: candidates})
    posterior, _ = reconcile(usage, [], alpha=0.0)

    cands = posterior.mapping[PaletteRole.accent]
    assert len(cands) == 1
    assert cands[0].color.hex == _color(strongest).hex
    assert cands[0].probability == 1.0


def test_token_only_color_survives_at_default_alpha() -> None:
    # The alpha=0 test proves token-only colors prune at pure usage; this pins the
    # opposite: at alpha=0.4 a declared-only color keeps enough pooled mass to survive
    # when the role's usage evidence is weak (a low-probability usage candidate).
    usage = RoleResults(mapping={PaletteRole.accent: [_candidate("#2563eb", 0.3)]})
    token_only = "#10b981"
    tokens = [_token("--green", token_only, {PaletteRole.accent: 1.0})]
    posterior, _ = reconcile(usage, tokens, alpha=0.4)

    cands = posterior.mapping[PaletteRole.accent]
    hexes = {c.color.hex for c in cands}
    assert _color(token_only).hex in hexes  # survived pruning
    assert _color("#2563eb").hex in hexes
    assert math.isclose(sum(c.probability for c in cands), 1.0, abs_tol=1e-9)
    # Usage still dominates: the declared-only color survives but does not win.
    assert _prob_for(posterior, PaletteRole.accent, "#2563eb") > _prob_for(
        posterior, PaletteRole.accent, token_only
    )


def test_used_but_undeclared_threshold_boundary() -> None:
    # UNDECLARED_MIN_PROB = 0.15 gates the used-but-undeclared report: a 0.14 candidate
    # stays silent while a 0.16 candidate is reported.
    below = "#2563eb"
    above = "#e11d48"
    usage = RoleResults(
        mapping={
            PaletteRole.accent: [
                _candidate(above, 0.16),
                _candidate(below, 0.14),
            ]
        }
    )
    _, divergence = reconcile(usage, [], alpha=0.4)

    undeclared_hexes = {d.color.hex for d in divergence if d.note == "used but undeclared"}
    assert undeclared_hexes == {_color(above).hex}
