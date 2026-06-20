"""Unit tests for the per-instance salience and role-aggregation helpers."""

from __future__ import annotations

import math

import pytest

from colorsense.config import (
    ContrastModulatorConfig,
    DetectionConfig,
    PositionModulatorConfig,
    RoleAggregationConfig,
    SiblingModulatorConfig,
    load_default_config,
)
from colorsense.models import BoundingBox, UsageRole, Viewport
from colorsense.palette.salience import (
    aggregate_salience,
    area_fraction,
    clamp,
    contrast_modulator,
    instance_prominence,
    intent_multiplier,
    position_modulator,
    sibling_modulator,
    vertical_fraction,
)

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


# ---------------------------------------------------------------------------
# clamp / area / vertical fraction
# ---------------------------------------------------------------------------


def test_clamp_bounds() -> None:
    assert clamp(-1.0, 0.0, 1.0) == 0.0
    assert clamp(2.0, 0.0, 1.0) == 1.0
    assert clamp(0.5, 0.0, 1.0) == 0.5


def test_area_fraction_clamped_to_unit_interval() -> None:
    full = BoundingBox(x=0.0, y=0.0, width=1280.0, height=800.0)
    assert area_fraction(full, VIEWPORT) == pytest.approx(1.0)
    # Oversized box clamps to 1.0, degenerate box stays strictly positive.
    huge = BoundingBox(x=0.0, y=0.0, width=5000.0, height=5000.0)
    assert area_fraction(huge, VIEWPORT) == pytest.approx(1.0)
    zero = BoundingBox(x=0.0, y=0.0, width=0.0, height=0.0)
    assert area_fraction(zero, VIEWPORT) > 0.0


def test_vertical_fraction_top_middle_and_below_fold() -> None:
    top = BoundingBox(x=0.0, y=0.0, width=10.0, height=0.0)
    assert vertical_fraction(top, VIEWPORT) == pytest.approx(0.0)
    middle = BoundingBox(x=0.0, y=400.0, width=10.0, height=0.0)
    assert vertical_fraction(middle, VIEWPORT) == pytest.approx(0.5)
    below = BoundingBox(x=0.0, y=2000.0, width=10.0, height=10.0)
    assert vertical_fraction(below, VIEWPORT) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# modulator clamping at both bounds and center
# ---------------------------------------------------------------------------


def test_position_modulator_clamps() -> None:
    cfg = PositionModulatorConfig()
    # y=0 -> intercept 1.3 (top of page, max).
    assert position_modulator(0.0, cfg) == pytest.approx(1.3)
    # Below the fold (y_frac=1) -> 1.3 - 0.6 = 0.7 (floor).
    assert position_modulator(1.0, cfg) == pytest.approx(0.7)
    # Center.
    assert position_modulator(0.5, cfg) == pytest.approx(1.0)


def test_contrast_modulator_centered_near_aa() -> None:
    cfg = ContrastModulatorConfig()
    # cr=4.5 -> 0.85 + 0.05*(4.5-3.0) = 0.925.
    assert contrast_modulator(4.5, cfg) == pytest.approx(0.925)
    # Low contrast clamps to the 0.85 floor.
    assert contrast_modulator(0.0, cfg) == pytest.approx(0.85)
    # Very high contrast clamps to the 1.3 ceiling.
    assert contrast_modulator(21.0, cfg) == pytest.approx(1.3)


def test_sibling_modulator_floor_ceiling_and_neutral() -> None:
    cfg = SiblingModulatorConfig()
    # Much smaller than siblings -> floor.
    assert sibling_modulator(0.0001, 1.0, cfg) == pytest.approx(0.7)
    # Much larger than siblings -> ceiling.
    assert sibling_modulator(1.0, 1e-6, cfg) == pytest.approx(1.5)
    # Equal to median -> exactly 1.0.
    assert sibling_modulator(0.01, 0.01, cfg) == pytest.approx(1.0)
    # No siblings -> neutral.
    assert sibling_modulator(0.01, 0.0, cfg) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# guardrail: modulators cannot lift a tiny element past a hero (tuning-spec §1.3)
# ---------------------------------------------------------------------------


def test_modulators_cannot_lift_tiny_icon_past_hero() -> None:
    cfg = DetectionConfig()
    # A 16x16px high-contrast centered icon vs. a large hero button.
    icon_box = BoundingBox(x=632.0, y=392.0, width=16.0, height=16.0)
    hero_box = BoundingBox(x=440.0, y=300.0, width=400.0, height=120.0)
    a_icon = area_fraction(icon_box, VIEWPORT)
    a_hero = area_fraction(hero_box, VIEWPORT)

    # Maximum possible modulator product (1.3 * 1.5 * 1.3 ~= 2.535).
    max_product = cfg.position.max * cfg.sibling.max * cfg.contrast.max
    assert max_product < 2.6

    # Even with every modulator maxed on the icon and minimised on the hero, the
    # icon's prominence stays far below the hero's.
    pi_icon_best = a_icon * max_product
    assert pi_icon_best < a_hero

    # And computed prominence (icon centered + high-contrast, hero plain) agrees.
    pi_icon = instance_prominence(
        a_icon,
        y_frac=vertical_fraction(icon_box, VIEWPORT),
        median_sibling_area=1e-6,  # icon dwarfs its siblings
        contrast=21.0,  # maximal contrast
        surface=False,
        cfg=cfg,
    )
    pi_hero = instance_prominence(
        a_hero,
        y_frac=vertical_fraction(hero_box, VIEWPORT),
        median_sibling_area=a_hero,  # neutral siblings
        contrast=3.0,  # neutral contrast
        surface=False,
        cfg=cfg,
    )
    assert pi_icon < pi_hero


