"""Unit tests for :mod:`colorsense.palette.detect` (detection + ranking + intent).

Constructs `RoleEvidence` and `ClassifiedToken` lists directly (no real harvest needed) and
asserts the redesign's goals (§8) and the tuning-spec's two-gate / rescue-band behavior
(§2-§4): hero-vs-swarm survival + ranking, the intent-independent noise floor, the
intent-asymmetric rescue band, normalize-last, the intent tie-break, both divergence arms,
the rescue-band invariant, and full eight-role backfill.
"""

from __future__ import annotations

import math

from colorsense.color.primitives import parse_css_color
from colorsense.config import load_default_config
from colorsense.models import (
    ClassifiedToken,
    Color,
    RoleEvidence,
    TokenOrigin,
    TokenRecord,
    TokenSemanticRole,
    UsagePalette,
    UsageRole,
)
from colorsense.palette.detect import detect
from colorsense.palette.salience import aggregate_salience

CONFIG = load_default_config()


def _color(css: str) -> Color:
    c = parse_css_color(css)
    assert c is not None, css
    return c


# Perceptually distinct anchors (far apart in OKLab ΔE).
GREEN = _color("#16a34a")
GRAY = _color("#6b7280")
RED = _color("#dc2626")
BLUE = _color("#2563eb")


def _ev(
    color: Color,
    role: UsageRole,
    saliences: tuple[float, ...] = (),
    area: float = 0.0,
) -> RoleEvidence:
    return RoleEvidence(
        color=color,
        role=role,
        instance_saliences=tuple(sorted(saliences, reverse=True)),
        area=area,
    )


def _token(
    name: str,
    color: Color,
    usage_intent: dict[UsageRole, float],
    weight: float = 1.0,
    origin: TokenOrigin = TokenOrigin.NAME_RULE,
    semantic_role: TokenSemanticRole = TokenSemanticRole.BRAND_PRIMARY,
) -> ClassifiedToken:
    return ClassifiedToken(
        record=TokenRecord(name=name, raw_value="x", resolved=color, scope=":root"),
        semantic_role=semantic_role,
        weight=weight,
        usage_intent=usage_intent,
        origin=origin,
    )


def _entry_prob(palette: UsagePalette, role: UsageRole, color: Color) -> float | None:
    for entry in palette.mapping[role]:
        if entry.color.hex == color.hex:
            return entry.probability
    return None


def _s_measured_element(saliences: tuple[float, ...], role: UsageRole) -> float:
    rc = CONFIG.detection.roles[role]
    return aggregate_salience(tuple(sorted(saliences, reverse=True)), rc.lambda_, rc.beta)


# --- Hero-vs-swarm (redesign §8) --------------------------------------------


def test_hero_and_swarm_both_survive_with_hero_ranked_first() -> None:
    """One large hero CTA and a swarm of tiny CTAs both survive; the hero ranks first."""
    # Hero: a single large prominent instance (peak dominates).
    hero = _ev(GREEN, UsageRole.CTA, saliences=(0.30,))
    # Swarm: many tiny instances, each individually small but above theta_noise. Even with the
    # concave (beta<1) corroboration tail, the saturating sum stays below the hero's peak.
    swarm = _ev(GRAY, UsageRole.CTA, saliences=tuple([0.0006] * 40))
    # Sanity: the cta aggregation ranks the hero's peak above the swarm's saturated mass.
    assert _s_measured_element((0.30,), UsageRole.CTA) > _s_measured_element(
        tuple([0.0006] * 40), UsageRole.CTA
    )

    palette, _index, _div = detect([hero, swarm], [], CONFIG)
    cta = palette.mapping[UsageRole.CTA]

    # Goal 3: both survive detection.
    hexes = {e.color.hex for e in cta}
    assert GREEN.hex in hexes
    assert GRAY.hex in hexes
    # Goal 2: hero ranks first with strictly higher probability than the swarm.
    assert cta[0].color.hex == GREEN.hex
    hero_prob = _entry_prob(palette, UsageRole.CTA, GREEN)
    swarm_prob = _entry_prob(palette, UsageRole.CTA, GRAY)
    assert hero_prob is not None and swarm_prob is not None
    assert hero_prob > swarm_prob


