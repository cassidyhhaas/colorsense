"""Unit tests for the config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from colorsense.config import (
    ChannelPrior,
    Config,
    DistributionPrior,
    PresenceRule,
    RelationalInfo,
    ScaleInfo,
    load_config,
    load_default_config,
)
from colorsense.models import TokenSemanticRole


@pytest.fixture
def config() -> Config:
    """The real, loaded palette config (the one bundled with the package)."""
    return load_default_config()


def test_load_returns_typed_config(config: Config) -> None:
    assert isinstance(config, Config)
    assert config.token_vocabulary.namespace_prefixes
    assert config.component_classifier.component_types


def test_distribution_rows_normalized_and_channels_recognized(config: Config) -> None:
    priors = config.token_vocabulary.role_to_usage_prior

    channel_roles = {
        TokenSemanticRole.text_on,
        TokenSemanticRole.status,
        TokenSemanticRole.ignore,
    }
    for role, row in priors.items():
        if role in channel_roles:
            assert isinstance(row, ChannelPrior)
            assert row.channel == role.value
        else:
            assert isinstance(row, DistributionPrior)
            total = sum(row.distribution.values())
            assert total == pytest.approx(1.0, abs=1e-6)


def test_match_name_rule_applies_known_system_boost(config: Config) -> None:
    result = config.match_name_rule("--bs-primary")
    assert result is not None
    role, weight = result
    assert role is TokenSemanticRole.brand_primary
    assert weight == pytest.approx(5.0 * 1.25)


def test_match_name_rule_no_boost_without_system_prefix(config: Config) -> None:
    result = config.match_name_rule("--primary")
    assert result is not None
    role, weight = result
    assert role is TokenSemanticRole.brand_primary
    assert weight == pytest.approx(5.0)


def test_match_relational_foreground(config: Config) -> None:
    result = config.match_relational("--primary-foreground")
    assert isinstance(result, RelationalInfo)
    assert result.type == "text_on"
    assert result.base == "primary"


def test_match_relational_none_when_no_modifier(config: Config) -> None:
    assert config.match_relational("--primary") is None


def test_detect_scale_chromatic(config: Config) -> None:
    info = config.detect_scale("--blue-500")
    assert isinstance(info, ScaleInfo)
    assert info.family == "blue"
    assert info.number == 500
    assert info.is_chromatic is True
    assert info.is_anchor is True


def test_detect_scale_neutral(config: Config) -> None:
    info = config.detect_scale("--gray-100")
    assert isinstance(info, ScaleInfo)
    assert info.family == "gray"
    assert info.number == 100
    assert info.is_chromatic is False


def test_detect_scale_none_without_number(config: Config) -> None:
    assert config.detect_scale("--blue") is None


def test_detect_scale_anchor_any_convention(config: Config) -> None:
    """A number is an anchor if it falls within ANY convention's range."""
    radix = config.detect_scale("--blue-9")
    assert isinstance(radix, ScaleInfo)
    assert radix.is_anchor is True  # radix [9, 10]

    tailwind = config.detect_scale("--blue-500")
    assert isinstance(tailwind, ScaleInfo)
    assert tailwind.is_anchor is True  # tailwind [500, 700]

    neither = config.detect_scale("--blue-50")
    assert isinstance(neither, ScaleInfo)
    assert neither.is_anchor is False

    neutral_radix = config.detect_scale("--gray-10")
    assert isinstance(neutral_radix, ScaleInfo)
    assert neutral_radix.is_anchor is True  # radix range applies to neutrals too


def test_detect_scale_unknown_family_never_anchor(config: Config) -> None:
    """An unknown family gets a scale number but is never an anchor."""
    info = config.detect_scale("--foo-500")
    assert isinstance(info, ScaleInfo)
    assert info.family == "foo"
    assert info.is_chromatic is False
    assert info.is_anchor is False


def test_strip_namespace(config: Config) -> None:
    assert config.strip_namespace("--bs-primary") == "primary"
    assert config.strip_namespace("--theme-color") == "theme"


def test_layout_noise_class_tokens_present(config: Config) -> None:
    rules = config.component_classifier.class_tokens
    container = next((r for r in rules if r.match == "container"), None)
    assert container is not None
    assert container.votes.get("page_bg") == pytest.approx(0.5)


def test_presence_feature_families_modeled(config: Config) -> None:
    """border_presence / text_presence load as typed PresenceRule rows.

    The vote targets are pinned (the classifier routes them structurally); the
    weights themselves are YAML-tunable and only checked for positivity.
    """
    cc = config.component_classifier
    assert isinstance(cc.border_presence, PresenceRule)
    assert isinstance(cc.text_presence, PresenceRule)
    assert cc.border_presence.votes.get("border", 0.0) > 0.0
    assert cc.text_presence.votes.get("page_text", 0.0) > 0.0


def test_presence_feature_families_required(tmp_path: Path) -> None:
    """A config missing the presence families fails validation loudly."""
    import yaml

    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "src/colorsense/data/palette_config.yaml").read_text()
    )
    del raw["component_classifier"]["border_presence"]
    bad = tmp_path / "no_presence.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_config(bad)
    assert "border_presence" in str(excinfo.value)