# ---------------------------------------------------------------------------
# surface roles zero all modulators
# ---------------------------------------------------------------------------


def test_surface_role_disables_modulators() -> None:
    cfg = DetectionConfig()
    box = BoundingBox(x=0.0, y=0.0, width=200.0, height=200.0)
    a_i = area_fraction(box, VIEWPORT)
    pi = instance_prominence(
        a_i,
        y_frac=vertical_fraction(box, VIEWPORT),
        median_sibling_area=1e-6,
        contrast=21.0,
        surface=True,
        cfg=cfg,
    )
    assert pi == pytest.approx(a_i)


# ---------------------------------------------------------------------------
# aggregate_salience: cta peak-dominant vs. text near-sum
# ---------------------------------------------------------------------------


def test_aggregate_salience_empty() -> None:
    assert aggregate_salience([], 0.2, 0.5) == 0.0


def test_cta_regime_is_peak_dominant() -> None:
    # cta: lambda=0.2, beta=0.5. One big hero beats a swarm of tiny buttons whose
    # per-instance salience is genuinely small (the hero-vs-swarm case, redesign §8).
    hero = [0.30]
    swarm = [0.0005] * 40
    s_hero = aggregate_salience(hero, lambda_r=0.2, beta_r=0.5)
    s_swarm = aggregate_salience(swarm, lambda_r=0.2, beta_r=0.5)
    assert s_hero > s_swarm


def test_text_regime_sums_many_medium_over_one_large() -> None:
    # text: lambda=1.0, beta=0.9. Many medium body-text instances beat a single
    # large headline (the hero-headline-vs-body-text case).
    headline = [0.20]
    body = [0.05] * 30
    s_headline = aggregate_salience(headline, lambda_r=1.0, beta_r=0.9)
    s_body = aggregate_salience(body, lambda_r=1.0, beta_r=0.9)
    assert s_body > s_headline


# ---------------------------------------------------------------------------
# intent_multiplier bounds
# ---------------------------------------------------------------------------


def test_intent_multiplier_bounds() -> None:
    assert intent_multiplier(0.0, 0.4) == pytest.approx(1.0)
    assert intent_multiplier(1.0, 0.4) == pytest.approx(1.4)
    # q clamped above 1.
    assert intent_multiplier(5.0, 0.4) == pytest.approx(1.4)
    # q clamped below 0.
    assert intent_multiplier(-1.0, 0.4) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# config round-trip (§8 defaults + lambda alias)
# ---------------------------------------------------------------------------


def test_default_config_detection_section() -> None:
    detection = load_default_config().detection
    assert detection.alpha == pytest.approx(0.4)
    assert detection.position.intercept == pytest.approx(1.3)
    assert detection.position.slope == pytest.approx(0.6)
    assert detection.sibling.exponent == pytest.approx(0.25)
    assert detection.contrast.slope == pytest.approx(0.05)

    cta = detection.roles[UsageRole.CTA]
    # The `lambda` YAML key loads into the `lambda_` field.
    assert cta.lambda_ == pytest.approx(0.2)
    assert cta.beta == pytest.approx(0.5)
    assert cta.theta_noise == pytest.approx(0.0001)
    # theta_present for cta is the calibrated value (3.0x theta_noise = 0.0003).
    assert cta.theta_present == pytest.approx(0.0003)

    text = detection.roles[UsageRole.TEXT]
    assert text.lambda_ == pytest.approx(1.0)
    assert text.beta == pytest.approx(0.9)

    surface = detection.roles[UsageRole.SURFACE]
    assert surface.theta_noise == pytest.approx(0.005)
    # theta_present for surface is the calibrated value (3.0x theta_noise = 0.015).
    assert surface.theta_present == pytest.approx(0.015)


def test_role_aggregation_lambda_alias_populate_by_name() -> None:
    # Both the alias ("lambda") and the field name ("lambda_") construct the model.
    by_alias = RoleAggregationConfig.model_validate({"lambda": 0.3, "beta": 0.8})
    assert by_alias.lambda_ == pytest.approx(0.3)
    by_name = RoleAggregationConfig(lambda_=0.3, beta=0.8)
    assert by_name.lambda_ == pytest.approx(0.3)


def test_detection_section_is_optional() -> None:
    # A config built without a detection section falls back to defaults.
    default = DetectionConfig()
    assert default.alpha == pytest.approx(0.4)
    assert default.roles == {}
    assert math.isclose(PositionModulatorConfig().max, 1.3)