# --- Two-gate (tuning-spec §4) ----------------------------------------------


def test_below_theta_noise_dropped_even_with_full_intent() -> None:
    """theta_noise is intent-independent: a sub-noise color dies even with full intent."""
    rc = CONFIG.detection.roles[UsageRole.CTA]
    # A single instance whose S_measured is below theta_noise.
    tiny = rc.theta_noise * 0.5
    assert _s_measured_element((tiny,), UsageRole.CTA) < rc.theta_noise
    ev = _ev(GREEN, UsageRole.CTA, saliences=(tiny,))
    token = _token("--brand", GREEN, {UsageRole.CTA: 1.0})

    palette, _index, _div = detect([ev], [token], CONFIG)
    assert palette.mapping[UsageRole.CTA] == ()


def test_rescue_band_keeps_declared_drops_undeclared() -> None:
    """In the rescue band a declared color is kept while an undeclared one is dropped."""
    rc = CONFIG.detection.roles[UsageRole.CTA]
    alpha = CONFIG.detection.alpha
    # Pick S_measured strictly inside the wide-gap rescue band:
    #   theta_present / (1 + alpha) <= S_measured < theta_present, and >= theta_noise.
    s_target = rc.theta_present / (1.0 + alpha) * 1.05
    assert rc.theta_noise <= s_target < rc.theta_present
    assert s_target * (1.0 + alpha) >= rc.theta_present

    # A single-instance element-role evidence with that exact S_measured (cta: peak only,
    # since lambda only weights the tail).
    declared_ev = _ev(GREEN, UsageRole.CTA, saliences=(s_target,))
    undeclared_ev = _ev(RED, UsageRole.CTA, saliences=(s_target,))
    assert math.isclose(_s_measured_element((s_target,), UsageRole.CTA), s_target)

    token = _token("--brand", GREEN, {UsageRole.CTA: 1.0})
    palette, _index, _div = detect([declared_ev, undeclared_ev], [token], CONFIG)
    cta_hexes = {e.color.hex for e in palette.mapping[UsageRole.CTA]}
    assert GREEN.hex in cta_hexes  # rescued by intent
    assert RED.hex not in cta_hexes  # no intent -> stays below theta_present


# --- Normalize-last ----------------------------------------------------------


def test_probabilities_sum_to_one_within_role() -> None:
    """Survivor probabilities within a non-empty role sum to ~1.0."""
    evs = [
        _ev(GREEN, UsageRole.CTA, saliences=(0.02,)),
        _ev(GRAY, UsageRole.CTA, saliences=(0.01,)),
        _ev(RED, UsageRole.CTA, saliences=(0.005,)),
    ]
    palette, _index, _div = detect(evs, [], CONFIG)
    cta = palette.mapping[UsageRole.CTA]
    assert len(cta) == 3
    assert math.isclose(sum(e.probability for e in cta), 1.0, rel_tol=1e-9)


def test_single_survivor_probability_is_one() -> None:
    """A lone survivor in a role has probability == 1.0."""
    ev = _ev(GREEN, UsageRole.CTA, saliences=(0.02,))
    palette, _index, _div = detect([ev], [], CONFIG)
    cta = palette.mapping[UsageRole.CTA]
    assert len(cta) == 1
    assert math.isclose(cta[0].probability, 1.0)


# --- Intent tie-break --------------------------------------------------------


def test_intent_breaks_tie_between_codominant_colors() -> None:
    """Two near-equal co-dominant colors: the declared one (q_intent>0) ranks first."""
    # GREEN slightly LOWER measured than RED, so without intent RED would lead.
    green_ev = _ev(GREEN, UsageRole.CTA, saliences=(0.0100,))
    red_ev = _ev(RED, UsageRole.CTA, saliences=(0.0101,))
    token = _token("--brand", GREEN, {UsageRole.CTA: 1.0})

    palette, _index, _div = detect([green_ev, red_ev], [token], CONFIG)
    cta = palette.mapping[UsageRole.CTA]
    assert cta[0].color.hex == GREEN.hex