@pytest.mark.parametrize(
    ("section", "entry", "expected_fragment"),
    [
        ("interactivity", {"when": "has_focus_ring", "votes": {"cta_bg": 0.5}}, "has_focus_ring"),
        ("interactivity", {"when": "is_animated", "votes": {"cta_bg": 1.0}}, "is_animated"),
        ("geometry", {"when": "left_half & tall", "votes": {"nav_bg": 1.0}}, "left_half"),
    ],
)
def test_unknown_when_predicate_rejected(
    tmp_path: Path, section: str, entry: dict[str, object], expected_fragment: str
) -> None:
    """A ``when`` predicate the classifier does not implement fails at load time.

    Regression guard for the 0.4.0 release review: the bundled YAML used to ship a
    ``has_focus_ring`` interactivity rule that the classifier hard-returned False
    for — a dead knob. Unknown predicates must fail loudly, not become silent no-ops.
    """
    import yaml

    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "src/colorsense/data/palette_config.yaml").read_text()
    )
    if section == "geometry":
        raw["component_classifier"]["geometry"]["rules"].append(entry)
    else:
        raw["component_classifier"][section].append(entry)
    bad = tmp_path / "unknown_when.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_config(bad)
    assert expected_fragment in str(excinfo.value)


def test_unknown_suppressor_key_rejected(tmp_path: Path) -> None:
    """A suppressor key the classifier does not implement fails at load time.

    Regression guard: ``consent_masked_region`` shipped as a suppressor the
    classifier never triggered (no mask info reaches the classification layer).
    """
    import yaml

    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "src/colorsense/data/palette_config.yaml").read_text()
    )
    raw["component_classifier"]["suppressors"]["consent_masked_region"] = {
        "factor": 0.0,
        "applies_to": "all",
    }
    bad = tmp_path / "unknown_suppressor.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_config(bad)
    assert "consent_masked_region" in str(excinfo.value)


def test_unknown_suppressor_scope_rejected(tmp_path: Path) -> None:
    """``applies_to`` outside the two implemented scopes fails at load time."""
    import yaml

    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "src/colorsense/data/palette_config.yaml").read_text()
    )
    raw["component_classifier"]["suppressors"]["aria_hidden"]["applies_to"] = "everything"
    bad = tmp_path / "bad_scope.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad)


def test_default_config_loads_from_package() -> None:
    """The bundled config resolves via the package, independent of the working dir."""
    config = load_default_config()
    assert isinstance(config, Config)
    # A representative value proves the YAML was actually parsed, not just located.
    assert config.token_vocabulary.name_rules


def test_invalid_config_raises_clear_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "token_vocabulary:\n"
        "  namespace_prefixes: ['c-']\n"
        "# missing component_classifier and most required keys\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as excinfo:
        load_config(bad)
    # Must be a clear validation error, never a bare KeyError.
    assert not isinstance(excinfo.value, KeyError)
    assert "component_classifier" in str(excinfo.value)


def _bundled_raw() -> dict:  # type: ignore[type-arg]
    import yaml

    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "src/colorsense/data/palette_config.yaml").read_text()
    )
    assert isinstance(raw, dict)
    return raw


@pytest.mark.parametrize(
    ("path", "value", "expected_fragment"),
    [
        # A negative prior would survive normalization and reach reconcile's ``** alpha``
        # pooling, where a negative base under a fractional exponent yields a COMPLEX
        # number — must fail at load, not deep in the math.
        (
            ("token_vocabulary", "role_to_usage_prior", "interactive"),
            {"interactive": -1.0, "surface": 2.0},
            "must be >= 0",
        ),
        # T=0 divides by zero at classify time; T<0 silently INVERTS the ranking.
        (("component_classifier", "softmax_temperature"), 0.0, "softmax_temperature"),
        (("component_classifier", "softmax_temperature"), -1.0, "softmax_temperature"),
        (("component_classifier", "min_component_prob"), 1.5, "min_component_prob"),
        # Groupless pattern: detect_scale reads match.group(1) — previously an IndexError
        # only when a numbered token first appeared.
        (
            ("token_vocabulary", "scale_detection", "number_pattern"),
            r"(?:^|[-_/.])\d{1,3}$",
            "capture group",
        ),
        # Invalid regex must be a load-time error too.
        (("token_vocabulary", "scale_detection", "number_pattern"), "(", "number_pattern"),
        (("token_vocabulary", "scale_detection", "base_weight"), 0.0, "base_weight"),
        (("token_vocabulary", "known_system_confidence_boost"), -0.1, "greater than or equal"),
    ],
)
def test_pathological_numeric_config_rejected_at_load(
    tmp_path: Path, path: tuple[str, ...], value: object, expected_fragment: str
) -> None:
    """Numeric knobs are range-validated at load (release-review hardening).

    ``config_path=`` is public API: a hand-authored YAML must fail loudly at load with
    the offending key named, never crash later (ZeroDivisionError/IndexError/complex
    numbers in reconcile) or silently corrupt results (negative softmax temperature).
    """
    import yaml

    raw = _bundled_raw()
    target: dict = raw  # type: ignore[type-arg]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    bad = tmp_path / "pathological.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_config(bad)
    assert expected_fragment in str(excinfo.value)


def test_scale_base_weight_required_and_loaded(config: Config) -> None:
    """The scale classifier's base weight comes from the YAML, not code."""
    assert config.token_vocabulary.scale_detection.base_weight == 3.0


def test_relational_pattern_without_base_group_rejected(tmp_path: Path) -> None:
    """A relational pattern missing the documented ``base`` group fails at load.

    ``match_relational`` skips matches without a ``base`` group, so such a pattern was
    previously a rule that silently never rerouted anything.
    """
    import yaml

    raw = _bundled_raw()
    raw["token_vocabulary"]["relational_modifiers"].append(
        {"pattern": "^on-", "type": "foreground", "weight": 3.0}
    )
    bad = tmp_path / "no_base_group.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_config(bad)
    assert "base" in str(excinfo.value)