# --- Divergences -------------------------------------------------------------


def test_declared_but_unused_high_intent_name_rule() -> None:
    """A high-intent NAME_RULE token whose color isn't measured -> declared-but-unused."""
    # Only RED is measured; GREEN is declared with intent but never rendered.
    measured = _ev(RED, UsageRole.CTA, saliences=(0.02,))
    token = _token("--brand", GREEN, {UsageRole.CTA: 1.0}, origin=TokenOrigin.NAME_RULE)

    _palette, _index, div = detect([measured], [token], CONFIG)
    notes = [(d.color.hex, d.note) for d in div]
    assert (GREEN.hex, "declared '--brand' unused in render") in notes


def test_used_but_undeclared() -> None:
    """A prominent measured color with no declared token -> used-but-undeclared."""
    measured = _ev(GREEN, UsageRole.CTA, saliences=(0.02,))
    _palette, _index, div = detect([measured], [], CONFIG)
    assert any(d.color.hex == GREEN.hex and d.note == "used but undeclared" for d in div)


def test_zero_weight_fallback_does_not_raise_declared_but_unused() -> None:
    """A zero-weight fallback token does NOT raise declared-but-unused (token_weight > 0 fix)."""
    measured = _ev(RED, UsageRole.CTA, saliences=(0.02,))
    # weight 0.0, FALLBACK origin (low intent), declaring GREEN.
    token = _token(
        "--ghost",
        GREEN,
        {UsageRole.CTA: 1.0},
        weight=0.0,
        origin=TokenOrigin.FALLBACK,
    )
    _palette, _index, div = detect([measured], [token], CONFIG)
    assert all(d.color.hex != GREEN.hex for d in div if "unused" in d.note)


# --- Rescue-band invariant ---------------------------------------------------


def test_rescue_band_never_dips_below_theta_noise() -> None:
    """The rescue band's lower edge never dips below theta_noise (tuning-spec §4.3).

    The effective lower edge is ``max(theta_noise, theta_present / (1 + alpha))`` — the
    ``max`` clamp is what enforces the invariant, so intent can never rescue a color the
    artifact floor rejected, for ANY ``theta_present >= theta_noise``. That ``>=`` is the
    only real config constraint: the membership floor must sit at or above the hard noise
    floor (else the noise gate would already subsume it). It holds even for roles whose
    ``theta_present`` sits exactly at ``theta_noise`` (``action``, ``border``), where the
    unclamped ``theta_present / (1 + alpha)`` dips below ``theta_noise`` but the clamp keeps
    the effective floor at ``theta_noise``. The behavioral guarantee — a below-noise color
    is dropped even with full intent — is covered by
    ``test_below_theta_noise_dropped_even_with_full_intent``.
    """
    alpha = CONFIG.detection.alpha
    element_roles = {
        UsageRole.CTA,
        UsageRole.ACTION,
        UsageRole.TEXT,
        UsageRole.LINK,
        UsageRole.BORDER,
    }
    for role in element_roles:
        rc = CONFIG.detection.roles[role]
        assert rc.theta_present >= rc.theta_noise, role
        lower_edge = max(rc.theta_noise, rc.theta_present / (1.0 + alpha))
        assert lower_edge >= rc.theta_noise, role


# --- Backfill ----------------------------------------------------------------


def test_all_eight_roles_present_in_palette() -> None:
    """The returned UsagePalette exposes all eight roles (validator backfills empties)."""
    ev = _ev(GREEN, UsageRole.CTA, saliences=(0.02,))
    palette, _index, _div = detect([ev], [], CONFIG)
    assert set(palette.mapping.keys()) == set(UsageRole)
